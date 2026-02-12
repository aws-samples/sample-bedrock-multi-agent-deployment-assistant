"""Local async worker — background thread + queue.Queue.

Mirrors the Lambda SQS handlers locally so the full async task lifecycle
(QUEUED → PROCESSING → COMPLETED / FAILED) can be exercised without AWS.

Handles both design and IaC tasks, routing by ``task_type`` in the message body.

Started/stopped by the FastAPI lifespan when SQS is not configured.
"""

import logging
import queue
import threading

from src.services.design_processing import mark_task_failed, process_design_task
from src.services.docs_processing import mark_docs_task_failed, process_docs_task
from src.services.iac_processing import mark_iac_task_failed, process_iac_task
from src.services.ws_manager import notify as ws_notify
from src.storage import get_store

logger = logging.getLogger(__name__)

_MAX_QUEUE_DEPTH = 100  # Prevent unbounded growth under load
_queue: queue.Queue[dict | None] = queue.Queue(maxsize=_MAX_QUEUE_DEPTH)
_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def _notify_local(tenant_id: str, project_id: str, task_id: str, status: str) -> None:
    """Notify local WebSocket subscribers after design task completes or fails."""
    if status == "completed":
        store = get_store()
        task = store.get_task(tenant_id, task_id)
        ws_notify(tenant_id, project_id, {
            "type": "design_complete",
            "task_id": task_id,
            "result": task.result if task else None,
        })
    elif status == "failed":
        ws_notify(tenant_id, project_id, {
            "type": "design_failed",
            "task_id": task_id,
            "error": "Design generation failed",
        })


def _notify_iac_local(tenant_id: str, project_id: str, task_id: str, status: str) -> None:
    """Notify local WebSocket subscribers of IaC task status changes."""
    if status == "completed":
        store = get_store()
        task = store.get_iac_task(tenant_id, task_id)
        ws_notify(tenant_id, project_id, {
            "type": "iac_complete",
            "task_id": task_id,
            "result": task.result if task else None,
        })
    elif status == "failed":
        ws_notify(tenant_id, project_id, {
            "type": "iac_failed",
            "task_id": task_id,
            "error": "IaC generation failed",
        })
    elif status in ("validating", "processing"):
        ws_notify(tenant_id, project_id, {
            "type": "iac_status",
            "task_id": task_id,
            "status": status,
        })


def _notify_docs_section_local(
    tenant_id: str, project_id: str, task_id: str, section_name: str, content: str,
) -> None:
    """Notify local WebSocket subscribers when a docs section is ready."""
    ws_notify(tenant_id, project_id, {
        "type": "docs_section",
        "task_id": task_id,
        "section": section_name,
        "content": content,
    })


def _notify_docs_local(tenant_id: str, project_id: str, task_id: str, status: str) -> None:
    """Notify local WebSocket subscribers of docs task status changes."""
    if status == "completed":
        store = get_store()
        task = store.get_docs_task(tenant_id, task_id)
        ws_notify(tenant_id, project_id, {
            "type": "docs_complete",
            "task_id": task_id,
            "result": task.result if task else None,
        })
    elif status == "failed":
        ws_notify(tenant_id, project_id, {
            "type": "docs_failed",
            "task_id": task_id,
            "error": "Documentation generation failed",
        })
    elif status == "processing":
        ws_notify(tenant_id, project_id, {
            "type": "docs_status",
            "task_id": task_id,
            "status": status,
        })


def _worker_loop() -> None:
    """Process tasks from the queue until a sentinel ``None`` is received.

    Wraps the inner loop in an outer crash recovery loop: if the inner
    loop raises an unexpected error (e.g., memory corruption), the worker
    restarts rather than dying silently.
    """
    logger.info("Local worker started")
    while True:
        try:
            body = _queue.get()
            if body is None:
                logger.info("Local worker received shutdown sentinel")
                break
            task_id = body.get("task_id", "?")
            task_type = body.get("task_type", "design")
            try:
                if task_type == "iac":
                    process_iac_task(body, notify_fn=_notify_iac_local)
                elif task_type == "docs":
                    process_docs_task(body, notify_fn=_notify_docs_local,
                                      section_notify_fn=_notify_docs_section_local)
                else:
                    process_design_task(body, notify_fn=_notify_local)
            except Exception:
                logger.exception("Local worker failed on task %s (type=%s)", task_id, task_type)
                if task_type == "iac":
                    mark_iac_task_failed(body, notify_fn=_notify_iac_local)
                elif task_type == "docs":
                    mark_docs_task_failed(body, notify_fn=_notify_docs_local)
                else:
                    mark_task_failed(body, notify_fn=_notify_local)
            finally:
                _queue.task_done()
        except Exception:
            logger.exception("Local worker loop encountered unexpected error — recovering")
    logger.info("Local worker stopped")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(body: dict) -> None:
    """Add a task (design or IaC) to the local processing queue.

    Raises queue.Full if the queue is at max capacity (prevents OOM).
    """
    try:
        _queue.put(body, timeout=5)
    except queue.Full:
        logger.error("Local worker queue full (max=%d), rejecting task %s",
                      _MAX_QUEUE_DEPTH, body.get("task_id", "?"))
        raise
    logger.info(
        "Enqueued %s task %s (queue depth: ~%d)",
        body.get("task_type", "design"),
        body.get("task_id", "?"),
        _queue.qsize(),
    )


def startup() -> None:
    """Start the background worker thread."""
    global _thread
    if _thread is not None and _thread.is_alive():
        logger.warning("Local worker already running")
        return
    _thread = threading.Thread(target=_worker_loop, name="local-worker", daemon=True)
    _thread.start()
    logger.info("Local worker thread started")


def shutdown(timeout: float = 30.0) -> None:
    """Send sentinel and wait for the worker thread to finish."""
    global _thread
    if _thread is None or not _thread.is_alive():
        return
    _queue.put(None)  # sentinel
    _thread.join(timeout=timeout)
    if _thread.is_alive():
        logger.warning("Local worker thread did not stop within %.0fs", timeout)
    _thread = None
