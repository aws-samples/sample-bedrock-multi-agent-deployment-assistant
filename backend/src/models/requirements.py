"""Interview requirements models — config-driven from catalog.lock.yaml.

The UseCases enum and USE_CASE_REGISTRY are maintained as backward-compatible shims
that delegate to the CatalogLoader singleton. All field definitions, blocking/soft
classification, and doc_type mappings come from the catalog lock file.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backward-compatible shims for UseCases, RoutingProtocol, WorkloadResilience
# ---------------------------------------------------------------------------
# These are kept as string constants (not Enums) so existing code that does
# `UseCases.SD_WAN` or `RoutingProtocol.BGP` still works during the transition.
# New code should use plain strings validated against the catalog.


class _UseCase(str):
    """A use case value — just a string with a .value property for Enum compatibility."""

    @property
    def value(self) -> str:
        return str(self)


class _UseCasesNamespace:
    """Backward-compatible namespace for use case constants.

    Supports iteration, attribute access, and `in` operator.
    Dynamically populated from catalog on first access.
    """

    NOTKNOWN = _UseCase("notknown")

    def __init__(self) -> None:
        self._values: list[_UseCase] | None = None

    def _load(self) -> None:
        if self._values is not None:
            return
        try:
            from src.services.catalog_loader import get_catalog
            catalog = get_catalog()
            uc_values = catalog.get_use_case_values()
            self._values = [_UseCase(v) for v in uc_values]
            # Set attributes for common access patterns
            for v in uc_values:
                attr_name = v.upper().replace("-", "_")
                setattr(self, attr_name, _UseCase(v))
        except Exception:
            self._values = []

    def __iter__(self):
        self._load()
        return iter(self._values)

    def __contains__(self, item) -> bool:
        self._load()
        val = item.value if hasattr(item, "value") else str(item)
        return val in [v.value for v in self._values] or val == "notknown"

    def __call__(self, value: str) -> _UseCase:
        """Construct a UseCase from a string value (Enum compatibility)."""
        return _UseCase(value)


UseCases = _UseCasesNamespace()


class RoutingProtocol:
    BGP = "bgp"
    STATIC_ROUTE = "static-route"
    NOTKNOWN = "notknown"


class WorkloadResilience:
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
    minLatency: Optional[float] = Field(None, description="Minimum acceptable latency in milliseconds")
    maxLatency: Optional[float] = Field(None, description="Maximum acceptable latency in milliseconds")
    minJitter: Optional[float] = Field(None, description="Minimum acceptable jitter in milliseconds")
    maxJitter: Optional[float] = Field(None, description="Maximum acceptable jitter in milliseconds")

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, v: Any) -> Any:
        """Parse a free-text performance string into structured fields."""
        if not isinstance(v, str):
            return v
        import re
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
# Use-case spec (backward-compatible interface, now catalog-driven)
# ---------------------------------------------------------------------------


@dataclass
class UseCaseSpec:
    """Runtime spec for a use case — populated from CatalogLoader."""

    model: type[BaseModel]
    required_fields: set[str]
    label: str
    optional_fields: set[str] = None
    field_doc_types: dict[str, str] = None

    def __post_init__(self) -> None:
        if self.optional_fields is None:
            self.optional_fields = set()
        if self.field_doc_types is None:
            self.field_doc_types = {}

    @property
    def all_fields(self) -> set[str]:
        return self.required_fields | (self.optional_fields or set())


def _get_use_case_registry() -> dict[str, "UseCaseSpec"]:
    """Build the USE_CASE_REGISTRY equivalent from the catalog.

    Returns a dict keyed by use-case value string.
    """
    try:
        from src.services.catalog_loader import get_catalog, build_use_case_model
        catalog = get_catalog()
        registry: dict[str, UseCaseSpec] = {}
        for spec in catalog.get_use_cases():
            model = catalog.get_use_case_model(spec.value) or BaseModel
            registry[spec.value] = UseCaseSpec(
                model=model,
                required_fields=spec.required_fields,
                optional_fields=spec.optional_fields,
                label=spec.label,
                field_doc_types=spec.field_doc_types,
            )
        return registry
    except Exception:
        return {}


# Lazy-loaded registry cache
_registry_cache: dict[str, UseCaseSpec] | None = None


def _get_registry() -> dict[str, UseCaseSpec]:
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = _get_use_case_registry()
    return _registry_cache


def reset_registry_cache() -> None:
    """Reset the registry cache — used in tests."""
    global _registry_cache
    _registry_cache = None


# Backward-compatible name
USE_CASE_REGISTRY = None  # Type: dict — access via _get_registry() instead


def get_model_for_use_case(uc) -> type[BaseModel] | None:
    """Return the use-case model class, or None if no specific model exists."""
    val = uc.value if hasattr(uc, "value") else str(uc)
    spec = _get_registry().get(val)
    return spec.model if spec else None


# ---------------------------------------------------------------------------
# Main requirements output
# ---------------------------------------------------------------------------


class InterviewOutput(BaseModel):
    """Structured output from the Interview Agent."""

    model_config = ConfigDict(extra="allow")

    use_cases: list[str] = Field(default_factory=list)
    gpu_budget: str = "notknown"
    availability_requirement: str = "notknown"
    data_sensitivity: str = "notknown"
    user_info: Optional[UserInformation] = None
    compliance: list[str] = Field(default_factory=list)
    solution_description: str = ""

    # Generic use-case detail payloads, keyed by use-case value (e.g. "realtime-inference")
    use_case_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base required fields for InterviewProgress completion tracking
# ---------------------------------------------------------------------------

_BLOCKING_BASE_FIELD_NAMES = {
    "use_cases",
    "gpu_budget",
    "availability_requirement",
    "data_sensitivity",
    "solution_description",
}

_SOFT_BASE_FIELD_NAMES = {"user_info", "compliance"}

_BASE_REQUIRED_FIELDS = _BLOCKING_BASE_FIELD_NAMES | _SOFT_BASE_FIELD_NAMES

# Fields provided by the seed form — never asked about during the interview
_SEED_FIELDS = {"use_cases", "gpu_budget", "solution_description"}

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


def get_missing_fields_schema(use_cases: list, populated: dict) -> dict:
    """Return JSON schema for ALL fields still missing — both base and use-case-specific.

    Fields from soft base fields or UseCaseSpec.optional_fields are tagged with
    "x-optional": true so the agent knows it can accept a user decline and move on.
    Blocking fields lack this tag — the agent must keep asking until answered.
    """
    missing: dict[str, Any] = {}
    registry = _get_registry()

    output_schema = InterviewOutput.model_json_schema()
    output_props = output_schema.get("properties", {})

    # --- base interview fields ---
    for field_name in _BASE_INTERVIEW_FIELDS:
        is_soft = field_name in _SOFT_BASE_FIELD_NAMES
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
        uc_key = uc.value if hasattr(uc, "value") else str(uc)
        if uc_key == "notknown":
            continue
        spec = registry.get(uc_key)
        if not spec:
            continue

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


def get_seed_context_block(use_cases: list, seed_data: dict) -> str:
    """Build a [SEED_CONTEXT] block injected into the agent's system prompt."""
    uc_labels = []
    for uc in use_cases:
        val = uc.value if hasattr(uc, "value") else str(uc)
        if val != "notknown":
            uc_labels.append(val)

    lines = [
        "[SEED_CONTEXT]",
        f"  use_cases: {', '.join(uc_labels) or 'unknown'}",
    ]
    for key in ("gpu_budget", "solution_description"):
        val = seed_data.get(key)
        if val:
            lines.append(f"  {key}: {val}")
    lines.append("[/SEED_CONTEXT]")
    return "\n".join(lines)


def get_field_doc_type(field_path: str, use_cases: list) -> str:
    """Return the KB document_type for a field path, derived from the catalog.

    Base fields have hardcoded mappings; use-case fields come from the catalog.
    Falls back to 'configuration'.
    """
    _BASE_DOC_TYPES: dict[str, str | None] = {
        "gpu_budget": "sizing",
        "availability_requirement": "architecture",
        "data_sensitivity": "configuration",
        "compliance": "best-practices",
        "user_info": None,
        "user_info.name": None,
        "user_info.experience_on_cloud": None,
    }
    if field_path in _BASE_DOC_TYPES:
        return _BASE_DOC_TYPES[field_path] or "configuration"

    registry = _get_registry()
    parts = field_path.split(".", 1)
    if len(parts) == 2:
        uc_key, field_name = parts
        spec = registry.get(uc_key)
        if spec and field_name in spec.field_doc_types:
            return spec.field_doc_types[field_name]

    return "configuration"


def get_use_case_config() -> list[dict]:
    """Return use-case metadata for the config endpoint."""
    try:
        from src.services.catalog_loader import get_catalog
        catalog = get_catalog()
        return catalog.get_use_case_config()
    except Exception:
        return []


def _sanitize_uc_data(model_cls: type[BaseModel], uc_data: dict) -> dict:
    """Coerce field values to match expected sub-model types, dropping only on failure."""
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
    use_cases: Optional[list[str]] = Field(None)
    gpu_budget: Optional[str] = Field(None, description="GPU compute budget constraint")
    availability_requirement: Optional[str] = Field(None, description="Availability and redundancy requirements")
    data_sensitivity: Optional[str] = Field(None, description="Data classification level")
    user_info: Optional[UserInformation] = Field(None)
    compliance: Optional[list[str]] = Field(None)
    solution_description: Optional[str] = Field(None)

    # Use-case-specific fields gathered during conversation
    use_case_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Use-case-specific fields gathered during conversation.",
    )

    # Completion tracking
    complete: bool = Field(
        False,
        description=(
            "True when all BLOCKING fields are populated: use_cases, gpu_budget, "
            "availability_requirement, data_sensitivity, solution_description, and all use-case required_fields. "
            "Soft/optional fields do NOT block complete=True — a user decline is accepted."
        ),
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description=(
            "All field names still not answered — both blocking and soft/optional. "
            "Remove a field from this list when the user has answered it."
        ),
    )

    def validate_and_correct_completion(self) -> None:
        """Server-side validation: enforce correct complete flag.

        Blocking fields must be present — their absence forces complete=False.
        Soft fields are tracked but do NOT prevent complete=True.
        """
        registry = _get_registry()
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

            check_val = val
            if _is_empty(check_val):
                bucket.append(field_name)

        # --- base fields ---
        for field_name in _BASE_REQUIRED_FIELDS - _SEED_FIELDS:
            _check_field(field_name, is_blocking=(field_name in _BLOCKING_BASE_FIELD_NAMES))

        # --- use-case-specific fields ---
        for uc in (self.use_cases or []):
            uc_val = uc.value if hasattr(uc, "value") else str(uc)
            spec = registry.get(uc_val)
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
        use_cases = self.use_cases or ["notknown"]
        uc_fields = self.use_case_fields or {}
        registry = _get_registry()

        use_case_details = {}
        for uc in use_cases:
            uc_val = uc.value if hasattr(uc, "value") else str(uc)
            spec = registry.get(uc_val)
            if spec:
                uc_data = {k: uc_fields[k] for k in spec.all_fields if k in uc_fields}
                clean_data = _sanitize_uc_data(spec.model, uc_data)
                use_case_details[uc_val] = spec.model(**clean_data).model_dump()

        return InterviewOutput(
            use_cases=use_cases,
            gpu_budget=self.gpu_budget or "notknown",
            availability_requirement=self.availability_requirement or "notknown",
            data_sensitivity=self.data_sensitivity or "notknown",
            user_info=self.user_info,
            compliance=self.compliance or [],
            solution_description=self.solution_description or "",
            use_case_details=use_case_details,
        )
