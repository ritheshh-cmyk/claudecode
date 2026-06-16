"""Circuit Breaker reliability infrastructure component."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitBreakerState(Enum):
    """Enum representing the possible states of a circuit breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when a request is blocked because the circuit breaker is OPEN."""

    def __init__(self, provider: str, remaining_cooldown: float) -> None:
        self.provider = provider
        self.remaining_cooldown = remaining_cooldown
        super().__init__(
            f"Circuit breaker for provider '{provider}' is OPEN. "
            f"Remaining cooldown: {remaining_cooldown:.2f}s"
        )


class ProviderState:
    """Tracks the state and lock for a single provider's circuit breaker."""

    def __init__(self) -> None:
        self.state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self.failures: int = 0
        self.successes: int = 0
        self.last_state_change: float = 0.0
        self.lock: asyncio.Lock = asyncio.Lock()


class CircuitBreakerContext:
    """Async context manager representing a guarded call within the circuit breaker."""

    def __init__(self, breaker: CircuitBreaker, provider: str) -> None:
        self.breaker = breaker
        self.provider = provider
        self.state: ProviderState | None = None

    async def __aenter__(self) -> CircuitBreakerContext:
        self.state = await self.breaker._get_state(self.provider)
        async with self.state.lock:
            now = time.monotonic()
            if self.state.state == CircuitBreakerState.OPEN:
                elapsed = now - self.state.last_state_change
                if elapsed >= self.breaker.recovery_timeout:
                    self.state.state = CircuitBreakerState.HALF_OPEN
                    self.state.successes = 0
                    self.state.last_state_change = now
                else:
                    remaining = self.breaker.recovery_timeout - elapsed
                    raise CircuitBreakerOpenError(self.provider, remaining)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if self.state is None:
            return False

        now = time.monotonic()
        async with self.state.lock:
            if exc_type is None:
                # Success path
                if self.state.state == CircuitBreakerState.HALF_OPEN:
                    self.state.successes += 1
                    if self.state.successes >= self.breaker.success_threshold:
                        self.state.state = CircuitBreakerState.CLOSED
                        self.state.failures = 0
                        self.state.successes = 0
                        self.state.last_state_change = now
                elif self.state.state == CircuitBreakerState.CLOSED:
                    self.state.failures = 0
            else:
                # Failure path
                if issubclass(exc_type, self.breaker.tripping_exceptions):
                    if self.state.state == CircuitBreakerState.CLOSED:
                        self.state.failures += 1
                        if self.state.failures >= self.breaker.failure_threshold:
                            self.state.state = CircuitBreakerState.OPEN
                            self.state.last_state_change = now
                    elif self.state.state == CircuitBreakerState.HALF_OPEN:
                        # Any failure in HALF_OPEN trips it immediately back to OPEN
                        self.state.state = CircuitBreakerState.OPEN
                        self.state.failures = self.breaker.failure_threshold
                        self.state.last_state_change = now
        return False


class CircuitBreaker:
    """Manages the circuit breaker states and locks per provider."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
        tripping_exceptions: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.tripping_exceptions = tripping_exceptions or (Exception,)
        self._states: dict[str, ProviderState] = {}
        self._global_lock: asyncio.Lock = asyncio.Lock()

    async def _get_state(self, provider: str) -> ProviderState:
        async with self._global_lock:
            if provider not in self._states:
                self._states[provider] = ProviderState()
            return self._states[provider]

    def guard(self, provider: str) -> CircuitBreakerContext:
        """Returns an async context manager that guards a block of code."""
        return CircuitBreakerContext(self, provider)

    async def call(
        self,
        provider: str,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Helper to directly call an async function within the circuit breaker."""
        async with self.guard(provider):
            return await func(*args, **kwargs)

    def get_state(self, provider: str) -> CircuitBreakerState:
        """Retrieves the current state of a provider breaker, checking recovery timeout."""
        if provider not in self._states:
            return CircuitBreakerState.CLOSED
        state = self._states[provider]
        if (
            state.state == CircuitBreakerState.OPEN
            and time.monotonic() - state.last_state_change >= self.recovery_timeout
        ):
            return CircuitBreakerState.HALF_OPEN
        return state.state

    def force_open(self, provider: str) -> None:
        """Manually force a provider's circuit breaker to OPEN."""
        if provider not in self._states:
            self._states[provider] = ProviderState()
        state = self._states[provider]
        state.state = CircuitBreakerState.OPEN
        state.last_state_change = time.monotonic()
        state.failures = self.failure_threshold

    def force_closed(self, provider: str) -> None:
        """Manually force a provider's circuit breaker to CLOSED."""
        if provider not in self._states:
            self._states[provider] = ProviderState()
        state = self._states[provider]
        state.state = CircuitBreakerState.CLOSED
        state.failures = 0
        state.successes = 0
        state.last_state_change = time.monotonic()

    def force_half_open(self, provider: str) -> None:
        """Manually force a provider's circuit breaker to HALF_OPEN."""
        if provider not in self._states:
            self._states[provider] = ProviderState()
        state = self._states[provider]
        state.state = CircuitBreakerState.HALF_OPEN
        state.successes = 0
        state.last_state_change = time.monotonic()
