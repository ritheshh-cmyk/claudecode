from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class DeadLetterQueue:
    """Renders failed request details to a local JSONL file for persistence."""

    def __init__(self, filepath: Path | None = None) -> None:
        self.filepath = filepath or Path.home() / ".fcc" / "dead_letters.jsonl"
        self._lock = asyncio.Lock()

    async def record_failure(
        self,
        provider_id: str,
        payload: dict[str, Any],
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log failed request details to the dead letters JSONL file."""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "provider_id": provider_id,
            "payload": payload,
            "error": error,
            "metadata": metadata or {},
        }
        async with self._lock:
            # Run blocking file I/O in executor to keep event loop free
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._append_entry, entry)

    def _append_entry(self, entry: dict[str, Any]) -> None:
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    async def read_failures(self) -> list[dict[str, Any]]:
        """Read all failures recorded in the JSONL file."""
        if not self.filepath.is_file():
            return []
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._read_entries)

    def _read_entries(self) -> list[dict[str, Any]]:
        entries = []
        with open(self.filepath, encoding="utf-8") as f:
            for line in f:
                if stripped := line.strip():
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
        return entries

    async def clear(self) -> None:
        """Clear all entries by deleting the JSONL file."""
        async with self._lock:
            if self.filepath.is_file():
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.filepath.unlink, True)
            if self.filepath.parent.exists() and not any(
                self.filepath.parent.iterdir()
            ):
                import contextlib

                with contextlib.suppress(OSError):
                    self.filepath.parent.rmdir()
