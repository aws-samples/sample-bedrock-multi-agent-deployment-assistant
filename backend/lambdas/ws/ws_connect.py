"""WebSocket $connect handler.

Stores the new WebSocket connection in DynamoDB with a 2-hour TTL.
API Gateway invokes this Lambda when a client opens a WebSocket connection.

If a Cognito authorizer is attached, the authenticated tenant_id is stored
on the connection record for downstream validation (e.g., subscribe).

DynamoDB item schema:
    pk: WS#{connection_id}
    sk: CONNECTION
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Handle WebSocket $connect route."""
    connection_id = event["requestContext"]["connectionId"]
    logger.info("WebSocket connect: %s", connection_id)

    # Extract authenticated tenant_id from authorizer context (if present).
    # This is populated when a Cognito or Lambda authorizer is attached to $connect.
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    auth_tenant_id = (
        authorizer.get("custom:tenant_id")
        or authorizer.get("tenant_id")
        or ""
    )

    table = boto3.resource("dynamodb").Table(os.environ["DYNAMODB_TABLE"])
    now = datetime.now(UTC)

    item = {
        "pk": f"WS#{connection_id}",
        "sk": "CONNECTION",
        "connection_id": connection_id,
        "connected_at": now.isoformat(),
        "ttl": int((now + timedelta(hours=2)).timestamp()),
    }
    if auth_tenant_id:
        item["auth_tenant_id"] = auth_tenant_id
        logger.info("Stored auth_tenant_id=%s for connection %s", auth_tenant_id, connection_id)

    table.put_item(Item=item)

    return {"statusCode": 200}
