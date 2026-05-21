"""Centralized boto3 client/resource factory.

Routes all AWS service calls through the configured endpoint_url (Floci)
except Bedrock services, which always connect to real AWS via the configured profile.
"""

import boto3

from src.config.settings import settings

_BEDROCK_SERVICES = {
    "bedrock", "bedrock-runtime", "bedrock-agent-runtime",
    "bedrock-agentcore", "bedrock-agentcore-control",
}


def _session_for(service: str) -> boto3.Session:
    """Return a boto3 Session — profile-based for real AWS, default for Floci."""
    if service in _BEDROCK_SERVICES and settings.aws_profile:
        return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
    return boto3.Session(region_name=settings.aws_region)


def aws_client(service: str, **kwargs):
    """Create a boto3 client with automatic endpoint routing."""
    session = _session_for(service)
    params = {**kwargs}
    if settings.aws_endpoint_url and service not in _BEDROCK_SERVICES:
        params["endpoint_url"] = settings.aws_endpoint_url
    return session.client(service, **params)


def aws_resource(service: str, **kwargs):
    """Create a boto3 resource with automatic endpoint routing."""
    session = _session_for(service)
    params = {**kwargs}
    if settings.aws_endpoint_url and service not in _BEDROCK_SERVICES:
        params["endpoint_url"] = settings.aws_endpoint_url
    return session.resource(service, **params)


def s3_encryption_kwargs() -> dict:
    """Return SSE kwargs for S3 put_object. Skipped when using Floci."""
    if settings.aws_endpoint_url:
        return {}
    return {"ServerSideEncryption": "aws:kms"}
