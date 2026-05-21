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
from src.storage.protocol import ActiveTaskConflictError

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

    # Atomically claim the active task slot (prevents TOCTOU race)
    try:
        store.claim_active_task(tenant_id, project_id, "active_docs_task_id", task_id)
    except ActiveTaskConflictError:
        store.update_docs_task(tenant_id, task_id, {
            "status": DocsTaskStatus.FAILED.value,
            "error_message": "Another docs task was claimed concurrently",
        })
        raise ValueError("Another docs task is already active for this project")

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
    import json

    from src.config.aws import aws_client

    task_id = body["task_id"]
    tenant_id = body["tenant_id"]
    project_id = body["project_id"]

    try:
        sqs = aws_client("sqs")
        sqs.send_message(
            QueueUrl=settings.sqs_docs_queue_url,
            MessageBody=json.dumps(body),
            MessageGroupId=f"{tenant_id}#{project_id}",
        )
    except Exception as e:
        logger.error("SQS send failed for docs task %s: %s", task_id, e)
        store = get_store()
        store.update_docs_task(tenant_id, task_id, {
            "status": DocsTaskStatus.FAILED.value,
            "error_message": f"Queue delivery failed: {e}",
        })
        project = store.get_project(tenant_id, project_id)
        if project:
            project.active_docs_task_id = None
            store.update_project(project)
        raise

    logger.info("Docs task %s submitted to SQS", task_id)


def _enqueue_local(body: dict) -> None:
    """Enqueue docs task to the local background worker."""
    from src.workers.local_worker import enqueue

    enqueue(body)
    logger.info("Docs task %s enqueued to local worker", body["task_id"])
