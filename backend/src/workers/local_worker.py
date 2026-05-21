"""Local async worker — background thread + queue.Queue or SQS polling.

Mirrors the Lambda SQS handlers locally so the full async task lifecycle
(QUEUED → PROCESSING → COMPLETED / FAILED) can be exercised without AWS.

Handles both design and IaC tasks, routing by ``task_type`` in the message body.

Two modes:
- In-memory queue (default): when SQS URLs are not configured
- SQS polling (Floci mode): when aws_endpoint_url AND SQS URLs are set

Started/stopped by the FastAPI lifespan.
"""

import json
import logging
import queue
import threading
import time

from src.config.settings import settings
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


_MAX_CONSECUTIVE_CRASHES = 10


def _worker_loop() -> None:
    """Process tasks from the queue until a sentinel ``None`` is received.

    Wraps the inner loop in an outer crash recovery loop: if the inner
    loop raises an unexpected error (e.g., memory corruption), the worker
    restarts with exponential backoff rather than spinning at full CPU.
    """
    logger.info("Local worker started")
    consecutive_crashes = 0
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
                consecutive_crashes = 0
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
            consecutive_crashes += 1
            if consecutive_crashes >= _MAX_CONSECUTIVE_CRASHES:
                logger.critical(
                    "Local worker exceeded %d consecutive crashes, stopping",
                    _MAX_CONSECUTIVE_CRASHES,
                )
                break
            backoff = min(2 ** consecutive_crashes, 30)
            logger.exception(
                "Local worker recovering (crash %d/%d), backing off %ds",
                consecutive_crashes, _MAX_CONSECUTIVE_CRASHES, backoff,
            )
            time.sleep(backoff)
    logger.info("Local worker stopped")


# ---------------------------------------------------------------------------
# SQS polling loop (Floci mode)
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


def _sqs_poll_loop() -> None:
    """Poll SQS queues for tasks (used when running against Floci)."""
    from src.config.aws import aws_client

    sqs = aws_client("sqs")
    queue_urls = [
        url for url in [
            settings.sqs_design_queue_url,
            settings.sqs_iac_queue_url,
            settings.sqs_docs_queue_url,
        ] if url
    ]

    logger.info("SQS poll worker started (queues: %d)", len(queue_urls))

    while not _shutdown_event.is_set():
        received_any = False
        for queue_url in queue_urls:
            try:
                resp = sqs.receive_message(
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=1,
                )
                messages = resp.get("Messages", [])
                for msg in messages:
                    received_any = True
                    body = json.loads(msg["Body"])
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
                        logger.exception("SQS worker failed on task %s (type=%s)", task_id, task_type)
                        if task_type == "iac":
                            mark_iac_task_failed(body, notify_fn=_notify_iac_local)
                        elif task_type == "docs":
                            mark_docs_task_failed(body, notify_fn=_notify_docs_local)
                        else:
                            mark_task_failed(body, notify_fn=_notify_local)
                    finally:
                        sqs.delete_message(
                            QueueUrl=queue_url,
                            ReceiptHandle=msg["ReceiptHandle"],
                        )
            except Exception:
                logger.exception("SQS poll error on %s", queue_url)
                time.sleep(2)

        if not received_any:
            time.sleep(0.5)

    logger.info("SQS poll worker stopped")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _use_sqs_mode() -> bool:
    """Determine if the worker should poll SQS (Floci mode)."""
    return bool(
        settings.aws_endpoint_url
        and settings.sqs_design_queue_url
        and settings.sqs_iac_queue_url
        and settings.sqs_docs_queue_url
    )


def enqueue(body: dict) -> None:
    """Add a task to the processing queue (in-memory or SQS).

    Raises queue.Full if in-memory queue is at max capacity (prevents OOM).
    """
    if _use_sqs_mode():
        from src.config.aws import aws_client

        sqs = aws_client("sqs")
        task_type = body.get("task_type", "design")
        queue_url = {
            "design": settings.sqs_design_queue_url,
            "iac": settings.sqs_iac_queue_url,
            "docs": settings.sqs_docs_queue_url,
        }.get(task_type, settings.sqs_design_queue_url)
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(body),
            MessageGroupId=body.get("project_id", "default"),
        )
        logger.info("Enqueued %s task %s to SQS", task_type, body.get("task_id", "?"))
        return

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
    """Start the background worker thread (in-memory or SQS mode)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        logger.warning("Local worker already running")
        return

    _shutdown_event.clear()
    target = _sqs_poll_loop if _use_sqs_mode() else _worker_loop
    mode = "SQS" if _use_sqs_mode() else "in-memory"
    _thread = threading.Thread(target=target, name="local-worker", daemon=True)
    _thread.start()
    logger.info("Local worker thread started (mode: %s)", mode)


def shutdown(timeout: float = 30.0) -> None:
    """Send sentinel and wait for the worker thread to finish."""
    global _thread
    if _thread is None or not _thread.is_alive():
        return
    _shutdown_event.set()
    _queue.put(None)  # sentinel for in-memory mode
    _thread.join(timeout=timeout)
    if _thread.is_alive():
        logger.warning("Local worker thread did not stop within %.0fs", timeout)
    _thread = None
