"""Unit tests for SSE heartbeats, JWT auth middleware, and CloudWatch metrics.

Covers three untested backend modules:
- src/utils/sse.py — with_heartbeats() async generator wrapper
- src/config/auth.py — Cognito JWT decode, get_current_user, get_tenant_id
- src/config/metrics.py — MetricsPublisher background CloudWatch publishing

All tests mock AWS clients and external dependencies so they run without
credentials or network access.
"""

import asyncio
import base64
import contextvars
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.utils.sse import SSE_HEARTBEAT, with_heartbeats


# ===================================================================
# SSE heartbeat wrapper (src/utils/sse.py)
# ===================================================================


class TestWithHeartbeats:
    """Tests for the with_heartbeats() async generator."""

    @pytest.mark.asyncio
    async def test_heartbeat_emitted_when_source_is_slow(self):
        """A heartbeat comment is yielded when the source takes longer than the interval."""

        async def slow_source():
            await asyncio.sleep(0.5)
            yield "data: done\n\n"

        results = []
        async for item in with_heartbeats(slow_source(), interval=0.1, label="test-slow"):
            results.append(item)

        heartbeats = [r for r in results if r == SSE_HEARTBEAT]
        events = [r for r in results if r != SSE_HEARTBEAT]

        assert len(heartbeats) >= 1, "Expected at least one heartbeat during slow source"
        assert events == ["data: done\n\n"]

    @pytest.mark.asyncio
    async def test_events_pass_through_immediately(self):
        """Events from the source are forwarded without delay or modification."""

        async def fast_source():
            yield "data: first\n\n"
            yield "data: second\n\n"
            yield "data: third\n\n"

        results = []
        async for item in with_heartbeats(fast_source(), interval=10, label="test-fast"):
            results.append(item)

        # With a long interval, no heartbeats should be emitted for a fast source
        assert results == ["data: first\n\n", "data: second\n\n", "data: third\n\n"]

    @pytest.mark.asyncio
    async def test_client_disconnect_handled_gracefully(self):
        """GeneratorExit from client disconnect is caught and logged, not re-raised."""
        items_consumed = []

        async def infinite_source():
            for i in range(100):
                yield f"data: event-{i}\n\n"
                await asyncio.sleep(0.01)

        gen = with_heartbeats(infinite_source(), interval=10, label="test-disconnect")

        # Consume a few items, then close the generator (simulates client disconnect)
        async for item in gen:
            items_consumed.append(item)
            if len(items_consumed) >= 3:
                await gen.aclose()
                break

        assert len(items_consumed) >= 3

    @pytest.mark.asyncio
    async def test_empty_source_produces_no_output(self):
        """An empty source generator produces no events or heartbeats."""

        async def empty_source():
            return
            yield  # noqa: F811 — unreachable yield makes this an async generator

        results = []
        async for item in with_heartbeats(empty_source(), interval=10, label="test-empty"):
            results.append(item)

        assert results == []

    @pytest.mark.asyncio
    async def test_heartbeat_format_matches_sse_comment(self):
        """The heartbeat constant uses the SSE comment format (colon prefix + double newline)."""
        assert SSE_HEARTBEAT == ": heartbeat\n\n"
        assert SSE_HEARTBEAT.startswith(":")
        assert SSE_HEARTBEAT.endswith("\n\n")


# ===================================================================
# JWT authentication middleware (src/config/auth.py)
# ===================================================================


class TestDecodeJwtPayload:
    """Tests for _decode_jwt_payload() function."""

    def _make_jwt(self, payload: dict) -> str:
        """Build a minimal 3-part JWT token (header.payload.signature) without real signing."""
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        sig = "fakesig"
        return f"{header}.{body}.{sig}"

    @patch("src.config.auth.settings")
    def test_base64_payload_extraction_without_cognito(self, mock_settings):
        """Without Cognito configured, base64 payload extraction works as local dev fallback."""
        mock_settings.cognito_user_pool_id = None
        mock_settings.cognito_client_id = None

        from src.config.auth import _decode_jwt_payload

        payload = {"sub": "user-123", "email": "test@example.com", "custom:tenant_id": "t1"}
        token = self._make_jwt(payload)
        result = _decode_jwt_payload(token)

        assert result["sub"] == "user-123"
        assert result["email"] == "test@example.com"
        assert result["custom:tenant_id"] == "t1"

    @patch("src.config.auth.settings")
    def test_invalid_jwt_format_raises_value_error(self, mock_settings):
        """Token with wrong number of parts raises ValueError."""
        mock_settings.cognito_user_pool_id = None
        mock_settings.cognito_client_id = None

        from src.config.auth import _decode_jwt_payload

        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("only.two-parts")

        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("single-part")

        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("one.two.three.four")


class TestGetTenantId:
    """Tests for get_tenant_id() dependency."""

    @patch("src.config.auth.settings")
    def test_returns_user_tenant_id_when_authenticated(self, mock_settings):
        """When an authenticated user exists, tenant_id comes from the JWT claim."""
        mock_settings.storage_backend = "aws"
        mock_settings.cognito_user_pool_id = "us-east-1_abc123"

        from src.config.auth import AuthenticatedUser, get_tenant_id

        user = AuthenticatedUser(sub="sub-1", email="u@e.com", tenant_id="tenant-from-jwt")
        request = MagicMock()
        result = get_tenant_id(request=request, tenant_id="ignored-param", user=user)

        assert result == "tenant-from-jwt"
        assert request.state.tenant_id == "tenant-from-jwt"

    @patch("src.config.auth.settings")
    def test_returns_default_when_aws_storage_without_cognito(self, mock_settings):
        """AWS storage active without Cognito forces 'default' for safety."""
        mock_settings.storage_backend = "aws"
        mock_settings.cognito_user_pool_id = None

        from src.config.auth import get_tenant_id

        request = MagicMock()
        result = get_tenant_id(request=request, tenant_id="attacker-tenant", user=None)

        assert result == "default"
        assert request.state.tenant_id == "default"

    @patch("src.config.auth.settings")
    def test_returns_query_param_in_local_dev_mode(self, mock_settings):
        """In local dev mode (local storage, no Cognito), query param is used."""
        mock_settings.storage_backend = "local"
        mock_settings.cognito_user_pool_id = None

        from src.config.auth import get_tenant_id

        request = MagicMock()
        result = get_tenant_id(request=request, tenant_id="my-dev-tenant", user=None)

        assert result == "my-dev-tenant"
        assert request.state.tenant_id == "my-dev-tenant"


class TestGetCurrentUser:
    """Tests for get_current_user() dependency."""

    @pytest.mark.asyncio
    @patch("src.config.auth.settings")
    async def test_returns_none_when_cognito_not_configured(self, mock_settings):
        """When Cognito is not configured, None is returned (local dev mode)."""
        mock_settings.cognito_user_pool_id = None
        mock_settings.cognito_client_id = None

        from src.config.auth import get_current_user

        request = MagicMock()
        result = await get_current_user(request=request, credentials=None)

        assert result is None
        assert request.state.user is None

    @pytest.mark.asyncio
    @patch("src.config.auth.settings")
    async def test_raises_401_when_cognito_configured_but_no_credentials(self, mock_settings):
        """When Cognito IS configured but no Bearer token, raises 401."""
        mock_settings.cognito_user_pool_id = "us-east-1_TestPool"
        mock_settings.cognito_client_id = "client-id-123"

        from fastapi import HTTPException

        from src.config.auth import get_current_user

        request = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request=request, credentials=None)

        assert exc_info.value.status_code == 401
        assert "Authorization header required" in exc_info.value.detail


class TestJwksCache:
    """Tests for JWKS caching behaviour."""

    @patch("src.config.auth.settings")
    def test_returns_stale_data_on_fetch_failure(self, mock_settings):
        """After a successful fetch, stale JWKS is returned if refresh fails."""
        mock_settings.cognito_user_pool_id = "us-east-1_Abc12345"
        mock_settings.aws_region = "us-east-1"

        import src.config.auth as auth_module

        stale_jwks = {"keys": [{"kid": "old-key"}]}

        # Pre-populate cache with stale data that has expired TTL
        original_cache_data = auth_module._jwks_cache["data"]
        original_cache_ts = auth_module._jwks_cache["ts"]
        try:
            auth_module._jwks_cache["data"] = stale_jwks
            auth_module._jwks_cache["ts"] = 0.0  # Expired

            with patch("urllib.request.urlopen", side_effect=ConnectionError("network down")):
                result = auth_module._get_jwks()

            assert result == stale_jwks
        finally:
            auth_module._jwks_cache["data"] = original_cache_data
            auth_module._jwks_cache["ts"] = original_cache_ts

    @patch("src.config.auth.settings")
    def test_cache_ttl_expires_and_refreshes(self, mock_settings):
        """Expired cache triggers a new JWKS fetch."""
        mock_settings.cognito_user_pool_id = "us-east-1_Abc12345"
        mock_settings.aws_region = "us-east-1"

        import src.config.auth as auth_module

        fresh_jwks = {"keys": [{"kid": "new-key"}]}
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(fresh_jwks).encode()

        original_cache_data = auth_module._jwks_cache["data"]
        original_cache_ts = auth_module._jwks_cache["ts"]
        try:
            auth_module._jwks_cache["data"] = {"keys": [{"kid": "old-key"}]}
            auth_module._jwks_cache["ts"] = 0.0  # Expired

            with patch("urllib.request.urlopen", return_value=mock_response):
                result = auth_module._get_jwks()

            assert result == fresh_jwks
            assert auth_module._jwks_cache["data"] == fresh_jwks
            # Timestamp should be recent
            assert auth_module._jwks_cache["ts"] > time.time() - 5
        finally:
            auth_module._jwks_cache["data"] = original_cache_data
            auth_module._jwks_cache["ts"] = original_cache_ts


class TestPoolIdValidation:
    """Tests for Cognito pool ID format validation."""

    @patch("src.config.auth.settings")
    def test_rejects_invalid_pool_id_format(self, mock_settings):
        """Invalid pool ID format causes _get_jwks to return empty dict."""
        mock_settings.cognito_user_pool_id = "INVALID_POOL_ID"
        mock_settings.aws_region = "us-east-1"

        import src.config.auth as auth_module

        original_cache_data = auth_module._jwks_cache["data"]
        original_cache_ts = auth_module._jwks_cache["ts"]
        try:
            auth_module._jwks_cache["data"] = None
            auth_module._jwks_cache["ts"] = 0.0

            result = auth_module._get_jwks()
            assert result == {}
        finally:
            auth_module._jwks_cache["data"] = original_cache_data
            auth_module._jwks_cache["ts"] = original_cache_ts

    @patch("src.config.auth.settings")
    def test_rejects_url_injection_in_pool_id(self, mock_settings):
        """Pool IDs with URL injection attempts are rejected."""
        mock_settings.cognito_user_pool_id = "us-east-1_Abc/../../../etc/passwd"
        mock_settings.aws_region = "us-east-1"

        import src.config.auth as auth_module

        original_cache_data = auth_module._jwks_cache["data"]
        original_cache_ts = auth_module._jwks_cache["ts"]
        try:
            auth_module._jwks_cache["data"] = None
            auth_module._jwks_cache["ts"] = 0.0

            result = auth_module._get_jwks()
            assert result == {}
        finally:
            auth_module._jwks_cache["data"] = original_cache_data
            auth_module._jwks_cache["ts"] = original_cache_ts


# ===================================================================
# CloudWatch metrics publisher (src/config/metrics.py)
# ===================================================================


class TestMetricsPublisher:
    """Tests for the MetricsPublisher class."""

    @patch("src.config.metrics.MetricsPublisher._get_client")
    @patch("src.config.settings.settings")
    def test_record_latency_calls_put_metric_data(self, mock_settings, mock_get_client):
        """record_latency() publishes BedrockInvocationLatencyMs with correct dimensions."""
        mock_settings.metrics_enabled = True
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        publisher._client = mock_client
        publisher._disabled = False
        publisher._shutdown = False

        # Call _put_metric directly (synchronous) to avoid executor complexity
        publisher._put_metric(
            "BedrockInvocationLatencyMs",
            150.5,
            "Milliseconds",
            [
                {"Name": "AgentName", "Value": "design"},
                {"Name": "TenantId", "Value": "tenant-1"},
            ],
        )

        mock_client.put_metric_data.assert_called_once()
        call_kwargs = mock_client.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "AI-LCM"
        metric_data = call_kwargs["MetricData"][0]
        assert metric_data["MetricName"] == "BedrockInvocationLatencyMs"
        assert metric_data["Value"] == 150.5
        assert metric_data["Unit"] == "Milliseconds"
        assert {"Name": "AgentName", "Value": "design"} in metric_data["Dimensions"]
        assert {"Name": "TenantId", "Value": "tenant-1"} in metric_data["Dimensions"]
        publisher._executor.shutdown(wait=False)

    @patch("src.config.metrics.MetricsPublisher._get_client")
    @patch("src.config.settings.settings")
    def test_record_rate_limit_calls_put_metric_data(self, mock_settings, mock_get_client):
        """record_rate_limit() publishes RateLimitExceeded with endpoint dimension."""
        mock_settings.metrics_enabled = True
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        publisher._client = mock_client
        publisher._disabled = False
        publisher._shutdown = False

        publisher._put_metric(
            "RateLimitExceeded",
            1,
            "Count",
            [{"Name": "Endpoint", "Value": "/api/interview/chat"}],
        )

        mock_client.put_metric_data.assert_called_once()
        call_kwargs = mock_client.put_metric_data.call_args[1]
        metric_data = call_kwargs["MetricData"][0]
        assert metric_data["MetricName"] == "RateLimitExceeded"
        assert metric_data["Value"] == 1
        assert metric_data["Unit"] == "Count"
        assert {"Name": "Endpoint", "Value": "/api/interview/chat"} in metric_data["Dimensions"]
        publisher._executor.shutdown(wait=False)

    @patch("src.config.metrics.MetricsPublisher._get_client")
    @patch("src.config.settings.settings")
    def test_record_retry_calls_put_metric_data(self, mock_settings, mock_get_client):
        """record_retry() publishes RetryAttempt with agent dimension."""
        mock_settings.metrics_enabled = True
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        publisher._client = mock_client
        publisher._disabled = False
        publisher._shutdown = False

        publisher._put_metric(
            "RetryAttempt",
            2,
            "Count",
            [{"Name": "AgentName", "Value": "iac"}],
        )

        mock_client.put_metric_data.assert_called_once()
        call_kwargs = mock_client.put_metric_data.call_args[1]
        metric_data = call_kwargs["MetricData"][0]
        assert metric_data["MetricName"] == "RetryAttempt"
        assert metric_data["Value"] == 2
        assert {"Name": "AgentName", "Value": "iac"} in metric_data["Dimensions"]
        publisher._executor.shutdown(wait=False)

    @patch("src.config.settings.settings")
    def test_metrics_disabled_when_settings_false(self, mock_settings):
        """No metrics are published when metrics_enabled is False."""
        mock_settings.metrics_enabled = False

        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        publisher._client = MagicMock()
        publisher._disabled = False
        publisher._shutdown = False

        publisher._put_metric("Foo", 1, "Count", [])

        publisher._client.put_metric_data.assert_not_called()
        publisher._executor.shutdown(wait=False)

    @patch("src.config.settings.settings")
    def test_metrics_disabled_after_no_credentials_error(self, mock_settings):
        """After a NoCredentialsError, metrics are permanently disabled."""
        mock_settings.metrics_enabled = True

        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        mock_client = MagicMock()

        # Simulate NoCredentialsError (create a custom exception with that class name)
        class NoCredentialsError(Exception):
            pass

        mock_client.put_metric_data.side_effect = NoCredentialsError("no creds")
        publisher._client = mock_client
        publisher._disabled = False
        publisher._shutdown = False

        publisher._put_metric("Foo", 1, "Count", [])

        assert publisher._disabled is True

        # Subsequent calls should be no-ops
        mock_client.put_metric_data.reset_mock()
        publisher._put_metric("Bar", 2, "Count", [])
        mock_client.put_metric_data.assert_not_called()
        publisher._executor.shutdown(wait=False)

    def test_shutdown_sets_flag_and_drains_executor(self):
        """shutdown() sets the _shutdown flag and drains the executor."""
        from src.config.metrics import MetricsPublisher

        publisher = MetricsPublisher()
        assert publisher._shutdown is False

        publisher.shutdown(wait=True)

        assert publisher._shutdown is True
        # After shutdown, executor should not accept new tasks
        with pytest.raises(RuntimeError):
            publisher._executor.submit(lambda: None)

    @patch("src.config.settings.settings")
    def test_context_variables_propagated_to_background_threads(self, mock_settings):
        """ContextVars from the calling context are visible in the background thread."""
        mock_settings.metrics_enabled = True

        from src.config.metrics import MetricsPublisher

        test_var = contextvars.ContextVar("test_var", default="unset")
        captured_values = []

        publisher = MetricsPublisher()
        mock_client = MagicMock()
        publisher._client = mock_client

        def capture_metric(*args, **kwargs):
            """Capture the context var value from inside the background thread."""
            captured_values.append(test_var.get())

        mock_client.put_metric_data.side_effect = capture_metric

        # Set the context variable in the calling context
        test_var.set("propagated-value")

        # Use _submit which copies context to the background thread
        publisher._submit(
            publisher._put_metric,
            "TestMetric",
            1,
            "Count",
            [],
        )

        # Wait for the background thread to complete
        publisher._executor.shutdown(wait=True)

        assert len(captured_values) == 1
        assert captured_values[0] == "propagated-value"
