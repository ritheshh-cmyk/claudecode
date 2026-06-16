import random
from dataclasses import dataclass

from loguru import logger

from config.provider_ids import SUPPORTED_PROVIDER_IDS
from config.settings import Settings
from core.anthropic import get_token_count

from .gateway_model_ids import decode_gateway_model_id
from .models.anthropic import MessagesRequest, TokenCountRequest

# Module-level storage for sticky sessions and failover memory
_SESSION_PROVIDER_MAP: dict[str, ResolvedModel] = {}
_LAST_SUCCESSFUL_PROVIDER: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    original_model: str
    provider_id: str
    provider_model: str
    provider_model_ref: str
    thinking_enabled: bool


@dataclass(frozen=True, slots=True)
class RoutedMessagesRequest:
    request: MessagesRequest
    resolved: ResolvedModel


@dataclass(frozen=True, slots=True)
class RoutedTokenCountRequest:
    request: TokenCountRequest
    resolved: ResolvedModel


class ModelRouter:
    """Resolve incoming Claude model names to configured provider/model pairs."""

    def __init__(self, settings: Settings):
        self._settings = settings

    @classmethod
    def set_last_successful_provider(cls, provider_id: str) -> None:
        """Store the last successful provider ID in failover memory."""
        global _LAST_SUCCESSFUL_PROVIDER
        _LAST_SUCCESSFUL_PROVIDER = provider_id
        logger.debug(
            "Failover Memory: Updated last successful provider to '{}'", provider_id
        )

    def _apply_aliases(self, model_name: str) -> str:
        alias_str = getattr(self._settings, "model_aliases", "")
        if not alias_str:
            return model_name
        aliases = {}
        for item in alias_str.split(","):
            if ":" in item:
                k, v = item.split(":", 1)
                aliases[k.strip()] = v.strip()
        return aliases.get(model_name, model_name)

    def resolve(self, claude_model_name: str) -> ResolvedModel:
        claude_model_name = self._apply_aliases(claude_model_name)
        (
            direct_provider_id,
            direct_provider_model,
            force_thinking_enabled,
        ) = self._direct_provider_model(claude_model_name)
        if direct_provider_id is not None and direct_provider_model is not None:
            thinking_enabled = (
                force_thinking_enabled
                if force_thinking_enabled is not None
                else self._settings.resolve_thinking(direct_provider_model)
            )
            logger.debug(
                "MODEL DIRECT: '{}' -> provider='{}' model='{}' thinking={}",
                claude_model_name,
                direct_provider_id,
                direct_provider_model,
                thinking_enabled,
            )
            return ResolvedModel(
                original_model=claude_model_name,
                provider_id=direct_provider_id,
                provider_model=direct_provider_model,
                provider_model_ref=claude_model_name,
                thinking_enabled=thinking_enabled,
            )

        provider_model_ref = self._settings.resolve_model(claude_model_name)
        thinking_enabled = self._settings.resolve_thinking(claude_model_name)
        provider_id = Settings.parse_provider_type(provider_model_ref)
        provider_model = Settings.parse_model_name(provider_model_ref)
        if provider_model != claude_model_name:
            logger.debug(
                "MODEL MAPPING: '{}' -> '{}'", claude_model_name, provider_model
            )
        return ResolvedModel(
            original_model=claude_model_name,
            provider_id=provider_id,
            provider_model=provider_model,
            provider_model_ref=provider_model_ref,
            thinking_enabled=thinking_enabled,
        )

    def _direct_provider_model(
        self, model_name: str
    ) -> tuple[str | None, str | None, bool | None]:
        decoded = decode_gateway_model_id(model_name)
        if decoded is not None:
            if decoded.provider_id not in SUPPORTED_PROVIDER_IDS:
                return None, None, None
            return (
                decoded.provider_id,
                decoded.provider_model,
                decoded.force_thinking_enabled,
            )

        provider_id, separator, provider_model = model_name.partition("/")
        if not separator:
            return None, None, None
        if provider_id not in SUPPORTED_PROVIDER_IDS:
            return None, None, None
        if not provider_model:
            return None, None, None
        return provider_id, provider_model, None

    def resolve_messages_request(
        self, request: MessagesRequest, headers: dict[str, str] | None = None
    ) -> RoutedMessagesRequest:
        """Return routed request context using smart routing rules."""
        model_name = request.model

        # 1. Model Alias Map (67)
        model_name = self._apply_aliases(model_name)

        # 2. Sticky Sessions (71)
        session_id = None
        if headers and getattr(self._settings, "enable_sticky_sessions", False):
            from core.trace import extract_claude_session_id_from_headers

            session_id = extract_claude_session_id_from_headers(headers)
            if session_id and session_id in _SESSION_PROVIDER_MAP:
                resolved = _SESSION_PROVIDER_MAP[session_id]
                logger.debug(
                    "Sticky Sessions: Reused resolved model '{}' for session '{}'",
                    resolved.provider_model_ref,
                    session_id,
                )
                routed = request.model_copy(deep=True)
                routed.model = resolved.provider_model
                return RoutedMessagesRequest(request=routed, resolved=resolved)

        # Extract user text for heuristic classification
        user_text = ""
        for msg in request.messages:
            if msg.role == "user":
                if isinstance(msg.content, str):
                    user_text += " " + msg.content
                elif isinstance(msg.content, list):
                    for part in msg.content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            user_text += " " + part.get("text", "")

        # Calculate token count if context length routing is active
        input_tokens = 0
        if getattr(self._settings, "enable_context_length_routing", False):
            input_tokens = get_token_count(
                request.messages, request.system, request.tools
            )

        # 3. Context-Length Router (63)
        if getattr(self._settings, "enable_context_length_routing", False):
            threshold = getattr(self._settings, "context_length_threshold", 100000)
            if input_tokens > threshold:
                target = getattr(
                    self._settings,
                    "context_length_provider_model",
                    "gemini/gemini-2.5-pro",
                )
                logger.info(
                    "Context-Length Router: Prompt tokens ({}) > threshold ({}). Routing to {}",
                    input_tokens,
                    threshold,
                    target,
                )
                model_name = target

        # 4. Tool-Use Router (74)
        elif (
            getattr(self._settings, "enable_tool_use_routing", False) and request.tools
        ):
            logger.info(
                "Tool-Use Router: Request contains tools. Prioritizing tool-optimized model."
            )
            model_name = "openai/gpt-4o"

        # 5. Language-Based Router (73)
        elif getattr(self._settings, "enable_language_routing", False):
            non_ascii_chars = sum(1 for c in user_text if ord(c) > 127)
            if len(user_text) > 0 and (non_ascii_chars / len(user_text)) > 0.15:
                logger.info(
                    "Language-Based Router: Non-English text detected. Routing to multilingual-optimized model."
                )
                model_name = "gemini/gemini-2.5-pro"

        # 6. Task-Type Detector (65)
        elif getattr(self._settings, "enable_task_type_routing", False):
            coding_keywords = [
                "def ",
                "function",
                "class ",
                "import ",
                "git",
                "docker",
                "rust",
                "java",
                "python",
                "code",
                "bug",
                "refactor",
                "lint",
            ]
            math_keywords = [
                "math",
                "equation",
                "solve",
                "integral",
                "matrix",
                "derivative",
                "formula",
                "calculate",
            ]
            text_lower = user_text.lower()

            is_coding = any(kw in text_lower for kw in coding_keywords)
            is_math = any(kw in text_lower for kw in math_keywords)

            if is_coding:
                logger.info(
                    "Task-Type Detector: Coding task detected. Routing to coding-tuned model."
                )
                model_name = "github_models/claude-3-5-sonnet"
            elif is_math:
                logger.info(
                    "Task-Type Detector: Math task detected. Routing to math/reasoning model."
                )
                model_name = "gemini/gemini-2.5-pro"

        # 7. Time-Based Routing (66)
        elif getattr(self._settings, "enable_time_based_routing", False):
            import datetime

            hour = datetime.datetime.now().hour
            night_start = getattr(self._settings, "time_based_night_start", 22)
            night_end = getattr(self._settings, "time_based_night_end", 7)
            is_night = False
            if night_start > night_end:
                is_night = hour >= night_start or hour < night_end
            else:
                is_night = night_start <= hour < night_end

            if is_night:
                logger.info(
                    "Time-Based Router: Night time detected. Routing to free provider."
                )
                model_name = "aerolink/claude-3-5-sonnet"

        # 8. Canary Mode (70)
        elif getattr(self._settings, "enable_canary_mode", False):
            canary_model = getattr(self._settings, "canary_provider_model", "")
            canary_pct = getattr(self._settings, "canary_percentage", 5.0)
            if canary_model and random.random() * 100.0 < canary_pct:
                logger.info(
                    "Canary Mode: Routing {}% traffic to canary model {}",
                    canary_pct,
                    canary_model,
                )
                model_name = canary_model

        # 9. Cost-Aware Router (64)
        elif getattr(self._settings, "enable_cost_aware_routing", False):
            if "/" not in model_name:
                logger.info(
                    "Cost-Aware Router: Generic model requested. Directing to free provider to save cost."
                )
                model_name = "aerolink/claude-3-5-sonnet"

        # 10. Failover Memory (72)
        if (
            getattr(self._settings, "enable_failover_memory", False)
            and _LAST_SUCCESSFUL_PROVIDER
            and "/" not in model_name
        ):
            logger.debug(
                "Failover Memory: Preferring last working provider '{}'",
                _LAST_SUCCESSFUL_PROVIDER,
            )
            model_name = f"{_LAST_SUCCESSFUL_PROVIDER}/{model_name}"

        # 11. Provider Priority Groups (68)
        priority_groups_str = getattr(self._settings, "provider_priority_groups", "")
        if priority_groups_str and "/" not in model_name:
            groups = []
            for group_part in priority_groups_str.split(";"):
                if ":" in group_part:
                    _, provs = group_part.split(":", 1)
                    groups.append([p.strip() for p in provs.split(",")])

            target_provider = None
            for prov_list in groups:
                for p in prov_list:
                    if p == "aerolink" or getattr(self._settings, f"{p}_api_key", ""):
                        target_provider = p
                        break
                if target_provider:
                    break
            if target_provider:
                logger.debug(
                    "Provider Priority Groups: Routed to provider '{}' based on group order",
                    target_provider,
                )
                model_name = f"{target_provider}/{model_name}"

        # Resolve model name
        resolved = self.resolve(model_name)

        # Save sticky session state if enabled
        if session_id and getattr(self._settings, "enable_sticky_sessions", False):
            _SESSION_PROVIDER_MAP[session_id] = resolved
            if len(_SESSION_PROVIDER_MAP) > 1000:
                first_key = next(iter(_SESSION_PROVIDER_MAP))
                _SESSION_PROVIDER_MAP.pop(first_key, None)

        routed = request.model_copy(deep=True)
        routed.model = resolved.provider_model
        return RoutedMessagesRequest(request=routed, resolved=resolved)

    def resolve_token_count_request(
        self, request: TokenCountRequest
    ) -> RoutedTokenCountRequest:
        """Return an internal token-count request context."""
        resolved = self.resolve(request.model)
        routed = request.model_copy(
            update={"model": resolved.provider_model}, deep=True
        )
        return RoutedTokenCountRequest(request=routed, resolved=resolved)
