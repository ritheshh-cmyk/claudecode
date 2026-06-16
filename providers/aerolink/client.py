"""Aerolink provider implementation (Anthropic-compatible Messages API)."""

from __future__ import annotations

from typing import Any

import httpx

from config.provider_catalog import AEROLINK_DEFAULT_BASE
from providers.anthropic_messages import AnthropicMessagesTransport
from providers.base import ProviderConfig

_ANTHROPIC_VERSION = "2023-06-01"


class AerolinkProvider(AnthropicMessagesTransport):
    """Aerolink using Anthropic-compatible Messages at capi.aerolink.lat/v1."""

    def __init__(self, config: ProviderConfig, settings: Any):
        super().__init__(
            config,
            provider_name="AEROLINK",
            default_base_url=AEROLINK_DEFAULT_BASE,
        )
        self._settings = settings

    async def _send_stream_request(self, body: dict) -> httpx.Response:
        """Create a streaming messages response using the resolved API key."""
        model = body.get("model", "")
        # Resolve key: check model prefix or suffix
        api_key = self._api_key  # default key

        if (
            "opus" in model.lower()
            and getattr(self._settings, "aerolink_api_key_opus", "").strip()
        ):
            api_key = self._settings.aerolink_api_key_opus
        elif (
            "sonnet" in model.lower()
            and getattr(self._settings, "aerolink_api_key_sonnet", "").strip()
        ):
            api_key = self._settings.aerolink_api_key_sonnet
        elif (
            "haiku" in model.lower()
            and getattr(self._settings, "aerolink_api_key_haiku", "").strip()
        ):
            api_key = self._settings.aerolink_api_key_haiku

        request = self._client.build_request(
            "POST",
            "/messages",
            json=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
        )
        return await self._client.send(request, stream=True)

    def _model_list_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
