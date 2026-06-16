from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from datetime import time as datetime_time
from typing import Any

from loguru import logger

from core.reliability.heartbeat import HeartbeatChecker, get_active_provider_ids
from core.trace import trace_event


async def warmup_connection_pools(registry: Any, settings: Any) -> None:
    """Warm up connection pools for active providers by querying model listings."""
    active_ids = get_active_provider_ids(settings)
    if not active_ids:
        logger.info("No active providers to warm up connection pools for")
        return

    logger.info("Warming up connection pools for active providers: {}", active_ids)

    tasks = {}
    for provider_id in active_ids:
        try:
            provider = registry.get(provider_id, settings)
            tasks[provider_id] = asyncio.create_task(provider.list_model_infos())
        except Exception as e:
            logger.warning("Failed to start warmup for provider {}: {}", provider_id, e)

    if not tasks:
        return

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for provider_id, result in zip(tasks.keys(), results, strict=True):
        if isinstance(result, Exception):
            logger.warning("Warmup failed for provider {}: {}", provider_id, result)
            trace_event(
                stage="reliability",
                event="warmup.failure",
                source="warmup",
                provider=provider_id,
                error=str(result),
            )
        else:
            logger.info("Warmup successful for provider {}", provider_id)
            trace_event(
                stage="reliability",
                event="warmup.success",
                source="warmup",
                provider=provider_id,
            )


async def graceful_shutdown(
    registry: Any, heartbeat_checker: HeartbeatChecker | None = None
) -> None:
    """Gracefully stop heartbeat checker and clean up provider registry resources."""
    logger.info("Initiating graceful shutdown of reliability components...")

    if heartbeat_checker is not None:
        try:
            await heartbeat_checker.stop()
        except Exception as e:
            logger.error("Error stopping heartbeat checker: {}", e)

    try:
        await registry.cleanup()
        logger.info("Provider registry cleanup completed successfully")
    except Exception as e:
        logger.error("Error during provider registry cleanup: {}", e)


def parse_blackout_windows(
    blackout_str: str,
) -> list[tuple[str, datetime_time, datetime_time]]:
    """Parse blackout windows string (Format: 'provider_id:HH:MM-HH:MM,HH:MM-HH:MM')."""
    windows = []
    if not blackout_str or not blackout_str.strip():
        return windows

    for part in blackout_str.split(","):
        part = part.strip()
        if not part:
            continue

        if ":" in part:
            potential_id, rest = part.split(":", 1)
            if potential_id.strip().isdigit():
                provider_id = "*"
                time_range = part
            else:
                provider_id = potential_id.strip()
                time_range = rest
        else:
            provider_id = "*"
            time_range = part

        time_range = time_range.strip()
        if "-" not in time_range:
            logger.warning(
                "Invalid blackout window time range format: '{}'", time_range
            )
            continue

        start_str, end_str = time_range.split("-", 1)
        try:
            start_h, start_m = map(int, start_str.strip().split(":"))
            end_h, end_m = map(int, end_str.strip().split(":"))

            start_time = datetime_time(start_h, start_m)
            end_time = datetime_time(end_h, end_m)
            windows.append((provider_id, start_time, end_time))
        except ValueError as e:
            logger.warning("Failed to parse blackout window '{}': {}", part, e)
            continue

    return windows


def is_in_blackout(
    provider_id: str, blackout_windows_str: str, current_dt: datetime | None = None
) -> bool:
    """Check if the given provider is currently in a blackout window (daily UTC)."""
    if not blackout_windows_str or not blackout_windows_str.strip():
        return False

    if current_dt is None:
        current_dt = datetime.now(UTC)

    if current_dt.tzinfo is not None:
        current_dt = current_dt.astimezone(UTC)
    current_time = current_dt.time()

    windows = parse_blackout_windows(blackout_windows_str)
    for p_id, start, end in windows:
        if p_id != "*" and p_id != provider_id:
            continue

        if start <= end:
            if start <= current_time <= end:
                return True
        else:
            if current_time >= start or current_time <= end:
                return True

    return False
