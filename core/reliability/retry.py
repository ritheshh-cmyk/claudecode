"""Retry logic with exponential backoff and random jitter."""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def calculate_delay(
    attempt: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    factor: float = 2.0,
    jitter: bool = True,
) -> float:
    """Calculates delay using exponential backoff with full random jitter."""
    if attempt < 1:
        attempt = 1
    delay = base_delay * (factor ** (attempt - 1))
    delay = min(delay, max_delay)
    if jitter:
        return random.uniform(0.0, delay)
    return delay


def retry(
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    factor: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    on_retry: Callable[[BaseException, int, float], None] | None = None,
) -> Callable[[F], F]:
    """Decorator to retry sync/async functions using exponential backoff and jitter."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if base_delay < 0:
        raise ValueError("base_delay must be >= 0")
    if max_delay < 0:
        raise ValueError("max_delay must be >= 0")
    if factor <= 0:
        raise ValueError("factor must be > 0")

    exceptions = retryable_exceptions or (Exception,)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as e:
                        attempt += 1
                        if attempt > max_retries:
                            raise
                        delay = calculate_delay(
                            attempt,
                            base_delay=base_delay,
                            max_delay=max_delay,
                            factor=factor,
                            jitter=jitter,
                        )
                        if on_retry:
                            on_retry(e, attempt, delay)
                        await asyncio.sleep(delay)

            return cast(F, async_wrapper)
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        attempt += 1
                        if attempt > max_retries:
                            raise
                        delay = calculate_delay(
                            attempt,
                            base_delay=base_delay,
                            max_delay=max_delay,
                            factor=factor,
                            jitter=jitter,
                        )
                        if on_retry:
                            on_retry(e, attempt, delay)
                        time.sleep(delay)

            return cast(F, sync_wrapper)

    return decorator
