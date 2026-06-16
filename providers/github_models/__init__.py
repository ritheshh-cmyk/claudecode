"""GitHub Models adapter (OpenAI-compatible chat completions)."""

from config.provider_catalog import GITHUB_MODELS_DEFAULT_BASE

from .client import GitHubModelsProvider

__all__ = ["GITHUB_MODELS_DEFAULT_BASE", "GitHubModelsProvider"]
