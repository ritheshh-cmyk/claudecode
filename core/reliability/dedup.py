from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class RequestDeduplicator:
    """Deduplicates concurrent identical in-flight requests."""

    def __init__(self) -> None:
        self._in_flight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def execute(self, key: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """If a request with the given key is in flight, wait and return its result.

        Otherwise, run the coroutine and resolve all waiters.
        """
        async with self._lock:
            if key in self._in_flight:
                # Close the duplicate coroutine to prevent "was never awaited" warnings.
                if hasattr(coro, "close") and callable(coro.close):
                    coro.close()
                fut = self._in_flight[key]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._in_flight[key] = fut
                # Run the coroutine in a background task
                asyncio.create_task(self._run_and_resolve(key, coro, fut))

        # Awaiting the future yields the result or propagates the exception
        return await fut

    async def _run_and_resolve(
        self, key: str, coro: Coroutine[Any, Any, Any], fut: asyncio.Future[Any]
    ) -> None:
        try:
            result = await coro
            fut.set_result(result)
        except Exception as e:
            fut.set_exception(e)
        finally:
            async with self._lock:
                if self._in_flight.get(key) is fut:
                    del self._in_flight[key]
