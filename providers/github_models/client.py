"""GitHub Models provider implementation (OpenAI-compatible chat completions).

Uses the official GitHub Models API at models.inference.ai.azure.com.
Free for all GitHub users — requires a GitHub Personal Access Token (PAT).
Supports Claude 3.5/3.7 Sonnet, GPT-4o, o1, Llama, Mistral, and more.

See: https://docs.github.com/en/github-models
"""

from __future__ import annotations

from typing import Any

from providers.base import ProviderConfig
from providers.defaults import GITHUB_MODELS_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport

from .request import build_request_body


class GitHubModelsProvider(OpenAIChatTransport):
    """GitHub Models API using ``https://models.inference.ai.azure.com``."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="GITHUB_MODELS",
            base_url=config.base_url or GITHUB_MODELS_DEFAULT_BASE,
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict:
        return build_request_body(
            request,
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )

    async def list_model_ids(self) -> frozenset[str]:
        """Return model ids from GitHub Models by parsing the raw JSON array."""
        import httpx
        headers = {"Authorization": f"Bearer {self._api_key}"}
        proxy = self._config.proxy if self._config.proxy else None
        timeout = httpx.Timeout(
            self._config.http_read_timeout,
            connect=self._config.http_connect_timeout,
            read=self._config.http_read_timeout,
            write=self._config.http_write_timeout,
        )
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
            response = await client.get(f"{self._base_url}/models", headers=headers)
            response.raise_for_status()
            data = response.json()
        model_ids = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item:
                    model_ids.add(item["name"])
        return frozenset(model_ids)
