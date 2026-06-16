from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.reliability.dead_letter import DeadLetterQueue
from core.reliability.dedup import RequestDeduplicator
from core.reliability.key_pool import KeyPool
from core.reliability.request_queue import RequestQueue


@pytest.mark.asyncio
async def test_key_pool_basic_operations() -> None:
    pool = KeyPool()

    # Empty key pool
    assert await pool.get_key("provider-a") is None

    # Add keys
    await pool.add_key("provider-a", "key-1")
    await pool.add_key("provider-a", "key-2")
    assert await pool.get_key("provider-a") == "key-1"
    assert await pool.get_key("provider-a") == "key-2"

    # Remove key
    await pool.remove_key("provider-a", "key-1")
    assert await pool.get_key("provider-a") == "key-2"
    assert await pool.get_key("provider-a") == "key-2"

    # Remove non-existent key
    await pool.remove_key("provider-a", "key-nonexistent")

    # Remove last key
    await pool.remove_key("provider-a", "key-2")
    assert await pool.get_key("provider-a") is None


@pytest.mark.asyncio
async def test_key_pool_rotation_and_cooldown() -> None:
    pool = KeyPool({"provider-a": ["key-1", "key-2", "key-3"]})

    # Rotation test
    assert await pool.get_key("provider-a") == "key-1"
    assert await pool.get_key("provider-a") == "key-2"
    assert await pool.get_key("provider-a") == "key-3"
    assert await pool.get_key("provider-a") == "key-1"

    # Cooldown test
    await pool.report_429("provider-a", "key-2", cooldown_duration=10.0)
    assert await pool.is_on_cooldown("provider-a", "key-2") is True

    # Check rotation skips key-2
    assert await pool.get_key("provider-a") == "key-3"
    assert await pool.get_key("provider-a") == "key-1"
    assert await pool.get_key("provider-a") == "key-3"

    # Clear cooldown on success
    await pool.report_success("provider-a", "key-2")
    assert await pool.is_on_cooldown("provider-a", "key-2") is False
    assert await pool.get_key("provider-a") == "key-1"
    assert await pool.get_key("provider-a") == "key-2"


@pytest.mark.asyncio
async def test_key_pool_cooldown_expiry() -> None:
    pool = KeyPool({"provider-a": ["key-1"]})

    # Very short cooldown
    await pool.report_429("provider-a", "key-1", cooldown_duration=0.01)
    assert await pool.is_on_cooldown("provider-a", "key-1") is True
    assert await pool.get_key("provider-a") is None

    # Wait for expiry
    await asyncio.sleep(0.02)
    assert await pool.is_on_cooldown("provider-a", "key-1") is False
    assert await pool.get_key("provider-a") == "key-1"


@pytest.mark.asyncio
async def test_request_queue_priority_and_fifo() -> None:
    rq = RequestQueue()

    # Enqueue requests with different priorities
    req1 = await rq.enqueue("provider-a", {"req": 1}, priority=1)
    req2 = await rq.enqueue("provider-a", {"req": 2}, priority=10)
    req3 = await rq.enqueue("provider-a", {"req": 3}, priority=5)

    # Enqueue request with same priority as req3 to test FIFO order
    req4 = await rq.enqueue("provider-a", {"req": 4}, priority=5)

    assert await rq.size("provider-a") == 4
    assert await rq.size() == 4

    # Dequeue should respect priority (highest priority first)
    dq1 = await rq.dequeue("provider-a")
    assert dq1 is not None
    assert dq1.payload == {"req": 2}
    assert dq1 is req2

    # dq2 and dq3 should be req3 and req4 (since they have priority 5, req3 was enqueued first)
    dq2 = await rq.dequeue("provider-a")
    assert dq2 is not None
    assert dq2.payload == {"req": 3}
    assert dq2 is req3

    dq3 = await rq.dequeue("provider-a")
    assert dq3 is not None
    assert dq3.payload == {"req": 4}
    assert dq3 is req4

    dq4 = await rq.dequeue("provider-a")
    assert dq4 is not None
    assert dq4.payload == {"req": 1}
    assert dq4 is req1

    assert await rq.size("provider-a") == 0
    assert await rq.dequeue("provider-a") is None


@pytest.mark.asyncio
async def test_request_queue_clear() -> None:
    rq = RequestQueue()
    req1 = await rq.enqueue("provider-a", {"req": 1})
    req2 = await rq.enqueue("provider-b", {"req": 2})

    assert await rq.size() == 2

    # Check future is not done
    assert not req1.future.done()
    assert not req2.future.done()

    # Clear queue
    await rq.clear()
    assert await rq.size() == 0

    # Pending futures should be cancelled
    assert req1.future.cancelled()
    assert req2.future.cancelled()


@pytest.mark.asyncio
async def test_dead_letter_queue_persistence_and_cleanup(tmp_path: Path) -> None:
    filepath = tmp_path / "dead_letters.jsonl"
    dlq = DeadLetterQueue(filepath=filepath)

    # Empty file read
    failures = await dlq.read_failures()
    assert failures == []

    # Record first failure
    await dlq.record_failure(
        "provider-b",
        {"test": "data"},
        "429 Rate Limit Exceeded",
        {"meta": "val"},
    )
    failures = await dlq.read_failures()
    assert len(failures) == 1
    assert failures[0]["provider_id"] == "provider-b"
    assert failures[0]["payload"] == {"test": "data"}
    assert failures[0]["error"] == "429 Rate Limit Exceeded"
    assert failures[0]["metadata"] == {"meta": "val"}
    assert isinstance(failures[0]["timestamp"], float)

    # Record second failure
    await dlq.record_failure(
        "provider-c",
        {"other": "payload"},
        "500 Internal Error",
    )
    failures = await dlq.read_failures()
    assert len(failures) == 2
    assert failures[1]["provider_id"] == "provider-c"
    assert failures[1]["payload"] == {"other": "payload"}
    assert failures[1]["error"] == "500 Internal Error"
    assert failures[1]["metadata"] == {}

    # Clear
    await dlq.clear()
    assert filepath.is_file() is False
    assert await dlq.read_failures() == []


@pytest.mark.asyncio
async def test_request_deduplication_basic() -> None:
    dedup = RequestDeduplicator()
    counter = 0

    async def mock_network_call(duration: float) -> str:
        nonlocal counter
        await asyncio.sleep(duration)
        counter += 1
        return f"result-{counter}"

    # Run three concurrent deduplicated calls
    t1 = dedup.execute("call-key", mock_network_call(0.02))
    t2 = dedup.execute("call-key", mock_network_call(0.02))
    t3 = dedup.execute("call-key", mock_network_call(0.02))

    results = await asyncio.gather(t1, t2, t3)
    # They should all get the same result and function is only called once
    assert results == ["result-1", "result-1", "result-1"]
    assert counter == 1

    # Next call should trigger a new execution
    assert await dedup.execute("call-key", mock_network_call(0.01)) == "result-2"
    assert counter == 2


@pytest.mark.asyncio
async def test_request_deduplication_exceptions() -> None:
    dedup = RequestDeduplicator()
    counter = 0

    async def failing_network_call() -> str:
        nonlocal counter
        await asyncio.sleep(0.01)
        counter += 1
        raise ValueError(f"Failed call {counter}")

    task1 = asyncio.create_task(dedup.execute("fail-key", failing_network_call()))
    task2 = asyncio.create_task(dedup.execute("fail-key", failing_network_call()))

    results = await asyncio.gather(task1, task2, return_exceptions=True)
    assert isinstance(results[0], ValueError)
    assert str(results[0]) == "Failed call 1"
    assert isinstance(results[1], ValueError)
    assert str(results[1]) == "Failed call 1"

    # Verification that key was removed from in_flight on failure
    async def successful_call() -> str:
        return "success"

    assert await dedup.execute("fail-key", successful_call()) == "success"


@pytest.mark.asyncio
async def test_request_deduplication_cancellation() -> None:
    dedup = RequestDeduplicator()

    async def sleep_call() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    t1 = asyncio.create_task(dedup.execute("cancel-key", sleep_call()))
    t2 = asyncio.create_task(dedup.execute("cancel-key", sleep_call()))

    await asyncio.sleep(0.01)
    t1.cancel()

    with pytest.raises(asyncio.CancelledError):
        await t1

    with pytest.raises(asyncio.CancelledError):
        await t2


@pytest.mark.asyncio
async def test_request_deduplication_coro_close() -> None:
    dedup = RequestDeduplicator()
    event = asyncio.Event()

    async def sleep_call() -> str:
        await event.wait()
        return "ok"

    t1 = asyncio.create_task(dedup.execute("close-key", sleep_call()))

    await asyncio.sleep(0.001)

    from collections.abc import Coroutine, Generator

    class MockCoro(Coroutine[Any, Any, Any]):
        def __init__(self) -> None:
            self.closed_called = False

        def send(self, value: Any) -> Any:
            raise StopIteration("ok")

        def throw(
            self,
            val: Any,
            exc: Any = None,
            tb: Any = None,
        ) -> Any:
            pass

        def close(self) -> None:
            self.closed_called = True

        def __await__(self) -> Generator[Any, Any, Any]:
            return sleep_call().__await__()

    mock_coro = MockCoro()
    t2 = asyncio.create_task(dedup.execute("close-key", mock_coro))

    await asyncio.sleep(0.001)
    event.set()

    await asyncio.gather(t1, t2)
    assert mock_coro.closed_called is True
