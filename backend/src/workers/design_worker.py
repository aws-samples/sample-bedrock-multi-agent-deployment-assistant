"""SQS Lambda handler for asynchronous design generation.

Processes design tasks from the SQS queue:
1. Parse the SQS message body
2. Delegate to the shared processing pipeline (design_processing)
3. On failure: mark task as FAILED with error details

WebSocket notifications are handled by the EventBridge Pipe notification
bridge — this worker no longer sends WS messages directly.
"""

import json
import logging

from src.services.design_processing import mark_task_failed, process_design_task

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """SQS Lambda handler for design generation."""
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        task_id = body.get("task_id", "?")
        logger.info("Received SQS message for task %s", task_id)
        try:
            process_design_task(body, notify_fn=None)
        except Exception:
            logger.exception("Failed to process design task %s", task_id)
            try:
                mark_task_failed(body, notify_fn=None)
            except Exception:
                logger.exception(
                    "Failed to mark task %s as FAILED — allowing SQS retry", task_id
                )
                raise  # Re-raise so SQS retries (FAILED status not persisted)
