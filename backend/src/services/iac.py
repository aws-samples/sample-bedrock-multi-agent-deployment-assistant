"""IaC service — orchestrates async IaC generation.

Supports two modes:
- **SQS async (production)**: Submits to SQS → Lambda processes → DynamoDB stores result
- **Local async (local dev)**: Enqueues to background thread worker when SQS is not configured
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import boto3

from src.config.settings import settings
from src.models.iac import IaCTask, IaCTaskStatus
from src.storage import get_store
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)


def submit_iac_task(
    project_id: str,
    tenant_id: str = "default",
    feedback: str | None = None,
) -> dict:
    """Submit an IaC generation task.

    Loads ResolvedIaCParameters from the stored design step — the worker
    doesn't need them in the SQS message, it loads them from the store.

    Preconditions:
    - Design step must have resolved_parameters
    - No active IaC task already running for this project
    """
    validate_safe_id(project_id, "project_id")
    store = get_store()

    # Verify resolved parameters exist
    design_data = store.load_step(tenant_id, project_id, "design")
    if not design_data or "resolved_parameters" not in design_data:
        raise ValueError("Design must have resolved parameters before IaC generation")

    # Check for active IaC task
    project = store.get_project(tenant_id, project_id)
    if project and getattr(project, "active_iac_task_id", None):
        existing = store.get_iac_task(tenant_id, project.active_iac_task_id)
        if existing and existing.status in (IaCTaskStatus.QUEUED, IaCTaskStatus.PROCESSING, IaCTaskStatus.VALIDATING):
            raise ValueError(f"IaC task {existing.task_id} is already active for this project")

    task_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    ttl_epoch = int((datetime.now(UTC) + timedelta(days=7)).timestamp())

    task = IaCTask(
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        submitted_at=now,
        ttl=ttl_epoch,
        feedback=feedback,
    )
    store.create_iac_task(tenant_id, task)

    # Track active task on project
    if project:
        project.active_iac_task_id = task_id
        store.update_project(project)

    body = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_type": "iac",
        "feedback": feedback,
    }

    if settings.sqs_iac_queue_url:
        return _submit_sqs(body)
    return _submit_local_async(body)


def _submit_sqs(body: dict) -> dict:
    """Submit IaC task to SQS for Lambda processing."""
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    sqs.send_message(
        QueueUrl=settings.sqs_iac_queue_url,
        MessageBody=json.dumps(body),
        MessageGroupId=f"{body['tenant_id']}#{body['project_id']}",
    )
    logger.info("IaC task %s submitted to SQS", body["task_id"])
    return {"task_id": body["task_id"], "status": "queued"}


def _submit_local_async(body: dict) -> dict:
    """Enqueue IaC task to the local background worker thread."""
    from src.workers.local_worker import enqueue

    enqueue(body)
    logger.info("IaC task %s enqueued to local worker", body["task_id"])
    return {"task_id": body["task_id"], "status": "queued"}


def get_iac_task(tenant_id: str, task_id: str) -> dict:
    """Retrieve the current state of an IaC task."""
    validate_safe_id(task_id, "task_id")
    store = get_store()
    task = store.get_iac_task(tenant_id, task_id)
    if not task:
        return {"error": "Task not found"}

    response: dict = {
        "task_id": task.task_id,
        "status": task.status.value,
        "submitted_at": task.submitted_at,
    }
    if task.status == IaCTaskStatus.COMPLETED and task.result:
        response["result"] = task.result
    elif task.status == IaCTaskStatus.FAILED:
        response["error"] = task.error_message or "Unknown error"
    return response
