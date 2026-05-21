"""Tests for src/config/auth.py — JWT verification and tenant isolation."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.config.auth import (
    AuthenticatedUser,
    _decode_jwt_payload,
    _get_jwks,
    _jwks_cache,
    _jwks_cache_lock,
    get_current_user,
    get_tenant_id,
)


def _make_jwt(payload: dict, kid: str = "test-kid") -> str:
    """Build an unsigned JWT (3-part base64) for testing."""
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    s = base64.urlsafe_b64encode(b"fake-signature").rstrip(b"=").decode()
    return f"{h}.{p}.{s}"


class TestDecodeJwtPayload:
    """Tests for _decode_jwt_payload."""

    def test_basic_decode_without_cognito(self):
        """Without Cognito configured, does basic base64 decode (no verification)."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.cognito_client_id = ""

            payload = {"sub": "user-123", "email": "test@test.com", "custom:tenant_id": "t1"}
            token = _make_jwt(payload)
            result = _decode_jwt_payload(token)
            assert result["sub"] == "user-123"
            assert result["custom:tenant_id"] == "t1"

    def test_basic_decode_rejects_invalid_format(self):
        """Without Cognito, rejects tokens that aren't 3-part JWTs."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.cognito_client_id = ""

            with pytest.raises(ValueError, match="Invalid JWT format"):
                _decode_jwt_payload("only-two.parts")

    def test_verified_decode_fails_without_jwks(self):
        """With Cognito configured but no JWKS available, raises ValueError."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = "us-west-2_TestPool"
            mock_settings.cognito_client_id = "test-client"
            mock_settings.aws_endpoint_url = ""
            mock_settings.aws_region = "us-west-2"

            with patch("src.config.auth._get_jwks", return_value={}):
                with pytest.raises(ValueError, match="Unable to fetch JWKS"):
                    _decode_jwt_payload(_make_jwt({"sub": "x"}))


class TestGetTenantId:
    """Tests for get_tenant_id — the tenant isolation boundary."""

    def test_jwt_tenant_overrides_query_param(self):
        """When user has JWT with tenant_id, query param is ignored."""
        request = MagicMock()
        user = AuthenticatedUser(sub="u1", email="e@e.com", tenant_id="jwt-tenant")
        result = get_tenant_id(request, tenant_id="attacker-tenant", user=user)
        assert result == "jwt-tenant"

    def test_no_cognito_no_debug_forces_default(self):
        """Without Cognito and debug=false, forces 'default' regardless of query param."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.debug = False

            request = MagicMock()
            result = get_tenant_id(request, tenant_id="attacker-tenant", user=None)
            assert result == "default"

    def test_no_cognito_debug_allows_query_param(self):
        """In debug mode without Cognito, accepts query param (local dev)."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.debug = True

            request = MagicMock()
            result = get_tenant_id(request, tenant_id="my-tenant", user=None)
            assert result == "my-tenant"

    def test_invalid_tenant_id_format_rejected(self):
        """Rejects tenant_id with path traversal or special characters."""
        from fastapi import HTTPException

        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.debug = True

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                get_tenant_id(request, tenant_id="../etc/passwd", user=None)
            assert exc_info.value.status_code == 400


class TestGetCurrentUser:
    """Tests for get_current_user — JWT extraction and validation."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_cognito(self):
        """When Cognito is not configured, returns None (local dev mode)."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.cognito_client_id = ""

            request = MagicMock()
            result = await get_current_user(request, credentials=None)
            assert result is None

    @pytest.mark.asyncio
    async def test_raises_401_when_no_credentials(self):
        """With Cognito configured but no Bearer token, returns 401."""
        from fastapi import HTTPException

        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = "us-west-2_TestPool"
            mock_settings.cognito_client_id = "test-client"

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(request, credentials=None)
            assert exc_info.value.status_code == 401


class TestJwksCache:
    """Tests for JWKS caching behavior."""

    def test_cache_returns_stale_data_on_fetch_failure(self):
        """On JWKS fetch failure, returns cached (stale) data if available."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = "us-west-2_TestPool"
            mock_settings.aws_region = "us-west-2"
            mock_settings.aws_endpoint_url = ""

            # Seed cache with valid but expired data
            with _jwks_cache_lock:
                _jwks_cache["data"] = {"keys": [{"kid": "cached"}]}
                _jwks_cache["ts"] = 0  # Expired

            with patch("urllib.request.urlopen", side_effect=Exception("network error")):
                result = _get_jwks()
                assert result == {"keys": [{"kid": "cached"}]}

    def test_cache_respects_ttl(self):
        """Fresh cache is returned without re-fetching."""
        with patch("src.config.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = "us-west-2_TestPool"

            cached_data = {"keys": [{"kid": "fresh"}]}
            with _jwks_cache_lock:
                _jwks_cache["data"] = cached_data
                _jwks_cache["ts"] = time.time()  # Fresh

            result = _get_jwks()
            assert result == cached_data
