"""WebSocket $connect authorizer Lambda.

Validates the JWT token passed as a query parameter on WebSocket connect.
When Cognito env vars are not configured, allows all connections (local dev).

Expected connection URL: wss://host/prod?token=<jwt>
"""

import base64
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_POOL_ID_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]_[a-zA-Z0-9]+$")

_jwks_cache: dict = {"data": None, "ts": 0.0}
_jwks_cache_lock = threading.Lock()
_JWKS_CACHE_TTL_S = 300


def _get_jwks(pool_id: str, region: str) -> dict:
    now = time.time()
    cached = _jwks_cache["data"]
    if cached is not None and (now - _jwks_cache["ts"]) < _JWKS_CACHE_TTL_S:
        return cached

    with _jwks_cache_lock:
        cached = _jwks_cache["data"]
        if cached is not None and (time.time() - _jwks_cache["ts"]) < _JWKS_CACHE_TTL_S:
            return cached

        jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
        try:
            import urllib.request

            with urllib.request.urlopen(jwks_url, timeout=5) as resp:
                data = json.loads(resp.read())
                _jwks_cache["data"] = data
                _jwks_cache["ts"] = time.time()
                return data
        except Exception as e:
            logger.warning("Failed to fetch JWKS: %s", e)
            return cached or {}


def _decode_jwt_basic(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def handler(event, context):
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    # No Cognito configured — allow all (local development)
    if not pool_id or not client_id:
        logger.info("No Cognito configured, allowing connection (local mode)")
        return _generate_policy("local-dev", "Allow", event, {"tenant_id": "default"})

    # Extract token from query string
    query_params = event.get("queryStringParameters") or {}
    token = query_params.get("token", "")

    if not token:
        logger.warning("No token in query string")
        return _generate_policy("anonymous", "Deny", event, {})

    try:
        if not _POOL_ID_PATTERN.match(pool_id):
            raise ValueError(f"Invalid pool ID format: {pool_id[:40]}")

        from jose import jwt as jose_jwt, JWTError

        jwks = _get_jwks(pool_id, region)
        if not jwks:
            raise ValueError("Unable to fetch JWKS")

        issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        payload = jose_jwt.decode(
            token, jwks, algorithms=["RS256"], audience=client_id, issuer=issuer
        )

        exp = payload.get("exp", 0)
        if exp and time.time() > exp:
            raise ValueError("Token expired")

        tenant_id = payload.get("custom:tenant_id", "default")
        sub = payload.get("sub", "unknown")

        logger.info("Authorized: sub=%s tenant=%s", sub, tenant_id)
        return _generate_policy(sub, "Allow", event, {"tenant_id": tenant_id})

    except Exception as e:
        logger.warning("Authorization failed: %s", e)
        return _generate_policy("anonymous", "Deny", event, {})


def _generate_policy(
    principal_id: str, effect: str, event: dict, context: dict
) -> dict:
    route_arn = event.get("methodArn", event.get("routeArn", "*"))

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": route_arn,
                }
            ],
        },
        "context": context,
    }
