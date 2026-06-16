from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.reliability.heartbeat import HeartbeatChecker, get_active_provider_ids
from core.reliability.helpers import (
    graceful_shutdown,
    is_in_blackout,
    parse_blackout_windows,
    warmup_connection_pools,
)


def test_get_active_provider_ids() -> None:
    settings = MagicMock()
    # Configure some API keys
    settings.nvidia_nim_api_key = "key1"
    settings.gemini_api_key = ""
    settings.openai_api_key = "  "
    settings.open_router_api_key = "key2"

    active = get_active_provider_ids(settings)

    assert "nvidia_nim" in active
    assert "open_router" in active
    assert "gemini" not in active
    assert "openai" not in active
    # Static credential providers should be in active list
    assert "lmstudio" in active
    assert "llamacpp" in active
    assert "ollama" in active


def test_parse_blackout_windows() -> None:
    # Empty cases
    assert parse_blackout_windows("") == []
    assert parse_blackout_windows("   ") == []

    # Valid cases
    res = parse_blackout_windows(
        "nvidia_nim:08:00-09:30, 14:00-15:00, openai:23:00-01:00"
    )
    assert len(res) == 3
    assert res[0] == ("nvidia_nim", time(8, 0), time(9, 30))
    assert res[1] == ("*", time(14, 0), time(15, 0))
    assert res[2] == ("openai", time(23, 0), time(1, 0))

    # Invalid cases
    res_inv = parse_blackout_windows("nvidia_nim:invalid, 08:00-09:00, missing-dash")
    assert len(res_inv) == 1
    assert res_inv[0] == ("*", time(8, 0), time(9, 0))


def test_is_in_blackout() -> None:
    windows = "nvidia_nim:08:00-10:00, 23:00-01:00"

    # Standard range
    # Inside (09:00 UTC)
    dt_inside = datetime(2026, 6, 16, 9, 0, tzinfo=UTC)
    assert is_in_blackout("nvidia_nim", windows, dt_inside) is True
    # Different provider not in window
    assert is_in_blackout("openai", windows, dt_inside) is False
    # Outside (11:00 UTC)
    dt_outside = datetime(2026, 6, 16, 11, 0, tzinfo=UTC)
    assert is_in_blackout("nvidia_nim", windows, dt_outside) is False

    # Overnight range (23:00 to 01:00)
    # Inside 23:30 UTC
    dt_inside_overnight1 = datetime(2026, 6, 16, 23, 30, tzinfo=UTC)
    assert is_in_blackout("openai", windows, dt_inside_overnight1) is True
    # Inside 00:30 UTC
    dt_inside_overnight2 = datetime(2026, 6, 16, 0, 30, tzinfo=UTC)
    assert is_in_blackout("openai", windows, dt_inside_overnight2) is True
    # Outside 02:00 UTC
    dt_outside_overnight = datetime(2026, 6, 16, 2, 0, tzinfo=UTC)
    assert is_in_blackout("openai", windows, dt_outside_overnight) is False

    # Empty windows
    assert is_in_blackout("nvidia_nim", "", dt_inside) is False


@pytest.mark.asyncio
async def test_warmup_connection_pools() -> None:
    settings = MagicMock()
    # Mock get_active_provider_ids to return controlled list
    with patch(
        "core.reliability.helpers.get_active_provider_ids", return_value=["p1", "p2"]
    ):
        registry = MagicMock()
        provider1 = MagicMock()
        provider1.list_model_infos = AsyncMock(return_value=frozenset())
        provider2 = MagicMock()
        provider2.list_model_infos = AsyncMock(
            side_effect=ValueError("connection error")
        )

        registry.get.side_effect = lambda pid, s: (
            provider1 if pid == "p1" else provider2
        )

        with patch("core.reliability.helpers.trace_event") as mock_trace:
            await warmup_connection_pools(registry, settings)

            # verify list_model_infos was called on both
            provider1.list_model_infos.assert_called_once()
            provider2.list_model_infos.assert_called_once()

            # Verify trace events
            mock_trace.assert_any_call(
                stage="reliability",
                event="warmup.success",
                source="warmup",
                provider="p1",
            )
            mock_trace.assert_any_call(
                stage="reliability",
                event="warmup.failure",
                source="warmup",
                provider="p2",
                error="connection error",
            )


@pytest.mark.asyncio
async def test_graceful_shutdown() -> None:
    registry = MagicMock()
    registry.cleanup = AsyncMock()

    checker = MagicMock()
    checker.stop = AsyncMock()

    await graceful_shutdown(registry, checker)

    checker.stop.assert_called_once()
    registry.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_heartbeat_checker_lifecycle() -> None:
    settings = MagicMock()
    settings.heartbeat_interval = 0.01

    registry = MagicMock()
    provider1 = MagicMock()
    provider1.list_model_ids = AsyncMock(return_value=frozenset(["m1"]))
    provider2 = MagicMock()
    provider2.list_model_ids = AsyncMock(side_effect=Exception("api timeout"))

    registry.get.side_effect = lambda pid, s: provider1 if pid == "p1" else provider2

    with (
        patch(
            "core.reliability.heartbeat.get_active_provider_ids",
            return_value=["p1", "p2"],
        ),
        patch("core.reliability.heartbeat.trace_event") as mock_trace,
    ):
        checker = HeartbeatChecker(settings, registry)

        # Initially healthy by default (not checked yet)
        assert checker.is_healthy("p1") is True
        assert checker.is_healthy("p2") is True

        # Perform check manually
        await checker.check_all()

        assert checker.is_healthy("p1") is True
        assert checker.is_healthy("p2") is False

        # Verify traces
        mock_trace.assert_any_call(
            stage="reliability",
            event="heartbeat.check",
            source="heartbeat",
            provider="p1",
            healthy=True,
        )
        mock_trace.assert_any_call(
            stage="reliability",
            event="heartbeat.check",
            source="heartbeat",
            provider="p2",
            healthy=False,
            error="api timeout",
        )

        # Test start and stop
        checker.start()
        assert checker._running is True
        assert checker._task is not None

        # Let it run for a tiny bit
        await asyncio.sleep(0.02)

        await checker.stop()
        assert checker._running is False
        assert checker._task is None
