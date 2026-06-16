from __future__ import annotations

import asyncio
import time


class KeyPool:
    """Manages multiple API keys per provider, rotates them, and tracks cooldowns."""

    def __init__(self, keys_by_provider: dict[str, list[str]] | None = None) -> None:
        self._keys: dict[str, list[str]] = {}
        if keys_by_provider:
            for provider, keys in keys_by_provider.items():
                self._keys[provider] = list(keys)
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._indices: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def add_key(self, provider_id: str, key: str) -> None:
        """Add a key for a given provider."""
        async with self._lock:
            if provider_id not in self._keys:
                self._keys[provider_id] = []
            if key not in self._keys[provider_id]:
                self._keys[provider_id].append(key)

    async def remove_key(self, provider_id: str, key: str) -> None:
        """Remove a key for a given provider."""
        async with self._lock:
            if provider_id in self._keys and key in self._keys[provider_id]:
                self._keys[provider_id].remove(key)
                self._cooldowns.pop((provider_id, key), None)

            # Clean up empty providers
            if provider_id in self._keys and not self._keys[provider_id]:
                self._keys.pop(provider_id)
                self._indices.pop(provider_id, None)

    def has_keys(self, provider_id: str) -> bool:
        """Return True if keys are configured for the provider in the pool."""
        return bool(self._keys.get(provider_id))

    async def get_key(self, provider_id: str) -> str | None:
        """Get the next available (not on cooldown) key for the provider, rotating them round-robin."""
        async with self._lock:
            keys = self._keys.get(provider_id)
            if not keys:
                return None

            now = time.monotonic()
            # Clean expired cooldowns
            self._prune_cooldowns(now)

            n = len(keys)
            start_idx = self._indices.get(provider_id, 0)

            # Try to find a key starting from the next index in round-robin fashion
            for i in range(n):
                idx = (start_idx + i) % n
                candidate = keys[idx]
                if (provider_id, candidate) not in self._cooldowns:
                    self._indices[provider_id] = (idx + 1) % n
                    return candidate

            # All keys are on cooldown
            return None

    async def report_429(
        self, provider_id: str, key: str, cooldown_duration: float = 60.0
    ) -> None:
        """Put a key on cooldown for the given duration."""
        async with self._lock:
            expiry = time.monotonic() + cooldown_duration
            self._cooldowns[(provider_id, key)] = expiry

    async def report_success(self, provider_id: str, key: str) -> None:
        """Clear cooldown status for a key upon a successful request."""
        async with self._lock:
            self._cooldowns.pop((provider_id, key), None)

    async def is_on_cooldown(self, provider_id: str, key: str) -> bool:
        """Check if a key is currently on cooldown."""
        async with self._lock:
            expiry = self._cooldowns.get((provider_id, key))
            if expiry is None:
                return False
            if time.monotonic() >= expiry:
                self._cooldowns.pop((provider_id, key), None)
                return False
            return True

    def _prune_cooldowns(self, now: float) -> None:
        expired = [k for k, expiry in self._cooldowns.items() if now >= expiry]
        for k in expired:
            self._cooldowns.pop(k, None)
