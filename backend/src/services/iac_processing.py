"""Shared IaC processing pipeline.

Extracted as a shared module to eliminate duplication between Lambda handler
and local worker — same pattern as design_processing.py.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from src.models.design import ResolvedIaCParameters
from src.models.iac import IaCTaskStatus
from src.storage import get_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core processing pipeline
# ---------------------------------------------------------------------------


def process_iac_task(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Process a single IaC generation task.

    Shared pipeline used by both the Lambda SQS handler and the local worker.

    Args:
        body: Task message body with task_id, tenant_id, project_id.
        notify_fn: Optional callback(tenant_id, project_id, task_id, status)
                   for post-completion notifications (e.g. WebSocket).
    """
    import asyncio

    from src.agents.iac import generate_iac

    task_id = body["task_id"]
    tenant_id = body["tenant_id"]
    project_id = body["project_id"]
    feedback = body.get("feedback")

    store = get_store()

    # --- Mark task as PROCESSING ---
    logger.info("Processing IaC task %s", task_id)
    store.update_iac_task(tenant_id, task_id, {
        "status": IaCTaskStatus.PROCESSING.value,
        "started_at": datetime.now(UTC).isoformat(),
    })

    # --- Load resolved parameters from stored design step ---
    design_data = store.load_step(tenant_id, project_id, "design")
    if not design_data or "resolved_parameters" not in design_data:
        raise ValueError(f"No resolved parameters found for project {project_id}")

    params = ResolvedIaCParameters.model_validate(design_data["resolved_parameters"])

    # --- Load previous IaC validation context for regeneration ---
    previous_validation_summary: str | None = None
    if feedback:
        prev_iac = store.load_step(tenant_id, project_id, "iac")
        if prev_iac and "validation_report" in prev_iac:
            vr = prev_iac["validation_report"]
            findings = vr.get("findings", [])
            error_ids = [f"{f.get('rule_id', '?')}: {f.get('message', '')}" for f in findings if f.get("severity") == "error"]
            warn_ids = [f"{f.get('rule_id', '?')}: {f.get('message', '')}" for f in findings if f.get("severity") == "warning"]
            parts = [f"Passed: {vr.get('passed', False)}", f"Fix attempts: {vr.get('fix_attempts', 0)}"]
            if error_ids:
                parts.append(f"Errors: {'; '.join(error_ids[:10])}")
            if warn_ids:
                parts.append(f"Warnings: {'; '.join(warn_ids[:10])}")
            previous_validation_summary = ", ".join(parts)

    # --- Mark as VALIDATING ---
    store.update_iac_task(tenant_id, task_id, {
        "status": IaCTaskStatus.VALIDATING.value,
    })

    if notify_fn:
        notify_fn(tenant_id, project_id, task_id, "validating")

    # --- Run async IaC generation pipeline ---
    loop = asyncio.new_event_loop()
    try:
        output = loop.run_until_complete(
            generate_iac(
                params, tenant_id, project_id,
                feedback=feedback,
                previous_validation_summary=previous_validation_summary,
            )
        )
    finally:
        loop.close()

    # --- Save result ---
    now = datetime.now(UTC).isoformat()
    result_dict = output.model_dump()

    store.update_iac_task(tenant_id, task_id, {
        "status": IaCTaskStatus.COMPLETED.value,
        "completed_at": now,
        "result": result_dict,
        "template_resolution_path": output.template_resolution_path,
        "validation_attempts": output.validation_report.fix_attempts,
    })

    # Save IaC output as the iac step — advance=False so the user stays on
    # the "iac" step to review the template before explicitly proceeding to docs.
    store.save_step(tenant_id, project_id, "iac", result_dict, advance=False)

    # Persist individual template files as standalone S3 artifacts
    if output.files:
        try:
            from src.tools.save_artifact import persist_artifacts
            persist_artifacts(tenant_id, project_id, output.files, content_type="application/x-yaml")
        except Exception:
            logger.debug("Artifact persistence failed (non-critical)", exc_info=True)

    # Clear active IaC task pointer
    project = store.get_project(tenant_id, project_id)
    if project:
        project.active_iac_task_id = None
        store.update_project(project)

    logger.info("IaC task %s completed (path=%s, attempts=%d)",
                task_id, output.template_resolution_path, output.validation_report.fix_attempts)

    if notify_fn:
        notify_fn(tenant_id, project_id, task_id, "completed")


def mark_iac_task_failed(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
    error_message: str = "IaC generation failed — see logs for details",
) -> None:
    """Mark an IaC task as FAILED. Best-effort — does not raise."""
    try:
        task_id = body.get("task_id")
        tenant_id = body.get("tenant_id")
        if not task_id or not tenant_id:
            logger.error("Cannot mark IaC task failed: missing task_id or tenant_id")
            return

        store = get_store()
        store.update_iac_task(tenant_id, task_id, {
            "status": IaCTaskStatus.FAILED.value,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_message": error_message,
        })

        project_id = body.get("project_id")
        if project_id:
            project = store.get_project(tenant_id, project_id)
            if project:
                project.active_iac_task_id = None
                store.update_project(project)

        if project_id and notify_fn:
            notify_fn(tenant_id, project_id, task_id, "failed")
    except Exception:
        logger.exception("Failed to mark IaC task %s as failed", body.get("task_id"))
