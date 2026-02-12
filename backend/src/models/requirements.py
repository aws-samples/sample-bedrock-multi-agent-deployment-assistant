import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UseCases(str, Enum):
    SD_WAN = "sd-wan"
    EGRESS = "egress"
    INGRESS = "ingress"
    INSPECTION = "inspection"
    NOTKNOWN = "notknown"


class RoutingProtocol(str, Enum):
    BGP = "bgp"
    STATIC_ROUTE = "static-route"
    NOTKNOWN = "notknown"


class WorkloadResilience(str, Enum):
    NONE = "none"
    HA_SINGLE_REGION_SINGLE_ZONE = "ha-single-region-single-zone"
    HA_SINGLE_REGION_DUAL_ZONE = "ha-single-region-dual-zone"
    HA_DUAL_REGION_SINGLE_ZONE = "ha-dual-region-single-zone"
    HA_DUAL_REGION_DUAL_ZONE = "ha-dual-region-dual-zone"
    NOTKNOWN = "notknown"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class UserInformation(BaseModel):
    name: str = Field("", description="User's full name")
    experience_on_cloud: str = Field(
        "",
        description="User's cloud experience level: beginner, intermediate, or advanced",
    )


class PerformanceRequirements(BaseModel):
    minLatency: Optional[float] = Field(
        None, description="Minimum acceptable latency in milliseconds"
    )
    maxLatency: Optional[float] = Field(
        None, description="Maximum acceptable latency in milliseconds"
    )
    minJitter: Optional[float] = Field(
        None, description="Minimum acceptable jitter in milliseconds"
    )
    maxJitter: Optional[float] = Field(
        None, description="Maximum acceptable jitter in milliseconds"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, v: Any) -> Any:
        """Parse a free-text performance string into structured fields.

        The LLM sometimes returns e.g. 'latency < 50ms, jitter < 10ms' instead
        of a dict.  This validator extracts numeric values using pattern matching
        so validation succeeds rather than crashing.
        """
        if not isinstance(v, str):
            return v
        data: dict[str, float] = {}
        _patterns = [
            (r"latency\s*<\s*(\d+(?:\.\d+)?)\s*ms", "maxLatency"),
            (r"latency\s*>\s*(\d+(?:\.\d+)?)\s*ms", "minLatency"),
            (r"jitter\s*<\s*(\d+(?:\.\d+)?)\s*ms", "maxJitter"),
            (r"jitter\s*>\s*(\d+(?:\.\d+)?)\s*ms", "minJitter"),
        ]
        for pattern, key in _patterns:
            m = re.search(pattern, v, re.IGNORECASE)
            if m:
                data[key] = float(m.group(1))
        return data


# ---------------------------------------------------------------------------
# Use-case-specific models
# ---------------------------------------------------------------------------


class SDWAN(BaseModel):
    role: str = Field(
        "",
        description="SD-WAN role: hub, spoke, or hub-and-spoke. "
        "See https://docs.fortinet.com/document/fortigate/latest/sd-wan-architecture",
    )
    number_of_branches: int = Field(
        0, description="Total number of SD-WAN branch sites to connect"
    )
    overlay_strategy: Optional[str] = Field(
        "",
        description="When Dual Hub overlay tunnel strategy: ipsec, gre, or vxlan. "
        "See https://docs.fortinet.com/document/fortigate/latest/sd-wan-overlay",
    )
    performance: Optional[PerformanceRequirements] = Field(
        None,
        description="Performance SLA requirements (latency, jitter thresholds) for SD-WAN links",
    )


class Inspection(BaseModel):
    number_public_ips: int = Field(
        0, description="Number of public IPs required for the inspection deployment"
    )
    security_features: list[str] = Field(
        default_factory=list,
        description="Security features to enable (e.g. IPS, antivirus, web-filter, "
        "application-control, ssl-inspection)",
    )


# ---------------------------------------------------------------------------
# Main requirements output
# ---------------------------------------------------------------------------


class InterviewOutput(BaseModel):
    """Structured output from the Interview Agent."""

    model_config = ConfigDict(extra="allow")

    use_cases: list[UseCases] = Field(default_factory=list)
    cloud_routing_protocol: RoutingProtocol = RoutingProtocol.NOTKNOWN
    resilience: WorkloadResilience = WorkloadResilience.NOTKNOWN
    bandwidth: float = 0.0  # Mbps
    user_info: Optional[UserInformation] = None
    compliance: list[str] = Field(default_factory=list)
    solution_description: str = ""

    # Generic use-case detail payloads, keyed by use-case value (e.g. "sd-wan")
    use_case_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Use-case registry — single source of truth for adding new use cases
# ---------------------------------------------------------------------------


@dataclass
class UseCaseSpec:
    model: type[BaseModel]
    required_fields: set[str]   # blocking — must be populated to proceed to design
    label: str
    optional_fields: set[str] = None  # try to collect, but accept a user decline
    # Maps field name → KB document_type for targeted Level-2 KB searches.
    # Fields not listed default to "configuration".
    field_doc_types: dict[str, str] = None

    def __post_init__(self) -> None:
        if self.optional_fields is None:
            self.optional_fields = set()
        if self.field_doc_types is None:
            self.field_doc_types = {}

    @property
    def all_fields(self) -> set[str]:
        return self.required_fields | (self.optional_fields or set())


USE_CASE_REGISTRY: dict[UseCases, UseCaseSpec] = {
    UseCases.SD_WAN: UseCaseSpec(
        model=SDWAN,
        required_fields={"role", "number_of_branches"},
        optional_fields={"overlay_strategy", "performance"},
        label="SD-WAN",
        field_doc_types={
            "role": "architecture",
            "number_of_branches": "sizing",
            "overlay_strategy": "configuration",
            "performance": "sizing",
        },
    ),
    UseCases.INSPECTION: UseCaseSpec(
        model=Inspection,
        required_fields={"number_public_ips"},
        optional_fields={"security_features"},
        label="Inspection",
        field_doc_types={
            "number_public_ips": "sizing",
            "security_features": "components",
        },
    ),
}


def get_model_for_use_case(uc: UseCases) -> type[BaseModel] | None:
    """Return the use-case model class, or None if no specific model exists."""
    spec = USE_CASE_REGISTRY.get(uc)
    return spec.model if spec else None


# ---------------------------------------------------------------------------
# Base required fields for InterviewProgress completion tracking
# ---------------------------------------------------------------------------

# Fields that MUST be present before the design agent can run.
# These block complete=True if missing.
_BLOCKING_BASE_FIELDS = {
    "use_cases",
    "cloud_routing_protocol",
    "resilience",
    "bandwidth",
    "solution_description",
}

# Fields the interview should try to collect, but a user decline is acceptable.
# These do NOT block complete=True.
_SOFT_BASE_FIELDS = {"user_info", "compliance"}

# Union — all base fields the interview cares about
_BASE_REQUIRED_FIELDS = _BLOCKING_BASE_FIELDS | _SOFT_BASE_FIELDS

# Fields provided by the seed form — never asked about during the interview
_SEED_FIELDS = {"use_cases", "bandwidth", "solution_description"}

# Base fields the interview must gather (everything except seed fields)
_BASE_INTERVIEW_FIELDS = _BASE_REQUIRED_FIELDS - _SEED_FIELDS


def _is_empty(val: Any) -> bool:
    """Return True if a field value should be treated as not-yet-gathered."""
    if val is None or val == "" or val == [] or val == 0 or val == 0.0:
        return True
    if isinstance(val, str) and val == "notknown":
        return True
    return False


def _is_optional_annotation(annotation: Any) -> bool:
    """Return True if annotation is Optional[X] (i.e. Union[X, None] or X | None)."""
    origin = get_origin(annotation)
    if origin is Union:
        return type(None) in get_args(annotation)
    return False


def _get_basemodel_class(annotation: Any) -> type[BaseModel] | None:
    """Extract the BaseModel subclass from an annotation, handling Optional[X]."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is Union:
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _empty_required_subfields(
    model_cls: type[BaseModel], data: dict, prefix: str,
) -> list[str]:
    """Return dotted names for non-optional fields on a sub-model that are still empty."""
    missing: list[str] = []
    for fname, finfo in model_cls.model_fields.items():
        if _is_optional_annotation(finfo.annotation):
            continue
        val = data.get(fname)
        if _is_empty(val):
            missing.append(f"{prefix}.{fname}")
    return missing


def _empty_required_subfields_instance(
    instance: BaseModel, prefix: str,
) -> list[str]:
    """Same as _empty_required_subfields but reads from a model instance."""
    missing: list[str] = []
    for fname, finfo in type(instance).model_fields.items():
        if _is_optional_annotation(finfo.annotation):
            continue
        val = getattr(instance, fname)
        if _is_empty(val):
            missing.append(f"{prefix}.{fname}")
    return missing


def _add_schema_entry(
    missing: dict[str, Any], key: str, schema_info: dict, is_optional: bool
) -> None:
    """Add a schema entry to the missing fields dict, marking it optional if needed."""
    entry = dict(schema_info)
    if is_optional:
        entry["x-optional"] = True
    missing[key] = entry


def get_missing_fields_schema(use_cases: list[UseCases], populated: dict) -> dict:
    """Return JSON schema for ALL fields still missing — both base and use-case-specific.

    Fields from _SOFT_BASE_FIELDS or UseCaseSpec.optional_fields are tagged with
    "x-optional": true so the agent knows it can accept a user decline and move on.
    Blocking fields lack this tag — the agent must keep asking until answered.
    """
    missing: dict[str, Any] = {}

    output_schema = InterviewOutput.model_json_schema()
    output_props = output_schema.get("properties", {})

    # --- base interview fields ---
    for field_name in _BASE_INTERVIEW_FIELDS:
        is_soft = field_name in _SOFT_BASE_FIELDS
        finfo = InterviewOutput.model_fields.get(field_name)
        sub_cls = _get_basemodel_class(finfo.annotation) if finfo else None

        if sub_cls:
            val = populated.get(field_name)
            sub_data = val if isinstance(val, dict) else {}
            sub_schema = sub_cls.model_json_schema().get("properties", {})
            for sub_name, sub_info in sub_schema.items():
                sub_finfo = sub_cls.model_fields.get(sub_name)
                if sub_finfo and _is_optional_annotation(sub_finfo.annotation):
                    continue
                if _is_empty(sub_data.get(sub_name)):
                    _add_schema_entry(missing, f"{field_name}.{sub_name}", sub_info, is_soft)
        else:
            val = populated.get(field_name)
            if _is_empty(val) and field_name in output_props:
                _add_schema_entry(missing, field_name, output_props[field_name], is_soft)

    # --- use-case-specific fields ---
    uc_details = populated.get("use_case_details", {}) or {}

    for uc in use_cases:
        spec = USE_CASE_REGISTRY.get(uc)
        if not spec:
            continue
        uc_key = uc.value
        uc_data = uc_details.get(uc_key, {})
        if not isinstance(uc_data, dict):
            uc_data = {}

        full_schema = spec.model.model_json_schema()
        properties = full_schema.get("properties", {})

        for prop_name, prop_info in properties.items():
            if prop_name not in spec.all_fields:
                continue
            is_soft = prop_name in spec.optional_fields
            val = uc_data.get(prop_name)
            if _is_empty(val):
                _add_schema_entry(missing, f"{uc_key}.{prop_name}", prop_info, is_soft)
            elif isinstance(val, dict):
                sub_finfo = spec.model.model_fields.get(prop_name)
                sub_cls = _get_basemodel_class(sub_finfo.annotation) if sub_finfo else None
                if sub_cls:
                    sub_schema = sub_cls.model_json_schema().get("properties", {})
                    for sub_name, sub_info in sub_schema.items():
                        inner_finfo = sub_cls.model_fields.get(sub_name)
                        if inner_finfo and _is_optional_annotation(inner_finfo.annotation):
                            continue
                        if _is_empty(val.get(sub_name)):
                            _add_schema_entry(
                                missing, f"{uc_key}.{prop_name}.{sub_name}", sub_info, is_soft
                            )

    return {"type": "object", "properties": missing}


def get_seed_context_block(use_cases: list[UseCases], seed_data: dict) -> str:
    """Build a [SEED_CONTEXT] block injected into the agent's system prompt.

    This gives the LLM everything it needs to form a targeted KB search query
    on turn 1 and decide which fields are auto-determinable without user input.
    The LLM — informed by KB results — is responsible for populating those
    fields immediately rather than asking the user about them.
    """
    uc_labels = [uc.value for uc in use_cases if uc != UseCases.NOTKNOWN]
    lines = [
        "[SEED_CONTEXT]",
        f"  use_cases: {', '.join(uc_labels) or 'unknown'}",
    ]
    for key in ("bandwidth", "solution_description"):
        val = seed_data.get(key)
        if val:
            lines.append(f"  {key}: {val}")
    lines.append("[/SEED_CONTEXT]")
    return "\n".join(lines)


def get_field_doc_type(field_path: str, use_cases: list[UseCases]) -> str:
    """Return the KB document_type for a field path, derived from the registry.

    Base fields have hardcoded mappings; use-case fields come from
    UseCaseSpec.field_doc_types. Falls back to 'configuration'.
    """
    _BASE_DOC_TYPES: dict[str, str | None] = {
        "cloud_routing_protocol": "configuration",
        "resilience": "architecture",
        "compliance": "best-practices",
        "user_info": None,  # no KB needed
        "user_info.name": None,
        "user_info.experience_on_cloud": None,
    }
    if field_path in _BASE_DOC_TYPES:
        return _BASE_DOC_TYPES[field_path] or "configuration"

    # Use-case-specific: "sd-wan.role" → spec for SD_WAN, field "role"
    parts = field_path.split(".", 1)
    if len(parts) == 2:
        uc_key, field_name = parts
        for uc in use_cases:
            if uc.value == uc_key:
                spec = USE_CASE_REGISTRY.get(uc)
                if spec and field_name in spec.field_doc_types:
                    return spec.field_doc_types[field_name]
                break

    return "configuration"


def get_use_case_config() -> list[dict]:
    """Return use-case metadata for the config endpoint."""
    return [
        {
            "value": uc.value,
            "label": spec.label if (spec := USE_CASE_REGISTRY.get(uc)) else uc.value.replace("-", " ").title(),
            "available": uc in USE_CASE_REGISTRY,
            "extra_fields": len(spec.required_fields) if spec else 0,
        }
        for uc in UseCases
        if uc != UseCases.NOTKNOWN
    ]


def _sanitize_uc_data(model_cls: type[BaseModel], uc_data: dict) -> dict:
    """Coerce field values to match expected sub-model types, dropping only on failure.

    The LLM occasionally returns free-text (e.g. 'latency < 50ms') for fields
    typed as nested Pydantic models (e.g. PerformanceRequirements).  When this
    happens, this helper tries model_validate() first — which triggers any
    coercion validators on the sub-model (e.g. PerformanceRequirements parses
    the string).  The value is only dropped if coercion itself raises.
    """
    sanitized: dict[str, Any] = {}
    for k, v in uc_data.items():
        finfo = model_cls.model_fields.get(k)
        if not finfo:
            continue
        sub_cls = _get_basemodel_class(finfo.annotation)
        if sub_cls and not isinstance(v, (dict, sub_cls)):
            try:
                sanitized[k] = sub_cls.model_validate(v)
            except Exception:
                logger.warning(
                    "Could not coerce %s.%s (%s %r) — using field default",
                    model_cls.__name__, k, type(v).__name__, v,
                )
            continue
        sanitized[k] = v
    return sanitized


class InterviewProgress(BaseModel):
    """Structured output from each interview turn."""

    response_message: str = Field(
        description="Conversational response to the user. Ask exactly ONE targeted question about the next missing field."
    )

    # Progressive requirement extraction (all Optional -- filled as conversation progresses)
    use_cases: Optional[list[UseCases]] = Field(None)
    cloud_routing_protocol: Optional[RoutingProtocol] = Field(None)
    resilience: Optional[WorkloadResilience] = Field(None)
    bandwidth: Optional[float] = Field(None, description="Bandwidth in Mbps")
    user_info: Optional[UserInformation] = Field(None)
    compliance: Optional[list[str]] = Field(None)
    solution_description: Optional[str] = Field(None)

    # Use-case-specific fields gathered during conversation
    use_case_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Use-case-specific fields (e.g. SD-WAN role, branch count). "
        "Keys match the field names on the use-case models.",
    )

    # Completion tracking
    complete: bool = Field(
        False,
        description=(
            "True when all BLOCKING fields are populated: use_cases, cloud_routing_protocol, "
            "resilience, bandwidth, solution_description, and all use-case required_fields. "
            "Soft/optional fields (compliance, user_info, use-case optional_fields marked "
            "x-optional in the schema) do NOT block complete=True — a user decline is accepted."
        ),
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description=(
            "All field names still not answered — both blocking and soft/optional. "
            "Remove a field from this list when the user has answered it (even with 'none'/'skip'). "
            "complete can be True while soft fields remain in this list."
        ),
    )

    def validate_and_correct_completion(self) -> None:
        """Server-side validation: enforce correct complete flag.

        Blocking fields (use_cases, routing, resilience, bandwidth, description,
        use-case required_fields) must be present — their absence forces complete=False.

        Soft fields (user_info, compliance, use-case optional_fields) are tracked
        in missing_fields so the agent keeps asking, but they do NOT prevent
        complete=True — a user decline is a valid answer for these.
        """
        blocking_missing: list[str] = []
        soft_missing: list[str] = []

        def _check_field(field_name: str, is_blocking: bool) -> None:
            val = getattr(self, field_name, None)
            bucket = blocking_missing if is_blocking else soft_missing

            if isinstance(val, BaseModel):
                bucket.extend(_empty_required_subfields_instance(val, field_name))
                return

            prog_finfo = InterviewProgress.model_fields.get(field_name)
            if prog_finfo:
                sub_cls = _get_basemodel_class(prog_finfo.annotation)
                if sub_cls and _is_empty(val):
                    for fname, finfo in sub_cls.model_fields.items():
                        if not _is_optional_annotation(finfo.annotation):
                            bucket.append(f"{field_name}.{fname}")
                    return

            check_val = val.value if isinstance(val, Enum) else val
            if _is_empty(check_val):
                bucket.append(field_name)

        # --- base fields ---
        for field_name in _BASE_REQUIRED_FIELDS - _SEED_FIELDS:
            _check_field(field_name, is_blocking=(field_name in _BLOCKING_BASE_FIELDS))

        # --- use-case-specific fields ---
        for uc in (self.use_cases or []):
            spec = USE_CASE_REGISTRY.get(uc)
            if not spec:
                continue
            for field_name in spec.required_fields:
                val = self.use_case_fields.get(field_name)
                if _is_empty(val):
                    blocking_missing.append(field_name)
                elif isinstance(val, dict):
                    sub_finfo = spec.model.model_fields.get(field_name)
                    if sub_finfo and (sub_cls := _get_basemodel_class(sub_finfo.annotation)):
                        blocking_missing.extend(_empty_required_subfields(sub_cls, val, field_name))

            for field_name in spec.optional_fields:
                val = self.use_case_fields.get(field_name)
                if _is_empty(val):
                    soft_missing.append(field_name)
                elif isinstance(val, dict):
                    sub_finfo = spec.model.model_fields.get(field_name)
                    if sub_finfo and (sub_cls := _get_basemodel_class(sub_finfo.annotation)):
                        soft_missing.extend(_empty_required_subfields(sub_cls, val, field_name))

        self.complete = len(blocking_missing) == 0
        self.missing_fields = blocking_missing + soft_missing

    def to_interview_output(self) -> InterviewOutput:
        """Convert to InterviewOutput."""
        use_cases = self.use_cases or [UseCases.NOTKNOWN]
        uc_fields = self.use_case_fields or {}

        use_case_details = {}
        for uc in use_cases:
            if spec := USE_CASE_REGISTRY.get(uc):
                uc_data = {k: uc_fields[k] for k in spec.all_fields if k in uc_fields}
                clean_data = _sanitize_uc_data(spec.model, uc_data)
                use_case_details[uc.value] = spec.model(**clean_data).model_dump()

        return InterviewOutput(
            use_cases=use_cases,
            cloud_routing_protocol=self.cloud_routing_protocol or RoutingProtocol.NOTKNOWN,
            resilience=self.resilience or WorkloadResilience.NOTKNOWN,
            bandwidth=self.bandwidth or 0.0,
            user_info=self.user_info,
            compliance=self.compliance or [],
            solution_description=self.solution_description or "",
            use_case_details=use_case_details,
        )
