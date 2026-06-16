from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

STOP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "is",
    "are",
    "was",
    "were",
    "to",
    "for",
    "in",
    "on",
    "at",
    "by",
    "of",
    "with",
    "from",
    "that",
    "this",
    "these",
    "those",
    "then",
    "it",
    "he",
    "she",
    "they",
    "we",
    "i",
}


def _get_words(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric words, filtering out stop words."""
    words = re.findall(r"\w+", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def _cosine_similarity(words1: list[str], words2: list[str]) -> float:
    """Calculate cosine similarity of two word frequency vectors."""
    if not words1 or not words2:
        return 0.0
    vec1: dict[str, int] = {}
    vec2: dict[str, int] = {}
    for w in words1:
        vec1[w] = vec1.get(w, 0) + 1
    for w in words2:
        vec2[w] = vec2.get(w, 0) + 1

    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum(vec1[w] * vec2[w] for w in intersection)

    sum1 = sum(val**2 for val in vec1.values())
    sum2 = sum(val**2 for val in vec2.values())
    denominator = math.sqrt(sum1) * math.sqrt(sum2)

    if not denominator:
        return 0.0
    return numerator / denominator


class ResponseCache:
    """In-memory cache for prompt responses supporting both exact and semantic matches with TTL control."""

    def __init__(
        self,
        ttl: int = 3600,
        semantic_threshold: float = 0.90,
        max_size: int = 128,
    ) -> None:
        self.ttl = ttl
        self.semantic_threshold = semantic_threshold
        self.max_size = max_size
        # Cache maps key_hash: dict with keys {"chunks": list[str], "expires_at": float, "prompt_words": list[str], "raw_request": dict}
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _generate_exact_key(self, request_dict: dict[str, Any]) -> str:
        """Create a deterministic hash of the request dictionary."""
        # Normalize request dict to ignore mutable or session-specific fields
        normalized = {
            "model": request_dict.get("model"),
            "messages": request_dict.get("messages"),
            "system": request_dict.get("system"),
            "tools": request_dict.get("tools"),
            "temperature": request_dict.get("temperature"),
            "max_tokens": request_dict.get("max_tokens"),
        }
        serialized = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _extract_prompt_text(self, request_dict: dict[str, Any]) -> str:
        """Extract the user's latest text prompt from request."""
        messages = request_dict.get("messages", [])
        if not messages:
            return ""
        # Get last user message content
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, list):
                    parts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    last_user_msg = " ".join(parts)
                elif isinstance(content, str):
                    last_user_msg = content
                break
        return last_user_msg

    def get(
        self, request_dict: dict[str, Any], enable_semantic: bool = False
    ) -> list[str] | None:
        """Retrieve cached response chunks if there is an exact or semantic hit and it hasn't expired."""
        now = time.time()
        exact_key = self._generate_exact_key(request_dict)

        # 1. Try exact hit
        entry = self._cache.get(exact_key)
        if entry:
            if entry["expires_at"] > now:
                self._cache.move_to_end(exact_key)
                logger.info("Exact cache hit for prompt hash {}", exact_key)
                return entry["chunks"]
            else:
                self._cache.pop(exact_key, None)

        # 2. Try semantic hit if enabled
        if enable_semantic:
            prompt_text = self._extract_prompt_text(request_dict)
            if prompt_text:
                target_words = _get_words(prompt_text)
                for cached_key, cached_entry in list(self._cache.items()):
                    if cached_entry["expires_at"] <= now:
                        self._cache.pop(cached_key, None)
                        continue

                    # Check model matching first
                    if cached_entry["raw_request"].get("model") != request_dict.get(
                        "model"
                    ):
                        continue

                    similarity = _cosine_similarity(
                        target_words, cached_entry["prompt_words"]
                    )
                    if similarity >= self.semantic_threshold:
                        self._cache.move_to_end(cached_key)
                        logger.info(
                            "Semantic cache hit (similarity: {:.2f}) for prompt hash {}",
                            similarity,
                            cached_key,
                        )
                        return cached_entry["chunks"]

        return None

    def set(
        self,
        request_dict: dict[str, Any],
        chunks: list[str],
        custom_ttl: int | None = None,
    ) -> None:
        """Save response chunks in cache with a TTL."""
        exact_key = self._generate_exact_key(request_dict)
        ttl = custom_ttl if custom_ttl is not None else self.ttl
        expires_at = time.time() + (ttl if ttl > 0 else 999999999.0)

        prompt_text = self._extract_prompt_text(request_dict)
        prompt_words = _get_words(prompt_text)

        # Enforce LRU eviction
        if exact_key in self._cache:
            self._cache.pop(exact_key)
        elif len(self._cache) >= self.max_size:
            oldest_key, _ = self._cache.popitem(last=False)
            logger.info("Evicted oldest cache entry {} to free memory", oldest_key)

        self._cache[exact_key] = {
            "chunks": list(chunks),
            "expires_at": expires_at,
            "prompt_words": prompt_words,
            "raw_request": request_dict,
        }
        logger.info("Cached response for prompt hash {}, TTL={}s", exact_key, ttl)

    def clear(self) -> None:
        """Flush the cache."""
        self._cache.clear()
        logger.info("Response cache cleared.")
