"""IaC API routes — async task submission and polling."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.config.auth import get_tenant_id
from src.config.circuit_breaker import CircuitOpenError
from src.models.iac import IaCSubmitRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/iac", tags=["iac"])


def _handle_circuit_open_error(exc: CircuitOpenError):
    """Convert CircuitOpenError to HTTP 503 with Retry-After header."""
    retry_after = int(exc.retry_after) if exc.retry_after else 30
    raise HTTPException(
        status_code=503,
        detail=f"AI service temporarily unavailable. Retry in {retry_after}s.",
        headers={"Retry-After": str(retry_after)},
    )


@router.post("/submit", status_code=202)
async def submit_iac(
    request: Request,
    req: IaCSubmitRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Submit an IaC generation task (async via SQS or local worker).

    Returns HTTP 202 with task_id for polling.
    Preconditions:
    - Project design must have resolved_parameters
    - No active IaC task already running for this project
    """
    from src.services.iac import submit_iac_task

    try:
        return await asyncio.to_thread(
            submit_iac_task,
            project_id=req.project_id,
            tenant_id=tenant_id,
            feedback=req.feedback,
        )
    except CircuitOpenError as exc:
        _handle_circuit_open_error(exc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("IaC submission failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/task/{task_id}")
async def get_iac_task_status(
    request: Request,
    task_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Poll the status of an async IaC generation task.

    Used as fallback when WebSocket is unavailable.
    """
    from src.services.iac import get_iac_task

    try:
        result = await asyncio.to_thread(get_iac_task, tenant_id, task_id)
        if "error" in result and result.get("status") is None:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("IaC task status check failed")
        raise HTTPException(status_code=500, detail="Internal server error")
