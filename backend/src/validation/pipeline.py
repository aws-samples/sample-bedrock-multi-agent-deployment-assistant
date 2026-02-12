"""Validation pipeline orchestrator — runs all 4 layers sequentially."""

import asyncio
import logging

from src.config.settings import settings
from src.models.iac import ValidationFinding, ValidationReport
from src.validation.cfn_guard import validate_cfn_guard
from src.validation.cfn_lint_local import validate_cfn_lint
from src.validation.checkov_local import validate_checkov
from src.validation.structural import validate_structural

logger = logging.getLogger(__name__)

# Per-layer timeouts
_TIMEOUTS = {
    "structural": 5,
    "cfn-lint": 30,
    "checkov": 60,
    "cfn-guard": 15,
}

# Only these layers produce blocking errors that prevent output
_BLOCKING_LAYERS = frozenset({"structural", "cfn-lint"})


async def run_validation_pipeline(
    template_str: str,
    region: str = "us-east-1",
) -> ValidationReport:
    """Run all validation layers sequentially. Stops on structural failure.

    Layer 0 (structural) is a gate — if it fails, downstream layers are skipped.
    Layers 1-3 always run (even if prior layers have warnings), collecting all findings.

    Only structural and cfn-lint errors are considered blocking. checkov and cfn-guard
    findings are reported but do not prevent the template from being accepted.
    """
    all_findings: list[ValidationFinding] = []
    layers_executed: list[str] = []

    # Layer 0: Structural (sync, fast)
    try:
        structural_findings = await asyncio.wait_for(
            asyncio.to_thread(validate_structural, template_str),
            timeout=_TIMEOUTS["structural"],
        )
        all_findings.extend(structural_findings)
        layers_executed.append("structural")

        # Gate: structural errors block downstream
        if any(f.severity == "error" for f in structural_findings):
            logger.info("Structural validation failed — skipping downstream layers")
            return ValidationReport(
                passed=False,
                findings=all_findings,
                layers_executed=layers_executed,
            )
    except asyncio.TimeoutError:
        all_findings.append(ValidationFinding(
            layer="structural", severity="warning", rule_id="TIMEOUT",
            message="Structural validation timed out",
        ))

    # Layer 1: cfn-lint
    try:
        cfn_lint_findings = await asyncio.wait_for(
            asyncio.to_thread(validate_cfn_lint, template_str, region),
            timeout=_TIMEOUTS["cfn-lint"],
        )
        all_findings.extend(cfn_lint_findings)
        layers_executed.append("cfn-lint")
    except asyncio.TimeoutError:
        all_findings.append(ValidationFinding(
            layer="cfn-lint", severity="warning", rule_id="TIMEOUT",
            message="cfn-lint validation timed out",
        ))

    # Layer 2: checkov
    try:
        checkov_findings = await asyncio.wait_for(
            asyncio.to_thread(
                validate_checkov, template_str, settings.checkov_skip_checks
            ),
            timeout=_TIMEOUTS["checkov"],
        )
        all_findings.extend(checkov_findings)
        layers_executed.append("checkov")
    except asyncio.TimeoutError:
        all_findings.append(ValidationFinding(
            layer="checkov", severity="warning", rule_id="TIMEOUT",
            message="checkov validation timed out",
        ))

    # Layer 3: cfn-guard
    try:
        guard_findings = await asyncio.wait_for(
            asyncio.to_thread(
                validate_cfn_guard, template_str, settings.cfn_guard_binary
            ),
            timeout=_TIMEOUTS["cfn-guard"],
        )
        all_findings.extend(guard_findings)
        layers_executed.append("cfn-guard")
    except asyncio.TimeoutError:
        all_findings.append(ValidationFinding(
            layer="cfn-guard", severity="warning", rule_id="TIMEOUT",
            message="cfn-guard validation timed out",
        ))

    passed = not any(
        f.severity == "error" and f.layer in _BLOCKING_LAYERS
        for f in all_findings
    )

    return ValidationReport(
        passed=passed,
        findings=all_findings,
        layers_executed=layers_executed,
    )
