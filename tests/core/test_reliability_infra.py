import asyncio
from collections.abc import AsyncIterator

import pytest

from core.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
)
from core.reliability.retry import calculate_delay, retry
from core.reliability.watchdog import StreamingTimeoutError, watchdog_stream

# =====================================================================
# Circuit Breaker Tests
# =====================================================================


@pytest.mark.asyncio
async def test_circuit_breaker_flow():
    breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_timeout=0.2,
        success_threshold=2,
    )
    provider = "test_provider"

    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED

    # First failure
    with pytest.raises(ValueError):
        async with breaker.guard(provider):
            raise ValueError("First test failure")

    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED

    # Second failure -> trips to OPEN
    with pytest.raises(ValueError):
        async with breaker.guard(provider):
            raise ValueError("Second test failure")

    assert breaker.get_state(provider) == CircuitBreakerState.OPEN

    # Fast fail in OPEN state
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        async with breaker.guard(provider):
            pass
    assert exc_info.value.provider == provider
    assert exc_info.value.remaining_cooldown > 0

    # Wait for recovery timeout
    await asyncio.sleep(0.25)

    # First attempt in HALF_OPEN
    called = False
    async with breaker.guard(provider):
        called = True
    assert called
    assert breaker.get_state(provider) == CircuitBreakerState.HALF_OPEN

    # Second attempt in HALF_OPEN -> transitions back to CLOSED
    called_second = False
    async with breaker.guard(provider):
        called_second = True
    assert called_second
    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_failure():
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=0.1,
        success_threshold=2,
    )
    provider = "test_provider_half_open_fail"

    # Trip breaker
    with pytest.raises(RuntimeError):
        async with breaker.guard(provider):
            raise RuntimeError("Trip")

    assert breaker.get_state(provider) == CircuitBreakerState.OPEN

    await asyncio.sleep(0.15)

    # Failure in HALF_OPEN -> immediately back to OPEN
    with pytest.raises(ValueError):
        async with breaker.guard(provider):
            raise ValueError("Failure in half-open")

    assert breaker.get_state(provider) == CircuitBreakerState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_manual_controls():
    breaker = CircuitBreaker()
    provider = "test_provider_manual"

    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED

    breaker.force_open(provider)
    assert breaker.get_state(provider) == CircuitBreakerState.OPEN

    breaker.force_half_open(provider)
    assert breaker.get_state(provider) == CircuitBreakerState.HALF_OPEN

    breaker.force_closed(provider)
    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_tripping_exceptions():
    # Only ValueError should trip the breaker
    breaker = CircuitBreaker(
        failure_threshold=1,
        tripping_exceptions=(ValueError,),
    )
    provider = "test_provider_exceptions"

    # RuntimeError should pass through without tripping
    with pytest.raises(RuntimeError):
        async with breaker.guard(provider):
            raise RuntimeError("Ignored exception")
    assert breaker.get_state(provider) == CircuitBreakerState.CLOSED

    # ValueError trips it
    with pytest.raises(ValueError):
        async with breaker.guard(provider):
            raise ValueError("Tripping exception")
    assert breaker.get_state(provider) == CircuitBreakerState.OPEN


# =====================================================================
# Retry Tests
# =====================================================================


def test_calculate_delay_no_jitter():
    # attempt 1: base_delay
    assert calculate_delay(1, base_delay=0.5, factor=2.0, jitter=False) == 0.5
    # attempt 2: base_delay * 2
    assert calculate_delay(2, base_delay=0.5, factor=2.0, jitter=False) == 1.0
    # attempt 3: base_delay * 4 capped at max_delay
    assert (
        calculate_delay(3, base_delay=0.5, max_delay=1.5, factor=2.0, jitter=False)
        == 1.5
    )


def test_calculate_delay_with_jitter():
    for attempt in range(1, 5):
        delay = calculate_delay(attempt, base_delay=0.1, factor=2.0, jitter=True)
        assert 0.0 <= delay <= 0.1 * (2 ** (attempt - 1))


@pytest.mark.asyncio
async def test_retry_async_decorator():
    call_count = 0
    on_retry_calls = []

    def log_retry(exc, attempt, delay):
        on_retry_calls.append((exc, attempt, delay))

    @retry(
        max_retries=3,
        base_delay=0.01,
        factor=1.5,
        jitter=False,
        retryable_exceptions=(ValueError,),
        on_retry=log_retry,
    )
    async def fail_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("Temporary failure")
        return "success"

    result = await fail_then_succeed()
    assert result == "success"
    assert call_count == 3
    assert len(on_retry_calls) == 2
    assert on_retry_calls[0][1] == 1  # First retry attempt 1
    assert isinstance(on_retry_calls[0][0], ValueError)


def test_retry_sync_decorator():
    call_count = 0

    @retry(
        max_retries=2,
        base_delay=0.01,
        factor=2.0,
        jitter=False,
        retryable_exceptions=(TypeError,),
    )
    def fail_sync():
        nonlocal call_count
        call_count += 1
        raise TypeError("Sync error")

    with pytest.raises(TypeError):
        fail_sync()
    assert call_count == 3  # Initial attempt + 2 retries


@pytest.mark.asyncio
async def test_retry_unretryable_exception():
    call_count = 0

    @retry(
        max_retries=3,
        retryable_exceptions=(ValueError,),
    )
    async def fail_unretryable():
        nonlocal call_count
        call_count += 1
        raise KeyError("Unretryable")

    with pytest.raises(KeyError):
        await fail_unretryable()
    assert call_count == 1  # Should not retry


# =====================================================================
# Watchdog Tests
# =====================================================================


@pytest.mark.asyncio
async def test_watchdog_stream_success():
    async def slow_stream() -> AsyncIterator[str]:
        yield "chunk1"
        await asyncio.sleep(0.02)
        yield "chunk2"
        await asyncio.sleep(0.02)
        yield "chunk3"

    generator = watchdog_stream(slow_stream(), chunk_timeout=0.1)
    results = [chunk async for chunk in generator]

    assert results == ["chunk1", "chunk2", "chunk3"]


@pytest.mark.asyncio
async def test_watchdog_stream_connect_timeout():
    async def slow_to_start() -> AsyncIterator[str]:
        await asyncio.sleep(0.1)
        yield "first"

    generator = watchdog_stream(
        slow_to_start(), chunk_timeout=0.2, connect_timeout=0.05
    )
    with pytest.raises(StreamingTimeoutError) as exc_info:
        async for _ in generator:
            pass
    assert "First chunk not received" in str(exc_info.value)


@pytest.mark.asyncio
async def test_watchdog_stream_chunk_timeout():
    async def stall_midway() -> AsyncIterator[str]:
        yield "chunk1"
        await asyncio.sleep(0.1)
        yield "chunk2"

    generator = watchdog_stream(stall_midway(), chunk_timeout=0.05)
    results = []
    with pytest.raises(StreamingTimeoutError) as exc_info:
        # We append chunks dynamically as we consume to assert partial results
        async for chunk in generator:
            results.append(chunk)  # noqa: PERF401
    assert results == ["chunk1"]
    assert "No chunks received" in str(exc_info.value)
