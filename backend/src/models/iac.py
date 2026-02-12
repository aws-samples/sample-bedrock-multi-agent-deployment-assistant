"""IaC generation models — task lifecycle and output schema."""

from enum import Enum

from pydantic import BaseModel, Field


class IaCTaskStatus(str, Enum):
    """Task lifecycle status for IaC generation."""

    QUEUED = "queued"
    PROCESSING = "processing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


class IaCTask(BaseModel):
    """Async IaC generation task tracked in DynamoDB.

    DynamoDB key schema:
      PK: TENANT#{tenant_id}
      SK: IAC_TASK#{task_id}
    """

    task_id: str
    tenant_id: str
    project_id: str
    task_type: str = "iac"
    status: IaCTaskStatus = IaCTaskStatus.QUEUED
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None

    template_resolution_path: str | None = None  # "parameterize" | "compose" | "generate"
    validation_attempts: int = 0
    feedback: str | None = None      # User feedback for regeneration

    result: dict | None = None       # IaCOutput on completion
    error_message: str | None = None  # On failure
    ttl: int | None = None           # 7-day auto-expiry


class ValidationFinding(BaseModel):
    """A single validation finding from any layer."""

    layer: str           # "structural" | "cfn-lint" | "checkov" | "cfn-guard"
    severity: str        # "error" | "warning" | "info"
    rule_id: str
    message: str
    resource: str | None = None
    line: int | None = None
    file: str | None = None


class ValidationReport(BaseModel):
    """Structured validation results from all layers.

    Blocking layers (structural, cfn-lint) produce errors that must be fixed
    before the template is accepted. Non-blocking layers (checkov, cfn-guard)
    produce findings that are reported but do not gate output.
    """

    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    fix_attempts: int = 0
    layers_executed: list[str] = Field(default_factory=list)

    _BLOCKING_LAYERS: frozenset[str] = frozenset({"structural", "cfn-lint"})

    def has_blocking_errors(self) -> bool:
        """Only structural + cfn-lint errors block template acceptance."""
        return any(
            f.severity == "error" and f.layer in self._BLOCKING_LAYERS
            for f in self.findings
        )

    def blocking_error_count(self) -> int:
        """Count errors from blocking layers only."""
        return sum(
            1 for f in self.findings
            if f.severity == "error" and f.layer in self._BLOCKING_LAYERS
        )

    def blocking_findings(self) -> list[ValidationFinding]:
        """Return only findings from blocking layers with error severity."""
        return [
            f for f in self.findings
            if f.severity == "error" and f.layer in self._BLOCKING_LAYERS
        ]

    def non_blocking_findings(self) -> list[ValidationFinding]:
        """Return findings from non-blocking layers (checkov, cfn-guard)."""
        return [
            f for f in self.findings
            if f.layer not in self._BLOCKING_LAYERS
        ]

    def error_count(self) -> int:
        """Total error count across all layers."""
        return sum(1 for f in self.findings if f.severity == "error")


class IaCOutput(BaseModel):
    """Final IaC generation output."""

    files: dict[str, str]            # {"template.yaml": "...", "fortigate_rules.guard": "..."}
    validation_report: ValidationReport
    template_resolution_path: str
    generation_duration_ms: int


class IaCSubmitRequest(BaseModel):
    """Request to submit IaC generation task."""

    project_id: str
    session_id: str = ""
    feedback: str | None = Field(default=None, max_length=5000)


class IaCTaskResponse(BaseModel):
    """Response from task submission or status poll."""

    task_id: str
    status: str
    submitted_at: str | None = None
    result: IaCOutput | None = None
    error: str | None = None
