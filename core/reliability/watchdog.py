"""Streaming watchdog to monitor SSE stream delays."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TypeVar

T = TypeVar("T")


class StreamingTimeoutError(TimeoutError):
    """Raised when an SSE stream stalls for too long between chunks."""

    pass


async def watchdog_stream[T](
    stream: AsyncIterator[T],
    chunk_timeout: float,
    connect_timeout: float | None = None,
) -> AsyncIterator[T]:
    """Wraps an async stream and raises StreamingTimeoutError if chunks stall."""
    iterator = aiter(stream)
    is_first = True
    current_timeout = connect_timeout if connect_timeout is not None else chunk_timeout

    while True:
        try:
            async with asyncio.timeout(current_timeout):
                item = await anext(iterator)
            is_first = False
            current_timeout = chunk_timeout
            yield item
        except StopAsyncIteration:
            break
        except TimeoutError as e:
            msg = (
                f"Connection stalled. First chunk not received in {current_timeout}s."
                if is_first
                else f"Stream stalled. No chunks received for {current_timeout}s."
            )
            raise StreamingTimeoutError(msg) from e
