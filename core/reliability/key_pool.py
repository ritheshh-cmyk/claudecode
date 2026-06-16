from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from core.reliability.key_encrypt import decrypt_key, encrypt_key


class KeyPool:
    """Manages multiple API keys per provider, rotates them, tracks cooldowns, enforces quotas, and encrypts them at rest."""

    def __init__(self, keys_by_provider: dict[str, list[str]] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._indices: dict[str, int] = {}

        # Internal key database
        # Keys are structured as: { provider_id: [ { "key": str, "alias": str, "revoked": bool, "quota": int, "request_times": list[float], "cooldown_expiry": float } ] }
        self._key_db: dict[str, list[dict[str, Any]]] = {}

        # Initialize from keys_by_provider (usually loaded from settings/env)
        if keys_by_provider:
            for provider, keys in keys_by_provider.items():
                for key in keys:
                    self._add_key_in_memory(provider, key)

        # Scan extra environment variables automatically (e.g. AEROLINK_API_KEY_1 to 20)
        self._scan_env_keys()

        # Load persisted encrypted keys
        self._load_persisted_keys()

    def _add_key_in_memory(
        self,
        provider_id: str,
        key: str,
        alias: str | None = None,
        quota: int = 0,
        revoked: bool = False,
    ) -> dict[str, Any]:
        """Helper to add a key to memory state if it doesn't already exist."""
        if provider_id not in self._key_db:
            self._key_db[provider_id] = []

        # Check if key already exists
        for k_entry in self._key_db[provider_id]:
            if k_entry["key"] == key:
                # Update attributes if specified
                if alias:
                    k_entry["alias"] = alias
                if quota:
                    k_entry["quota"] = quota
                k_entry["revoked"] = revoked
                return k_entry

        if not alias:
            short_key = key[-6:] if len(key) >= 6 else "key"
            alias = f"{provider_id}-{short_key}"

        entry = {
            "key": key,
            "alias": alias,
            "revoked": revoked,
            "quota": quota,
            "request_times": [],
            "cooldown_expiry": 0.0,
        }
        self._key_db[provider_id].append(entry)
        return entry

    def _scan_env_keys(self) -> None:
        """Scan environment variables dynamically for multi-key pool setup (e.g., PROVIDER_API_KEY_1..20)."""
        for env_name, val in os.environ.items():
            if not val or not val.strip():
                continue
            env_upper = env_name.upper()
            if env_upper.endswith("_API_KEY"):
                provider_id = env_upper[:-8].lower()
                self._add_key_in_memory(
                    provider_id, val.strip(), alias=f"{provider_id}-env-primary"
                )
            elif "_API_KEY_" in env_upper:
                parts = env_upper.split("_API_KEY_")
                if len(parts) == 2 and parts[1].isdigit():
                    provider_id = parts[0].lower()
                    idx = parts[1]
                    self._add_key_in_memory(
                        provider_id, val.strip(), alias=f"{provider_id}-env-{idx}"
                    )

    def _get_persistence_path(self) -> Path:
        fcc_dir = Path.home() / ".fcc"
        fcc_dir.mkdir(parents=True, exist_ok=True)
        return fcc_dir / "keys.json"

    def _load_persisted_keys(self) -> None:
        """Load AES-256 encrypted keys from keys.json."""
        path = self._get_persistence_path()
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            persisted_keys = data.get("keys", [])
            for item in persisted_keys:
                prov = item.get("provider_id")
                enc_key = item.get("encrypted_key")
                alias = item.get("alias")
                quota = item.get("quota", 0)
                revoked = item.get("revoked", False)

                if prov and enc_key:
                    raw_key = decrypt_key(enc_key)
                    if raw_key:
                        self._add_key_in_memory(
                            provider_id=prov,
                            key=raw_key,
                            alias=alias,
                            quota=quota,
                            revoked=revoked,
                        )
        except Exception as e:
            logger.error("Failed to load persisted keys: {}", e)

    def _save_persisted_keys(self) -> None:
        """Encrypt and save key database to keys.json."""
        path = self._get_persistence_path()
        try:
            serialized_keys = []
            for prov, keys in self._key_db.items():
                for k_entry in keys:
                    # Only persist keys that are not temporary env-based keys, or persist everything if needed.
                    # We can encrypt all keys.
                    enc = encrypt_key(k_entry["key"])
                    serialized_keys.append(
                        {
                            "provider_id": prov,
                            "encrypted_key": enc,
                            "alias": k_entry["alias"],
                            "quota": k_entry["quota"],
                            "revoked": k_entry["revoked"],
                        }
                    )

            with open(path, "w", encoding="utf-8") as f:
                json.dump({"keys": serialized_keys}, f, indent=2)
        except Exception as e:
            logger.error("Failed to save keys: {}", e)

    async def add_key(
        self,
        provider_id: str,
        key: str,
        alias: str | None = None,
        quota: int = 0,
        revoked: bool = False,
    ) -> None:
        """Add a key for a given provider, encrypting and persisting it."""
        async with self._lock:
            self._add_key_in_memory(
                provider_id=provider_id,
                key=key,
                alias=alias,
                quota=quota,
                revoked=revoked,
            )
            self._save_persisted_keys()

    async def remove_key(self, provider_id: str, key: str) -> None:
        """Remove a key for a given provider."""
        async with self._lock:
            if provider_id in self._key_db:
                self._key_db[provider_id] = [
                    k for k in self._key_db[provider_id] if k["key"] != key
                ]
                if not self._key_db[provider_id]:
                    self._key_db.pop(provider_id, None)
                    self._indices.pop(provider_id, None)
            self._save_persisted_keys()

    def has_keys(self, provider_id: str) -> bool:
        """Return True if active keys are configured for the provider in the pool."""
        keys = self._key_db.get(provider_id, [])
        return any(not k["revoked"] for k in keys)

    async def get_key(self, provider_id: str) -> str | None:
        """Get the next available (not revoked, not on cooldown, under quota) key, rotating round-robin."""
        async with self._lock:
            keys = self._key_db.get(provider_id)
            if not keys:
                return None

            now_monotonic = time.monotonic()
            now_real = time.time()
            n = len(keys)
            start_idx = self._indices.get(provider_id, 0)

            # Find next key round-robin
            for i in range(n):
                idx = (start_idx + i) % n
                candidate = keys[idx]

                # 1. Skip if revoked
                if candidate["revoked"]:
                    continue

                # 2. Skip if on cooldown
                if candidate["cooldown_expiry"] > now_monotonic:
                    continue

                # 3. Check quota (max requests per hour)
                quota = candidate.get("quota", 0)
                if quota > 0:
                    # Prune old request times (> 1 hour)
                    candidate["request_times"] = [
                        t for t in candidate["request_times"] if now_real - t < 3600
                    ]
                    if len(candidate["request_times"]) >= quota:
                        logger.warning(
                            "Key '{}' for provider '{}' exceeded hourly quota ({} requests). Skipping.",
                            candidate["alias"],
                            provider_id,
                            quota,
                        )
                        continue

                # Record usage time for quota
                candidate["request_times"].append(now_real)
                self._indices[provider_id] = (idx + 1) % n
                return candidate["key"]

            # All keys are revoked, on cooldown, or over quota
            return None

    async def report_429(
        self, provider_id: str, key: str, cooldown_duration: float = 60.0
    ) -> None:
        """Put a key on cooldown for the given duration."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if k_entry["key"] == key:
                    k_entry["cooldown_expiry"] = time.monotonic() + cooldown_duration
                    break

    async def report_success(self, provider_id: str, key: str) -> None:
        """Clear cooldown status for a key upon a successful request."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if k_entry["key"] == key:
                    k_entry["cooldown_expiry"] = 0.0
                    break

    async def is_on_cooldown(self, provider_id: str, key: str) -> bool:
        """Check if a key is currently on cooldown."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if k_entry["key"] == key:
                    if k_entry["cooldown_expiry"] > time.monotonic():
                        return True
                    else:
                        k_entry["cooldown_expiry"] = 0.0
                        return False
            return False

    async def get_all_keys_status(self) -> list[dict[str, Any]]:
        """Return metadata of all keys (without raw key strings) for admin dashboard."""
        async with self._lock:
            status_list = []
            now_monotonic = time.monotonic()
            now_real = time.time()
            for prov, keys in self._key_db.items():
                for k_entry in keys:
                    cooldown_rem = max(0.0, k_entry["cooldown_expiry"] - now_monotonic)

                    # Clean/prune quota timestamps
                    k_entry["request_times"] = [
                        t for t in k_entry["request_times"] if now_real - t < 3600
                    ]

                    status_list.append(
                        {
                            "provider_id": prov,
                            "alias": k_entry["alias"],
                            "revoked": k_entry["revoked"],
                            "quota": k_entry["quota"],
                            "cooldown_remaining": round(cooldown_rem, 1),
                            "used_quota_hour": len(k_entry["request_times"]),
                            # Redact key for security in response
                            "masked_key": (
                                k_entry["key"][:6] + "..." + k_entry["key"][-4:]
                                if len(k_entry["key"]) >= 10
                                else "..."
                            ),
                            "key_hash": hash(k_entry["key"]),
                        }
                    )
            return status_list

    async def toggle_revoke_key(self, provider_id: str, key_hash: int) -> bool:
        """Toggle revocation status of a key using its hash."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if hash(k_entry["key"]) == key_hash:
                    k_entry["revoked"] = not k_entry["revoked"]
                    self._save_persisted_keys()
                    return True
            return False

    async def update_key_meta(
        self, provider_id: str, key_hash: int, alias: str, quota: int
    ) -> bool:
        """Update key metadata (alias, quota)."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if hash(k_entry["key"]) == key_hash:
                    k_entry["alias"] = alias
                    k_entry["quota"] = quota
                    self._save_persisted_keys()
                    return True
            return False

    async def remove_key_by_hash(self, provider_id: str, key_hash: int) -> bool:
        """Remove a key using its hash."""
        async with self._lock:
            keys = self._key_db.get(provider_id, [])
            for k_entry in keys:
                if hash(k_entry["key"]) == key_hash:
                    keys.remove(k_entry)
                    if not keys:
                        self._key_db.pop(provider_id, None)
                        self._indices.pop(provider_id, None)
                    self._save_persisted_keys()
                    return True
            return False
