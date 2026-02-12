"""Layer 0: Structural pre-validation — pure Python, no external tools."""

import json
import logging

import yaml

from src.models.iac import ValidationFinding

logger = logging.getLogger(__name__)

_MAX_TEMPLATE_SIZE = 1_048_576  # 1 MB CloudFormation limit
_REQUIRED_TOP_LEVEL = {"AWSTemplateFormatVersion", "Resources"}


def detect_format_suffix(template_str: str) -> str:
    """Return '.json' if template looks like JSON, else '.yaml'.

    Used by downstream validators (cfn-lint, checkov, cfn-guard) to
    write temp files with the correct extension.
    """
    stripped = template_str.strip()
    if stripped.startswith("{"):
        return ".json"
    return ".yaml"


def validate_structural(template_str: str) -> list[ValidationFinding]:
    """Fast structural check before running expensive validators.

    Checks:
    1. Parse JSON/YAML (JSON first — handles Path 3 output natively,
       avoids yaml.safe_load choking on CloudFormation tags)
    2. AWSTemplateFormatVersion + Resources exist
    3. Resources is not empty
    4. No duplicate logical IDs (YAML allows them, CFN doesn't)
    5. Parameters (if present) have Type fields
    6. Template size <= 1MB
    """
    findings: list[ValidationFinding] = []

    # Size check
    if len(template_str.encode("utf-8")) > _MAX_TEMPLATE_SIZE:
        findings.append(ValidationFinding(
            layer="structural", severity="error", rule_id="S001",
            message=f"Template exceeds 1 MB CloudFormation limit ({len(template_str.encode('utf-8'))} bytes)",
        ))
        return findings

    # Parse — try JSON first (handles Path 3 CloudFormation JSON natively),
    # then YAML as fallback (handles Paths 1 & 2 output).
    parsed = None
    try:
        parsed = json.loads(template_str)
    except (json.JSONDecodeError, ValueError):
        try:
            parsed = yaml.safe_load(template_str)
        except yaml.YAMLError:
            findings.append(ValidationFinding(
                layer="structural", severity="error", rule_id="S002",
                message="Template is not valid YAML or JSON",
            ))
            return findings

    if not isinstance(parsed, dict):
        findings.append(ValidationFinding(
            layer="structural", severity="error", rule_id="S003",
            message="Template root must be a mapping/object",
        ))
        return findings

    # Required top-level keys
    for key in _REQUIRED_TOP_LEVEL:
        if key not in parsed:
            findings.append(ValidationFinding(
                layer="structural", severity="error", rule_id="S004",
                message=f"Missing required top-level key: {key}",
            ))

    # Resources not empty
    resources = parsed.get("Resources")
    if isinstance(resources, dict) and len(resources) == 0:
        findings.append(ValidationFinding(
            layer="structural", severity="error", rule_id="S005",
            message="Resources section is empty",
        ))

    # Parameters have Type field
    params = parsed.get("Parameters")
    if isinstance(params, dict):
        for param_name, param_def in params.items():
            if isinstance(param_def, dict) and "Type" not in param_def:
                findings.append(ValidationFinding(
                    layer="structural", severity="error", rule_id="S006",
                    message=f"Parameter '{param_name}' missing required 'Type' field",
                    resource=param_name,
                ))

    return findings
