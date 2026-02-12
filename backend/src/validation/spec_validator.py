"""Phase 5: Spec validation — validates a ResourcePlan against CloudFormation
resource specifications BEFORE template assembly.

Catches errors such as:
  - Invalid resource types (e.g., "AWS::EC2::VPX" typos)
  - Invalid property names for a resource type
  - Missing required properties
  - Dangling Ref/GetAtt targets (referencing resources or parameters that
    don't exist in the plan)

Uses cfn-lint's bundled JSON schema files for spec data. Falls back to an
empty finding list if cfn-lint schemas are unavailable (graceful degradation).
"""

import importlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.models.iac import ValidationFinding
from src.models.resource_plan import ResourcePlan

logger = logging.getLogger(__name__)

# Rule ID prefix: "SP" = spec-validator
_LAYER = "spec"


# ---------------------------------------------------------------------------
# Schema loading with caching
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _load_region_type_map(region: str) -> dict[str, str]:
    """Load the resource-type -> schema-hash mapping for a region.

    Returns an empty dict if cfn-lint schemas are unavailable.
    """
    module_name = f"cfnlint.data.schemas.providers.{region.replace('-', '_')}"
    try:
        mod = importlib.import_module(module_name)
        return dict(mod.types)  # copy so we don't mutate the module-level dict
    except Exception:
        logger.warning("Could not load cfn-lint region module %s", module_name)
        return {}


@lru_cache(maxsize=256)
def _load_resource_schema(schema_hash: str) -> dict[str, Any] | None:
    """Load a resource JSON schema by its hash from cfn-lint's bundled data.

    Returns None if the schema file cannot be found or parsed.
    """
    try:
        import cfnlint.data.schemas.resources  # noqa: F401
        schemas_dir = Path(cfnlint.data.schemas.resources.__file__).parent
        schema_path = schemas_dir / f"{schema_hash}.json"
        if not schema_path.exists():
            return None
        return json.loads(schema_path.read_text())
    except Exception:
        logger.warning("Could not load schema hash %s", schema_hash)
        return None


def _get_resource_schema(resource_type: str, region: str) -> dict[str, Any] | None:
    """Return the JSON schema for a CloudFormation resource type, or None."""
    type_map = _load_region_type_map(region)
    schema_hash = type_map.get(resource_type)
    if schema_hash is None:
        return None
    return _load_resource_schema(schema_hash)


def _writable_properties(schema: dict[str, Any]) -> set[str]:
    """Return the set of property names that users can specify.

    Excludes read-only properties (those returned by CloudFormation but not
    settable by the user, e.g., VpcId on AWS::EC2::VPC).
    """
    all_props = set(schema.get("properties", {}).keys())
    read_only = set()
    for path in schema.get("readOnlyProperties", []):
        # paths look like "/properties/VpcId"
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "properties":
            read_only.add(parts[1])
    return all_props - read_only


def _required_properties(schema: dict[str, Any]) -> set[str]:
    """Return the set of unconditionally required property names."""
    return set(schema.get("required", []))


# ---------------------------------------------------------------------------
# Ref / GetAtt target collection
# ---------------------------------------------------------------------------

_PSEUDO_PARAMETERS = frozenset({
    "AWS::AccountId",
    "AWS::NotificationARNs",
    "AWS::NoValue",
    "AWS::Partition",
    "AWS::Region",
    "AWS::StackId",
    "AWS::StackName",
    "AWS::URLSuffix",
})


def _collect_ref_targets(plan: ResourcePlan) -> set[str]:
    """Build the set of valid Ref targets from the plan.

    Valid targets are: parameter logical IDs, resource logical IDs,
    condition logical IDs, and AWS pseudo-parameters.
    """
    targets: set[str] = set(_PSEUDO_PARAMETERS)
    for p in plan.parameters:
        targets.add(p.logical_id)
    for r in plan.resources:
        targets.add(r.logical_id)
    for c in plan.conditions:
        targets.add(c.logical_id)
    return targets


def _collect_getatt_resources(plan: ResourcePlan) -> set[str]:
    """Build the set of valid GetAtt resource targets (resource logical IDs)."""
    return {r.logical_id for r in plan.resources}


def _walk_for_refs(value: Any) -> list[str]:
    """Recursively walk a property value tree and return all Ref targets."""
    refs: list[str] = []
    if isinstance(value, dict):
        if "ref" in value and isinstance(value["ref"], str):
            refs.append(value["ref"])
        # Also check Ref (capital) for safety
        if "Ref" in value and isinstance(value["Ref"], str):
            refs.append(value["Ref"])
        for v in value.values():
            refs.extend(_walk_for_refs(v))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_walk_for_refs(item))
    return refs


def _walk_for_getatts(value: Any) -> list[tuple[str, str]]:
    """Recursively walk a property value tree and return all GetAtt targets.

    Returns list of (logical_id, attribute) tuples.
    """
    results: list[tuple[str, str]] = []
    if isinstance(value, dict):
        if "get_att" in value and isinstance(value["get_att"], list):
            parts = value["get_att"]
            if len(parts) >= 2 and isinstance(parts[0], str):
                results.append((parts[0], str(parts[1])))
        # Also check Fn::GetAtt (fully-qualified) for safety
        if "Fn::GetAtt" in value and isinstance(value["Fn::GetAtt"], list):
            parts = value["Fn::GetAtt"]
            if len(parts) >= 2 and isinstance(parts[0], str):
                results.append((parts[0], str(parts[1])))
        for v in value.values():
            results.extend(_walk_for_getatts(v))
    elif isinstance(value, list):
        for item in value:
            results.extend(_walk_for_getatts(item))
    return results


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------

def validate_resource_plan(
    plan: ResourcePlan,
    region: str = "us-east-1",
) -> list[ValidationFinding]:
    """Validate a ResourcePlan against CloudFormation resource specifications.

    Checks:
      SP001 — Invalid resource type (not in cfn-lint specs for the region)
      SP002 — Invalid property name for the resource type
      SP003 — Missing required property
      SP004 — Dangling Ref target (referencing a nonexistent resource/parameter)
      SP005 — Dangling GetAtt target (referencing a nonexistent resource)

    Returns an empty list on graceful degradation (cfn-lint unavailable).
    """
    findings: list[ValidationFinding] = []

    # Load the region type map; if empty, we degrade gracefully
    type_map = _load_region_type_map(region)
    if not type_map:
        logger.info(
            "No cfn-lint spec data for region %s — spec validation skipped", region
        )
        return findings

    # --- Check each resource ---
    for resource in plan.resources:
        # Skip Custom:: resources — they have user-defined schemas
        if resource.type.startswith("Custom::"):
            continue

        schema = _get_resource_schema(resource.type, region)

        # SP001: Invalid resource type
        if schema is None:
            findings.append(ValidationFinding(
                layer=_LAYER,
                severity="error",
                rule_id="SP001",
                message=(
                    f"Unknown resource type '{resource.type}' "
                    f"— not found in CloudFormation spec for {region}"
                ),
                resource=resource.logical_id,
            ))
            continue  # Can't check properties without a schema

        # SP002: Invalid property names
        writable = _writable_properties(schema)
        for prop_name in resource.properties:
            if prop_name not in writable:
                findings.append(ValidationFinding(
                    layer=_LAYER,
                    severity="error",
                    rule_id="SP002",
                    message=(
                        f"Invalid property '{prop_name}' for "
                        f"resource type '{resource.type}'"
                    ),
                    resource=resource.logical_id,
                ))

        # SP003: Missing required properties
        # Properties whose values are intrinsic functions (Ref, Sub, etc.)
        # still count as "present" — we only check key existence.
        required = _required_properties(schema)
        for req_prop in required:
            if req_prop not in resource.properties:
                findings.append(ValidationFinding(
                    layer=_LAYER,
                    severity="error",
                    rule_id="SP003",
                    message=(
                        f"Missing required property '{req_prop}' for "
                        f"resource type '{resource.type}'"
                    ),
                    resource=resource.logical_id,
                ))

    # --- Cross-resource reference checks ---
    valid_ref_targets = _collect_ref_targets(plan)
    valid_getatt_resources = _collect_getatt_resources(plan)

    # Collect all Ref and GetAtt usages from resource properties, outputs,
    # and conditions.
    all_ref_sources: list[tuple[str, str]] = []  # (context_label, ref_target)
    all_getatt_sources: list[tuple[str, str, str]] = []  # (context, logical_id, attr)

    for resource in plan.resources:
        ctx = f"resource '{resource.logical_id}'"
        for ref_target in _walk_for_refs(resource.properties):
            all_ref_sources.append((ctx, ref_target))
        for logical_id, attr in _walk_for_getatts(resource.properties):
            all_getatt_sources.append((ctx, logical_id, attr))
        # Also check DependsOn
        if resource.depends_on:
            for dep in resource.depends_on:
                if dep not in valid_getatt_resources:
                    all_ref_sources.append((ctx + " DependsOn", dep))

    for output in plan.outputs:
        ctx = f"output '{output.logical_id}'"
        for ref_target in _walk_for_refs(output.value):
            all_ref_sources.append((ctx, ref_target))
        for logical_id, attr in _walk_for_getatts(output.value):
            all_getatt_sources.append((ctx, logical_id, attr))
        if output.export_name is not None:
            for ref_target in _walk_for_refs(output.export_name):
                all_ref_sources.append((ctx, ref_target))

    for condition in plan.conditions:
        ctx = f"condition '{condition.logical_id}'"
        for ref_target in _walk_for_refs(condition.condition):
            all_ref_sources.append((ctx, ref_target))

    # SP004: Dangling Ref targets
    for ctx, ref_target in all_ref_sources:
        if ref_target not in valid_ref_targets:
            findings.append(ValidationFinding(
                layer=_LAYER,
                severity="error",
                rule_id="SP004",
                message=(
                    f"Dangling Ref target '{ref_target}' in {ctx} "
                    f"— no matching resource, parameter, or pseudo-parameter"
                ),
                resource=ref_target,
            ))

    # SP005: Dangling GetAtt targets
    for ctx, logical_id, attr in all_getatt_sources:
        if logical_id not in valid_getatt_resources:
            findings.append(ValidationFinding(
                layer=_LAYER,
                severity="error",
                rule_id="SP005",
                message=(
                    f"Dangling GetAtt target '{logical_id}.{attr}' in {ctx} "
                    f"— resource '{logical_id}' not found in plan"
                ),
                resource=logical_id,
            ))

    return findings
