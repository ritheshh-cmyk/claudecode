"""Always-on recovery helpers for truncated provider streams."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx
import jsonschema
import openai
from loguru import logger

EARLY_TRANSPARENT_TOTAL_ATTEMPTS = 5
EARLY_TRANSPARENT_MAX_RETRIES = EARLY_TRANSPARENT_TOTAL_ATTEMPTS - 1
MIDSTREAM_RECOVERY_ATTEMPTS = 5
EARLY_HOLDBACK_SECONDS = 0.75
RECOVERY_BUFFER_MAX_BYTES = 65_536

_RECOVERY_USER_PREFIX = (
    "The previous provider stream was interrupted. Continue the assistant response "
    "exactly where it stopped. Do not repeat text already written."
)


class TruncatedProviderStreamError(RuntimeError):
    """Raised internally when an upstream stream ends without a terminal marker."""


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Tool schema resolved from the original Anthropic request."""

    name: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolRepair:
    """Accepted append-only tool JSON repair."""

    suffix: str
    parsed_input: dict[str, Any]


class RecoveryHoldbackBuffer:
    """Briefly hold downstream SSE so early stream cutoffs can be retried invisibly."""

    def __init__(
        self,
        *,
        holdback_seconds: float = EARLY_HOLDBACK_SECONDS,
        max_bytes: int = RECOVERY_BUFFER_MAX_BYTES,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._holdback_seconds = holdback_seconds
        self._max_bytes = max_bytes
        self._now = now or time.monotonic
        self._events: list[str] = []
        self._bytes = 0
        self._started_at: float | None = None
        self.committed = False

    def push(self, event: str) -> list[str]:
        """Buffer ``event`` until holdback expires or cap is reached."""
        if self.committed:
            return [event]
        if self._started_at is None:
            self._started_at = self._now()
        self._events.append(event)
        self._bytes += len(event.encode("utf-8", errors="replace"))
        if (
            self._bytes >= self._max_bytes
            or self._now() - self._started_at >= self._holdback_seconds
        ):
            return self.flush()
        return []

    def flush(self) -> list[str]:
        """Commit and return all held events."""
        if self.committed:
            return []
        self.committed = True
        events = self._events
        self._events = []
        self._bytes = 0
        self._started_at = None
        return events

    def discard(self) -> None:
        """Drop held events without committing them downstream."""
        self._events = []
        self._bytes = 0
        self._started_at = None

    @property
    def has_buffered(self) -> bool:
        return bool(self._events)


def is_retryable_stream_error(exc: BaseException) -> bool:
    """Return whether a provider stream error can be retried/recovered."""
    if isinstance(exc, TruncatedProviderStreamError):
        return True
    if isinstance(exc, openai.AuthenticationError | openai.BadRequestError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status <= 599
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return isinstance(status, int) and (status == 429 or 500 <= status <= 599)
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.NetworkError,
            openai.APITimeoutError,
            openai.APIConnectionError,
        ),
    )


def tool_schemas_by_name(request: Any) -> dict[str, ToolSchema]:
    """Return Anthropic tool input schemas keyed by tool name."""
    schemas: dict[str, ToolSchema] = {}
    tools = getattr(request, "tools", None)
    if not tools:
        return schemas

    for tool in tools:
        name = _tool_attr(tool, "name")
        if not isinstance(name, str) or not name:
            continue
        schema = _tool_attr(tool, "input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        schemas[name] = ToolSchema(name=name, input_schema=deepcopy(schema))
    return schemas


def validate_tool_input(
    tool_name: str, parsed_input: dict[str, Any], schemas: dict[str, ToolSchema]
) -> bool:
    """Validate tool input against its JSON schema; unknown tools accept any object."""
    tool_schema = schemas.get(tool_name)
    if tool_schema is None:
        return True
    try:
        validator_cls = jsonschema.validators.validator_for(tool_schema.input_schema)
        validator_cls.check_schema(tool_schema.input_schema)
        validator_cls(tool_schema.input_schema).validate(parsed_input)
    except jsonschema.exceptions.SchemaError as exc:
        logger.warning("Skipping invalid tool schema for {}: {}", tool_name, exc)
        return True
    except jsonschema.exceptions.ValidationError:
        return False
    return True


def parse_complete_tool_input(
    raw_json: str, tool_name: str, schemas: dict[str, ToolSchema]
) -> dict[str, Any] | None:
    """Return parsed input when raw JSON is complete and schema-valid."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not validate_tool_input(tool_name, parsed, schemas):
        return None
    return parsed


def accept_tool_json_repair(
    prefix: str,
    candidate: str,
    *,
    tool_name: str,
    schemas: dict[str, ToolSchema],
) -> ToolRepair | None:
    """Accept only append-only JSON repairs that make ``prefix`` valid."""
    suffix_candidates = _repair_suffix_candidates(prefix, candidate)
    for suffix in suffix_candidates:
        combined = prefix + suffix
        parsed = parse_complete_tool_input(combined, tool_name, schemas)
        if parsed is not None:
            return ToolRepair(suffix=suffix, parsed_input=parsed)
    return None


def continuation_suffix(existing: str, candidate: str) -> str | None:
    """Return only the new suffix from a text/thinking continuation candidate."""
    existing = existing or ""
    candidate = candidate or ""
    if not candidate:
        return ""
    if not existing:
        return candidate
    if candidate.startswith(existing):
        return candidate[len(existing) :]

    max_overlap = min(len(existing), len(candidate))
    for size in range(max_overlap, 0, -1):
        if existing.endswith(candidate[:size]):
            return candidate[size:]

    # Accept short standalone continuations, but reject full unrelated rewrites.
    if len(candidate) < max(200, len(existing) // 2):
        return candidate
    return None


def make_openai_text_recovery_body(
    body: dict[str, Any], partial: str
) -> dict[str, Any]:
    """Build a text-only OpenAI-chat continuation request."""
    recovery = deepcopy(body)
    recovery.pop("tools", None)
    recovery.pop("tool_choice", None)
    recovery["stream"] = True
    messages = _copied_messages(recovery)
    if partial:
        messages.append({"role": "assistant", "content": partial})
    messages.append({"role": "user", "content": _RECOVERY_USER_PREFIX})
    recovery["messages"] = messages
    return recovery


def make_openai_tool_repair_body(
    body: dict[str, Any],
    *,
    tool_name: str,
    prefix: str,
    input_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a text-only OpenAI-chat request asking for a JSON suffix."""
    recovery = deepcopy(body)
    recovery.pop("tools", None)
    recovery.pop("tool_choice", None)
    recovery["stream"] = True
    messages = _copied_messages(recovery)
    messages.append(
        {
            "role": "user",
            "content": _tool_repair_prompt(
                tool_name=tool_name, prefix=prefix, input_schema=input_schema
            ),
        }
    )
    recovery["messages"] = messages
    return recovery


def make_native_text_recovery_body(
    body: dict[str, Any], partial: str
) -> dict[str, Any]:
    """Build a text-only native Anthropic continuation request."""
    recovery = deepcopy(body)
    recovery.pop("tools", None)
    recovery.pop("tool_choice", None)
    recovery["stream"] = True
    messages = _copied_messages(recovery)
    if partial:
        messages.append({"role": "assistant", "content": partial})
    messages.append({"role": "user", "content": _RECOVERY_USER_PREFIX})
    recovery["messages"] = messages
    return recovery


def make_native_tool_repair_body(
    body: dict[str, Any],
    *,
    tool_name: str,
    prefix: str,
    input_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a text-only native Anthropic request asking for a JSON suffix."""
    recovery = deepcopy(body)
    recovery.pop("tools", None)
    recovery.pop("tool_choice", None)
    recovery["stream"] = True
    messages = _copied_messages(recovery)
    messages.append(
        {
            "role": "user",
            "content": _tool_repair_prompt(
                tool_name=tool_name, prefix=prefix, input_schema=input_schema
            ),
        }
    )
    recovery["messages"] = messages
    return recovery


def _tool_attr(tool: Any, attr: str) -> Any:
    if isinstance(tool, dict):
        return tool.get(attr)
    return getattr(tool, attr, None)


def _copied_messages(body: dict[str, Any]) -> list[Any]:
    messages = body.get("messages")
    return deepcopy(messages) if isinstance(messages, list) else []


def _repair_suffix_candidates(prefix: str, candidate: str) -> list[str]:
    raw = candidate.strip()
    if not raw:
        return []
    candidates: list[str] = []
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    candidates.append(raw)
    if raw.startswith(prefix):
        candidates.append(raw[len(prefix) :])
    return list(dict.fromkeys(candidates))


def _tool_repair_prompt(
    *, tool_name: str, prefix: str, input_schema: dict[str, Any] | None
) -> str:
    schema_text = json.dumps(input_schema or {"type": "object"}, separators=(",", ":"))
    return (
        "A streamed tool call was interrupted while writing JSON arguments.\n"
        f"Tool name: {tool_name}\n"
        f"JSON schema: {schema_text}\n"
        f"Already emitted JSON prefix: {prefix}\n\n"
        "Return only the exact missing JSON suffix needed to complete the same object. "
        "Do not repeat the prefix. Do not include markdown or explanation."
    )
