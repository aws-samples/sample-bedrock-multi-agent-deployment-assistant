"""WebSocket heartbeat Lambda — scheduled every 5 minutes.

Pings all active WebSocket connections via the API Gateway Management API.
Stale connections (GoneException) are cleaned up from DynamoDB along with
their subscription items.

Publishes CloudWatch metrics:
    - WsActiveConnections: number of healthy connections
    - WsStaleConnectionsCleaned: number of stale connections removed
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Module-level initialization (reused across Lambda warm starts)
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(os.environ["DYNAMODB_TABLE"])
_apigw = None
_cw = None


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


def _get_cw():
    """Lazy-init CloudWatch client."""
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch")
    return _cw


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
    except Exception:
        logger.exception("Failed to clean up connection %s", connection_id)


def handler(event, context):
    """Heartbeat: ping all WS connections, clean up stale ones."""
    apigw = _get_apigw()
    if not apigw:
        logger.warning("WEBSOCKET_CALLBACK_URL not configured, skipping")
        return

    active = 0
    stale = 0

    # Scan for all CONNECTION items
    scan_kwargs = {
        "FilterExpression": "sk = :sk AND begins_with(pk, :prefix)",
        "ExpressionAttributeValues": {":sk": "CONNECTION", ":prefix": "WS#"},
        "ProjectionExpression": "pk, connection_id",
    }

    done = False
    while not done:
        response = _table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            connection_id = item.get("connection_id", "")
            if not connection_id:
                continue
            try:
                apigw.post_to_connection(
                    ConnectionId=connection_id,
                    Data=b'{"type":"heartbeat"}',
                )
                active += 1
            except apigw.exceptions.GoneException:
                logger.info("Stale connection %s", connection_id)
                _cleanup_connection(connection_id)
                stale += 1
            except Exception:
                logger.exception("Heartbeat failed for %s", connection_id)

        if response.get("LastEvaluatedKey"):
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        else:
            done = True

    logger.info("Heartbeat complete: active=%d stale=%d", active, stale)

    # Publish CloudWatch metrics
    cw = _get_cw()
    cw.put_metric_data(
        Namespace="AI-LCM",
        MetricData=[
            {
                "MetricName": "WsActiveConnections",
                "Value": active,
                "Unit": "Count",
            },
            {
                "MetricName": "WsStaleConnectionsCleaned",
                "Value": stale,
                "Unit": "Count",
            },
        ],
    )
