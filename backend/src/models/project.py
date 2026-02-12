from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ProjectStatus(str, Enum):
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IAC = "iac"
    DOCUMENTATION = "documentation"
    COMPLETE = "complete"


class Project(BaseModel):
    """Project record stored in DynamoDB."""

    tenant_id: str
    project_id: str
    name: str
    mode: str = "wizard"
    status: ProjectStatus = ProjectStatus.REQUIREMENTS
    current_step: str = "requirements"
    use_case: Optional[str] = None
    approved_design_index: Optional[int] = None
    active_design_task_id: Optional[str] = None
    active_iac_task_id: Optional[str] = None
    active_docs_task_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
