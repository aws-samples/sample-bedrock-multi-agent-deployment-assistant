"""Cognito JWT authentication middleware.

When AI_LCM_COGNITO_USER_POOL_ID and AI_LCM_COGNITO_CLIENT_ID are set,
API endpoints require a valid JWT Bearer token. The tenant_id is extracted
from the token's custom:tenant_id claim.

When not configured, authentication is disabled and tenant_id defaults
to the query/body parameter value (for local development).
"""

import base64
import json
import logging
import re
import threading
import time
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import settings
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

_POOL_ID_PATTERN = re.compile(r'^[a-z]{2}-[a-z]+-[0-9]_[a-zA-Z0-9]+$')

# TTL-based JWKS cache (5 minutes) — balances freshness for key rotation with
# avoiding excessive JWKS fetches. Forced refresh on key-not-found for fast
# recovery during Cognito key rotation.
_jwks_cache: dict = {"data": None, "ts": 0.0}
_jwks_cache_lock = threading.Lock()
_JWKS_CACHE_TTL_S = 300  # 5 minutes


def _invalidate_jwks_cache() -> None:
    """Force-invalidate the JWKS cache (e.g., on key-not-found during JWT verification)."""
    with _jwks_cache_lock:
        _jwks_cache["ts"] = 0.0


def _get_jwks() -> dict:
    """Fetch and cache JWKS from Cognito for token validation.

    Cached for 5 minutes. After TTL expiry, a fresh fetch is performed
    so that rotated Cognito signing keys are picked up automatically.
    """
    if not settings.cognito_user_pool_id:
        return {}

    now = time.time()
    cached = _jwks_cache["data"]
    if cached is not None and (now - _jwks_cache["ts"]) < _JWKS_CACHE_TTL_S:
        return cached

    with _jwks_cache_lock:
        cached = _jwks_cache["data"]
        if cached is not None and (time.time() - _jwks_cache["ts"]) < _JWKS_CACHE_TTL_S:
            return cached

        pool_id = settings.cognito_user_pool_id
        if not _POOL_ID_PATTERN.match(pool_id):
            logger.error("Invalid Cognito pool ID format: %s", pool_id[:40])
            return {}

        region = settings.aws_region
        jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"

        try:
            import urllib.request

            if not jwks_url.startswith("https://"):
                logger.error("JWKS URL must use HTTPS scheme, got: %s", jwks_url[:30])
                return {}

            with urllib.request.urlopen(jwks_url, timeout=5) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected -- URL is constructed from validated Cognito pool ID, scheme is verified as HTTPS above
                data = json.loads(resp.read())
                _jwks_cache["data"] = data
                _jwks_cache["ts"] = time.time()
                return data
        except Exception as e:
            logger.warning("Failed to fetch JWKS: %s", e)
            if cached is not None:
                logger.info("Returning stale JWKS cache after fetch failure")
                return cached
            return {}


def _decode_jwt_payload(token: str) -> dict:
    """Decode and verify JWT using JWKS when Cognito is configured.

    When Cognito IS configured: uses python-jose with RS256 JWKS verification.
    When Cognito is NOT configured: falls back to basic base64 payload extraction (local dev).
    """
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format")
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)

    from jose import jwt as jose_jwt, JWTError

    jwks = _get_jwks()
    if not jwks:
        raise ValueError("Unable to fetch JWKS for token verification")

    region = settings.aws_region
    pool_id = settings.cognito_user_pool_id
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"

    try:
        return jose_jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=settings.cognito_client_id,
            issuer=issuer,
        )
    except JWTError as e:
        error_msg = str(e).lower()
        if "key" in error_msg or "signature" in error_msg:
            logger.info("JWT verification failed, attempting JWKS refresh: %s", e)
            _invalidate_jwks_cache()
            refreshed_jwks = _get_jwks()
            if refreshed_jwks and refreshed_jwks != jwks:
                try:
                    return jose_jwt.decode(
                        token,
                        refreshed_jwks,
                        algorithms=["RS256"],
                        audience=settings.cognito_client_id,
                        issuer=issuer,
                    )
                except JWTError:
                    pass
        raise ValueError(f"JWT verification failed: {e}") from e


class AuthenticatedUser:
    """Represents an authenticated user extracted from JWT claims."""

    def __init__(self, sub: str, email: str, tenant_id: str):
        self.sub = sub
        self.email = email
        self.tenant_id = tenant_id


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[AuthenticatedUser]:
    """Extract authenticated user from JWT Bearer token.

    Returns None when Cognito is not configured (local development).
    Raises HTTPException 401 when Cognito IS configured but token is invalid.
    """
    # Auth disabled — local development mode
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        request.state.user = None
        return None

    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = _decode_jwt_payload(credentials.credentials)

        exp = payload.get("exp", 0)
        if exp and time.time() > exp:
            raise HTTPException(status_code=401, detail="Token expired")

        aud = payload.get("aud") or payload.get("client_id")
        if aud != settings.cognito_client_id:
            raise HTTPException(status_code=401, detail="Invalid token audience")

        user = AuthenticatedUser(
            sub=payload.get("sub", ""),
            email=payload.get("email", ""),
            tenant_id=payload.get("custom:tenant_id", "default"),
        )
        request.state.user = user
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_tenant_id(
    request: Request,
    tenant_id: str = Query(default="default"),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
) -> str:
    """Extract tenant_id — JWT-authoritative when Cognito is enabled.

    Deployed (Cognito configured): tenant_id comes from the JWT only.
        The query param is ignored to prevent cross-tenant access.
    Local (no Cognito): falls back to query param for dev/testing.
        Defaults to "default" if not provided.
    """
    if user:
        request.state.tenant_id = user.tenant_id
        return user.tenant_id

    if settings.storage_backend == "aws" and not settings.cognito_user_pool_id:
        logger.error(
            "SECURITY: AWS storage backend active without Cognito authentication. "
            "Refusing client-supplied tenant_id. Forcing 'default'."
        )
        request.state.tenant_id = "default"
        return "default"

    try:
        tenant_id = validate_safe_id(tenant_id, "tenant_id")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tenant_id format")
    request.state.tenant_id = tenant_id
    return tenant_id
