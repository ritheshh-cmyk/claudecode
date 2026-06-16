from __future__ import annotations

import asyncio
from typing import Any


class QueuedRequest:
    """Represents a request waiting in the queue."""

    def __init__(
        self, provider_id: str, payload: dict[str, Any], priority: int = 0
    ) -> None:
        self.provider_id = provider_id
        self.payload = payload
        self.priority = priority
        self.future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()


class RequestQueue:
    """Queue system to hold requests when all keys/providers are on cooldown."""

    def __init__(self) -> None:
        # Maps provider_id to a PriorityQueue of (priority, sequence_number, QueuedRequest)
        self._queues: dict[
            str, asyncio.PriorityQueue[tuple[int, int, QueuedRequest]]
        ] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    async def enqueue(
        self, provider_id: str, payload: dict[str, Any], priority: int = 0
    ) -> QueuedRequest:
        """Enqueue a request, return a QueuedRequest instance with a Future to await."""
        async with self._lock:
            if provider_id not in self._queues:
                self._queues[provider_id] = asyncio.PriorityQueue()

            self._counter += 1
            req = QueuedRequest(provider_id, payload, priority)
            # Store with negative priority so that higher priority number comes first.
            # Use self._counter as a tie-breaker to ensure FIFO for same-priority items.
            await self._queues[provider_id].put((-priority, self._counter, req))
            return req

    async def dequeue(self, provider_id: str) -> QueuedRequest | None:
        """Dequeue the highest priority request for a provider. Returns None if empty."""
        async with self._lock:
            queue = self._queues.get(provider_id)
            if not queue or queue.empty():
                return None
            _, _, req = await queue.get()
            return req

    async def size(self, provider_id: str | None = None) -> int:
        """Get size of the queue for a given provider, or total size if None."""
        async with self._lock:
            if provider_id is not None:
                queue = self._queues.get(provider_id)
                return queue.qsize() if queue else 0
            return sum(q.qsize() for q in self._queues.values())

    async def clear(self) -> None:
        """Clear all queues and cancel any pending futures in them."""
        async with self._lock:
            for queue in self._queues.values():
                while not queue.empty():
                    _, _, req = queue.get_nowait()
                    if not req.future.done():
                        req.future.cancel()
            self._queues.clear()
