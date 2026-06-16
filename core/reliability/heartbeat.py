from __future__ import annotations

import asyncio
import contextlib
import importlib
from typing import Any

from loguru import logger

from core.trace import trace_event


def get_active_provider_ids(settings: Any) -> list[str]:
    """Identify which providers have credentials configured in settings."""
    PROVIDER_CATALOG = importlib.import_module(
        "config.provider_catalog"
    ).PROVIDER_CATALOG
    active = []
    for provider_id, descriptor in PROVIDER_CATALOG.items():
        if descriptor.static_credential is not None:
            active.append(provider_id)
            continue
        if descriptor.credential_attr:
            val = getattr(settings, descriptor.credential_attr, "")
            if isinstance(val, str) and val.strip():
                active.append(provider_id)
    return active


class HeartbeatChecker:
    """Background task checking provider endpoint health."""

    def __init__(self, settings: Any, registry: Any) -> None:
        self.settings = settings
        self.registry = registry
        self._health_status: dict[str, bool] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def is_healthy(self, provider_id: str) -> bool:
        """Return True if the provider is healthy, or hasn't been checked yet."""
        return self._health_status.get(provider_id, True)

    def start(self) -> None:
        """Start the background check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Heartbeat checker started with interval {}s",
            getattr(self.settings, "heartbeat_interval", 60),
        )

    async def stop(self) -> None:
        """Stop the background check loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Heartbeat checker stopped")

    async def check_all(self) -> None:
        """Query all active providers' health concurrently."""
        active_ids = get_active_provider_ids(self.settings)
        if not active_ids:
            return

        tasks = {}
        for provider_id in active_ids:
            try:
                provider = self.registry.get(provider_id, self.settings)
                tasks[provider_id] = asyncio.create_task(provider.list_model_ids())
            except Exception as exc:
                self._health_status[provider_id] = False
                trace_event(
                    stage="reliability",
                    event="heartbeat.check",
                    source="heartbeat",
                    provider=provider_id,
                    healthy=False,
                    error=str(exc),
                )
                logger.warning(
                    "Heartbeat check failed to initialize provider {}: {}",
                    provider_id,
                    exc,
                )

        if not tasks:
            return

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for provider_id, result in zip(tasks.keys(), results, strict=True):
            if isinstance(result, Exception):
                self._health_status[provider_id] = False
                trace_event(
                    stage="reliability",
                    event="heartbeat.check",
                    source="heartbeat",
                    provider=provider_id,
                    healthy=False,
                    error=str(result),
                )
                logger.warning(
                    "Heartbeat check failed for provider {}: {}",
                    provider_id,
                    result,
                )
            else:
                self._health_status[provider_id] = True
                trace_event(
                    stage="reliability",
                    event="heartbeat.check",
                    source="heartbeat",
                    provider=provider_id,
                    healthy=True,
                )
                logger.debug("Heartbeat check passed for provider {}", provider_id)

    async def _run_loop(self) -> None:
        """Background loop executing checks periodically."""
        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                logger.error("Error in heartbeat check loop: {}", e)

            try:
                await asyncio.sleep(getattr(self.settings, "heartbeat_interval", 60))
            except asyncio.CancelledError:
                break
