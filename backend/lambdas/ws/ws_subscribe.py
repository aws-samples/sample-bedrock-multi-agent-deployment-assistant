"""WebSocket subscribe route handler.

Clients send a message to subscribe to design task updates for a project:
    {"action": "subscribe", "project_id": "xxx", "tenant_id": "default"}

Stores the subscription in DynamoDB so the design worker can find
connected clients when sending status updates.

Security: validates that the requested tenant_id matches the authenticated
tenant stored at $connect time (if an authorizer is configured). Also
validates input format to prevent injection via DynamoDB keys.

DynamoDB item schema:
    pk: WS#{connection_id}
    sk: SUB#{tenant_id}#{project_id}
"""

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Same pattern as backend validate_safe_id — alphanumeric, hyphens, underscores
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_id(value: str, field: str) -> str:
    """Validate an ID is safe for use in DynamoDB keys."""
    if not value or not _SAFE_ID_RE.match(value) or len(value) > 128:
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


def handler(event, context):
    """Handle WebSocket subscribe action."""
    connection_id = event["requestContext"]["connectionId"]
    body = json.loads(event.get("body", "{}"))

    project_id = body.get("project_id", "")
    tenant_id = body.get("tenant_id", "default")

    # --- Input validation ---
    if not project_id:
        logger.warning(
            "Subscribe request from %s missing project_id", connection_id,
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "project_id is required"}),
        }

    try:
        tenant_id = _validate_id(tenant_id, "tenant_id")
        project_id = _validate_id(project_id, "project_id")
    except ValueError as exc:
        logger.warning("Subscribe input validation failed: %s", exc)
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(exc)}),
        }

    # --- Tenant ownership validation ---
    # If the $connect handler stored an auth_tenant_id (from a Cognito/Lambda
    # authorizer), verify the subscribe request matches the authenticated tenant.
    table = boto3.resource("dynamodb").Table(os.environ["DYNAMODB_TABLE"])

    conn_record = table.get_item(
        Key={"pk": f"WS#{connection_id}", "sk": "CONNECTION"},
        ProjectionExpression="auth_tenant_id",
    ).get("Item")

    if conn_record and conn_record.get("auth_tenant_id"):
        auth_tenant = conn_record["auth_tenant_id"]
        if tenant_id != auth_tenant:
            logger.warning(
                "Tenant mismatch: connection %s authenticated as %s but "
                "requested subscription for %s",
                connection_id, auth_tenant, tenant_id,
            )
            return {
                "statusCode": 403,
                "body": json.dumps({"error": "tenant_id mismatch"}),
            }

    logger.info(
        "WebSocket subscribe: connection=%s tenant=%s project=%s",
        connection_id,
        tenant_id,
        project_id,
    )

    now = datetime.now(UTC)

    table.put_item(
        Item={
            "pk": f"WS#{connection_id}",
            "sk": f"SUB#{tenant_id}#{project_id}",
            "connection_id": connection_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "gsi2pk": f"SUB#{tenant_id}#{project_id}",
            "gsi2sk": f"WS#{connection_id}",
            "subscribed_at": now.isoformat(),
            "ttl": int((now + timedelta(hours=2)).timestamp()),
        }
    )

    return {"statusCode": 200}
