"""Unit tests for guardrails configuration.

Tests verify that Bedrock guardrail kwargs are correctly generated based
on whether a guardrail_id is configured in settings.
"""

from unittest.mock import patch


class TestGetGuardrailKwargs:
    """Tests for src.config.guardrails.get_guardrail_kwargs."""

    def test_disabled_when_guardrail_id_is_none(self):
        """Returns empty dict when guardrail_id is not set."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = None
            from src.config.guardrails import get_guardrail_kwargs

            result = get_guardrail_kwargs()
            assert result == {}

    def test_disabled_when_guardrail_id_is_empty_string(self):
        """Returns empty dict when guardrail_id is empty string."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = ""
            from src.config.guardrails import get_guardrail_kwargs

            result = get_guardrail_kwargs()
            assert result == {}

    def test_enabled_with_guardrail_id(self):
        """Returns full guardrail kwargs when guardrail_id is configured."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = "abc123"
            mock_settings.guardrail_version = "DRAFT"
            from src.config.guardrails import get_guardrail_kwargs

            result = get_guardrail_kwargs()
            assert result == {
                "guardrail_id": "abc123",
                "guardrail_version": "DRAFT",
                "guardrail_trace": "enabled",
            }

    def test_custom_guardrail_version(self):
        """Supports numbered guardrail versions."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = "gr-xyz789"
            mock_settings.guardrail_version = "1"
            from src.config.guardrails import get_guardrail_kwargs

            result = get_guardrail_kwargs()
            assert result["guardrail_id"] == "gr-xyz789"
            assert result["guardrail_version"] == "1"

    def test_guardrail_trace_always_enabled(self):
        """guardrail_trace is always 'enabled' when guardrails are active."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = "test-id"
            mock_settings.guardrail_version = "DRAFT"
            from src.config.guardrails import get_guardrail_kwargs

            result = get_guardrail_kwargs()
            assert result["guardrail_trace"] == "enabled"

    def test_kwargs_can_be_unpacked_into_bedrock_model(self):
        """Result should be safely unpackable as **kwargs."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = "test-id"
            mock_settings.guardrail_version = "DRAFT"
            from src.config.guardrails import get_guardrail_kwargs

            kwargs = get_guardrail_kwargs()
            # Verify all keys are strings (valid keyword arguments)
            for key in kwargs:
                assert isinstance(key, str)
            # Verify all values are serializable
            for value in kwargs.values():
                assert isinstance(value, str)

    def test_empty_kwargs_safe_to_unpack(self):
        """Empty dict from disabled guardrails should be safely unpackable."""
        with patch("src.config.guardrails.settings") as mock_settings:
            mock_settings.guardrail_id = None
            from src.config.guardrails import get_guardrail_kwargs

            kwargs = get_guardrail_kwargs()
            # Should work without error
            combined = {"model_id": "test", **kwargs}
            assert combined == {"model_id": "test"}
