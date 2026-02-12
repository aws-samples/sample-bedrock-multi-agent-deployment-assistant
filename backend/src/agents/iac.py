"""IaC Agent — CloudFormation-only async generation (Structured Output + Assembly).

Three-path template resolution:
  1. PARAMETERIZE — KB template → programmatic parameter injection (zero LLM)
  2. COMPOSE — LLM-generated SnippetAssemblyPlan → deterministic merge
  3. GENERATE — Layered pipeline: Architecture Planner → per-layer ResourcePlan
     generation (parallel within dependency groups) → deterministic merge →
     JSON assembly via json.dumps()

The LLM never produces raw YAML/JSON.  It generates structured Pydantic models
via Strands ``structured_output_model``, and deterministic Python code converts
them to valid CloudFormation (YAML for Paths 1 & 2, JSON for Path 3).

Followed by a validation-fix loop (up to 3 attempts) that routes errors back
to originating layers for per-layer fixes, avoiding the error cascade of
monolithic fix loops.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path

from strands import Agent

from src.agents.common import bedrock_retry, create_bedrock_model
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.models.design import ResolvedIaCParameters
from src.models.iac import IaCOutput, ValidationFinding, ValidationReport
from src.models.layer_plan import LayerName, LayerPlan, LayerSpec
from src.models.resource_plan import ResourcePlan, SnippetAssemblyPlan
from src.services.cfn_assembler import assemble, assemble_json, inject_parameter_defaults
from src.services.layer_merger import merge_layers
from src.services.parameter_mapper import build_parameter_defaults
from src.services.template_resolver import TemplatePath, resolve_template_path
from src.tools.kb_search import kb_search_filtered
from src.tools.snippet_discovery import fetch_all_snippets
from src.utils.cfn_yaml import CfnTag, cfn_dump, cfn_load
from src.validation.pipeline import run_validation_pipeline

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_MAX_FIX_ATTEMPTS = 3


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


# ---------------------------------------------------------------------------
# Path 1: Parameterize KB template (zero LLM)
# ---------------------------------------------------------------------------


def _parameterize_template(params: ResolvedIaCParameters) -> str:
    """Fill parameter defaults in an existing KB template.

    Pure Python — no LLM involvement.  Parses the KB template,
    maps resolved values to parameter names, and re-serialises.
    """
    template_content = ""
    for key, content in (params.code_template_files or {}).items():
        if "template" in key.lower() and key.endswith((".yaml", ".yml", ".json")):
            template_content = content
            break

    if not template_content:
        raise ValueError("No CloudFormation template found in code_template_files")

    defaults = build_parameter_defaults(params, template_content)
    return inject_parameter_defaults(template_content, defaults)


# ---------------------------------------------------------------------------
# Path 2: Compose snippets (LLM generates SnippetAssemblyPlan)
# ---------------------------------------------------------------------------


def _summarize_snippets(parsed_snippets: dict[str, dict]) -> str:
    """Build a text summary of each snippet for the LLM prompt."""
    parts: list[str] = []
    for key, parsed in parsed_snippets.items():
        resources = parsed.get("Resources", {})
        param_keys = list((parsed.get("Parameters", {}) or {}).keys())
        output_keys = list((parsed.get("Outputs", {}) or {}).keys())
        resource_info = (
            [f"  - {lid}: {rdef.get('Type', '?')}" for lid, rdef in resources.items()]
            if isinstance(resources, dict)
            else []
        )
        parts.append(
            f"### {key}\n"
            f"Parameters: {param_keys}\n"
            f"Resources:\n" + "\n".join(resource_info) + "\n"
            f"Outputs: {output_keys}"
        )
    return "\n\n".join(parts)


def _set_nested_property(obj: dict, dot_path: str, value) -> None:
    """Set a value at a dot-delimited path in a nested dict/list structure."""
    parts = dot_path.split(".")
    current = obj
    for part in parts[:-1]:
        if part.isdigit():
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})
    last = parts[-1]
    if last.isdigit():
        current[int(last)] = value
    else:
        current[last] = value


def _execute_assembly_plan(
    plan: SnippetAssemblyPlan,
    parsed_snippets: dict[str, dict],
    params: ResolvedIaCParameters,
) -> str:
    """Deterministically execute the SnippetAssemblyPlan."""
    merged_params: dict[str, dict] = {}
    merged_resources: dict[str, dict] = {}
    merged_outputs: dict[str, dict] = {}
    merged_conditions: dict[str, object] = {}
    merged_mappings: dict[str, dict] = {}

    for snippet_key, parsed in parsed_snippets.items():
        # Resources (with renames)
        for lid, rdef in (parsed.get("Resources", {}) or {}).items():
            new_lid = plan.resource_renames.get(lid, lid)
            merged_resources[new_lid] = rdef

        # Parameters (with dedup)
        for pname, pdef in (parsed.get("Parameters", {}) or {}).items():
            if pname in merged_params:
                keep_snippet = plan.parameter_dedup_keep.get(pname)
                if keep_snippet and keep_snippet != snippet_key:
                    continue
            merged_params[pname] = pdef

        # Outputs (filtered)
        for oname, odef in (parsed.get("Outputs", {}) or {}).items():
            if not plan.output_selection or oname in plan.output_selection:
                merged_outputs[oname] = odef

        # Conditions + Mappings
        merged_conditions.update(parsed.get("Conditions", {}) or {})
        merged_mappings.update(parsed.get("Mappings", {}) or {})

    # Apply wiring
    for wire in plan.wiring:
        target_lid = plan.resource_renames.get(
            wire.target_resource_logical_id, wire.target_resource_logical_id
        )
        source_lid = plan.resource_renames.get(
            wire.source_logical_id, wire.source_logical_id
        )

        if target_lid not in merged_resources:
            logger.warning("Wiring target %s not found, skipping", target_lid)
            continue

        if wire.source_attribute:
            ref_value = CfnTag("!GetAtt", f"{source_lid}.{wire.source_attribute}")
        else:
            ref_value = CfnTag("!Ref", source_lid)

        props = merged_resources[target_lid].get("Properties", {})
        if isinstance(props, dict):
            _set_nested_property(props, wire.target_property_path, ref_value)

    # Inject parameter defaults
    defaults = build_parameter_defaults(params, "")
    for pname, pdef in merged_params.items():
        if pname in defaults and isinstance(pdef, dict):
            pdef["Default"] = defaults[pname]

    # Build final template
    template: dict = {"AWSTemplateFormatVersion": "2010-09-09"}
    if merged_params:
        template["Parameters"] = merged_params
    if merged_mappings:
        template["Mappings"] = merged_mappings
    if merged_conditions:
        template["Conditions"] = merged_conditions
    template["Resources"] = merged_resources
    if merged_outputs:
        template["Outputs"] = merged_outputs

    return cfn_dump(template)


@bedrock_retry("iac-compose")
def _compose_snippets(
    params: ResolvedIaCParameters,
    snippets_by_type: dict,
    invocation_state: dict,
    feedback_section: str = "",
) -> str:
    """Assemble snippets using an LLM-generated wiring plan."""
    fetched = fetch_all_snippets(snippets_by_type)

    # Parse all snippets
    parsed_snippets: dict[str, dict] = {}
    for resource_type, items in fetched.items():
        for info, content in items:
            key = f"{resource_type}/{info.filename}"
            parsed_snippets[key] = cfn_load(content)

    if not parsed_snippets:
        raise ValueError("No snippet contents fetched")

    snippet_summaries = _summarize_snippets(parsed_snippets)
    system_prompt = _load_prompt("iac_compose.txt")

    user_prompt = system_prompt.replace(
        "{snippets_summary}", snippet_summaries
    ).replace(
        "{resolved_params_json}", params.model_dump_json(indent=2)
    ) + feedback_section

    model = create_bedrock_model(max_tokens=settings.iac_compose_max_tokens)
    agent = Agent(
        name="iac-compose",
        model=model,
        system_prompt="You produce a JSON snippet assembly plan.",
        structured_output_model=SnippetAssemblyPlan,
        callback_handler=logging_callback_handler,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=invocation_state)

    plan = getattr(result, "structured_output", None)
    if not isinstance(plan, SnippetAssemblyPlan):
        logger.warning("LLM did not produce structured SnippetAssemblyPlan, using empty plan")
        plan = SnippetAssemblyPlan()

    return _execute_assembly_plan(plan, parsed_snippets, params)


# ---------------------------------------------------------------------------
# Path 3: KB-grounded generation (LLM generates ResourcePlan)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Layered generation pipeline (Path 3: decomposed)
# ---------------------------------------------------------------------------

# In-memory cache for LLM-generated LayerPlans, keyed by tenant + normalized pattern.
# Avoids repeated Architecture Planner calls for the same deployment pattern.
_LAYER_PLAN_CACHE: dict[str, LayerPlan] = {}
_LAYER_PLAN_LOCK = threading.Lock()


def _normalize_pattern_key(pattern: str) -> str:
    """Normalize a deployment pattern name for cache lookup."""
    return pattern.lower().replace("_", "-").replace(" ", "-").strip()


def _get_kb_context(use_case: str) -> tuple[str, str]:
    """Fetch KB context and reference template for a use case.

    Returns (kb_context, reference_template) strings.
    """
    kb_results = []
    for doc_type in ["architecture", "components", "configuration"]:
        results = kb_search_filtered(
            query=f"{use_case} {doc_type} CloudFormation FortiGate",
            use_case=use_case,
            document_type=doc_type,
            max_results=3,
        )
        kb_results.extend(results)

    kb_context = "\n\n---\n\n".join(
        f"[{r.document_type or 'doc'} | {r.source_uri}]\n{r.text}" for r in kb_results
    ) if kb_results else "No KB documentation available. Use best practices for FortiGate on AWS."

    reference_template = "No reference template available."
    ref_results = kb_search_filtered(
        query=f"{use_case} CloudFormation template cft.json",
        use_case=use_case,
        document_type="cft",
        max_results=1,
    )
    if ref_results:
        reference_template = ref_results[0].text

    return kb_context, reference_template


@bedrock_retry("iac-layer-plan")
def _generate_layer_plan(
    params: ResolvedIaCParameters,
    invocation_state: dict,
    kb_context: str,
) -> LayerPlan:
    """Generate a LayerPlan via the Architecture Planner LLM."""
    system_prompt = _load_prompt("iac_architecture_planner.txt")

    user_prompt = system_prompt.replace(
        "{kb_context}", kb_context
    ).replace(
        "{resolved_params_json}", params.model_dump_json(indent=2)
    )

    model = create_bedrock_model(max_tokens=settings.iac_layer_plan_max_tokens)
    agent = Agent(
        name="iac-layer-plan",
        model=model,
        system_prompt="You produce a JSON layer plan for CloudFormation architecture decomposition.",
        structured_output_model=LayerPlan,
        callback_handler=logging_callback_handler,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=invocation_state)

    plan = getattr(result, "structured_output", None)
    if not isinstance(plan, LayerPlan):
        raise ValueError(
            "LLM did not produce a valid LayerPlan. "
            f"Got: {type(plan).__name__ if plan else 'None'}"
        )
    return plan


def _get_or_generate_layer_plan(
    params: ResolvedIaCParameters,
    invocation_state: dict,
    kb_context: str,
    tenant_id: str,
) -> LayerPlan:
    """Get a cached LayerPlan or generate one via LLM.

    Implements the LLM + cache strategy with double-checked locking:
    first request generates via LLM and caches; subsequent requests
    with the same tenant+pattern reuse the cache.
    """
    from src.config.metrics import metrics

    pattern_key = _normalize_pattern_key(params.deployment_pattern)
    cache_key = f"{tenant_id}:{pattern_key}"

    # Fast path (no lock)
    if cache_key in _LAYER_PLAN_CACHE:
        logger.info("LayerPlan cache hit for '%s'", cache_key)
        metrics.record_layer_plan_cache(hit=True, pattern_name=pattern_key, tenant_id=tenant_id)
        return _LAYER_PLAN_CACHE[cache_key]

    # Slow path (lock + double-check)
    with _LAYER_PLAN_LOCK:
        if cache_key in _LAYER_PLAN_CACHE:
            logger.info("LayerPlan cache hit (after lock) for '%s'", cache_key)
            metrics.record_layer_plan_cache(hit=True, pattern_name=pattern_key, tenant_id=tenant_id)
            return _LAYER_PLAN_CACHE[cache_key]

        logger.info("LayerPlan cache miss for '%s', generating via LLM", cache_key)
        metrics.record_layer_plan_cache(hit=False, pattern_name=pattern_key, tenant_id=tenant_id)

        plan = _generate_layer_plan(params, invocation_state, kb_context)

        _LAYER_PLAN_CACHE[cache_key] = plan
        logger.info("Cached LayerPlan for '%s' (%d layers)", cache_key, len(plan.layers))

    return plan


@bedrock_retry("iac-generate-layer")
def _generate_layer_resources(
    layer_spec: LayerSpec,
    params: ResolvedIaCParameters,
    invocation_state: dict,
    kb_context: str,
    reference_template: str,
    feedback_section: str = "",
) -> ResourcePlan:
    """Generate a ResourcePlan for a single layer via LLM."""
    system_prompt = _load_prompt("iac_layer_generate.txt")

    # Build imports JSON for the prompt
    imports_json = "No imports — this is a root layer." if not layer_spec.imports else "\n".join(
        f"- {imp.parameter_name}: {imp.name} from {imp.source_layer.value} layer"
        + (f" ({imp.description})" if imp.description else "")
        for imp in layer_spec.imports
    )

    # Build exports required
    exports_required = "No exports required." if not layer_spec.exports else "\n".join(
        f"- {exp.resource_logical_id}: {exp.name}"
        + (f" (GetAtt .{exp.attribute})" if exp.attribute else " (Ref)")
        + (f" — {exp.description}" if exp.description else "")
        for exp in layer_spec.exports
    )

    user_prompt = system_prompt.replace(
        "{layer_name}", layer_spec.name.value
    ).replace(
        "{layer_description}", layer_spec.description
    ).replace(
        "{allowed_resource_types}", ", ".join(layer_spec.resource_types)
    ).replace(
        "{imports_json}", imports_json
    ).replace(
        "{exports_required}", exports_required
    ).replace(
        "{layer_prompt_context}", layer_spec.prompt_context or ""
    ).replace(
        "{kb_context}", kb_context
    ).replace(
        "{reference_template}", reference_template
    ).replace(
        "{resolved_params_json}", params.model_dump_json(indent=2)
    ) + feedback_section

    model = create_bedrock_model(max_tokens=settings.iac_layer_generate_max_tokens)
    agent = Agent(
        name="iac-layer-generate",
        model=model,
        system_prompt=f"You produce a JSON resource plan for the {layer_spec.name.value} layer.",
        structured_output_model=ResourcePlan,
        callback_handler=logging_callback_handler,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=invocation_state)

    plan = getattr(result, "structured_output", None)
    if not isinstance(plan, ResourcePlan):
        raise ValueError(
            f"LLM did not produce a valid ResourcePlan for layer {layer_spec.name.value}. "
            f"Got: {type(plan).__name__ if plan else 'None'}"
        )
    return plan


async def _generate_all_layers(
    layer_plan: LayerPlan,
    params: ResolvedIaCParameters,
    invocation_state: dict,
    kb_context: str,
    reference_template: str,
    feedback_section: str = "",
) -> dict[LayerName, ResourcePlan]:
    """Generate ResourcePlans for all layers, respecting dependency groups.

    Layers within the same dependency group run in parallel via asyncio.gather().
    Groups execute sequentially (each group's imports are satisfied by prior groups).
    """
    layer_resource_plans: dict[LayerName, ResourcePlan] = {}
    groups = layer_plan.parallelizable_groups()

    for group_idx, group in enumerate(groups):
        logger.info(
            "Generating layer group %d/%d: %s",
            group_idx + 1, len(groups),
            [s.name.value for s in group],
        )

        async def _gen_one(spec: LayerSpec) -> tuple[LayerName, ResourcePlan]:
            rp = await asyncio.to_thread(
                _generate_layer_resources,
                spec, params, invocation_state,
                kb_context, reference_template, feedback_section,
            )
            return spec.name, rp

        results = await asyncio.gather(*[_gen_one(spec) for spec in group])
        for name, rp in results:
            layer_resource_plans[name] = rp

    return layer_resource_plans


# ---------------------------------------------------------------------------
# Per-layer fix loop helpers (Phase 6)
# ---------------------------------------------------------------------------


def _map_errors_to_layers(
    layer_plan: LayerPlan,
    layer_resource_plans: dict[LayerName, ResourcePlan],
    report: ValidationReport,
) -> dict[LayerName, list[ValidationFinding]]:
    """Route validation errors to their originating layers by logical ID.

    Builds a reverse map: resource_logical_id -> LayerName.
    Errors that can't be attributed go to ALL layers.
    """
    # Build reverse map
    resource_to_layer: dict[str, LayerName] = {}
    for layer_name, rp in layer_resource_plans.items():
        for r in rp.resources:
            resource_to_layer[r.logical_id] = layer_name

    layer_errors: dict[LayerName, list[ValidationFinding]] = {
        name: [] for name in layer_resource_plans
    }

    for finding in report.blocking_findings():
        if finding.resource and finding.resource in resource_to_layer:
            layer_errors[resource_to_layer[finding.resource]].append(finding)
        else:
            # Unattributed error — distribute to all layers
            for name in layer_resource_plans:
                layer_errors[name].append(finding)

    return layer_errors


@bedrock_retry("iac-fix-layer")
def _fix_layer_resource_plan(
    layer_spec: LayerSpec,
    plan: ResourcePlan,
    error_text: str,
    invocation_state: dict,
) -> ResourcePlan:
    """Fix a single layer's ResourcePlan based on validation errors."""
    system_prompt = _load_prompt("iac_layer_fix.txt")

    import_param_names = ", ".join(imp.parameter_name for imp in layer_spec.imports) or "None"
    export_resource_ids = ", ".join(exp.resource_logical_id for exp in layer_spec.exports) or "None"

    user_prompt = system_prompt.replace(
        "{layer_name}", layer_spec.name.value
    ).replace(
        "{import_param_names}", import_param_names
    ).replace(
        "{export_resource_ids}", export_resource_ids
    ).replace(
        "{resource_plan_json}", plan.model_dump_json(indent=2)
    ).replace(
        "{validation_errors}", error_text
    )

    model = create_bedrock_model(max_tokens=settings.iac_layer_fix_max_tokens)
    agent = Agent(
        name="iac-layer-fix",
        model=model,
        system_prompt=f"You fix a JSON resource plan for the {layer_spec.name.value} layer.",
        structured_output_model=ResourcePlan,
        callback_handler=logging_callback_handler,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=invocation_state)

    fixed = getattr(result, "structured_output", None)
    if not isinstance(fixed, ResourcePlan):
        raise ValueError(
            f"LLM did not produce a valid fixed ResourcePlan for layer {layer_spec.name.value}. "
            f"Got: {type(fixed).__name__ if fixed else 'None'}"
        )
    return fixed


# ---------------------------------------------------------------------------
# Fix agent — ResourcePlan-level (Path 3) and YAML fallback (Paths 1 & 2)
# ---------------------------------------------------------------------------


_ERROR_PRIORITY_HEADER = """\
## Error Priority (fix in this order):
1. structural errors (YAML syntax, required keys) — MUST fix
2. cfn-lint errors (property names, types, required fields) — MUST fix
3. cfn-guard errors (FortiGate best practices) — fix if possible
4. checkov warnings (security findings) — fix unless FortiGate-intentional

"""


def _format_errors(findings: list) -> str:
    """Format validation findings into a text block for the fix prompt."""
    lines = [_ERROR_PRIORITY_HEADER]
    lines.extend(
        f"- [{f.layer}|{f.severity}] {f.rule_id}: {f.message}"
        + (f" (resource: {f.resource})" if f.resource else "")
        + (f" (line: {f.line})" if f.line else "")
        for f in findings
    )
    return "\n".join(lines)


@bedrock_retry("iac-fix")
def _fix_template_yaml(
    template_str: str,
    report: ValidationReport,
    invocation_state: dict,
) -> str:
    """YAML-level fix fallback for Paths 1 & 2.

    Gives the LLM the full template + errors and asks for a fixed
    ResourcePlan, then re-assembles.  Falls back to returning the
    original template on any failure.
    """
    errors = [f for f in report.findings if f.severity == "error"]
    if not errors:
        return template_str

    error_text = _format_errors(errors)
    system_prompt = _load_prompt("iac_fix.txt")

    # Try to convert the existing YAML into a ResourcePlan-like summary
    # for the fix prompt so the LLM stays in structured JSON mode.
    try:
        parsed = cfn_load(template_str)
        if not isinstance(parsed, dict) or "Resources" not in parsed:
            raise ValueError("Not a valid CFN template")

        # Build a lightweight resource summary
        resources_summary = {}
        for lid, rdef in (parsed.get("Resources", {}) or {}).items():
            if isinstance(rdef, dict):
                resources_summary[lid] = {
                    "Type": rdef.get("Type", "?"),
                    "Properties": "(see errors)",
                }

        summary_text = (
            f"Template has {len(resources_summary)} resources: "
            + ", ".join(f"{lid} ({info['Type']})" for lid, info in resources_summary.items())
        )
    except Exception:
        summary_text = "Could not parse template for summary."

    # Use the fix prompt but provide the full template as the plan
    user_prompt = system_prompt.replace(
        "{resource_plan_json}", f"(YAML template — see resource summary below)\n{summary_text}\n\nFull template:\n{template_str}"
    ).replace(
        "{validation_errors}", error_text
    )

    model = create_bedrock_model(max_tokens=settings.iac_fix_max_tokens)
    agent = Agent(
        name="iac-fix",
        model=model,
        system_prompt="You fix a JSON resource plan based on validation errors.",
        structured_output_model=ResourcePlan,
        callback_handler=logging_callback_handler,
    )
    result = bedrock_breaker.call(agent, user_prompt, invocation_state=invocation_state)

    fixed_plan = getattr(result, "structured_output", None)
    if isinstance(fixed_plan, ResourcePlan):
        return assemble(fixed_plan)

    logger.warning("YAML-level fix failed to produce ResourcePlan, returning original")
    return template_str


# ---------------------------------------------------------------------------
# Feedback context builder
# ---------------------------------------------------------------------------


def _build_feedback_section(
    feedback: str | None,
    previous_validation_summary: str | None = None,
) -> str:
    """Build a feedback context block to append to generation prompts."""
    if not feedback:
        return ""
    parts = [f"\n\n## User Feedback on Previous Generation\n{feedback}"]
    if previous_validation_summary:
        parts.append(f"\n\n## Previous Validation Report\n{previous_validation_summary}")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main generation pipeline
# ---------------------------------------------------------------------------


async def generate_iac(
    params: ResolvedIaCParameters,
    tenant_id: str,
    project_id: str,
    feedback: str | None = None,
    previous_validation_summary: str | None = None,
) -> IaCOutput:
    """Run the full IaC generation pipeline: resolve → generate → validate → fix.

    Produces a single CloudFormation template file validated through a 4-layer
    pipeline (structural, cfn-lint, checkov, cfn-guard) with up to 3 fix attempts.

    The function signature and return type are unchanged from the previous
    implementation, making this a drop-in replacement.
    """
    bedrock_breaker.pre_check()
    start = time.perf_counter()
    invocation_state = {"tenant_id": tenant_id, "project_id": project_id}
    feedback_section = _build_feedback_section(feedback, previous_validation_summary)

    if feedback:
        logger.info("IaC regeneration with feedback for project %s", project_id)

    # Step 1: Resolve template path (unchanged)
    path, snippets_by_type = resolve_template_path(params)
    logger.info("IaC generation: path=%s project=%s", path.value, project_id)

    # Step 2: Generate template
    resource_plan: ResourcePlan | None = None
    layer_plan: LayerPlan | None = None
    layer_resource_plans: dict[LayerName, ResourcePlan] | None = None

    if path == TemplatePath.PARAMETERIZE:
        # Path 1: Pure Python — no LLM, no Bedrock call
        template = await asyncio.to_thread(_parameterize_template, params)
    elif path == TemplatePath.COMPOSE:
        # Path 2: LLM generates SnippetAssemblyPlan, code merges snippets
        template = await asyncio.to_thread(
            _compose_snippets, params, snippets_by_type,
            invocation_state, feedback_section,
        )
    else:
        # Path 3: Layered generation pipeline
        try:
            # Step A: Fetch KB context (shared across all layers)
            kb_context, reference_template = await asyncio.to_thread(
                _get_kb_context, params.deployment_pattern,
            )

            # Step B: Get LayerPlan (cached or LLM-generated)
            layer_plan = await asyncio.to_thread(
                _get_or_generate_layer_plan,
                params, invocation_state, kb_context, tenant_id,
            )

            from src.config.metrics import metrics as _metrics
            _metrics.record_layer_count(len(layer_plan.layers), layer_plan.pattern_name, tenant_id)

            # Step C: Generate all layers (parallel within dependency groups)
            layer_resource_plans = await _generate_all_layers(
                layer_plan, params, invocation_state,
                kb_context, reference_template, feedback_section,
            )

            # Step D: Pre-assembly spec validation per layer (fix if needed)
            try:
                from src.validation.spec_validator import validate_resource_plan as _validate_spec
                for layer_name, rp in list(layer_resource_plans.items()):
                    spec_findings = _validate_spec(rp, region=params.region)
                    spec_errors = [f for f in spec_findings if f.severity == "error"]
                    if spec_errors:
                        logger.info(
                            "Spec validation found %d errors in layer %s, fixing",
                            len(spec_errors), layer_name.value,
                        )
                        error_text = _format_errors(spec_errors)
                        layer_spec = layer_plan.get_layer(layer_name)
                        if layer_spec:
                            layer_resource_plans[layer_name] = await asyncio.to_thread(
                                _fix_layer_resource_plan,
                                layer_spec, rp, error_text, invocation_state,
                            )
            except ImportError:
                logger.debug("spec_validator not available, skipping pre-assembly validation")

            # Step E: Merge + assemble JSON
            resource_plan = merge_layers(layer_plan, layer_resource_plans)
            template = assemble_json(resource_plan)

        except (ValueError, TypeError) as exc:
            # Graceful fallback: LLM failed to produce valid structured output.
            # Return a failed IaCOutput rather than crashing the worker.
            logger.error("Path 3 layered generation failed: %s", exc, exc_info=True)
            duration_ms = int((time.perf_counter() - start) * 1000)
            from src.config.metrics import metrics as _metrics
            _metrics.record_latency("iac", duration_ms, tenant_id)
            _metrics.record_iac_path(path.value, tenant_id)
            return IaCOutput(
                files={},
                validation_report=ValidationReport(
                    passed=False,
                    findings=[ValidationFinding(
                        rule_id="GENERATION_FAILED",
                        severity="error",
                        message=f"Layered generation failed: {exc}",
                        layer="iac-layer-plan",
                    )],
                    layers_executed=[],
                ),
                template_resolution_path=path.value,
                generation_duration_ms=duration_ms,
            )

    # Step 3: Validation-fix loop with best-version tracking
    report = None
    best_template = template
    best_blocking: float = float("inf")
    best_plan = resource_plan

    for attempt in range(1, _MAX_FIX_ATTEMPTS + 1):
        logger.info("Validation attempt %d/%d for project %s",
                     attempt, _MAX_FIX_ATTEMPTS, project_id)

        report = await run_validation_pipeline(template, region=params.region)
        report.fix_attempts = attempt
        blocking = report.blocking_error_count()

        # Track best version
        if blocking < best_blocking:
            best_blocking = blocking
            best_template = template
            best_plan = resource_plan

        if not report.has_blocking_errors():
            logger.info("Validation passed on attempt %d (non-blocking findings: %d)",
                        attempt, len(report.non_blocking_findings()))
            break

        if attempt < _MAX_FIX_ATTEMPTS:
            logger.info("Validation found %d blocking errors, attempting fix", blocking)

            if layer_plan is not None and layer_resource_plans is not None:
                # Path 3 (layered): Route errors to layers, fix per-layer, re-merge
                errors_by_layer = _map_errors_to_layers(
                    layer_plan, layer_resource_plans, report,
                )
                for lname, layer_errors in errors_by_layer.items():
                    if not layer_errors:
                        continue
                    layer_spec = layer_plan.get_layer(lname)
                    if layer_spec is None:
                        continue
                    error_text = _format_errors(layer_errors)
                    layer_resource_plans[lname] = await asyncio.to_thread(
                        _fix_layer_resource_plan,
                        layer_spec, layer_resource_plans[lname],
                        error_text, invocation_state,
                    )
                resource_plan = merge_layers(layer_plan, layer_resource_plans)
                fixed = assemble_json(resource_plan)
            else:
                # Paths 1 & 2: YAML-level fix fallback
                fixed = await asyncio.to_thread(
                    _fix_template_yaml, template, report, invocation_state
                )

            # Check if fix made things worse
            check_report = await run_validation_pipeline(fixed, region=params.region)
            new_blocking = check_report.blocking_error_count()

            if new_blocking > blocking:
                logger.warning(
                    "Fix attempt %d increased blocking errors (%d -> %d), reverting",
                    attempt, blocking, new_blocking,
                )
                template = best_template
                resource_plan = best_plan
                report.fix_attempts = attempt
                break

            # Accept the fix
            template = fixed
            report = check_report
            report.fix_attempts = attempt

            if new_blocking < best_blocking:
                best_blocking = new_blocking
                best_template = template
                best_plan = resource_plan
    else:
        # Exhausted all attempts — use best version if current is worse
        if report and report.blocking_error_count() > best_blocking:
            logger.info("Using best version from attempt history (%d blocking vs current %d)",
                        best_blocking, report.blocking_error_count())
            template = best_template
            report = await run_validation_pipeline(template, region=params.region)
            report.fix_attempts = _MAX_FIX_ATTEMPTS

    duration_ms = int((time.perf_counter() - start) * 1000)

    # Record metrics
    from src.config.metrics import metrics
    metrics.record_latency("iac", duration_ms, tenant_id)
    metrics.record_iac_path(path.value, tenant_id)
    if report:
        metrics.record_validation_result(
            report.passed, report.fix_attempts, path.value, tenant_id,
        )
        layer_errors: dict[str, int] = {}
        for f in report.findings:
            if f.severity == "error":
                layer_errors[f.layer] = layer_errors.get(f.layer, 0) + 1
        for layer, count in layer_errors.items():
            metrics.record_validation_layer_errors(layer, count, tenant_id)

    # Path 3 produces CloudFormation JSON; Paths 1 & 2 produce YAML
    filename = "template.json" if path == TemplatePath.GENERATE else "template.yaml"

    return IaCOutput(
        files={filename: template},
        validation_report=report or ValidationReport(
            passed=False, findings=[], layers_executed=[],
        ),
        template_resolution_path=path.value,
        generation_duration_ms=duration_ms,
    )
