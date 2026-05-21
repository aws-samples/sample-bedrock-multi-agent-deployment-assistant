"""Frontend error reporting endpoint."""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/errors", tags=["errors"])


class ClientErrorReport(BaseModel):
    message: str = Field(..., max_length=2000)
    stack: Optional[str] = Field(default=None, max_length=10000)
    page: Optional[str] = Field(default=None, max_length=500)
    user_agent: Optional[str] = Field(default=None, max_length=500)
    timestamp: Optional[str] = Field(default=None, max_length=50)


@router.post("", status_code=204)
async def report_client_error(report: ClientErrorReport, request: Request):
    """Receive frontend error reports and log them as structured entries."""
    logger.error(
        "client_error",
        extra={
            "error_message": report.message,
            "error_stack": report.stack,
            "page": report.page,
            "user_agent": report.user_agent or request.headers.get("user-agent", ""),
            "client_timestamp": report.timestamp,
            "client_ip": request.client.host if request.client else None,
        },
    )
