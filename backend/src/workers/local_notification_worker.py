"""Local DynamoDB Streams notification worker.

Polls DynamoDB Streams from Floci and posts WebSocket notifications to the
FastAPI backend's /internal/ws-notify endpoint. Mirrors the production
ws_notification_bridge Lambda handler.

Run as: uv run python -m src.workers.local_notification_worker
"""

import json
import logging
import sys
import time
import urllib.request
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("notification-worker")

BACKEND_URL = "http://localhost:8000"
POLL_INTERVAL_S = 0.5
RESHARDING_CHECK_INTERVAL_S = 30


def _get_stream_arn(client, table_name: str) -> str | None:
    resp = client.describe_table(TableName=table_name)
    stream_arn = resp["Table"].get("LatestStreamArn")
    if not stream_arn:
        logger.error("Table %s does not have streams enabled", table_name)
    return stream_arn


def _get_shard_iterators(streams_client, stream_arn: str) -> dict[str, str]:
    """Get LATEST iterators for all active shards."""
    iterators = {}
    resp = streams_client.describe_stream(StreamArn=stream_arn)
    for shard in resp["StreamDescription"].get("Shards", []):
        shard_id = shard["ShardId"]
        try:
            it_resp = streams_client.get_shard_iterator(
                StreamArn=stream_arn,
                ShardId=shard_id,
                ShardIteratorType="LATEST",
            )
            iterators[shard_id] = it_resp["ShardIterator"]
        except Exception as e:
            logger.warning("Failed to get iterator for shard %s: %s", shard_id, e)
    return iterators


def _determine_message_type(sk: str, status: str) -> str | None:
    """Map SK prefix + status to WebSocket message type."""
    if sk.startswith("IAC_TASK#"):
        domain = "iac"
    elif sk.startswith("DOCS_TASK#"):
        domain = "docs"
    elif sk.startswith("TASK#"):
        domain = "design"
    else:
        return None

    if status == "completed":
        return f"{domain}_complete"
    elif status == "failed":
        return f"{domain}_failed"
    else:
        return f"{domain}_status"


def _extract_task_info(record: dict) -> dict | None:
    """Extract notification payload from a DDB stream record."""
    if record.get("eventName") != "MODIFY":
        return None

    ddb = record.get("dynamodb", {})
    new_image = ddb.get("NewImage", {})
    old_image = ddb.get("OldImage", {})
    keys = ddb.get("Keys", {})

    new_status = new_image.get("status", {}).get("S", "")
    old_status = old_image.get("status", {}).get("S", "")
    if not new_status or new_status == old_status:
        return None

    sk = keys.get("sk", {}).get("S", "")
    msg_type = _determine_message_type(sk, new_status)
    if not msg_type:
        return None

    tenant_id = new_image.get("tenant_id", {}).get("S", "")
    project_id = new_image.get("project_id", {}).get("S", "")
    task_id = new_image.get("task_id", {}).get("S", "")

    if not tenant_id or not project_id:
        return None

    message: dict = {"type": msg_type, "task_id": task_id, "status": new_status}

    if new_status == "failed":
        message["error"] = new_image.get("error_message", {}).get("S", "Task failed")

    return {"tenant_id": tenant_id, "project_id": project_id, "message": message}


def _post_notification(payload: dict) -> None:
    """POST notification to backend's internal endpoint."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND_URL}/internal/ws-notify",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 — localhost only
    except URLError as e:
        logger.debug("Failed to post notification: %s", e)


def main() -> None:
    from src.config.aws import aws_client
    from src.config.settings import settings

    if not settings.aws_endpoint_url:
        logger.error("AI_DEPLOY_AWS_ENDPOINT_URL not set — notification worker requires Floci")
        sys.exit(1)

    table_name = settings.dynamodb_table
    logger.info("Starting notification worker for table: %s", table_name)

    ddb_client = aws_client("dynamodb")
    streams_client = aws_client("dynamodbstreams")

    stream_arn = None
    while not stream_arn:
        stream_arn = _get_stream_arn(ddb_client, table_name)
        if not stream_arn:
            logger.info("Waiting for stream on table %s...", table_name)
            time.sleep(5)

    logger.info("Stream ARN: %s", stream_arn)
    iterators = _get_shard_iterators(streams_client, stream_arn)
    logger.info("Tracking %d shard(s)", len(iterators))

    last_reshard_check = time.time()

    while True:
        try:
            if time.time() - last_reshard_check > RESHARDING_CHECK_INTERVAL_S:
                iterators = _get_shard_iterators(streams_client, stream_arn)
                last_reshard_check = time.time()

            expired_shards = []
            for shard_id, iterator in list(iterators.items()):
                if not iterator:
                    expired_shards.append(shard_id)
                    continue

                try:
                    resp = streams_client.get_records(ShardIterator=iterator, Limit=100)
                except Exception as e:
                    err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                    if err_code in ("ExpiredIteratorException", "TrimmedDataAccessException"):
                        expired_shards.append(shard_id)
                        continue
                    raise

                iterators[shard_id] = resp.get("NextShardIterator")

                for record in resp.get("Records", []):
                    payload = _extract_task_info(record)
                    if payload:
                        logger.info(
                            "Notifying: %s/%s — %s",
                            payload["tenant_id"],
                            payload["project_id"],
                            payload["message"]["type"],
                        )
                        _post_notification(payload)

            for shard_id in expired_shards:
                del iterators[shard_id]

            time.sleep(POLL_INTERVAL_S)

        except KeyboardInterrupt:
            logger.info("Shutting down notification worker")
            break
        except Exception:
            logger.exception("Notification worker error — recovering in 5s")
            time.sleep(5)
            stream_arn = _get_stream_arn(ddb_client, table_name)
            if stream_arn:
                iterators = _get_shard_iterators(streams_client, stream_arn)


if __name__ == "__main__":
    main()
