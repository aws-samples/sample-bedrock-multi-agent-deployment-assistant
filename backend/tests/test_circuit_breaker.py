"""Unit tests for the circuit breaker module.

Tests verify state transitions, thread safety, and the module-level
bedrock_breaker singleton.
"""

import concurrent.futures
import time
from unittest.mock import MagicMock

import pytest

from src.config.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    bedrock_breaker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _failing_fn():
    raise ConnectionError("simulated Bedrock failure")


def _succeeding_fn(value=42):
    return value


# ---------------------------------------------------------------------------
# CLOSED state
# ---------------------------------------------------------------------------


class TestClosedState:
    """Tests for normal CLOSED operation."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_call_passes_through(self):
        cb = CircuitBreaker()
        result = cb.call(_succeeding_fn, value=99)
        assert result == 99

    def test_call_passes_args_and_kwargs(self):
        cb = CircuitBreaker()
        fn = MagicMock(return_value="ok")
        result = cb.call(fn, "a", "b", key="val")
        fn.assert_called_once_with("a", "b", key="val")
        assert result == "ok"

    def test_failure_below_threshold_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        # 2 failures
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        # 1 success resets the counter
        cb.call(_succeeding_fn)
        assert cb.state == CircuitState.CLOSED
        # 2 more failures should still be below threshold
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# CLOSED -> OPEN transition
# ---------------------------------------------------------------------------


class TestTransitionToOpen:
    """Tests for tripping the circuit breaker open."""

    def test_opens_after_failure_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN

    def test_opens_exactly_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=1)
        with pytest.raises(ConnectionError):
            cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# OPEN state
# ---------------------------------------------------------------------------


class TestOpenState:
    """Tests for the OPEN state behavior."""

    def _open_breaker(self, failure_threshold=2, recovery_timeout=30.0):
        cb = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
        for _ in range(failure_threshold):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN
        return cb

    def test_raises_circuit_open_error(self):
        cb = self._open_breaker()
        with pytest.raises(CircuitOpenError):
            cb.call(_succeeding_fn)

    def test_does_not_call_function_when_open(self):
        cb = self._open_breaker()
        fn = MagicMock()
        with pytest.raises(CircuitOpenError):
            cb.call(fn)
        fn.assert_not_called()

    def test_pre_check_raises_when_open(self):
        cb = self._open_breaker()
        with pytest.raises(CircuitOpenError):
            cb.pre_check()

    def test_circuit_open_error_has_retry_after(self):
        cb = self._open_breaker(recovery_timeout=60.0)
        with pytest.raises(CircuitOpenError) as exc_info:
            cb.call(_succeeding_fn)
        assert exc_info.value.retry_after > 0


# ---------------------------------------------------------------------------
# OPEN -> HALF_OPEN transition
# ---------------------------------------------------------------------------


class TestTransitionToHalfOpen:
    """Tests for automatic transition to HALF_OPEN after recovery_timeout."""

    def test_transitions_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

    def test_allows_limited_calls_in_half_open(self):
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1
        )
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        time.sleep(0.1)
        # First call should pass through
        result = cb.call(_succeeding_fn, value=123)
        assert result == 123

    def test_rejects_excess_half_open_calls(self):
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1
        )
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        time.sleep(0.1)

        # First call goes through (pre_check consumes the slot)
        cb.pre_check()

        # Second call should be rejected
        with pytest.raises(CircuitOpenError):
            cb.pre_check()


# ---------------------------------------------------------------------------
# HALF_OPEN -> CLOSED on success
# ---------------------------------------------------------------------------


class TestHalfOpenToClosed:
    """Tests for successful recovery from HALF_OPEN."""

    def test_closes_on_success(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        cb.call(_succeeding_fn)
        assert cb.state == CircuitState.CLOSED

    def test_fully_operational_after_close(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        time.sleep(0.1)

        # Recover
        cb.call(_succeeding_fn)
        assert cb.state == CircuitState.CLOSED

        # Should work normally
        for _ in range(5):
            assert cb.call(_succeeding_fn) == 42


# ---------------------------------------------------------------------------
# HALF_OPEN -> OPEN on failure
# ---------------------------------------------------------------------------


class TestHalfOpenToOpen:
    """Tests for failing back to OPEN from HALF_OPEN."""

    def test_opens_on_failure(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        with pytest.raises(ConnectionError):
            cb.call(_failing_fn)
        # Should be back to OPEN (not considering recovery timeout)
        assert cb._state == CircuitState.OPEN

    def test_rejects_after_half_open_failure(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        # Trip to OPEN
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)

        # Manually set last failure time to the past to trigger HALF_OPEN
        cb._last_failure_time = time.monotonic() - 20.0
        assert cb.state == CircuitState.HALF_OPEN

        # Fail in HALF_OPEN
        with pytest.raises(ConnectionError):
            cb.call(_failing_fn)

        # Should now reject
        with pytest.raises(CircuitOpenError):
            cb.call(_succeeding_fn)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Tests for concurrent access to the circuit breaker."""

    def test_concurrent_failures_trip_correctly(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        errors = []

        def fail_once():
            try:
                cb.call(_failing_fn)
            except (ConnectionError, CircuitOpenError) as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fail_once) for _ in range(20)]
            concurrent.futures.wait(futures)

        # After 20 concurrent failures with threshold 5, circuit must be OPEN
        assert cb.state == CircuitState.OPEN
        # Some should be ConnectionError (passed through), rest CircuitOpenError
        conn_errors = [e for e in errors if isinstance(e, ConnectionError)]
        open_errors = [e for e in errors if isinstance(e, CircuitOpenError)]
        assert len(conn_errors) >= 5  # At least threshold failures got through
        assert len(conn_errors) + len(open_errors) == 20

    def test_concurrent_successes_keep_closed(self):
        cb = CircuitBreaker(failure_threshold=5)
        results = []

        def succeed_once():
            results.append(cb.call(_succeeding_fn, value=1))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(succeed_once) for _ in range(50)]
            concurrent.futures.wait(futures)

        assert cb.state == CircuitState.CLOSED
        assert len(results) == 50
        assert all(r == 1 for r in results)


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for the reset() method."""

    def test_reset_from_open(self):
        cb = CircuitBreaker(failure_threshold=2)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_reset_clears_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        cb.reset()
        # Should need 3 more failures to trip
        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(_failing_fn)
        assert cb.state == CircuitState.CLOSED

    def test_allows_calls_after_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        with pytest.raises(ConnectionError):
            cb.call(_failing_fn)
        assert cb.state == CircuitState.OPEN

        cb.reset()
        result = cb.call(_succeeding_fn)
        assert result == 42


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestBedrockBreakerSingleton:
    """Tests for the bedrock_breaker module-level instance."""

    def setup_method(self):
        bedrock_breaker.reset()

    def test_singleton_exists(self):
        assert isinstance(bedrock_breaker, CircuitBreaker)

    def test_singleton_default_config(self):
        assert bedrock_breaker._failure_threshold == 5
        assert bedrock_breaker._recovery_timeout == 30

    def test_singleton_starts_closed(self):
        assert bedrock_breaker.state == CircuitState.CLOSED

    def test_singleton_can_be_tripped(self):
        for _ in range(5):
            with pytest.raises(ConnectionError):
                bedrock_breaker.call(_failing_fn)
        assert bedrock_breaker.state == CircuitState.OPEN

    def test_singleton_reset_works(self):
        for _ in range(5):
            with pytest.raises(ConnectionError):
                bedrock_breaker.call(_failing_fn)
        bedrock_breaker.reset()
        assert bedrock_breaker.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# CircuitOpenError
# ---------------------------------------------------------------------------


class TestCircuitOpenError:
    """Tests for the CircuitOpenError exception."""

    def test_is_exception(self):
        assert issubclass(CircuitOpenError, Exception)

    def test_has_retry_after(self):
        err = CircuitOpenError(retry_after=15.0)
        assert err.retry_after == 15.0

    def test_default_retry_after(self):
        err = CircuitOpenError()
        assert err.retry_after == 0.0

    def test_str_representation(self):
        err = CircuitOpenError(retry_after=30.0)
        assert "OPEN" in str(err)
        assert "30.0" in str(err)
