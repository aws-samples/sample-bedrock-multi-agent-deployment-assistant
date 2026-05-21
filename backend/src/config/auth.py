"""Cognito JWT authentication middleware.

When AI_DEPLOY_COGNITO_USER_POOL_ID and AI_DEPLOY_COGNITO_CLIENT_ID are set,
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
        if settings.aws_endpoint_url:
            jwks_url = f"{settings.aws_endpoint_url}/{pool_id}/.well-known/jwks.json"
        else:
            jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"

        try:
            import urllib.request

            if not jwks_url.startswith(("https://", "http://localhost")):
                logger.error("JWKS URL must use HTTPS or localhost scheme, got: %s", jwks_url[:30])
                return {}

            with urllib.request.urlopen(jwks_url, timeout=5) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected -- URL is constructed from validated Cognito pool ID, scheme is verified above
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

    When Cognito IS configured: uses PyJWT with RS256 JWKS verification.
      - Real AWS: JWKS from cognito-idp.{region}.amazonaws.com/{poolId}
      - Floci: JWKS from {endpoint_url}/{poolId}/.well-known/jwks.json
    When Cognito is NOT configured: falls back to basic base64 payload extraction.
    """
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format")
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)

    import jwt as pyjwt
    from jwt import PyJWKClient, InvalidTokenError

    jwks = _get_jwks()
    if not jwks:
        raise ValueError("Unable to fetch JWKS for token verification")

    region = settings.aws_region
    pool_id = settings.cognito_user_pool_id
    if settings.aws_endpoint_url:
        issuer = f"{settings.aws_endpoint_url}/{pool_id}"
    else:
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"

    try:
        jwk_client = PyJWKClient.__new__(PyJWKClient)
        jwk_client.jwk_set = pyjwt.PyJWKSet.from_dict(jwks)
        unverified_header = pyjwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise ValueError("JWT header missing 'kid'")
        signing_key = jwk_client.jwk_set[kid]
        return pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.cognito_client_id,
            issuer=issuer,
        )
    except (InvalidTokenError, KeyError) as e:
        error_msg = str(e).lower()
        if "kid" in error_msg or "key" in error_msg or "signature" in error_msg:
            logger.info("JWT verification failed, attempting JWKS refresh: %s", e)
            _invalidate_jwks_cache()
            refreshed_jwks = _get_jwks()
            if refreshed_jwks and refreshed_jwks != jwks:
                try:
                    jwk_client.jwk_set = pyjwt.PyJWKSet.from_dict(refreshed_jwks)
                    signing_key = jwk_client.jwk_set[kid]
                    return pyjwt.decode(
                        token,
                        signing_key.key,
                        algorithms=["RS256"],
                        audience=settings.cognito_client_id,
                        issuer=issuer,
                    )
                except (InvalidTokenError, KeyError):
                    pass
        raise ValueError(f"JWT verification failed: {e}") from e


def _lookup_tenant_id(username: str) -> str | None:
    """Look up custom:tenant_id from Cognito user attributes (Floci fallback).

    Floci doesn't propagate custom attributes into JWT claims, so we fetch them directly.
    """
    if not username or not settings.cognito_user_pool_id:
        return None
    try:
        from src.config.aws import aws_client
        cognito = aws_client("cognito-idp")
        resp = cognito.admin_get_user(
            UserPoolId=settings.cognito_user_pool_id,
            Username=username,
        )
        for attr in resp.get("UserAttributes", []):
            if attr["Name"] == "custom:tenant_id":
                return attr["Value"]
    except Exception as e:
        logger.debug("Failed to look up tenant_id for %s: %s", username, e)
    return None


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

        tenant_id = payload.get("custom:tenant_id")
        if not tenant_id and settings.aws_endpoint_url:
            tenant_id = _lookup_tenant_id(payload.get("username") or payload.get("cognito:username", ""))
        user = AuthenticatedUser(
            sub=payload.get("sub", ""),
            email=payload.get("email", ""),
            tenant_id=tenant_id or "default",
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

    if not settings.cognito_user_pool_id and not settings.debug:
        logger.error(
            "SECURITY: Cognito authentication not configured in non-debug mode. "
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
