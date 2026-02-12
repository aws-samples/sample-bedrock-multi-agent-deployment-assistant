"""Design service — orchestrates async design generation, selection, and refinement.

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
from src.models.design import (
    DeploymentParameters,
    DesignOption,
    DesignRecommendation,
    DesignTask,
    DesignTaskStatus,
)
from src.models.requirements import InterviewOutput
from src.services.parameter_resolver import ParameterResolver
from src.services.refinement import generate_refinement_plan
from src.storage import get_store
from src.utils.validation import sanitize_requirements, validate_safe_id

logger = logging.getLogger(__name__)

_resolver = ParameterResolver()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_and_rebuild_requirements(requirements: InterviewOutput) -> InterviewOutput:
    sanitized = sanitize_requirements(requirements.model_dump())
    return InterviewOutput(**sanitized)


# ---------------------------------------------------------------------------
# Submit design task (SQS async or local async)
# ---------------------------------------------------------------------------


def _create_task_and_body(
    task_id: str, tenant_id: str, project_id: str, task_type: str, now: str,
    requirements: InterviewOutput,
    feedback: str | None,
    previous_options: list[DesignOption] | None,
) -> dict:
    """Create a DesignTask in the store and return the message body dict.

    Shared by both _submit_sqs and _submit_local_async.
    """
    store = get_store()

    ttl_epoch = int((datetime.now(UTC) + timedelta(days=7)).timestamp())
    task = DesignTask(
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        task_type=task_type,
        submitted_at=now,
        requirements_json=requirements.model_dump_json(),
        feedback=feedback,
        previous_options_json=(
            json.dumps([opt.model_dump() for opt in previous_options])
            if previous_options else None
        ),
        ttl=ttl_epoch,
    )
    store.create_task(tenant_id, task)

    # Track the active task on the project so the frontend can detect
    # in-progress generation when the user navigates back.
    project = store.get_project(tenant_id, project_id)
    if project:
        project.active_design_task_id = task_id
        store.update_project(project)

    return {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_type": task_type,
        "requirements": requirements.model_dump(),
        "feedback": feedback,
        "previous_options": (
            [opt.model_dump() for opt in previous_options]
            if previous_options else None
        ),
    }


def submit_design_task(
    requirements: InterviewOutput,
    project_id: str,
    tenant_id: str = "default",
    feedback: str | None = None,
    previous_options: list[DesignOption] | None = None,
) -> dict:
    """Submit a design generation task.

    If SQS is configured, submits to SQS for Lambda processing.
    Otherwise enqueues to the local background worker thread.
    Both paths produce the same task lifecycle: QUEUED → PROCESSING → COMPLETED/FAILED.
    """
    validate_safe_id(project_id, "project_id")
    requirements = _sanitize_and_rebuild_requirements(requirements)

    task_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    task_type = "redesign" if feedback else "design"

    if settings.sqs_design_queue_url:
        return _submit_sqs(
            task_id, tenant_id, project_id, task_type, now,
            requirements, feedback, previous_options,
        )
    return _submit_local_async(
        task_id, tenant_id, project_id, task_type, now,
        requirements, feedback, previous_options,
    )


def _submit_sqs(
    task_id: str, tenant_id: str, project_id: str, task_type: str, now: str,
    requirements: InterviewOutput,
    feedback: str | None,
    previous_options: list[DesignOption] | None,
) -> dict:
    """Submit design task to SQS for Lambda processing."""
    body = _create_task_and_body(
        task_id, tenant_id, project_id, task_type, now,
        requirements, feedback, previous_options,
    )

    sqs = boto3.client("sqs", region_name=settings.aws_region)
    sqs.send_message(
        QueueUrl=settings.sqs_design_queue_url,
        MessageBody=json.dumps(body),
        MessageGroupId=f"{tenant_id}#{project_id}",
    )

    logger.info("Design task %s submitted to SQS (type=%s)", task_id, task_type)
    return {"task_id": task_id, "status": "queued"}


def _submit_local_async(
    task_id: str, tenant_id: str, project_id: str, task_type: str, now: str,
    requirements: InterviewOutput,
    feedback: str | None,
    previous_options: list[DesignOption] | None,
) -> dict:
    """Enqueue design task to the local background worker thread."""
    from src.workers.local_worker import enqueue

    body = _create_task_and_body(
        task_id, tenant_id, project_id, task_type, now,
        requirements, feedback, previous_options,
    )
    enqueue(body)

    logger.info("Design task %s enqueued to local worker (type=%s)", task_id, task_type)
    return {"task_id": task_id, "status": "queued"}


# ---------------------------------------------------------------------------
# Poll task status
# ---------------------------------------------------------------------------


def get_design_task(tenant_id: str, task_id: str) -> dict:
    """Retrieve the current state of a design task."""
    validate_safe_id(task_id, "task_id")
    store = get_store()
    task = store.get_task(tenant_id, task_id)
    if not task:
        return {"error": "Task not found"}

    response: dict = {
        "task_id": task.task_id,
        "status": task.status.value,
        "submitted_at": task.submitted_at,
    }
    if task.status == DesignTaskStatus.COMPLETED and task.result:
        response["result"] = task.result
    elif task.status == DesignTaskStatus.FAILED:
        response["error"] = task.error_message or "Unknown error"
    return response


# ---------------------------------------------------------------------------
# Select design + get refinement plan
# ---------------------------------------------------------------------------


def select_design(
    tenant_id: str,
    project_id: str,
    option_index: int,
) -> dict:
    """Select a design option and generate a refinement plan for parameter collection."""
    validate_safe_id(project_id, "project_id")
    store = get_store()

    design_data = store.load_step(tenant_id, project_id, "design")
    if not design_data:
        raise ValueError("No design found for this project")

    recommendation = DesignRecommendation.model_validate(design_data)
    if option_index < 0 or option_index >= len(recommendation.options):
        raise ValueError(f"Invalid option_index: {option_index}")

    selected = recommendation.options[option_index]

    req_data = store.load_step(tenant_id, project_id, "requirements")
    if not req_data:
        raise ValueError("No requirements found for this project")
    requirements = InterviewOutput.model_validate(req_data)

    refinement = generate_refinement_plan(selected, requirements, tenant_id=tenant_id)

    project = store.get_project(tenant_id, project_id)
    if project:
        project.approved_design_index = option_index
        store.update_project(project)

    return {
        "selected_option": selected.model_dump(),
        "refinement_plan": refinement.model_dump(),
    }


# ---------------------------------------------------------------------------
# Refine (collect deployment params) → resolve IaC parameters
# ---------------------------------------------------------------------------


def refine_design(
    tenant_id: str,
    project_id: str,
    deployment_params: DeploymentParameters,
) -> dict:
    """Apply deployment parameters and resolve to IaC-ready values."""
    validate_safe_id(project_id, "project_id")
    store = get_store()

    design_data = store.load_step(tenant_id, project_id, "design")
    if not design_data:
        raise ValueError("No design found for this project")

    recommendation = DesignRecommendation.model_validate(design_data)

    project = store.get_project(tenant_id, project_id)
    if not project or project.approved_design_index is None:
        raise ValueError("No design option has been selected")

    selected = recommendation.options[project.approved_design_index]

    req_data = store.load_step(tenant_id, project_id, "requirements")
    if not req_data:
        raise ValueError("No requirements found for this project")
    requirements = InterviewOutput.model_validate(req_data)

    resolved = _resolver.resolve(selected, deployment_params, requirements)

    design_data["resolved_parameters"] = resolved.model_dump()
    design_data["deployment_parameters"] = deployment_params.model_dump()
    store.save_step(tenant_id, project_id, "design", design_data)

    return {"resolved_parameters": resolved.model_dump()}
