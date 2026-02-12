"""EventBridge Pipe → WebSocket notification bridge.

Receives DynamoDB stream records (filtered for task status changes) from an
EventBridge Pipe and fans out status updates to all WebSocket connections
subscribed to the relevant project via GSI2.

DynamoDB subscription schema (GSI2):
    gsi2pk: SUB#{tenant_id}#{project_id}
    gsi2sk: WS#{connection_id}
"""

import json
import logging
import os
from datetime import UTC, datetime

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Module-level initialization (reused across Lambda warm starts)
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(os.environ["DYNAMODB_TABLE"])
_apigw = None


def _get_apigw():
    """Lazy-init API Gateway Management API client."""
    global _apigw
    callback_url = os.environ.get("WEBSOCKET_CALLBACK_URL", "")
    if _apigw is None and callback_url:
        _apigw = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=callback_url,
        )
    return _apigw


def _cleanup_connection(connection_id: str) -> None:
    """Remove a stale WebSocket connection and all its subscriptions."""
    try:
        items = []
        response = _table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"WS#{connection_id}"},
        )
        items.extend(response.get("Items", []))
        while response.get("LastEvaluatedKey"):
            response = _table.query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": f"WS#{connection_id}"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        if items:
            with _table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            logger.info(
                "Cleaned up %d items for stale connection %s",
                len(items),
                connection_id,
            )
    except Exception:
        logger.exception("Failed to clean up connection %s", connection_id)


def handler(event, context):
    """Process DynamoDB stream records from EventBridge Pipe."""
    apigw = _get_apigw()
    if not apigw:
        logger.warning("WEBSOCKET_CALLBACK_URL not configured, skipping")
        return

    for record in event:
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        old_image = record.get("dynamodb", {}).get("OldImage", {})

        # Skip if status hasn't actually changed
        new_status = new_image.get("status", {}).get("S", "")
        old_status = old_image.get("status", {}).get("S", "")
        if new_status == old_status:
            continue

        tenant_id = new_image.get("tenant_id", {}).get("S", "")
        project_id = new_image.get("project_id", {}).get("S", "")
        task_id = new_image.get("task_id", {}).get("S", "")

        if not all([tenant_id, project_id, task_id]):
            continue

        # Determine task domain from the DynamoDB sort key prefix
        sk = record.get("dynamodb", {}).get("Keys", {}).get("sk", {}).get("S", "")
        pk = record.get("dynamodb", {}).get("Keys", {}).get("pk", {}).get("S", "")
        if sk.startswith("IAC_TASK#"):
            domain = "iac"
        elif sk.startswith("DOCS_TASK#"):
            domain = "docs"
        else:
            domain = "design"

        # Map terminal statuses to the message types the frontend expects
        if new_status == "completed":
            msg_type = f"{domain}_complete"
        elif new_status == "failed":
            msg_type = f"{domain}_failed"
        else:
            msg_type = f"{domain}_status"

        logger.info(
            "Task %s status changed: %s → %s (type=%s, tenant=%s, project=%s)",
            task_id,
            old_status,
            new_status,
            msg_type,
            tenant_id,
            project_id,
        )

        # Build the message payload
        payload: dict = {
            "type": msg_type,
            "task_id": task_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "status": new_status,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # For completed tasks, fetch the result via GetItem so the frontend
        # can render immediately without a follow-up poll.
        if new_status == "completed":
            try:
                item = _table.get_item(Key={"pk": pk, "sk": sk}).get("Item", {})
                payload["result"] = item.get("result")
            except Exception:
                logger.exception("Failed to fetch result for completed task %s", task_id)

        # For failed tasks, include the error message from the stream record
        if new_status == "failed":
            payload["error"] = (
                new_image.get("error_message", {}).get("S", "")
                or "Task failed"
            )

        # Query GSI2 for all subscribed connections
        response = _table.query(
            IndexName="GSI2",
            KeyConditionExpression="gsi2pk = :gpk",
            ExpressionAttributeValues={
                ":gpk": f"SUB#{tenant_id}#{project_id}",
            },
        )
        connections = response.get("Items", [])

        message = json.dumps(payload, default=str)

        for conn in connections:
            connection_id = conn.get("connection_id", "")
            if not connection_id:
                continue
            try:
                apigw.post_to_connection(
                    ConnectionId=connection_id,
                    Data=message.encode("utf-8"),
                )
            except apigw.exceptions.GoneException:
                logger.info("Stale connection %s, cleaning up", connection_id)
                _cleanup_connection(connection_id)
            except Exception:
                logger.exception(
                    "Failed to post to connection %s", connection_id,
                )
