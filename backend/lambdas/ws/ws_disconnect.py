"""WebSocket $disconnect handler.

Removes the WebSocket connection and all its subscriptions from DynamoDB.
API Gateway invokes this Lambda when a client closes the connection.
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Handle WebSocket $disconnect route."""
    connection_id = event["requestContext"]["connectionId"]
    logger.info("WebSocket disconnect: %s", connection_id)

    try:
        table = boto3.resource("dynamodb").Table(os.environ["DYNAMODB_TABLE"])

        # Query all items for this connection (CONNECTION + SUB#... entries)
        response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"WS#{connection_id}"},
        )
        items = response.get("Items", [])

        # Paginate if there are more results
        while response.get("LastEvaluatedKey"):
            response = table.query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": f"WS#{connection_id}"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        # Batch delete all connection and subscription items
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            logger.info(
                "Deleted %d item(s) for connection %s", len(items), connection_id,
            )

        return {"statusCode": 200}
    except Exception:
        logger.exception("WebSocket disconnect failed for %s", connection_id)
        return {"statusCode": 200}
