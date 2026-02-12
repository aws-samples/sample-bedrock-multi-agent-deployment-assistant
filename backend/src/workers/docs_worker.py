"""SQS Lambda handler for asynchronous documentation generation.

Processes docs tasks from the SQS queue:
1. Parse the SQS message body
2. Delegate to the shared processing pipeline (docs_processing)
3. On failure: mark task as FAILED with error details

WebSocket notifications are handled by the EventBridge Pipe notification
bridge — this worker does not send WS messages directly.
"""

import json
import logging

from src.services.docs_processing import mark_docs_task_failed, process_docs_task

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """SQS Lambda handler for documentation generation."""
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        task_id = body.get("task_id", "?")
        logger.info("Received SQS message for docs task %s", task_id)
        try:
            process_docs_task(body, notify_fn=None)
        except Exception:
            logger.exception("Failed to process docs task %s", task_id)
            try:
                mark_docs_task_failed(body, notify_fn=None)
            except Exception:
                logger.exception(
                    "Failed to mark docs task %s as FAILED — allowing SQS retry", task_id
                )
                raise  # Re-raise so SQS retries (FAILED status not persisted)
