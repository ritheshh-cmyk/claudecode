"""OpenAI adapter (OpenAI-compatible chat completions)."""

from config.provider_catalog import OPENAI_DEFAULT_BASE

from .client import OpenAIProvider

__all__ = ["OPENAI_DEFAULT_BASE", "OpenAIProvider"]
