"""CFN Assembler — converts ResourcePlan to valid CloudFormation YAML or JSON.

Pure Python, no LLM calls.  Takes a structured ``ResourcePlan`` and
produces CloudFormation output via ``cfn_dump()`` (YAML) or
``json.dumps()`` (JSON).

Key responsibilities
--------------------
1. Convert intrinsic-function dicts to ``CfnTag`` objects (YAML path)
   or CloudFormation long-form dicts (JSON path).
2. Build the template dict with correct CFN section ordering.
3. Output via ``cfn_dump()`` (Paths 1 & 2) or ``json.dumps()`` (Path 3).

Also provides ``inject_parameter_defaults()`` for Path 1 (PARAMETERIZE)
— a zero-LLM code path that programmatically sets Parameter Default
values in an existing template.
"""

import json
from typing import Any

from src.models.resource_plan import (
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)
from src.utils.cfn_yaml import CfnTag, cfn_dump, cfn_load


# ---------------------------------------------------------------------------
# Intrinsic function mapping (lowercase JSON key -> CFN YAML tag)
# ---------------------------------------------------------------------------

_INTRINSIC_MAP: dict[str, str] = {
    "ref": "!Ref",
    "sub": "!Sub",
    "get_att": "!GetAtt",
    "select": "!Select",
    "join": "!Join",
    "find_in_map": "!FindInMap",
    "if": "!If",
    "get_azs": "!GetAZs",
    "base64": "!Base64",
    "cidr": "!Cidr",
    "equals": "!Equals",
    "condition": "!Condition",
    "not": "!Not",
    "and": "!And",
    "or": "!Or",
    "import_value": "!ImportValue",
    "split": "!Split",
}


# ---------------------------------------------------------------------------
# Intrinsic function conversion
# ---------------------------------------------------------------------------


def _convert_intrinsics(value: Any) -> Any:
    """Recursively convert intrinsic-function dicts to ``CfnTag`` objects.

    Examples::

        {"ref": "VPC"}                     -> CfnTag("!Ref", "VPC")
        {"get_att": ["Instance", "Ip"]}    -> CfnTag("!GetAtt", "Instance.Ip")
        {"sub": "${AWS::StackName}-vpc"}   -> CfnTag("!Sub", "...")
        {"join": [",", [{"ref": "A"}]]}    -> CfnTag("!Join", [",", [CfnTag(...)]])

    Plain values (str, int, bool, None) pass through unchanged.
    """
    if isinstance(value, dict):
        # Check if this dict is a single-key intrinsic function
        for key, tag in _INTRINSIC_MAP.items():
            if key in value and len(value) == 1:
                raw_val = value[key]

                # Special case: !GetAtt ["Resource", "Attr"] -> "Resource.Attr"
                if key == "get_att" and isinstance(raw_val, list) and len(raw_val) == 2:
                    return CfnTag(tag, f"{raw_val[0]}.{raw_val[1]}")

                # Recursively convert nested intrinsics
                return CfnTag(tag, _convert_intrinsics(raw_val))

        # Regular dict — recurse into values
        return {k: _convert_intrinsics(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_convert_intrinsics(item) for item in value]

    return value


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_parameter(param: CfnParameter) -> dict[str, Any]:
    """Convert a ``CfnParameter`` to a CFN Parameters dict entry."""
    d: dict[str, Any] = {"Type": param.type}
    if param.default is not None:
        d["Default"] = param.default
    if param.description:
        d["Description"] = param.description
    if param.allowed_values:
        d["AllowedValues"] = param.allowed_values
    if param.constraint_description:
        d["ConstraintDescription"] = param.constraint_description
    if param.no_echo:
        d["NoEcho"] = True
    return d


def _build_resource(resource: CfnResource) -> dict[str, Any]:
    """Convert a ``CfnResource`` to a CFN Resources dict entry."""
    d: dict[str, Any] = {
        "Type": resource.type,
        "Properties": _convert_intrinsics(resource.properties),
    }
    if resource.depends_on:
        d["DependsOn"] = resource.depends_on
    if resource.condition:
        d["Condition"] = resource.condition
    if resource.metadata:
        d["Metadata"] = resource.metadata
    return d


def _build_output(output: CfnOutput) -> dict[str, Any]:
    """Convert a ``CfnOutput`` to a CFN Outputs dict entry."""
    d: dict[str, Any] = {"Value": _convert_intrinsics(output.value)}
    if output.description:
        d["Description"] = output.description
    if output.export_name is not None:
        d["Export"] = {"Name": _convert_intrinsics(output.export_name)}
    if output.condition:
        d["Condition"] = output.condition
    return d


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------


def assemble(plan: ResourcePlan) -> str:
    """Convert a ``ResourcePlan`` to CloudFormation YAML.

    Builds the template dict in canonical CFN section order and delegates
    to ``cfn_dump()`` for YAML serialisation.

    Returns:
        Valid CloudFormation YAML string.
    """
    template: dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
    }

    if plan.description:
        template["Description"] = plan.description

    if plan.parameters:
        template["Parameters"] = {
            p.logical_id: _build_parameter(p) for p in plan.parameters
        }

    if plan.mappings:
        template["Mappings"] = {m.logical_id: m.mapping for m in plan.mappings}

    if plan.conditions:
        template["Conditions"] = {
            c.logical_id: _convert_intrinsics(c.condition)
            for c in plan.conditions
        }

    template["Resources"] = {
        r.logical_id: _build_resource(r) for r in plan.resources
    }

    if plan.outputs:
        template["Outputs"] = {
            o.logical_id: _build_output(o) for o in plan.outputs
        }

    return cfn_dump(template)


# ---------------------------------------------------------------------------
# JSON-native intrinsic function mapping (lowercase key -> CFN JSON long-form)
# ---------------------------------------------------------------------------

_INTRINSIC_JSON_MAP: dict[str, str] = {
    "ref": "Ref",
    "sub": "Fn::Sub",
    "get_att": "Fn::GetAtt",
    "select": "Fn::Select",
    "join": "Fn::Join",
    "find_in_map": "Fn::FindInMap",
    "if": "Fn::If",
    "get_azs": "Fn::GetAZs",
    "base64": "Fn::Base64",
    "cidr": "Fn::Cidr",
    "equals": "Fn::Equals",
    "condition": "Condition",
    "not": "Fn::Not",
    "and": "Fn::And",
    "or": "Fn::Or",
    "import_value": "Fn::ImportValue",
    "split": "Fn::Split",
}


def _convert_intrinsics_json(value: Any) -> Any:
    """Recursively convert intrinsic-function dicts to CFN JSON long-form.

    Unlike ``_convert_intrinsics()`` which produces ``CfnTag`` objects for
    YAML serialisation, this produces plain dicts suitable for
    ``json.dumps()``.

    Examples::

        {"ref": "VPC"}                     -> {"Ref": "VPC"}
        {"get_att": ["Instance", "Ip"]}    -> {"Fn::GetAtt": ["Instance", "Ip"]}
        {"sub": "${AWS::StackName}-vpc"}   -> {"Fn::Sub": "${AWS::StackName}-vpc"}
        {"base64": {"sub": "..."}}         -> {"Fn::Base64": {"Fn::Sub": "..."}}

    Plain values (str, int, bool, None) pass through unchanged.
    """
    if isinstance(value, dict):
        for key, cfn_key in _INTRINSIC_JSON_MAP.items():
            if key in value and len(value) == 1:
                raw_val = value[key]
                # Note: GetAtt keeps list form in JSON (no dot-joining)
                return {cfn_key: _convert_intrinsics_json(raw_val)}

        # Regular dict — recurse into values
        return {k: _convert_intrinsics_json(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_convert_intrinsics_json(item) for item in value]

    return value


# ---------------------------------------------------------------------------
# JSON section builders
# ---------------------------------------------------------------------------


def _build_resource_json(resource: CfnResource) -> dict[str, Any]:
    """Convert a ``CfnResource`` to a CFN Resources dict entry (JSON path)."""
    d: dict[str, Any] = {
        "Type": resource.type,
        "Properties": _convert_intrinsics_json(resource.properties),
    }
    if resource.depends_on:
        d["DependsOn"] = resource.depends_on
    if resource.condition:
        d["Condition"] = resource.condition
    if resource.metadata:
        d["Metadata"] = resource.metadata
    return d


def _build_output_json(output: CfnOutput) -> dict[str, Any]:
    """Convert a ``CfnOutput`` to a CFN Outputs dict entry (JSON path)."""
    d: dict[str, Any] = {"Value": _convert_intrinsics_json(output.value)}
    if output.description:
        d["Description"] = output.description
    if output.export_name is not None:
        d["Export"] = {"Name": _convert_intrinsics_json(output.export_name)}
    if output.condition:
        d["Condition"] = output.condition
    return d


# ---------------------------------------------------------------------------
# JSON assembler (Path 3)
# ---------------------------------------------------------------------------


def assemble_json(plan: ResourcePlan) -> str:
    """Convert a ``ResourcePlan`` to CloudFormation JSON.

    Uses ``json.dumps()`` — no YAML tags, no ``CfnTag`` objects.  Intrinsic
    functions use CloudFormation JSON long-form:
    ``{"Ref": "X"}``, ``{"Fn::Sub": "..."}``, ``{"Fn::GetAtt": ["R", "A"]}``.

    Returns:
        Valid CloudFormation JSON string.
    """
    template: dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
    }

    if plan.description:
        template["Description"] = plan.description

    if plan.parameters:
        template["Parameters"] = {
            p.logical_id: _build_parameter(p) for p in plan.parameters
        }

    if plan.mappings:
        template["Mappings"] = {m.logical_id: m.mapping for m in plan.mappings}

    if plan.conditions:
        template["Conditions"] = {
            c.logical_id: _convert_intrinsics_json(c.condition)
            for c in plan.conditions
        }

    template["Resources"] = {
        r.logical_id: _build_resource_json(r) for r in plan.resources
    }

    if plan.outputs:
        template["Outputs"] = {
            o.logical_id: _build_output_json(o) for o in plan.outputs
        }

    return json.dumps(template, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Path 1 helper — programmatic parameter injection
# ---------------------------------------------------------------------------


def inject_parameter_defaults(
    template_str: str,
    defaults: dict[str, str],
) -> str:
    """Update Parameter Default values in an existing CFN template.

    Used by Path 1 (PARAMETERIZE) to programmatically set defaults
    without any LLM involvement.  Unmatched parameter names are left
    unchanged.

    Args:
        template_str: Raw CFN YAML/JSON string.
        defaults: Mapping of parameter logical ID -> new default value.

    Returns:
        Updated template YAML string.
    """
    parsed = cfn_load(template_str)
    if not isinstance(parsed, dict):
        raise ValueError("Template root is not a mapping")

    params = parsed.get("Parameters", {})
    if not isinstance(params, dict):
        return cfn_dump(parsed)

    for param_name, default_value in defaults.items():
        if param_name in params and isinstance(params[param_name], dict):
            params[param_name]["Default"] = default_value

    return cfn_dump(parsed)
