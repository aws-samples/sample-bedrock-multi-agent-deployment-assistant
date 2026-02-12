"""Unit tests for observability configuration.

Tests cover structured JSON logging, correlation ID generation, and
the Timer context manager.
"""

import json
import logging
import time

from src.config.observability import (
    StructuredFormatter,
    Timer,
    correlation_id,
    new_correlation_id,
    setup_logging,
)


# ===================================================================
# new_correlation_id
# ===================================================================


class TestNewCorrelationId:
    """Tests for new_correlation_id generation."""

    def test_length_is_12(self):
        cid = new_correlation_id()
        assert len(cid) == 12

    def test_is_alphanumeric(self):
        cid = new_correlation_id()
        assert cid.isalnum()

    def test_is_hex_characters(self):
        """Correlation IDs are hex substrings of UUID4."""
        cid = new_correlation_id()
        # Should be valid hex (uuid4().hex[:12])
        int(cid, 16)  # Should not raise ValueError

    def test_unique_across_many_calls(self):
        ids = {new_correlation_id() for _ in range(100)}
        assert len(ids) == 100, "Collision detected among 100 correlation IDs"

    def test_sets_context_var(self):
        cid = new_correlation_id()
        assert correlation_id.get() == cid

    def test_subsequent_call_updates_context_var(self):
        cid1 = new_correlation_id()
        cid2 = new_correlation_id()
        assert cid1 != cid2
        assert correlation_id.get() == cid2


# ===================================================================
# correlation_id context var
# ===================================================================


class TestCorrelationIdContextVar:
    """Tests for the correlation_id ContextVar."""

    def test_default_is_empty_string(self):
        """Default value before any correlation ID is set."""
        # Reset by setting to default
        token = correlation_id.set("")
        assert correlation_id.get() == ""
        correlation_id.reset(token)

    def test_set_and_get(self):
        token = correlation_id.set("test-cid-123")
        assert correlation_id.get() == "test-cid-123"
        correlation_id.reset(token)

    def test_get_with_default(self):
        """get() with explicit default parameter."""
        token = correlation_id.set("")
        assert correlation_id.get("fallback") == ""
        correlation_id.reset(token)


# ===================================================================
# StructuredFormatter
# ===================================================================


class TestStructuredFormatter:
    """Tests for the StructuredFormatter JSON log formatter."""

    def _make_record(self, msg="Test message", level=logging.INFO, **extras):
        """Create a LogRecord with optional extra attributes."""
        record = logging.LogRecord(
            name="test.logger",
            level=level,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for key, value in extras.items():
            setattr(record, key, value)
        return record

    def test_produces_valid_json(self):
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)  # Should not raise
        assert isinstance(parsed, dict)

    def test_includes_message(self):
        formatter = StructuredFormatter()
        record = self._make_record(msg="Hello world")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "Hello world"

    def test_includes_level(self):
        formatter = StructuredFormatter()
        record = self._make_record(level=logging.WARNING)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "WARNING"

    def test_includes_timestamp(self):
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert len(parsed["timestamp"]) > 0

    def test_includes_logger_name(self):
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["logger"] == "test.logger"

    def test_includes_correlation_id(self):
        cid = new_correlation_id()
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["correlation_id"] == cid

    def test_includes_tenant_id_extra(self):
        formatter = StructuredFormatter()
        record = self._make_record(tenant_id="tenant-abc")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["tenant_id"] == "tenant-abc"

    def test_includes_project_id_extra(self):
        formatter = StructuredFormatter()
        record = self._make_record(project_id="proj-123")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["project_id"] == "proj-123"

    def test_includes_duration_ms_extra(self):
        formatter = StructuredFormatter()
        record = self._make_record(duration_ms=42)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["duration_ms"] == 42

    def test_includes_agent_extra(self):
        formatter = StructuredFormatter()
        record = self._make_record(agent="design-agent")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["agent"] == "design-agent"

    def test_includes_step_extra(self):
        formatter = StructuredFormatter()
        record = self._make_record(step="iac")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["step"] == "iac"

    def test_excludes_missing_extras(self):
        """Extra fields not set on the record should not appear in output."""
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "tenant_id" not in parsed
        assert "duration_ms" not in parsed
        assert "agent" not in parsed

    def test_includes_exception_info(self):
        formatter = StructuredFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="Error occurred",
                args=(),
                exc_info=sys.exc_info(),
            )

        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "test error" in parsed["exception"]

    def test_no_exception_when_exc_info_is_none(self):
        formatter = StructuredFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" not in parsed

    def test_all_log_levels(self):
        formatter = StructuredFormatter()
        for level, name in [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]:
            record = self._make_record(level=level)
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["level"] == name


# ===================================================================
# Timer context manager
# ===================================================================


class TestTimer:
    """Tests for the Timer context manager."""

    def test_measures_elapsed_time(self):
        with Timer() as t:
            time.sleep(0.02)
        assert t.duration_ms >= 15  # Allow slight timing variance
        assert t.duration_ms < 2000  # Sanity upper bound

    def test_zero_work_fast(self):
        with Timer() as t:
            pass
        assert t.duration_ms >= 0
        assert t.duration_ms < 100  # Should be nearly instant

    def test_initial_duration_is_zero(self):
        t = Timer()
        assert t.duration_ms == 0

    def test_duration_set_after_exit(self):
        t = Timer()
        t.__enter__()
        time.sleep(0.01)
        t.__exit__(None, None, None)
        assert t.duration_ms >= 5

    def test_duration_is_float(self):
        with Timer() as t:
            pass
        assert isinstance(t.duration_ms, float)

    def test_multiple_uses(self):
        """Timer can be used multiple times (not a one-shot)."""
        t = Timer()

        with t:
            time.sleep(0.01)
        first = t.duration_ms
        assert first >= 5

        with t:
            time.sleep(0.02)
        second = t.duration_ms

        # Second measurement should be independent
        assert second >= 15


# ===================================================================
# setup_logging
# ===================================================================


class TestSetupLogging:
    """Tests for the setup_logging configuration function."""

    def test_setup_logging_debug_mode(self):
        setup_logging(debug=True)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_setup_logging_info_mode(self):
        setup_logging(debug=False)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_setup_logging_installs_structured_formatter(self):
        setup_logging(debug=False)
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        handler = root.handlers[0]
        assert isinstance(handler.formatter, StructuredFormatter)

    def test_setup_logging_reduces_noise(self):
        """Third-party loggers should be set to WARNING level."""
        setup_logging(debug=False)
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("botocore").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING
