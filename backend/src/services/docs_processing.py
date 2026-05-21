"""Shared documentation processing pipeline.

Used by both the Lambda SQS handler and the local async worker.
Follows the same pattern as design_processing.py and iac_processing.py.

generate_documentation() is now async, so this pipeline runs it via
asyncio.new_event_loop() when called from synchronous context (local worker,
Lambda handler).
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from src.models.docs import DocsTaskStatus
from src.storage import get_store

logger = logging.getLogger(__name__)


def process_docs_task(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
    section_notify_fn: Callable[[str, str, str, str, str], None] | None = None,
) -> None:
    """Process a single documentation generation task.

    Loads design, requirements, and IaC output from the project's stored state,
    then generates all 3 documentation deliverables (User Guide, STRIDE Threat
    Model, Architecture Diagram).

    Args:
        body: Task message body with task_id, tenant_id, project_id.
        notify_fn: Optional callback(tenant_id, project_id, task_id, status)
                   for post-completion notifications (e.g. WebSocket).
        section_notify_fn: Optional callback(tenant_id, project_id, task_id,
                           section_name, content) for progressive rendering.
    """
    from src.agents.documentation import generate_documentation

    task_id = body["task_id"]
    tenant_id = body["tenant_id"]
    project_id = body["project_id"]

    store = get_store()

    # --- Mark task as PROCESSING ---
    logger.info("Processing docs task %s", task_id)
    store.update_docs_task(tenant_id, task_id, {
        "status": DocsTaskStatus.PROCESSING.value,
        "started_at": datetime.now(UTC).isoformat(),
    })

    if notify_fn:
        notify_fn(tenant_id, project_id, task_id, "processing")

    # --- Load project state ---
    design_data = store.load_step(tenant_id, project_id, "design")
    requirements_data = store.load_step(tenant_id, project_id, "requirements")
    iac_data = store.load_step(tenant_id, project_id, "iac")

    if not design_data or not requirements_data:
        raise ValueError(
            f"Missing required project state for docs generation "
            f"(design={bool(design_data)}, requirements={bool(requirements_data)})"
        )

    # Extract the approved design option
    approved_index = design_data.get("recommended_option_index", 0)
    options = design_data.get("options", [])
    design = options[approved_index] if options and approved_index < len(options) else design_data

    requirements_json = json.dumps(requirements_data, indent=2)

    # Extract CFT template from IaC output
    cft_template = ""
    if iac_data:
        files = iac_data.get("files", {})
        cft_template = files.get("template.yaml", "") or files.get("template.json", "")

    # --- Generate documentation with progressive section notifications ---
    def _on_section_complete(section_name: str, content: str) -> None:
        """Called when each docs section finishes — emit WS notification with content."""
        if section_notify_fn:
            section_notify_fn(tenant_id, project_id, task_id, section_name, content)

    start = time.perf_counter()

    # generate_documentation() is async — run via event loop from sync context
    loop = asyncio.new_event_loop()
    try:
        output = loop.run_until_complete(
            generate_documentation(
                design=design,
                requirements_json=requirements_json,
                cft_template=cft_template,
                tenant_id=tenant_id,
                project_id=project_id,
                on_section_complete=_on_section_complete,
            )
        )
    finally:
        loop.close()

    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("Docs generation completed for task %s in %.0fms", task_id, duration_ms)

    # --- Save result ---
    now = datetime.now(UTC).isoformat()
    result_dict = output.model_dump()

    store.update_docs_task(tenant_id, task_id, {
        "status": DocsTaskStatus.COMPLETED.value,
        "completed_at": now,
        "result": result_dict,
    })

    store.save_step(tenant_id, project_id, "docs", result_dict, advance=True)

    # Persist docs as standalone S3 artifacts for direct access
    try:
        from src.tools.save_artifact import persist_artifacts
        doc_files = {}
        if output.user_guide:
            doc_files["docs/user_guide.md"] = output.user_guide
        if output.architecture_diagram:
            doc_files["docs/architecture_diagram.md"] = output.architecture_diagram
        if doc_files:
            persist_artifacts(tenant_id, project_id, doc_files, content_type="text/markdown")
    except Exception:
        logger.debug("Artifact persistence failed (non-critical)", exc_info=True)

    # Clear active task tracker on the project
    project = store.get_project(tenant_id, project_id)
    if project:
        project.active_docs_task_id = None
        store.update_project(project)

    logger.info("Docs task %s completed successfully", task_id)

    # --- Notify subscribers ---
    if notify_fn:
        notify_fn(tenant_id, project_id, task_id, "completed")


def mark_docs_task_failed(
    body: dict,
    notify_fn: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Mark a docs task as FAILED in the store. Best-effort — does not raise."""
    try:
        task_id = body.get("task_id")
        tenant_id = body.get("tenant_id")
        if not task_id or not tenant_id:
            logger.error("Cannot mark docs task failed: missing task_id or tenant_id")
            return

        store = get_store()
        store.update_docs_task(tenant_id, task_id, {
            "status": DocsTaskStatus.FAILED.value,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_message": "Documentation generation failed — see logs for details",
        })

        # Clear active task tracker
        project_id = body.get("project_id")
        if project_id:
            project = store.get_project(tenant_id, project_id)
            if project:
                project.active_docs_task_id = None
                store.update_project(project)

        if project_id and notify_fn:
            notify_fn(tenant_id, project_id, task_id, "failed")
    except Exception:
        logger.exception("Failed to mark docs task %s as failed", body.get("task_id"))
