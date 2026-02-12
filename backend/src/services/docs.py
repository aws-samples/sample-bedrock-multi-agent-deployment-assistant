"""Documentation generation services — async task submission.

Follows the same async pattern as design.py and iac.py:
submit task → SQS queue or local worker → process → DynamoDB + WebSocket.
"""

import logging
import uuid
from datetime import UTC, datetime

from src.config.settings import settings
from src.models.docs import DocsTask, DocsTaskStatus
from src.storage import get_store

logger = logging.getLogger(__name__)


def submit_docs_task(
    tenant_id: str,
    project_id: str,
) -> DocsTask:
    """Submit a documentation generation task.

    Creates a DocsTask in DynamoDB with QUEUED status, then dispatches to
    either SQS (production) or the local background worker (dev).

    The worker loads design, requirements, and IaC output from the project's
    stored state — no need to send them in the task message.
    """
    now = datetime.now(UTC).isoformat()
    task_id = f"docs-{project_id}-{uuid.uuid4().hex[:8]}"

    task = DocsTask(
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        task_type="docs",
        status=DocsTaskStatus.QUEUED,
        submitted_at=now,
    )

    store = get_store()
    store.create_docs_task(tenant_id, task)

    # Track active task on the project for hydration
    project = store.get_project(tenant_id, project_id)
    if project:
        project.active_docs_task_id = task_id
        store.update_project(project)

    body = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_type": "docs",
    }

    # Dispatch to SQS (dedicated docs queue) or local worker
    if settings.sqs_docs_queue_url:
        _submit_to_sqs(body)
    else:
        _enqueue_local(body)

    return task


def _submit_to_sqs(body: dict) -> None:
    """Submit docs task to the dedicated docs SQS queue for Lambda processing."""
    import boto3
    import json

    sqs = boto3.client("sqs", region_name=settings.aws_region)
    sqs.send_message(
        QueueUrl=settings.sqs_docs_queue_url,
        MessageBody=json.dumps(body),
        MessageGroupId=f"{body['tenant_id']}#{body['project_id']}",
    )
    logger.info("Docs task %s submitted to SQS", body["task_id"])


def _enqueue_local(body: dict) -> None:
    """Enqueue docs task to the local background worker."""
    from src.workers.local_worker import enqueue

    enqueue(body)
    logger.info("Docs task %s enqueued to local worker", body["task_id"])
