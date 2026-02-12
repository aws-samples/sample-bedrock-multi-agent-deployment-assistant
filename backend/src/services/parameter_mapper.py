"""Parameter mapper — extracts CFN parameter defaults from ResolvedIaCParameters.

Used by Path 1 (PARAMETERIZE) to map resolved deployment values to
CloudFormation Parameter Default values.  Pure Python, no LLM.

The mapper uses heuristic name-matching against the template's existing
Parameter logical IDs and descriptions.  Unmatched parameters retain
their original defaults from the KB template.
"""

import logging

from src.models.design import ResolvedIaCParameters
from src.utils.cfn_yaml import cfn_load

logger = logging.getLogger(__name__)


def _normalise(name: str) -> str:
    """Lowercase and strip separators for fuzzy matching."""
    return name.lower().replace("-", "").replace("_", "")


def build_parameter_defaults(
    params: ResolvedIaCParameters,
    template_str: str,
) -> dict[str, str]:
    """Analyse existing template parameters and map resolved values.

    Examines parameter names and descriptions to determine the correct
    mapping from ``ResolvedIaCParameters`` fields.

    Args:
        params: Fully resolved deployment parameters from the design step.
        template_str: Raw CFN template YAML/JSON string.

    Returns:
        Mapping of parameter logical ID -> default value string.
        Only contains entries for parameters that could be matched.
    """
    parsed = cfn_load(template_str)
    if not isinstance(parsed, dict):
        return {}

    cfn_params = parsed.get("Parameters", {})
    if not isinstance(cfn_params, dict):
        return {}

    # Build lookup tables from resolved params
    subnet_cidrs: dict[str, str] = {}
    for vpc in params.vpcs:
        for subnet in vpc.subnets:
            subnet_cidrs[_normalise(subnet.name)] = subnet.cidr
            # Also index by role for broader matching
            key = f"{_normalise(vpc.role)}{_normalise(subnet.role)}"
            subnet_cidrs[key] = subnet.cidr

    fgt_ips: dict[str, str] = {}
    for fgt in params.fortigate_instances:
        for iface in fgt.interfaces:
            fgt_ips[_normalise(f"{fgt.name}{iface.port_name}")] = iface.private_ip
            fgt_ips[_normalise(f"{fgt.role}{iface.port_name}")] = iface.private_ip

    defaults: dict[str, str] = {}

    for param_name, param_def in cfn_params.items():
        norm = _normalise(param_name)
        desc = ""
        if isinstance(param_def, dict):
            desc = _normalise(param_def.get("Description", "") or "")

        matched = False

        # --- Region ---
        if not matched and ("region" in norm):
            defaults[param_name] = params.region
            matched = True

        # --- Availability zones ---
        if not matched and ("az" in norm or "availabilityzone" in norm):
            for i, az in enumerate(params.availability_zones):
                if str(i + 1) in param_name or f"az{i + 1}" in norm:
                    defaults[param_name] = az
                    matched = True
                    break
            if not matched and params.availability_zones:
                defaults[param_name] = params.availability_zones[0]
                matched = True

        # --- VPC CIDR ---
        if not matched and "cidr" in norm and ("vpc" in norm or "vpc" in desc):
            # Try matching by VPC role
            for vpc in params.vpcs:
                if _normalise(vpc.role) in norm or _normalise(vpc.name) in norm:
                    defaults[param_name] = vpc.cidr
                    matched = True
                    break
            if not matched and params.vpcs:
                defaults[param_name] = params.vpcs[0].cidr
                matched = True

        # --- Subnet CIDRs ---
        if not matched and "cidr" in norm and "subnet" in norm:
            for skey, scidr in subnet_cidrs.items():
                if skey in norm:
                    defaults[param_name] = scidr
                    matched = True
                    break

        # --- FortiGate IPs ---
        if not matched and ("ip" in norm or "address" in norm) and (
            "fortigate" in norm or "fgt" in norm or "fortigate" in desc or "fgt" in desc
        ):
            for fkey, fip in fgt_ips.items():
                if fkey in norm:
                    defaults[param_name] = fip
                    matched = True
                    break

        # --- Instance type ---
        if not matched and "instancetype" in norm:
            if params.fortigate_instances:
                defaults[param_name] = params.fortigate_instances[0].instance_type
                matched = True

        # --- Project name ---
        if not matched and "project" in norm and ("name" in norm or "name" in desc):
            defaults[param_name] = params.project_name
            matched = True

        # --- Environment ---
        if not matched and ("environment" in norm or norm == "env"):
            defaults[param_name] = params.environment
            matched = True

        # --- Tags ---
        if not matched and "tag" in norm:
            for tag_key, tag_val in params.tags.items():
                if _normalise(tag_key) in norm:
                    defaults[param_name] = tag_val
                    matched = True
                    break

        # --- Pattern-specific from additional_resolved ---
        if not matched:
            for key, val in params.additional_resolved.items():
                if _normalise(key) in norm:
                    defaults[param_name] = str(val)
                    matched = True
                    break

        if matched:
            logger.debug("Mapped parameter %s -> %s", param_name, defaults[param_name])

    logger.info(
        "Parameter mapper: matched %d/%d parameters",
        len(defaults), len(cfn_params),
    )
    return defaults
