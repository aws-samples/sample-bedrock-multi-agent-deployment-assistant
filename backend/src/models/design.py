"""Design agent data models — topology blueprints, resolved parameters, async tasks.

Follows the principle: schema enforces grammar, KB provides vocabulary.
No hardcoded enums for business values (deployment_pattern, ha_mode, etc.).
The only enum is DesignTaskStatus — a fixed infrastructure lifecycle concept.
"""

import hashlib
import ipaddress
import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Topology Blueprints — structural contracts (shape = code, values = KB)
# ---------------------------------------------------------------------------


class VPCBlueprint(BaseModel):
    """A VPC needed in the design. Shape is code-enforced; values are KB-sourced."""

    role: str = Field(description="VPC purpose — KB-sourced (e.g., 'security', 'inspection', 'spoke')")
    subnet_roles: list[str] = Field(
        min_length=1,
        description="Subnet types per AZ — KB-sourced (e.g., ['public', 'private', 'ha-sync', 'ha-mgmt'])",
    )
    availability_zones: int = Field(ge=1, le=2)


class InterfaceBlueprint(BaseModel):
    """A FortiGate network interface — maps port to subnet role."""

    port_name: str = Field(description="FortiGate port (e.g., 'port1', 'port2')")
    subnet_role: str = Field(description="Which subnet role this port connects to")
    description: str = Field(description="Interface purpose (e.g., 'External/WAN', 'HA heartbeat')")


class FortiGateBlueprint(BaseModel):
    """A FortiGate instance's placement and interface layout."""

    role: str = Field(description="Instance role — KB-sourced (e.g., 'active', 'passive', 'target')")
    vpc_role: str = Field(description="Which VPC (by role) this FortiGate belongs to")
    interfaces: list[InterfaceBlueprint] = Field(min_length=1)


class KBReference(BaseModel):
    """Citation from knowledge base grounding a design decision."""

    source_uri: str = Field(description="S3 URI of the KB document")
    excerpt: str = Field(description="Relevant excerpt (max 500 chars)")
    relevance_score: float = Field(ge=0.0, le=1.0)

    @field_validator("excerpt")
    @classmethod
    def _truncate_excerpt(cls, v: str) -> str:
        return v[:500] if len(v) > 500 else v


# ---------------------------------------------------------------------------
# Design Option — LLM-generated, KB-grounded
# ---------------------------------------------------------------------------


class DesignOption(BaseModel):
    """A single architecture design option.

    Shape is enforced by code. Values are sourced from KB.
    No enum constraints on deployment_pattern, ha_mode — the KB defines what
    patterns exist, and the LLM selects from KB content.
    """

    # --- Human-readable (displayed in UI) ---
    name: str
    description: str
    architecture_summary: str
    pros: list[str] = Field(min_length=2)
    cons: list[str] = Field(min_length=2)
    estimated_monthly_cost_usd: float
    security_posture_rating: int = Field(ge=1, le=5)
    complexity_rating: int = Field(ge=1, le=5)

    # --- Machine-actionable (KB-sourced values, code-enforced shape) ---
    deployment_pattern: str = Field(
        description="KB-sourced pattern name (e.g., 'hub-spoke', 'gwlb-transit', 'ha-dual-az'). "
        "NOT an enum — valid values come from KB architecture docs."
    )
    use_case: str = Field(description="Primary use case (e.g., 'sd-wan', 'inspection')")
    ha_mode: str = Field(
        description="HA configuration (e.g., 'active-passive', 'active-active', 'standalone')"
    )
    fortigate_instance_type: str = Field(
        description="EC2 instance type from KB sizing (e.g., 'c5.xlarge')"
    )
    aws_services: list[str] = Field(description="All AWS services used")

    # --- Topology blueprints (key structural output) ---
    vpc_topology: list[VPCBlueprint] = Field(
        min_length=1,
        description="VPCs needed, with subnet roles per VPC. Drives parameter resolver.",
    )
    fortigate_topology: list[FortiGateBlueprint] = Field(
        min_length=1,
        description="FortiGate instances with interface-to-subnet mappings. Drives IP assignment.",
    )

    # --- Code template match ---
    has_code_template: bool = Field(
        default=False,
        description="True if a matching code template was found in KB/S3",
    )
    template_s3_prefix: str | None = Field(
        default=None,
        description="S3 prefix of the matching code template (e.g., 'sd-wan/hub-spoke/code/')",
    )

    # --- KB grounding (mandatory) ---
    kb_references: list[KBReference] = Field(
        min_length=1,
        description="KB documents that informed this design. At least 1 required.",
    )
    well_architected_assessment: dict[str, str] | None = Field(
        default=None,
        description="Per-pillar WA scores: {'security': 'PASS: ...', 'reliability': 'REVIEW: ...'}",
    )

    @model_validator(mode="after")
    def _validate_interface_subnet_roles(self) -> "DesignOption":
        """Every FortiGate interface subnet_role must exist in some VPC's subnet_roles."""
        all_subnet_roles: set[str] = set()
        for vpc in self.vpc_topology:
            all_subnet_roles.update(vpc.subnet_roles)

        for fgt in self.fortigate_topology:
            for iface in fgt.interfaces:
                if iface.subnet_role not in all_subnet_roles:
                    raise ValueError(
                        f"Interface {iface.port_name} references subnet_role "
                        f"'{iface.subnet_role}' which does not exist in any VPC's "
                        f"subnet_roles. Available: {sorted(all_subnet_roles)}"
                    )
        return self

    @model_validator(mode="after")
    def _validate_template_consistency(self) -> "DesignOption":
        """has_code_template=True requires a non-null template_s3_prefix."""
        if self.has_code_template and not self.template_s3_prefix:
            raise ValueError(
                "has_code_template is True but template_s3_prefix is not set"
            )
        return self


class DesignRecommendation(BaseModel):
    """3 design options with a recommendation."""

    options: list[DesignOption] = Field(min_length=3, max_length=3)
    recommended_option_index: int = Field(ge=0, le=2)
    rationale: str
    requirements_summary: str

    available_templates: list[str] = Field(
        default_factory=list,
        description="S3 prefixes of all code templates found for these use cases",
    )


# ---------------------------------------------------------------------------
# Deployment Parameters — user-provided after design selection
# ---------------------------------------------------------------------------

_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+-\d+){1,2}$")


class DeploymentParameters(BaseModel):
    """Deployment parameters collected from user after design selection.

    Base fields are always required. Pattern-specific fields live in
    additional_parameters — their names and types are determined at runtime
    by the RefinementPlan (generated from KB configuration docs).
    """

    aws_region: str = Field(description="AWS region (e.g., us-east-1)")
    vpc_cidr: str = Field(description="Primary VPC CIDR (e.g., 10.0.0.0/16)")
    environment: str = Field(default="production", description="dev, staging, production")
    project_name: str = Field(description="Project name for resource naming and tagging")

    additional_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Pattern-specific parameters. Keys and types determined by RefinementPlan.",
    )

    @field_validator("vpc_cidr")
    @classmethod
    def _validate_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR notation: {v}") from exc
        return v

    @field_validator("aws_region")
    @classmethod
    def _validate_region(cls, v: str) -> str:
        if not _AWS_REGION_RE.match(v):
            raise ValueError(f"Invalid AWS region format: {v}")
        return v


# ---------------------------------------------------------------------------
# Refinement Plan — LLM-generated (identifies what to collect)
# ---------------------------------------------------------------------------


class RefinementField(BaseModel):
    """A single parameter to collect during refinement."""

    field_name: str = Field(description="Key for DeploymentParameters.additional_parameters")
    label: str = Field(description="Human-readable form label")
    description: str = Field(description="Help text from KB configuration docs")
    required: bool = True
    default_value: str | None = Field(default=None, description="KB-derived default")
    default_rationale: str | None = Field(default=None, description="Why this default (from KB)")
    input_type: str = Field(default="text", description="text, select, cidr, number")
    options: list[str] | None = Field(default=None, description="For 'select' type")
    validation_pattern: str | None = Field(default=None, description="Regex for validation")


class RefinementPlan(BaseModel):
    """Plan for collecting deployment parameters.

    Generated by Haiku + KB analysis. If a code template exists, Haiku also
    reads the template's Parameters section to identify required parameters.
    """

    fields: list[RefinementField]
    kb_configuration_notes: str = Field(description="Configuration guidance summary from KB")
    template_parameters_found: list[str] = Field(
        default_factory=list,
        description="Template parameter names found in the code template",
    )
    kb_references: list[KBReference] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolved IaC Parameters — deterministic output to IaC agent
# ---------------------------------------------------------------------------


class SubnetSpec(BaseModel):
    """A resolved subnet with computed CIDR."""

    name: str
    role: str
    cidr: str
    availability_zone: str


class ResolvedVPC(BaseModel):
    """A fully resolved VPC."""

    name: str
    role: str
    cidr: str
    subnets: list[SubnetSpec]


class ResolvedInterface(BaseModel):
    """A FortiGate interface with assigned IP."""

    port_name: str
    subnet_name: str
    private_ip: str
    description: str
    source_dest_check: bool = False


class ResolvedFortiGate(BaseModel):
    """A fully resolved FortiGate instance."""

    name: str
    role: str
    instance_type: str
    availability_zone: str
    interfaces: list[ResolvedInterface]


class ResolvedIaCParameters(BaseModel):
    """Complete, resolved parameters for IaC generation.

    The IaC agent receives this + optionally the code template from S3.
    If a template exists: parameterize it with these values.
    If no template: generate code from KB architecture docs using these as constraints.
    """

    project_name: str
    environment: str
    region: str
    availability_zones: list[str]

    vpcs: list[ResolvedVPC]
    fortigate_instances: list[ResolvedFortiGate]

    code_template_s3_prefix: str | None = None
    code_template_files: dict[str, str] | None = None

    additional_resolved: dict[str, Any] = Field(
        default_factory=dict,
        description="Pattern-specific resolved values (e.g., tgw_asn, gwlb_cross_zone).",
    )

    tags: dict[str, str] = Field(default_factory=dict)

    design_option_name: str
    deployment_pattern: str
    requirements_hash: str


# ---------------------------------------------------------------------------
# Async Task — tracked in DynamoDB
# ---------------------------------------------------------------------------


class DesignTaskStatus(str, Enum):
    """Task lifecycle status (the ONLY enum — fixed infrastructure concept)."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DesignTask(BaseModel):
    """Async design generation task tracked in DynamoDB.

    DynamoDB key schema:
      PK: TENANT#{tenant_id}
      SK: TASK#{task_id}
    """

    task_id: str
    tenant_id: str
    project_id: str
    task_type: str = "design"
    status: DesignTaskStatus = DesignTaskStatus.QUEUED
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None

    requirements_json: str = Field(description="Serialized InterviewOutput")
    feedback: str | None = None
    previous_options_json: str | None = None

    result: dict | None = None
    error_message: str | None = None

    ttl: int | None = None


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def compute_requirements_hash(requirements_dict: dict) -> str:
    """Compute a stable hash of requirements for traceability."""
    canonical = json.dumps(requirements_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
