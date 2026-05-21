"""Authentication routes — USER_PASSWORD_AUTH flow against Cognito (or Floci emulator)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.config.aws import aws_client
from src.config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    id_token: str
    access_token: str
    expires_in: int
    token_type: str
    tenant_id: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest):
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        raise HTTPException(status_code=501, detail="Cognito not configured")

    cognito = aws_client("cognito-idp")

    try:
        resp = cognito.initiate_auth(
            ClientId=settings.cognito_client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": body.email,
                "PASSWORD": body.password,
            },
        )
    except cognito.exceptions.NotAuthorizedException:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except cognito.exceptions.UserNotFoundException:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except Exception as e:
        logger.error("Cognito auth error: %s", e)
        raise HTTPException(status_code=500, detail="Authentication service error")

    result = resp.get("AuthenticationResult")
    if not result:
        challenge = resp.get("ChallengeName")
        raise HTTPException(
            status_code=400,
            detail=f"Auth challenge required: {challenge}",
        )

    from src.config.auth import _decode_jwt_payload, _lookup_tenant_id
    payload = _decode_jwt_payload(result["IdToken"])
    tenant_id = payload.get("custom:tenant_id")
    if not tenant_id and settings.aws_endpoint_url:
        tenant_id = _lookup_tenant_id(payload.get("username") or payload.get("cognito:username", ""))

    return LoginResponse(
        id_token=result["IdToken"],
        access_token=result["AccessToken"],
        expires_in=result.get("ExpiresIn", 3600),
        token_type=result.get("TokenType", "Bearer"),
        tenant_id=tenant_id or "default",
    )
