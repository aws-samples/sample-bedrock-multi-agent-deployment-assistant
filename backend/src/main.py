"""AI-Deploy Backend — FastAPI application.

Local dev:  uv run uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler as _slowapi_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.config.auth import get_tenant_id
from src.utils.validation import validate_safe_id
from src.config.circuit_breaker import CircuitOpenError
from src.config.observability import new_correlation_id, setup_logging
from src.config.settings import settings
from src.models.requirements import InterviewOutput
from src.routes.auth import router as auth_router
from src.routes.config import router as config_router
from src.routes.errors import router as errors_router
from src.routes.export import router as export_router
from src.routes.iac import router as iac_router
from src.routes.projects import router as projects_router
from src.storage import get_store

# Initialize structured logging
setup_logging(debug=settings.debug)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def _get_client_ip(request: Request) -> str:
    """Proxy-aware client IP extraction for rate limiting.

    Behind ALB/CloudFront, the real client IP is in X-Forwarded-For.
    Only trusted when settings.trusted_proxy is True (deployed behind a
    known reverse proxy). Without this gate, local dev clients could
    spoof X-Forwarded-For to bypass rate limiting.
    """
    if settings.trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # X-Forwarded-For: client, proxy1, proxy2 — leftmost is the real client
            return forwarded.split(",")[0].strip()
    return get_remote_address(request)


def _get_rate_limit_key(request: Request) -> str:
    """Rate-limit by stable user identity to prevent bypass via token refresh.

    When a JWT Bearer token is present, extracts the 'sub' claim (stable user
    identifier) from the unverified payload for rate limiting. Using 'sub' rather
    than a token hash ensures the same user gets the same bucket regardless of
    token refresh. Full JWT verification still happens in the auth dependency.
    Falls back to client IP for unauthenticated requests.
    """
    import base64
    import json

    ip = _get_client_ip(request)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and len(auth_header) > 50:
        try:
            token = auth_header[7:]
            parts = token.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                sub = payload.get("sub")
                if sub and len(sub) <= 128:
                    return f"user:{sub}"
        except Exception:
            pass
    return ip


limiter = Limiter(key_func=_get_rate_limit_key)


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Custom rate-limit handler that publishes a CloudWatch metric before responding."""
    from src.config.metrics import metrics

    client_ip = _get_client_ip(request)
    metrics.record_rate_limit(request.url.path, client_ip)
    return _slowapi_handler(request, exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start local background worker at startup, stop at shutdown."""
    # Capture the event loop so the worker thread can post WS messages
    from src.services.ws_manager import set_loop as _ws_set_loop
    _ws_set_loop(asyncio.get_running_loop())

    # Start the local async worker when any SQS queue is not configured,
    # OR when running against Floci (endpoint_url set) with SQS queues configured
    _has_all_queues = (
        settings.sqs_design_queue_url
        and settings.sqs_iac_queue_url
        and settings.sqs_docs_queue_url
    )
    _local_worker_active = not _has_all_queues or bool(settings.aws_endpoint_url)
    if _local_worker_active:
        from src.workers.local_worker import startup as start_worker
        start_worker()
    yield
    if _local_worker_active:
        from src.workers.local_worker import shutdown as stop_worker
        stop_worker()
    # Drain pending CloudWatch metric publishes
    from src.config.metrics import metrics
    metrics.shutdown(wait=True)


app = FastAPI(
    title="AI Deploy Assistant",
    description="AI-powered product deployment assistant",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
)

# Validate CORS origins at startup
if not settings.debug:
    for _origin in settings.cors_origins:
        if _origin == "*":
            raise RuntimeError(
                "Wildcard CORS origin ('*') is not allowed in production. "
                "Set AI_DEPLOY_DEBUG=true for local development or configure explicit origins."
            )
        if not _origin.startswith(("http://", "https://")):
            raise RuntimeError(f"Invalid CORS origin (must start with http:// or https://): {_origin}")
elif "*" in settings.cors_origins:
    logger.warning(
        "CORS allow_origins contains '*' — this permits requests from ANY origin. "
        "Restrict to specific origins for production deployments."
    )

# Warn if using DynamoDB without authentication
if not settings.cognito_user_pool_id:
    logger.critical(
        "SECURITY WARNING: Cognito authentication is NOT configured. "
        "Tenant isolation is NOT enforced. "
        "This configuration MUST NOT be used in production."
    )

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(export_router)
app.include_router(config_router)
app.include_router(iac_router)
app.include_router(errors_router)

MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB


@app.middleware("http")
async def request_size_limit_middleware(request: Request, call_next):
    """Reject requests with bodies exceeding the size limit (DoS protection).

    Checks both the Content-Length header (fast path) and the actual body
    size (catches chunked transfer encoding which omits Content-Length).
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid Content-Length header"},
            )
        if length > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
    elif request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add standard security headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'"
    )
    # HSTS only when not in local dev (local uses HTTP)
    if settings.cognito_user_pool_id:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


_PROJECT_ID_RE = re.compile(r"/api/(?:projects|export)/([a-zA-Z0-9_-]+)")
_TASK_ID_RE = re.compile(r"/api/(?:design|iac|docs)/task/([a-zA-Z0-9_-]+)")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Attach correlation ID and log request metrics with audit context."""
    cid = new_correlation_id()
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Correlation-ID"] = cid

    # Audit fields — populated by auth dependencies during endpoint processing
    user = getattr(request.state, "user", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    user_sub = user.sub if user else None

    # Extract project_id / task_id from URL path
    project_id = None
    task_id = None
    m = _PROJECT_ID_RE.search(request.url.path)
    if m:
        project_id = m.group(1)
    t = _TASK_ID_RE.search(request.url.path)
    if t:
        task_id = t.group(1)

    raw_size = request.headers.get("content-length")
    request_size = None
    if raw_size:
        try:
            request_size = int(raw_size)
        except ValueError:
            pass

    logger.info(
        "%s %s %s %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        extra={
            "duration_ms": round(duration_ms),
            "correlation_id": cid,
            "user_sub": user_sub,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "task_id": task_id,
            "request_size": request_size,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/ping")
def ping():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Deep health check — verifies downstream dependencies
# ---------------------------------------------------------------------------

_health_cache: dict = {}
_HEALTH_CACHE_TTL_S = 30
_health_lock: asyncio.Lock | None = None


def _get_health_lock() -> asyncio.Lock:
    """Lazy-init asyncio.Lock (must be created inside a running event loop)."""
    global _health_lock
    if _health_lock is None:
        _health_lock = asyncio.Lock()
    return _health_lock


def _is_cache_valid(now: float) -> bool:
    """Check if health cache is still valid."""
    cached = _health_cache.get("result")
    return bool(cached and _health_cache.get("ts", 0) + _HEALTH_CACHE_TTL_S > now)


def _make_health_response(result: dict) -> JSONResponse:
    """Build a JSONResponse from a health check result dict."""
    code = 200 if result["status"] == "healthy" else 503
    # Only expose dependency details in debug mode to prevent
    # infrastructure fingerprinting by attackers.
    content = result if settings.debug else {"status": result["status"]}
    return JSONResponse(content=content, status_code=code)


async def _run_health_checks() -> dict[str, str]:
    """Execute all downstream dependency checks and return a status dict."""
    checks: dict[str, str] = {}

    # --- DynamoDB / storage ---
    try:
        store = get_store()
        await asyncio.to_thread(store.list_projects, "__healthcheck__")
        checks["storage"] = "ok"
    except Exception as exc:
        checks["storage"] = f"error: {type(exc).__name__}"

    # --- Bedrock ---
    try:
        from src.config.aws import aws_client as _aws_client

        bedrock = _aws_client("bedrock")
        await asyncio.to_thread(
            bedrock.list_foundation_models, byProvider="anthropic"
        )
        checks["bedrock"] = "ok"
    except Exception as exc:
        checks["bedrock"] = f"error: {type(exc).__name__}"

    return checks


@app.get("/health")
async def health():
    """Dependency-aware health check. Returns 503 when any downstream is unreachable.

    Results are cached for 30s to avoid hammering dependencies.
    Uses an asyncio.Lock with double-check-locking to prevent thundering herd.
    """
    now = time.time()
    if _is_cache_valid(now):
        return _make_health_response(_health_cache["result"])

    async with _get_health_lock():
        # Re-check after acquiring lock — another request may have filled cache.
        now = time.time()
        if _is_cache_valid(now):
            return _make_health_response(_health_cache["result"])

        checks = await _run_health_checks()
        overall = "healthy" if all(
            v.startswith("ok") for v in checks.values()
        ) else "degraded"
        detailed_result = {"status": overall, "checks": checks}
        _health_cache.update(ts=now, result=detailed_result)

    return _make_health_response(detailed_result)


def _handle_circuit_open_error(exc: CircuitOpenError):
    """Convert CircuitOpenError to HTTP 503 with Retry-After header."""
    retry_after = int(exc.retry_after) if exc.retry_after else 30
    raise HTTPException(
        status_code=503,
        detail=f"AI service temporarily unavailable. Retry in {retry_after}s.",
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# REST API — interview chat (hybrid: form + AI refinement)
# ---------------------------------------------------------------------------


class InterviewChatRequest(BaseModel):
    message: str
    requirements: Optional[dict] = None  # Seed data dict (first turn only)
    populated_fields: Optional[dict] = None  # Accumulated gathered fields (every turn)
    use_case: Optional[str] = None  # Use case for model-driven interview
    project_id: str = "default"


@app.post("/api/interview/chat")
@limiter.limit("10/minute")
async def interview_chat(
    request: Request,
    req: InterviewChatRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Stream interview agent response via SSE with session persistence."""
    from src.services.interview import interview_chat_stream

    return StreamingResponse(
        interview_chat_stream(
            message=req.message,
            tenant_id=tenant_id,
            project_id=req.project_id,
            requirements=req.requirements,
            populated_fields=req.populated_fields,
            use_case=req.use_case,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# REST API — per-step wizard endpoints
# ---------------------------------------------------------------------------


class DesignSubmitRequest(BaseModel):
    requirements: InterviewOutput
    project_id: str = "default"
    feedback: str | None = Field(default=None, max_length=5000)
    previous_options: list[dict] | None = Field(default=None, max_length=10)


@app.post("/api/design/submit")
@limiter.limit("5/minute")
async def submit_design(
    request: Request,
    req: DesignSubmitRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Submit a design generation task (async via SQS or sync for local dev)."""
    from src.models.design import DesignOption
    from src.services.design import submit_design_task

    from src.utils.validation import sanitize_text as _sanitize

    previous = None
    if req.previous_options:
        previous = [DesignOption.model_validate(opt) for opt in req.previous_options]

    sanitized_feedback = _sanitize(req.feedback, "feedback") if req.feedback else None

    try:
        return await asyncio.to_thread(
            submit_design_task,
            requirements=req.requirements,
            project_id=req.project_id,
            tenant_id=tenant_id,
            feedback=sanitized_feedback,
            previous_options=previous,
        )
    except CircuitOpenError as exc:
        _handle_circuit_open_error(exc)
    except Exception:
        logger.exception("Design submission failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/design/task/{task_id}")
@limiter.limit("30/minute")
async def get_design_task_status(
    request: Request,
    task_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Poll the status of an async design generation task."""
    from src.services.design import get_design_task

    try:
        result = await asyncio.to_thread(get_design_task, tenant_id, task_id)
        if "error" in result and result.get("status") is None:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Design task status check failed")
        raise HTTPException(status_code=500, detail="Internal server error")


class DesignSelectRequest(BaseModel):
    project_id: str = "default"
    option_index: int = Field(ge=0, le=2)


@app.post("/api/design/select")
@limiter.limit("10/minute")
async def select_design_option(
    request: Request,
    req: DesignSelectRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Select a design option and get a refinement plan for deployment parameters."""
    from src.services.design import select_design

    try:
        return await asyncio.to_thread(
            select_design,
            tenant_id=tenant_id,
            project_id=req.project_id,
            option_index=req.option_index,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Design selection failed")
        raise HTTPException(status_code=500, detail="Internal server error")


class DesignRefineRequest(BaseModel):
    project_id: str = "default"
    aws_region: str
    vpc_cidr: str
    environment: str = "production"
    project_name: str
    additional_parameters: dict = Field(default_factory=dict)


@app.post("/api/design/refine")
@limiter.limit("10/minute")
async def refine_design_endpoint(
    request: Request,
    req: DesignRefineRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Submit deployment parameters and resolve to IaC-ready values."""
    from src.models.design import DeploymentParameters
    from src.services.design import refine_design
    from src.utils.validation import sanitize_requirements as _sanitize_dict

    sanitized_params = _sanitize_dict(req.additional_parameters) if req.additional_parameters else {}

    try:
        params = DeploymentParameters(
            aws_region=req.aws_region,
            vpc_cidr=req.vpc_cidr,
            environment=req.environment,
            project_name=req.project_name,
            additional_parameters=sanitized_params,
        )
        return await asyncio.to_thread(
            refine_design,
            tenant_id=tenant_id,
            project_id=req.project_id,
            deployment_params=params,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Design refinement failed")
        raise HTTPException(status_code=500, detail="Internal server error")


class DocsSubmitRequest(BaseModel):
    project_id: str = "default"


@app.post("/api/docs/submit")
@limiter.limit("5/minute")
async def submit_docs(
    request: Request,
    req: DocsSubmitRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Submit async documentation generation task.

    The worker loads design, requirements, and IaC output from the project's
    stored state — no need to send them in the request body.
    """
    from src.services.docs import submit_docs_task

    try:
        task = submit_docs_task(tenant_id=tenant_id, project_id=req.project_id)
        return {"task_id": task.task_id, "status": task.status.value}
    except Exception:
        logger.exception("Failed to submit docs task")
        raise HTTPException(status_code=500, detail="Failed to submit documentation task")


@app.get("/api/docs/task/{task_id}")
@limiter.limit("30/minute")
async def get_docs_task(
    request: Request,
    task_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Poll documentation task status."""
    from src.storage import get_store

    store = get_store()
    task = store.get_docs_task(tenant_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = {
        "task_id": task.task_id,
        "status": task.status if isinstance(task.status, str) else task.status.value,
        "submitted_at": task.submitted_at,
    }
    if task.result:
        result["result"] = task.result
    if task.error_message:
        result["error"] = task.error_message
    return result


class DocsRegenerateSectionRequest(BaseModel):
    project_id: str = "default"
    section: str = Field(description="Section to regenerate: user_guide or architecture_diagram")


@app.post("/api/docs/regenerate-section")
@limiter.limit("5/minute")
async def regenerate_docs_section(
    request: Request,
    req: DocsRegenerateSectionRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Regenerate a single documentation section synchronously.

    Loads project state, re-runs the section generator, updates stored docs,
    and returns the new content directly.
    """
    import json as _json

    from src.agents.documentation import regenerate_section
    from src.models.docs import VALID_DOC_SECTIONS

    if req.section not in VALID_DOC_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid section: {req.section}. Must be one of {sorted(VALID_DOC_SECTIONS)}",
        )

    store = get_store()

    # Load project state (same pattern as docs_processing.py)
    design_data = store.load_step(tenant_id, req.project_id, "design")
    requirements_data = store.load_step(tenant_id, req.project_id, "requirements")
    iac_data = store.load_step(tenant_id, req.project_id, "iac")

    if not design_data or not requirements_data:
        raise HTTPException(status_code=400, detail="Missing required project state for regeneration")

    approved_index = design_data.get("recommended_option_index", 0)
    options = design_data.get("options", [])
    design = options[approved_index] if options and approved_index < len(options) else design_data
    requirements_json = _json.dumps(requirements_data, indent=2)

    cft_template = ""
    if iac_data:
        files = iac_data.get("files", {})
        cft_template = files.get("template.yaml", "") or files.get("template.json", "")

    try:
        content = await regenerate_section(
            section_name=req.section,
            design=design,
            requirements_json=requirements_json,
            cft_template=cft_template,
            tenant_id=tenant_id,
            project_id=req.project_id,
        )
    except CircuitOpenError:
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry in a few moments.",
        )
    except Exception:
        logger.exception("Failed to regenerate docs section %s", req.section)
        raise HTTPException(status_code=500, detail="Failed to regenerate documentation section")

    # Update stored docs with the new section content
    existing_docs = store.load_step(tenant_id, req.project_id, "docs") or {}
    existing_docs[req.section] = content
    store.save_step(tenant_id, req.project_id, "docs", existing_docs, advance=False)

    return {"section": req.section, "content": content}


# ---------------------------------------------------------------------------
# Internal endpoint — local notification worker posts WS messages here
# ---------------------------------------------------------------------------

if settings.debug:
    @app.post("/internal/ws-notify")
    async def internal_ws_notify(body: dict):
        """Accept notification from local DDB stream worker and broadcast via WebSocket."""
        from src.services import ws_manager
        ws_manager.notify(body["tenant_id"], body["project_id"], body["message"])
        return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket endpoint — local dev mirror of API Gateway WS protocol
# ---------------------------------------------------------------------------


def _authenticate_ws_token(token: str | None) -> str:
    """Validate WS connection token and return tenant_id.

    When Cognito is configured, verifies the JWT and extracts tenant_id.
    When not configured (local dev), returns "default".
    """
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        return "default"

    if not token:
        raise ValueError("Token required when authentication is enabled")

    from src.config.auth import _decode_jwt_payload, _lookup_tenant_id
    payload = _decode_jwt_payload(token)
    tenant_id = payload.get("custom:tenant_id")
    if not tenant_id and settings.aws_endpoint_url:
        tenant_id = _lookup_tenant_id(payload.get("username") or payload.get("cognito:username", ""))
    return tenant_id or "default"


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Local WebSocket endpoint matching the API Gateway subscribe/notify protocol.

    When Cognito is configured, requires a valid JWT token as a query parameter
    and enforces that subscriptions match the token's tenant_id.
    """
    from src.services import ws_manager

    token = ws.query_params.get("token")
    try:
        authenticated_tenant = _authenticate_ws_token(token)
    except (ValueError, Exception) as e:
        logger.warning("WS connection rejected: %s", e)
        await ws.close(code=4001, reason="Authentication failed")
        return

    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            if data.get("action") == "subscribe":
                tenant_id = data.get("tenant_id", "default")
                project_id = data.get("project_id")
                if not project_id:
                    continue
                try:
                    validate_safe_id(tenant_id, "tenant_id")
                    validate_safe_id(project_id, "project_id")
                except ValueError:
                    logger.warning("WS subscribe rejected: invalid tenant_id or project_id")
                    continue
                # Enforce tenant isolation: subscriptions must match authenticated tenant
                if settings.cognito_user_pool_id and tenant_id != authenticated_tenant:
                    logger.warning(
                        "WS subscribe rejected: tenant mismatch (token=%s, requested=%s)",
                        authenticated_tenant, tenant_id,
                    )
                    continue
                ws_manager.subscribe(ws, tenant_id, project_id)
    except WebSocketDisconnect:
        ws_manager.unsubscribe(ws)
    except Exception:
        ws_manager.unsubscribe(ws)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
