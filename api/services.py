"""Application services for the Claude-compatible API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from config.settings import Settings
from core.anthropic import get_token_count, get_user_facing_error_message
from core.anthropic.sse import ANTHROPIC_SSE_RESPONSE_HEADERS
from core.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerState
from core.reliability.dead_letter import DeadLetterQueue
from core.reliability.dedup import RequestDeduplicator
from core.reliability.helpers import is_in_blackout
from core.reliability.key_pool import KeyPool
from core.reliability.request_queue import RequestQueue
from core.reliability.retry import calculate_delay
from core.reliability.watchdog import StreamingTimeoutError, watchdog_stream
from core.trace import api_messages_request_snapshot, trace_event, traced_async_stream
from providers.base import BaseProvider
from providers.exceptions import (
    AuthenticationError,
    InvalidRequestError,
    OverloadedError,
    ProviderError,
    RateLimitError,
)

from .model_router import ModelRouter, RoutedMessagesRequest
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.responses import TokenCountResponse
from .optimization_handlers import try_optimizations
from .web_tools.egress import WebFetchEgressPolicy
from .web_tools.request import (
    is_web_server_tool_request,
    openai_chat_upstream_server_tool_error,
)
from .web_tools.streaming import stream_web_server_tool_response

TokenCounter = Callable[[list[Any], str | list[Any] | None, list[Any] | None], int]

ProviderGetter = Callable[..., BaseProvider]

# Providers that use ``/chat/completions`` + Anthropic-to-OpenAI conversion (not native Messages).
_OPENAI_CHAT_UPSTREAM_IDS = frozenset({"nvidia_nim", "opencode", "opencode_go"})

# Provider error status codes that should trigger automatic fallback to the backup model.
_FALLBACK_TRIGGER_STATUS_CODES = frozenset({401, 403, 429, 529})


async def _stream_with_fallback(
    primary: AsyncIterator[str],
    fallback: AsyncIterator[str] | None,
    *,
    primary_provider_id: str,
    fallback_desc: str,
) -> AsyncIterator[str]:
    """Yield chunks from ``primary``; on retryable errors switch to ``fallback``.

    Only switches when the error arrives **before any bytes have been sent**
    (i.e. before the first yield).  Mid-stream failures still propagate normally.
    """
    first_chunk_received = False
    try:
        async for chunk in primary:
            first_chunk_received = True
            yield chunk
    except (RateLimitError, AuthenticationError, OverloadedError) as exc:
        if fallback is not None and not first_chunk_received:
            logger.warning(
                "FALLBACK: primary={} status={} message={!r} -> switching to {}",
                primary_provider_id,
                exc.status_code,
                str(exc)[:120],
                fallback_desc,
            )
            async for chunk in fallback:
                yield chunk
        else:
            raise


def anthropic_sse_streaming_response(
    body: AsyncIterator[str],
) -> StreamingResponse:
    """Return a :class:`StreamingResponse` for Anthropic-style SSE streams."""
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers=ANTHROPIC_SSE_RESPONSE_HEADERS,
    )


def _http_status_for_unexpected_service_exception(_exc: BaseException) -> int:
    """HTTP status for uncaught non-provider failures (stable client contract)."""
    return 500


def _log_unexpected_service_exception(
    settings: Settings,
    exc: BaseException,
    *,
    context: str,
    request_id: str | None = None,
) -> None:
    """Log service-layer failures without echoing exception text unless opted in."""
    if getattr(settings, "log_api_error_tracebacks", False):
        if request_id is not None:
            logger.error("{} request_id={}: {}", context, request_id, exc)
        else:
            logger.error("{}: {}", context, exc)
        logger.error(traceback.format_exc())
        return
    if request_id is not None:
        logger.error(
            "{} request_id={} exc_type={}",
            context,
            request_id,
            type(exc).__name__,
        )
    else:
        logger.error("{} exc_type={}", context, type(exc).__name__)


def hash_request(request: MessagesRequest) -> str:

    h = hashlib.sha256()
    h.update(request.model.encode("utf-8"))
    try:
        msgs_str = json.dumps(
            [m if isinstance(m, dict) else m.model_dump() for m in request.messages]
        )
        h.update(msgs_str.encode("utf-8"))
    except Exception:
        h.update(str(request.messages).encode("utf-8"))
    if request.system:
        if isinstance(request.system, str):
            h.update(request.system.encode("utf-8"))
        else:
            try:
                system_str = json.dumps(
                    [
                        s if isinstance(s, dict) else s.model_dump()
                        for s in request.system
                    ]
                )
                h.update(system_str.encode("utf-8"))
            except Exception:
                h.update(str(request.system).encode("utf-8"))
    return h.hexdigest()


class StreamMultiplexer:
    """Multiplexes a single async iterator of strings to multiple readers."""

    def __init__(self, stream: AsyncIterator[str]) -> None:
        self._stream = stream
        self._chunks: list[str] = []
        self._done = False
        self._error: Exception | None = None
        self._waiters: list[asyncio.Event] = []
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        try:
            async for chunk in self._stream:
                async with self._lock:
                    self._chunks.append(chunk)
                    for waiter in self._waiters:
                        waiter.set()
        except Exception as e:
            self._error = e
            async with self._lock:
                for waiter in self._waiters:
                    waiter.set()
        finally:
            self._done = True
            async with self._lock:
                for waiter in self._waiters:
                    waiter.set()

    async def iterate(self) -> AsyncIterator[str]:
        idx = 0
        event = asyncio.Event()
        async with self._lock:
            self._waiters.append(event)

        try:
            while True:
                async with self._lock:
                    if idx < len(self._chunks):
                        yield self._chunks[idx]
                        idx += 1
                        continue
                    if self._done:
                        if self._error:
                            raise self._error
                        break
                    event.clear()

                await event.wait()
        finally:
            async with self._lock:
                if event in self._waiters:
                    self._waiters.remove(event)


def _require_non_empty_messages(messages: list[Any]) -> None:
    if not messages:
        raise InvalidRequestError("messages cannot be empty")


class ClaudeProxyService:
    """Coordinate request optimization, model routing, token count, and providers."""

    def __init__(
        self,
        settings: Settings,
        provider_getter: ProviderGetter,
        model_router: ModelRouter | None = None,
        token_counter: TokenCounter = get_token_count,
        circuit_breaker: CircuitBreaker | None = None,
        key_pool: KeyPool | None = None,
        request_queue: RequestQueue | None = None,
        dead_letter_queue: DeadLetterQueue | None = None,
        deduplicator: RequestDeduplicator | None = None,
    ):
        self._settings = settings
        self._provider_getter = provider_getter
        self._model_router = model_router or ModelRouter(settings)
        self._token_counter = token_counter

        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            success_threshold=2,
            tripping_exceptions=(Exception,),
        )

        if key_pool is not None:
            self._key_pool = key_pool
        else:
            keys_by_provider = {}
            try:
                from core.reliability.heartbeat import get_active_provider_ids
                from providers.registry import PROVIDER_DESCRIPTORS, _credential_for

                for p_id in get_active_provider_ids(settings):
                    descriptor = PROVIDER_DESCRIPTORS.get(p_id)
                    if descriptor:
                        key = _credential_for(descriptor, settings)
                        if key and key.strip():
                            keys_by_provider[p_id] = [key.strip()]
            except Exception as e:
                logger.warning("Failed to initialize key pool from settings: {}", e)
            self._key_pool = KeyPool(keys_by_provider=keys_by_provider)

        self._request_queue = request_queue or RequestQueue()
        self._dead_letter_queue = dead_letter_queue or DeadLetterQueue()
        self._deduplicator = deduplicator or RequestDeduplicator()

    def create_message(self, request_data: MessagesRequest) -> object:
        """Create a message response or streaming response."""
        try:
            _require_non_empty_messages(request_data.messages)

            routed = self._model_router.resolve_messages_request(request_data)
            if routed.resolved.provider_id in _OPENAI_CHAT_UPSTREAM_IDS:
                tool_err = openai_chat_upstream_server_tool_error(
                    routed.request,
                    web_tools_enabled=self._settings.enable_web_server_tools,
                )
                if tool_err is not None:
                    raise InvalidRequestError(tool_err)

            if self._settings.enable_web_server_tools and is_web_server_tool_request(
                routed.request
            ):
                input_tokens = self._token_counter(
                    routed.request.messages, routed.request.system, routed.request.tools
                )
                trace_event(
                    stage="routing",
                    event="api.optimization.web_server_tool",
                    source="api",
                    model=routed.request.model,
                )
                egress = WebFetchEgressPolicy(
                    allow_private_network_targets=self._settings.web_fetch_allow_private_networks,
                    allowed_schemes=self._settings.web_fetch_allowed_scheme_set(),
                )
                return anthropic_sse_streaming_response(
                    stream_web_server_tool_response(
                        routed.request,
                        input_tokens=input_tokens,
                        web_fetch_egress=egress,
                        verbose_client_errors=self._settings.log_api_error_tracebacks,
                    ),
                )

            optimized = try_optimizations(routed.request, self._settings)
            if optimized is not None:
                trace_event(
                    stage="routing",
                    event="api.optimization.short_circuit",
                    source="api",
                    model=routed.request.model,
                )
                return optimized
            logger.debug("No optimization matched, routing to provider")

            # Proactively run preflight validation and handle mock synchronous stream exceptions
            try:
                primary_provider = self._get_provider(routed.resolved.provider_id)
                primary_provider.preflight_stream(
                    routed.request,
                    thinking_enabled=routed.resolved.thinking_enabled,
                )
                import inspect

                if not inspect.isasyncgenfunction(primary_provider.stream_response):
                    _ = primary_provider.stream_response(
                        routed.request,
                        input_tokens=1,
                        request_id="preflight_check",
                        thinking_enabled=routed.resolved.thinking_enabled,
                    )
            except Exception as e:
                has_fallback = bool(
                    getattr(self._settings, "fallback_chain", "")
                    or getattr(self._settings, "fallback_model", "")
                )
                from providers.exceptions import (
                    AuthenticationError,
                    UnknownProviderTypeError,
                )

                if has_fallback and isinstance(
                    e, (AuthenticationError, UnknownProviderTypeError)
                ):
                    logger.debug(
                        "Suppressing primary provider error for fallback: {}", e
                    )
                else:
                    raise e

            request_id = f"req_{uuid.uuid4().hex[:12]}"
            if getattr(self._settings, "log_raw_api_payloads", False):
                logger.debug(
                    "FULL_PAYLOAD [{}]: {}", request_id, routed.request.model_dump()
                )

            # Hash the request for deduplication
            req_key = hash_request(routed.request)

            async def get_stream_multiplexer() -> StreamMultiplexer:
                raw_generator = self._execute_request_with_retry_and_fallback(routed)
                traced_generator = traced_async_stream(
                    raw_generator,
                    stage="egress",
                    source="api",
                    complete_event="api.response.stream_completed",
                    interrupted_event="api.response.stream_interrupted",
                    chunk_event=None,
                    extra={
                        "request_id": f"req_{uuid.uuid4().hex[:12]}",
                        "provider_id": routed.resolved.provider_id,
                        "gateway_model": routed.request.model,
                    },
                )
                return StreamMultiplexer(traced_generator)

            async def resolve_multiplexer_and_yield() -> AsyncIterator[str]:
                multiplexer = await self._deduplicator.execute(
                    req_key, get_stream_multiplexer()
                )
                async for chunk in multiplexer.iterate():
                    yield chunk

            return anthropic_sse_streaming_response(resolve_multiplexer_and_yield())

        except ProviderError:
            raise
        except Exception as e:
            _log_unexpected_service_exception(
                self._settings, e, context="CREATE_MESSAGE_ERROR"
            )
            raise HTTPException(
                status_code=_http_status_for_unexpected_service_exception(e),
                detail=get_user_facing_error_message(e),
            ) from e

    def _get_provider(
        self, provider_id: str, api_key: str | None = None
    ) -> BaseProvider:
        import inspect

        sig = inspect.signature(self._provider_getter)
        if "api_key" in sig.parameters:
            return self._provider_getter(provider_id, api_key=api_key)
        return self._provider_getter(provider_id)

    async def _execute_request_with_retry_and_fallback(
        self, routed: RoutedMessagesRequest
    ) -> AsyncIterator[str]:
        fallback_models = []
        fallback_chain_str = getattr(self._settings, "fallback_chain", "")
        if fallback_chain_str:
            fallback_models = [
                m.strip() for m in fallback_chain_str.split(",") if m.strip()
            ]
        else:
            fallback_model_str = getattr(self._settings, "fallback_model", "")
            if fallback_model_str:
                fallback_models = [fallback_model_str.strip()]

        candidates = []
        candidates.append(
            (
                routed.resolved.provider_id,
                routed.resolved.provider_model,
                routed.resolved.thinking_enabled,
            )
        )

        for fb in fallback_models:
            fb_provider_id = Settings.parse_provider_type(fb)
            fb_model_name = Settings.parse_model_name(fb)
            if fb_provider_id and fb_model_name:
                candidates.append((fb_provider_id, fb_model_name, False))

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        input_tokens = self._token_counter(
            routed.request.messages,
            routed.request.system,
            routed.request.tools,
        )

        start_time = time.monotonic()
        eligible: list[tuple[str, str, str, bool]] = []

        while time.monotonic() - start_time < 30.0:
            for provider_id, model_name, thinking in candidates:
                if is_in_blackout(
                    provider_id, getattr(self._settings, "blackout_windows", "")
                ):
                    continue
                if (
                    self._circuit_breaker.get_state(provider_id)
                    == CircuitBreakerState.OPEN
                ):
                    continue

                if self._key_pool.has_keys(provider_id):
                    api_key = await self._key_pool.get_key(provider_id)
                else:
                    api_key = "default_key"

                if api_key:
                    eligible.append((provider_id, api_key, model_name, thinking))

            if eligible:
                break

            await asyncio.sleep(0.5)

        if not eligible:
            await self._request_queue.enqueue(
                provider_id=routed.resolved.provider_id,
                payload=routed.request.model_dump(),
                priority=1,
            )
            try:
                await asyncio.sleep(5.0)
                for provider_id, model_name, thinking in candidates:
                    if is_in_blackout(
                        provider_id, getattr(self._settings, "blackout_windows", "")
                    ):
                        continue
                    if (
                        self._circuit_breaker.get_state(provider_id)
                        == CircuitBreakerState.OPEN
                    ):
                        continue
                    if self._key_pool.has_keys(provider_id):
                        api_key = await self._key_pool.get_key(provider_id)
                    else:
                        api_key = "default_key"

                    if api_key:
                        eligible.append((provider_id, api_key, model_name, thinking))
                        break
            finally:
                await self._request_queue.dequeue(routed.resolved.provider_id)

            if not eligible:
                err_msg = "All providers/keys in cooldown or blocked (blackout/circuit breaker)"
                await self._dead_letter_queue.record_failure(
                    provider_id=routed.resolved.provider_id,
                    payload=routed.request.model_dump(),
                    error=err_msg,
                )
                raise RateLimitError(err_msg)

        last_exc: Exception | None = None
        for provider_id, api_key, model_name, thinking in eligible:
            req_copy = routed.request.model_copy(deep=True)
            req_copy.model = model_name

            try:
                async with self._circuit_breaker.guard(provider_id):

                    async def attempt_provider(
                        provider_id: str = provider_id,
                        api_key: str = api_key,
                        req_copy: MessagesRequest = req_copy,
                        thinking: bool = thinking,
                    ) -> AsyncIterator[str]:
                        attempt = 0
                        max_retries = 3
                        while True:
                            attempt += 1
                            try:
                                provider = self._get_provider(
                                    provider_id,
                                    api_key=(
                                        None if api_key == "default_key" else api_key
                                    ),
                                )
                                provider.preflight_stream(
                                    req_copy,
                                    thinking_enabled=thinking,
                                )
                                raw_stream = provider.stream_response(
                                    req_copy,
                                    input_tokens=input_tokens,
                                    request_id=(
                                        request_id
                                        if attempt == 1
                                        else f"{request_id}_retry_{attempt}"
                                    ),
                                    thinking_enabled=thinking,
                                )
                                watched_stream = watchdog_stream(
                                    raw_stream,
                                    chunk_timeout=15.0,
                                    connect_timeout=getattr(
                                        self._settings, "provider_timeout", 30.0
                                    ),
                                )
                                iterator = aiter(watched_stream)
                                first_chunk = await anext(iterator)

                                if api_key != "default_key":
                                    await self._key_pool.report_success(
                                        provider_id, api_key
                                    )

                                yield first_chunk
                                async for chunk in iterator:
                                    yield chunk
                                return
                            except (
                                RateLimitError,
                                OverloadedError,
                                AuthenticationError,
                                TimeoutError,
                                StreamingTimeoutError,
                                Exception,
                            ) as exc:
                                if (
                                    isinstance(exc, RateLimitError)
                                    or (
                                        hasattr(exc, "status_code")
                                        and exc.status_code == 429
                                    )
                                ) and api_key != "default_key":
                                    await self._key_pool.report_429(
                                        provider_id, api_key, cooldown_duration=60.0
                                    )

                                if attempt <= max_retries:
                                    delay = calculate_delay(
                                        attempt,
                                        base_delay=0.5,
                                        max_delay=10.0,
                                        jitter=True,
                                    )
                                    logger.warning(
                                        "Request failed on {} (attempt {}/{}): {}. Retrying in {:.2f}s...",
                                        provider_id,
                                        attempt,
                                        max_retries,
                                        exc,
                                        delay,
                                    )
                                    await asyncio.sleep(delay)
                                    continue
                                else:
                                    raise

                    async for chunk in attempt_provider():
                        yield chunk
                    return
            except (
                RateLimitError,
                OverloadedError,
                AuthenticationError,
                TimeoutError,
                StreamingTimeoutError,
                Exception,
            ) as exc:
                logger.error("Candidate {} failed: {}", provider_id, exc)
                last_exc = exc
                continue

        err_msg = f"All eligible candidates failed. Last error: {last_exc}"
        await self._dead_letter_queue.record_failure(
            provider_id=routed.resolved.provider_id,
            payload=routed.request.model_dump(),
            error=err_msg,
        )
        if last_exc:
            raise last_exc
        raise RateLimitError(err_msg)

    def count_tokens(self, request_data: TokenCountRequest) -> TokenCountResponse:
        """Count tokens for a request after applying configured model routing."""
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        with logger.contextualize(request_id=request_id):
            try:
                _require_non_empty_messages(request_data.messages)
                routed = self._model_router.resolve_token_count_request(request_data)
                tokens = self._token_counter(
                    routed.request.messages, routed.request.system, routed.request.tools
                )
                trace_event(
                    stage="routing",
                    event="api.route.resolved",
                    source="api",
                    kind="count_tokens",
                    provider_id=routed.resolved.provider_id,
                    provider_model=routed.resolved.provider_model,
                    provider_model_ref=routed.resolved.provider_model_ref,
                    gateway_model=routed.request.model,
                )
                trace_event(
                    stage="ingress",
                    event="api.count_tokens.completed",
                    source="api",
                    message_count=len(routed.request.messages),
                    input_tokens=tokens,
                    snapshot=api_messages_request_snapshot(routed.request),
                )
                return TokenCountResponse(input_tokens=tokens)
            except ProviderError:
                raise
            except Exception as e:
                _log_unexpected_service_exception(
                    self._settings,
                    e,
                    context="COUNT_TOKENS_ERROR",
                    request_id=request_id,
                )
                raise HTTPException(
                    status_code=_http_status_for_unexpected_service_exception(e),
                    detail=get_user_facing_error_message(e),
                ) from e
