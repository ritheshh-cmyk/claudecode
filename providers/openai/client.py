"""OpenAI provider implementation (OpenAI-compatible chat completions).

Direct access to OpenAI's API (api.openai.com/v1).
Supports GPT-4o, GPT-4o-mini, o1, o3-mini, and all other OpenAI models.
Requires an OpenAI API key from https://platform.openai.com/api-keys.
"""

from __future__ import annotations

from typing import Any

from providers.base import ProviderConfig
from providers.defaults import OPENAI_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport

from .request import build_request_body


class OpenAIProvider(OpenAIChatTransport):
    """OpenAI API using ``https://api.openai.com/v1/chat/completions``."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="OPENAI",
            base_url=config.base_url or OPENAI_DEFAULT_BASE,
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict:
        return build_request_body(
            request,
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )
