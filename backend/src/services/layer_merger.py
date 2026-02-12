"""Deterministic layer merger — merges per-layer ResourcePlans into one.

Pure Python, no LLM calls.  Wires cross-layer references by replacing
import parameters with Ref/GetAtt to the exporting resource.

Algorithm
---------
1. For each layer (in dependency order), iterate its imports.
2. For each import, look up the corresponding export in the source layer.
3. Build the replacement intrinsic: ``{"ref": logical_id}`` or
   ``{"get_att": [logical_id, attribute]}``.
4. In the importing layer's ResourcePlan, replace all
   ``{"ref": param_name}`` with the export intrinsic.
5. Remove the import parameter from the layer's parameter list.
6. Concatenate all layers' resources, remaining parameters, mappings,
   conditions, outputs into a single ResourcePlan.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from src.models.layer_plan import LayerName, LayerPlan
from src.models.resource_plan import (
    CfnCondition,
    CfnMapping,
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def merge_layers(
    layer_plan: LayerPlan,
    layer_resource_plans: dict[LayerName, ResourcePlan],
) -> ResourcePlan:
    """Merge per-layer ResourcePlans into a single ResourcePlan.

    Steps:
    1. Wire cross-layer imports to exports via Ref/GetAtt.
    2. Remove import-only parameters.
    3. Concatenate all resources, parameters, outputs, etc.
    4. Detect and resolve duplicate logical IDs.

    Args:
        layer_plan: The LayerPlan defining import/export contracts.
        layer_resource_plans: Per-layer ResourcePlans keyed by LayerName.

    Returns:
        A single merged ResourcePlan ready for JSON assembly.

    Raises:
        ValueError: If an import cannot be resolved to an export.
    """
    # Deep copy to avoid mutating the originals
    plans = {k: _deep_copy_plan(v) for k, v in layer_resource_plans.items()}

    # Phase 1: Wire imports to exports and remove import parameters
    for layer_spec in layer_plan.layers:
        layer_name = layer_spec.name
        if layer_name not in plans:
            continue

        plan = plans[layer_name]
        import_param_names: set[str] = set()

        for imp in layer_spec.imports:
            replacement = _resolve_import(imp.name, imp.source_layer, layer_plan, plans)
            _replace_refs_in_plan(plan, imp.parameter_name, replacement)
            import_param_names.add(imp.parameter_name)

        # Remove import-only parameters
        plan.parameters = [
            p for p in plan.parameters if p.logical_id not in import_param_names
        ]

    # Phase 2: Concatenate all sections
    all_resources: list[CfnResource] = []
    all_parameters: list[CfnParameter] = []
    all_outputs: list[CfnOutput] = []
    all_mappings: list[CfnMapping] = []
    all_conditions: list[CfnCondition] = []
    descriptions: list[str] = []

    for layer_spec in layer_plan.layers:
        layer_name = layer_spec.name
        if layer_name not in plans:
            continue

        plan = plans[layer_name]
        all_resources.extend(plan.resources)
        all_parameters.extend(plan.parameters)
        all_outputs.extend(plan.outputs)
        all_mappings.extend(plan.mappings)
        all_conditions.extend(plan.conditions)
        if plan.description:
            descriptions.append(plan.description)

    # Phase 3: Deduplicate parameters (keep first occurrence)
    seen_params: dict[str, CfnParameter] = {}
    deduped_params: list[CfnParameter] = []
    for p in all_parameters:
        if p.logical_id not in seen_params:
            seen_params[p.logical_id] = p
            deduped_params.append(p)

    # Phase 4: Detect duplicate resource logical IDs
    seen_resources: set[str] = set()
    for r in all_resources:
        if r.logical_id in seen_resources:
            raise ValueError(
                f"Duplicate resource logical ID '{r.logical_id}' across layers. "
                "Predefined layer plans should use unique IDs."
            )
        seen_resources.add(r.logical_id)

    # Phase 5: Deduplicate outputs and mappings (keep first occurrence)
    seen_output_ids: set[str] = set()
    deduped_outputs: list[CfnOutput] = []
    for o in all_outputs:
        if o.logical_id not in seen_output_ids:
            seen_output_ids.add(o.logical_id)
            deduped_outputs.append(o)

    seen_mapping_ids: set[str] = set()
    deduped_mappings: list[CfnMapping] = []
    for m in all_mappings:
        if m.logical_id not in seen_mapping_ids:
            seen_mapping_ids.add(m.logical_id)
            deduped_mappings.append(m)

    return ResourcePlan(
        description=layer_plan.description or " | ".join(descriptions),
        parameters=deduped_params,
        mappings=deduped_mappings,
        conditions=all_conditions,
        resources=all_resources,
        outputs=deduped_outputs,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deep_copy_plan(plan: ResourcePlan) -> ResourcePlan:
    """Create a deep copy of a ResourcePlan to avoid mutation."""
    return ResourcePlan.model_validate(copy.deepcopy(plan.model_dump()))


def _resolve_import(
    import_name: str,
    source_layer: LayerName,
    layer_plan: LayerPlan,
    plans: dict[LayerName, ResourcePlan],
) -> dict[str, Any]:
    """Resolve an import to its corresponding export intrinsic.

    Returns:
        ``{"ref": "LogicalId"}`` or ``{"get_att": ["LogicalId", "Attribute"]}``

    Raises:
        ValueError: If the export is not found in the source layer's spec.
    """
    source_spec = layer_plan.get_layer(source_layer)
    if source_spec is None:
        raise ValueError(
            f"Import '{import_name}' references source layer "
            f"'{source_layer}' which does not exist in the plan"
        )

    for exp in source_spec.exports:
        if exp.name == import_name:
            # Verify the resource exists in the source plan
            if source_layer in plans:
                resource_ids = {r.logical_id for r in plans[source_layer].resources}
                if exp.resource_logical_id not in resource_ids:
                    logger.warning(
                        "Export '%s' references resource '%s' not found in "
                        "layer %s ResourcePlan (may be a parameter instead)",
                        import_name, exp.resource_logical_id, source_layer,
                    )

            if exp.attribute:
                return {"get_att": [exp.resource_logical_id, exp.attribute]}
            return {"ref": exp.resource_logical_id}

    raise ValueError(
        f"Import '{import_name}' not found in exports of layer "
        f"'{source_layer}'. Available exports: "
        f"{[e.name for e in source_spec.exports]}"
    )


def _replace_refs_in_plan(
    plan: ResourcePlan,
    param_name: str,
    replacement: dict[str, Any],
) -> None:
    """Replace all ``{"ref": param_name}`` in the plan with the replacement.

    Walks all resource properties, output values, and condition
    expressions recursively.
    """
    for resource in plan.resources:
        resource.properties = _replace_refs_recursive(
            resource.properties, param_name, replacement,
        )

    for output in plan.outputs:
        output.value = _replace_refs_recursive(
            output.value, param_name, replacement,
        )
        if output.export_name is not None:
            output.export_name = _replace_refs_recursive(
                output.export_name, param_name, replacement,
            )

    for condition in plan.conditions:
        condition.condition = _replace_refs_recursive(
            condition.condition, param_name, replacement,
        )


def _replace_refs_recursive(
    value: Any,
    param_name: str,
    replacement: dict[str, Any],
) -> Any:
    """Recursively replace ``{"ref": param_name}`` with the replacement dict.

    Handles nested dicts, lists, and intrinsic function representations.
    """
    if isinstance(value, dict):
        # Check if this is a Ref to the import parameter
        if len(value) == 1 and value.get("ref") == param_name:
            return copy.deepcopy(replacement)

        # Recurse into dict values
        return {k: _replace_refs_recursive(v, param_name, replacement) for k, v in value.items()}

    if isinstance(value, list):
        return [_replace_refs_recursive(item, param_name, replacement) for item in value]

    return value
