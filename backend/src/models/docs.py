"""Documentation agent data models — output types and async task tracking."""

from enum import Enum

from pydantic import BaseModel, Field


class DocumentationOutput(BaseModel):
    """Final output of the documentation agent.

    Three deliverables:
    - user_guide: Comprehensive deployment guide (Markdown)
    - threat_model: STRIDE threat analysis (Markdown)
    - architecture_diagram: Mermaid architecture-beta diagram code
    """

    user_guide: str = Field(default="", description="Complete user/deployment guide in Markdown")
    threat_model: str = Field(default="", description="STRIDE threat model in Markdown")
    architecture_diagram: str = Field(default="", description="Mermaid architecture-beta diagram code")
    diagram_fix_attempts: int = Field(default=0, description="Number of diagram validation-fix iterations used")
    diagram_validation_passed: bool = Field(default=False, description="Whether the diagram passed Mermaid validation")


VALID_DOC_SECTIONS: set[str] = {"user_guide", "threat_model", "architecture_diagram"}


class DocsTaskStatus(str, Enum):
    """Docs task lifecycle status."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocsTask(BaseModel):
    """Async documentation generation task tracked in DynamoDB.

    DynamoDB key schema:
      PK: TENANT#{tenant_id}
      SK: TASK#{task_id}
    """

    task_id: str
    tenant_id: str
    project_id: str
    task_type: str = "docs"
    status: DocsTaskStatus = DocsTaskStatus.QUEUED
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None

    result: dict | None = None
    error_message: str | None = None

    ttl: int | None = None
