"""Refinement plan generator — identifies deployment parameters from KB + code templates.

After a user selects a design option, this service analyses KB configuration docs
and (optionally) the matching code template's Parameters section to build a
RefinementPlan: the set of deployment parameters the user needs to provide before
IaC generation.

The flow:
1. Always include base fields (aws_region, vpc_cidr, environment, project_name).
2. If the design has a code template, fetch it from S3 and parse YAML Parameters.
3. Query KB for configuration docs for the deployment pattern.
4. Invoke Haiku (lightweight model) to merge template params + KB guidance into
   pattern-specific RefinementField entries.
5. Prepend base fields to the LLM-generated fields and return the complete plan.
"""

import logging
import time
from pathlib import Path

import boto3
import yaml
from strands import Agent

from src.agents.common import bedrock_retry, create_bedrock_model
from src.config.callback import logging_callback_handler
from src.config.circuit_breaker import bedrock_breaker
from src.config.settings import settings
from src.models.design import (
    DesignOption,
    KBReference,
    RefinementField,
    RefinementPlan,
)
from src.models.requirements import InterviewOutput
from src.tools.kb_search import KBResult, kb_search_filtered

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_refinement_prompt_template = (PROMPTS_DIR / "refinement.txt").read_text()


# ---------------------------------------------------------------------------
# Base fields — always present as the first entries in every RefinementPlan
# ---------------------------------------------------------------------------

_BASE_FIELDS: list[RefinementField] = [
    RefinementField(
        field_name="aws_region",
        label="AWS Region",
        description="AWS region for the deployment (e.g., us-east-1, eu-west-1).",
        required=True,
        default_value="us-east-1",
        default_rationale="Most common region with full service availability.",
        input_type="select",
        options=[
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            "eu-west-1", "eu-west-2", "eu-central-1",
            "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
            "ca-central-1", "sa-east-1",
        ],
    ),
    RefinementField(
        field_name="vpc_cidr",
        label="VPC CIDR Block",
        description="Primary VPC CIDR block for the deployment (e.g., 10.0.0.0/16).",
        required=True,
        default_value="10.0.0.0/16",
        default_rationale="Standard /16 provides 65,536 addresses for subnet allocation.",
        input_type="cidr",
        validation_pattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$",
    ),
    RefinementField(
        field_name="environment",
        label="Environment",
        description="Deployment environment for resource naming and tagging.",
        required=True,
        default_value="production",
        default_rationale="Default to production; override for dev/staging deployments.",
        input_type="select",
        options=["production", "staging", "dev"],
    ),
    RefinementField(
        field_name="project_name",
        label="Project Name",
        description="Project name used for resource naming and tagging across all AWS resources.",
        required=True,
        input_type="text",
        validation_pattern=r"^[a-zA-Z][a-zA-Z0-9-]{1,62}$",
    ),
]


# ---------------------------------------------------------------------------
# S3 template fetching
# ---------------------------------------------------------------------------


def _fetch_template_parameters(s3_prefix: str) -> tuple[list[str], str]:
    """Fetch a code template from S3 and extract YAML Parameters.

    Returns:
        Tuple of (parameter_names, raw_parameters_yaml).
        Returns ([], "") on any failure.
    """
    try:
        s3 = boto3.client("s3", region_name=settings.aws_region)
        bucket = settings.s3_knowledge_base_bucket

        # List objects under the prefix to find the main template file
        response = s3.list_objects_v2(Bucket=bucket, Prefix=s3_prefix, MaxKeys=20)
        contents = response.get("Contents", [])

        if not contents:
            logger.warning(
                "No objects found under s3://%s/%s", bucket, s3_prefix
            )
            return [], ""

        # Look for common template filenames
        template_keys = [
            obj["Key"] for obj in contents
            if obj["Key"].endswith((".yaml", ".yml", ".tf", ".json"))
        ]
        if not template_keys:
            logger.warning(
                "No template files found under s3://%s/%s", bucket, s3_prefix
            )
            return [], ""

        # Prefer main.yaml / template.yaml / variables.tf
        priority_names = ("main.yaml", "template.yaml", "main.yml", "template.yml", "variables.tf")
        selected_key = template_keys[0]
        for key in template_keys:
            filename = key.rsplit("/", 1)[-1]
            if filename in priority_names:
                selected_key = key
                break

        logger.info("Fetching template from s3://%s/%s", bucket, selected_key)
        obj = s3.get_object(Bucket=bucket, Key=selected_key)
        body = obj["Body"].read().decode("utf-8")

        return _parse_template_parameters(body)

    except Exception:
        logger.warning(
            "Failed to fetch template from S3 prefix %s", s3_prefix, exc_info=True
        )
        return [], ""


def _parse_template_parameters(template_body: str) -> tuple[list[str], str]:
    """Parse YAML/CloudFormation template and extract the Parameters section.

    Returns:
        Tuple of (parameter_names, raw_parameters_text).
    """
    try:
        docs = yaml.safe_load(template_body)
        if not isinstance(docs, dict):
            return [], ""

        # CloudFormation-style Parameters section
        parameters = docs.get("Parameters") or docs.get("parameters") or {}
        if not isinstance(parameters, dict):
            return [], ""

        if not parameters:
            # Try Terraform-style: top-level "variable" blocks
            variables = docs.get("variable") or docs.get("variables") or {}
            if isinstance(variables, dict) and variables:
                param_names = list(variables.keys())
                raw_text = yaml.dump(variables, default_flow_style=False)
                logger.info("Parsed %d Terraform variables from template", len(param_names))
                return param_names, raw_text
            return [], ""

        param_names = list(parameters.keys())
        raw_text = yaml.dump(parameters, default_flow_style=False)
        logger.info("Parsed %d parameters from template", len(param_names))
        return param_names, raw_text

    except yaml.YAMLError:
        logger.warning("Failed to parse template YAML", exc_info=True)
        return [], ""


# ---------------------------------------------------------------------------
# KB search
# ---------------------------------------------------------------------------


def _search_kb_for_configuration(
    design: DesignOption,
) -> list[KBResult]:
    """Query KB for configuration docs relevant to the deployment pattern."""
    try:
        query = f"configuration parameters for {design.deployment_pattern}"
        results = kb_search_filtered(
            query,
            document_type="configuration",
            max_results=5,
        )
        logger.info(
            "KB configuration search returned %d results for pattern %r",
            len(results),
            design.deployment_pattern,
        )
        return results
    except Exception:
        logger.warning(
            "KB search failed for pattern %r", design.deployment_pattern, exc_info=True
        )
        return []


def _format_kb_results(results: list[KBResult]) -> str:
    """Format KB results for prompt injection."""
    if not results:
        return "No knowledge base configuration docs available."
    parts = []
    for r in results:
        parts.append(f"[Source: {r.source_uri} | Score: {r.score:.2f}]\n{r.text}")
    return "\n---\n".join(parts)


def _kb_results_to_references(results: list[KBResult]) -> list[KBReference]:
    """Convert KBResult list to KBReference list for the RefinementPlan."""
    return [
        KBReference(
            source_uri=r.source_uri,
            excerpt=r.text[:500],
            relevance_score=r.score,
        )
        for r in results
        if r.source_uri
    ]


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _build_refinement_prompt(
    design: DesignOption,
    requirements: InterviewOutput,
    kb_results: list[KBResult],
    template_param_names: list[str],
    template_params_text: str,
) -> str:
    """Build the user prompt for Haiku refinement analysis."""
    design_summary = (
        f"Design: {design.name}\n"
        f"Deployment Pattern: {design.deployment_pattern}\n"
        f"Use Case: {design.use_case}\n"
        f"HA Mode: {design.ha_mode}\n"
        f"Instance Type: {design.fortigate_instance_type}\n"
        f"AWS Services: {', '.join(design.aws_services)}\n"
        f"Architecture: {design.architecture_summary}\n"
        f"Has Code Template: {design.has_code_template}"
    )

    requirements_summary = (
        f"Use Cases: {', '.join(uc.value for uc in requirements.use_cases)}\n"
        f"Routing Protocol: {requirements.cloud_routing_protocol.value}\n"
        f"Resilience: {requirements.resilience.value}\n"
        f"Bandwidth: {requirements.bandwidth} Mbps\n"
        f"Description: {requirements.solution_description}"
    )

    template_section = "No code template available."
    if template_param_names:
        template_section = (
            f"Template parameter names: {', '.join(template_param_names)}\n\n"
            f"Template parameters YAML:\n```yaml\n{template_params_text}\n```"
        )

    return (
        f"## Design Summary\n\n{design_summary}\n\n"
        f"## Requirements Summary\n\n{requirements_summary}\n\n"
        f"## Knowledge Base Configuration Docs\n\n{_format_kb_results(kb_results)}\n\n"
        f"## Code Template Parameters\n\n{template_section}\n\n"
        "Analyze the above and generate the RefinementPlan with pattern-specific "
        "deployment parameters. Remember: do NOT include aws_region, vpc_cidr, "
        "environment, or project_name — those are added automatically."
    )


# ---------------------------------------------------------------------------
# LLM invocation
# ---------------------------------------------------------------------------


def _create_lightweight_model():
    """Create a BedrockModel configured for the lightweight (Haiku) model.

    Uses the full max_tokens limit (not interview_max_tokens) because the
    RefinementPlan structured output can be large — many fields with
    descriptions, defaults, options, and KB references.
    """
    return create_bedrock_model(settings.max_tokens, lightweight=True)


@bedrock_retry("refinement-planner")
def _invoke_refinement_agent(agent: Agent, prompt: str) -> object:
    """Invoke the refinement agent with retry and circuit breaker."""
    return bedrock_breaker.call(agent, prompt)


def _generate_llm_refinement(
    design: DesignOption,
    requirements: InterviewOutput,
    kb_results: list[KBResult],
    template_param_names: list[str],
    template_params_text: str,
    tenant_id: str = "default",
) -> RefinementPlan | None:
    """Invoke Haiku to generate pattern-specific refinement fields.

    Returns None if the LLM fails to produce valid structured output.
    """
    from src.config.metrics import metrics

    user_prompt = _build_refinement_prompt(
        design, requirements, kb_results, template_param_names, template_params_text
    )

    agent = Agent(
        name="refinement-planner",
        model=_create_lightweight_model(),
        system_prompt=_refinement_prompt_template,
        tools=[],
        structured_output_model=RefinementPlan,
        callback_handler=logging_callback_handler,
    )

    start = time.perf_counter()
    result = _invoke_refinement_agent(agent, user_prompt)
    duration_ms = (time.perf_counter() - start) * 1000
    metrics.record_latency("refinement-planner", duration_ms, tenant_id)

    output = getattr(result, "structured_output", None)
    if not isinstance(output, RefinementPlan):
        logger.warning(
            "Refinement agent did not return structured output (got %s)",
            type(output).__name__ if output else "None",
        )
        return None

    logger.info(
        "Refinement plan generated: %d fields, %d template params found",
        len(output.fields),
        len(output.template_parameters_found),
    )
    return output


# ---------------------------------------------------------------------------
# Fallback plan
# ---------------------------------------------------------------------------


def _base_only_plan(
    kb_results: list[KBResult] | None = None,
    template_param_names: list[str] | None = None,
) -> RefinementPlan:
    """Return a minimal RefinementPlan with only the base fields.

    Used as a fallback when KB or LLM is unavailable.
    """
    return RefinementPlan(
        fields=list(_BASE_FIELDS),
        kb_configuration_notes="Configuration guidance unavailable. Base deployment parameters only.",
        template_parameters_found=template_param_names or [],
        kb_references=_kb_results_to_references(kb_results) if kb_results else [],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_refinement_plan(
    design: DesignOption,
    requirements: InterviewOutput,
    tenant_id: str = "default",
) -> RefinementPlan:
    """Generate a RefinementPlan for the selected design option.

    Analyses KB configuration docs and (optionally) the code template's
    Parameters section to determine what deployment parameters the user
    needs to provide.

    The returned plan always starts with base fields (aws_region, vpc_cidr,
    environment, project_name), followed by pattern-specific fields derived
    from KB + template analysis.

    Args:
        design: The selected DesignOption from the design recommendation.
        requirements: The InterviewOutput from the requirements gathering phase.

    Returns:
        A RefinementPlan with all fields the user needs to fill in.
    """
    logger.info(
        "Generating refinement plan for design=%r pattern=%r has_template=%s",
        design.name,
        design.deployment_pattern,
        design.has_code_template,
    )

    # Step 1: Fetch template parameters from S3 (if applicable)
    template_param_names: list[str] = []
    template_params_text: str = ""

    if design.has_code_template and design.template_s3_prefix:
        template_param_names, template_params_text = _fetch_template_parameters(
            design.template_s3_prefix
        )
        if template_param_names:
            logger.info(
                "Found %d template parameters: %s",
                len(template_param_names),
                ", ".join(template_param_names[:10]),
            )
        else:
            logger.warning(
                "Design has code template but no parameters could be extracted"
            )

    # Step 2: Query KB for configuration docs
    kb_results = _search_kb_for_configuration(design)

    # Step 3: Invoke Haiku to merge template params + KB guidance
    try:
        llm_plan = _generate_llm_refinement(
            design=design,
            requirements=requirements,
            kb_results=kb_results,
            template_param_names=template_param_names,
            template_params_text=template_params_text,
            tenant_id=tenant_id,
        )
    except Exception:
        logger.warning(
            "LLM refinement generation failed, returning base fields only",
            exc_info=True,
        )
        return _base_only_plan(kb_results, template_param_names)

    if llm_plan is None:
        logger.warning("LLM returned no structured output, returning base fields only")
        return _base_only_plan(kb_results, template_param_names)

    # Step 4: Filter out any base fields the LLM may have included despite instructions
    base_field_names = {f.field_name for f in _BASE_FIELDS}
    filtered_fields = [
        f for f in llm_plan.fields
        if f.field_name not in base_field_names
    ]

    # Step 5: Prepend base fields to LLM-generated fields
    combined_fields = list(_BASE_FIELDS) + filtered_fields

    # Step 6: Ensure KB references include results from our search
    kb_refs = llm_plan.kb_references or []
    search_refs = _kb_results_to_references(kb_results)
    existing_uris = {ref.source_uri for ref in kb_refs}
    for ref in search_refs:
        if ref.source_uri not in existing_uris:
            kb_refs.append(ref)

    # Step 7: Ensure template_parameters_found reflects what we actually found
    merged_template_params = list(set(
        llm_plan.template_parameters_found + template_param_names
    ))

    plan = RefinementPlan(
        fields=combined_fields,
        kb_configuration_notes=llm_plan.kb_configuration_notes,
        template_parameters_found=merged_template_params,
        kb_references=kb_refs,
    )

    logger.info(
        "Refinement plan complete: %d total fields (%d base + %d pattern-specific)",
        len(plan.fields),
        len(_BASE_FIELDS),
        len(filtered_fields),
    )
    return plan
