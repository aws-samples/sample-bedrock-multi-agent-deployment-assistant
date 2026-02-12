"""Circuit breaker and concurrency limiter for Bedrock API calls.

Prevents hammering a failing Bedrock service by tracking consecutive
failures and short-circuiting calls when the failure threshold is reached.

States:
    CLOSED   — Normal operation; calls pass through.
    OPEN     — Failures exceeded threshold; calls are rejected immediately.
    HALF_OPEN — Recovery timeout elapsed; a limited number of test calls are
                allowed through. Success resets to CLOSED; failure returns to OPEN.

Concurrency limiting:
    A process-wide ``threading.Semaphore`` caps the number of simultaneous
    Bedrock API calls.  Excess calls block (queue) until a slot opens — they
    are never rejected.  The limit is read from ``settings.bedrock_max_concurrency``.
"""

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is OPEN and rejecting calls."""

    def __init__(self, retry_after: float = 0.0):
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker is OPEN. Retry after {retry_after:.1f}s."
        )


class CircuitBreaker:
    """Thread-safe circuit breaker for Bedrock API calls.

    Args:
        failure_threshold: Consecutive failures before opening the circuit.
        recovery_timeout: Seconds to wait in OPEN before transitioning to HALF_OPEN.
        half_open_max_calls: Max calls allowed in HALF_OPEN state for testing recovery.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0

        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state, considering automatic OPEN -> HALF_OPEN transition."""
        with self._lock:
            return self._effective_state()

    def _effective_state(self) -> CircuitState:
        """Return effective state (must be called while holding _lock)."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    def pre_check(self) -> None:
        """Check circuit state and raise CircuitOpenError if OPEN.

        Use this for streaming paths where you want to check the circuit
        before starting a long-running operation.
        """
        with self._lock:
            effective = self._effective_state()

            if effective == CircuitState.OPEN:
                retry_after = self._recovery_timeout - (time.monotonic() - self._last_failure_time)
                raise CircuitOpenError(retry_after=max(retry_after, 0.0))

            if effective == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self._half_open_max_calls:
                    raise CircuitOpenError(retry_after=self._recovery_timeout)
                if self._state == CircuitState.OPEN:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("Circuit breaker transitioned to HALF_OPEN")
                self._half_open_calls += 1

    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute *fn* through the circuit breaker and concurrency limiter.

        Acquires a concurrency slot *after* the circuit-state pre-check so
        that an OPEN circuit never wastes a semaphore slot.  When all slots
        are occupied the call **blocks** until one is released — it is never
        rejected.

        Raises:
            CircuitOpenError: If the circuit is OPEN.
        """
        self.pre_check()

        sem = _get_semaphore()
        acquired = sem.acquire(blocking=False)
        if not acquired:
            logger.info(
                "Bedrock concurrency limit reached, queuing call to %s",
                getattr(fn, "__name__", fn),
            )
            if not sem.acquire(timeout=120):  # block with timeout
                raise TimeoutError(
                    "Bedrock concurrency limit exceeded: waited 120s for a free slot"
                )

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result
        finally:
            sem.release()

    def record_success(self) -> None:
        """Record a successful call — reset to CLOSED if in HALF_OPEN."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker reset to CLOSED after successful HALF_OPEN call")
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call — may trip the circuit to OPEN."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.warning("Circuit breaker tripped back to OPEN from HALF_OPEN")
                self._state = CircuitState.OPEN
                self._last_failure_time = time.monotonic()
                self._half_open_calls = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    logger.warning(
                        "Circuit breaker tripped to OPEN after %d consecutive failures",
                        self._failure_count,
                    )
                    self._state = CircuitState.OPEN
                    self._last_failure_time = time.monotonic()
            elif self._state == CircuitState.OPEN:
                self._last_failure_time = time.monotonic()

    def reset(self) -> None:
        """Reset the circuit breaker to its initial CLOSED state (for testing)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0
            self._half_open_calls = 0


# ---------------------------------------------------------------------------
# Concurrency limiter (process-wide semaphore)
# ---------------------------------------------------------------------------

_bedrock_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _get_semaphore() -> threading.Semaphore:
    """Lazy-initialize the concurrency semaphore from settings.

    The semaphore is created on first use so that ``settings`` is fully
    loaded before we read ``bedrock_max_concurrency``.
    """
    global _bedrock_semaphore
    if _bedrock_semaphore is None:
        with _semaphore_lock:
            if _bedrock_semaphore is None:  # double-checked locking
                from src.config.settings import settings

                _bedrock_semaphore = threading.Semaphore(
                    settings.bedrock_max_concurrency
                )
                logger.info(
                    "Bedrock concurrency limiter initialized: max_concurrency=%d",
                    settings.bedrock_max_concurrency,
                )
    return _bedrock_semaphore


# ---------------------------------------------------------------------------
# Module-level singleton for Bedrock API calls
# ---------------------------------------------------------------------------

bedrock_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30)
