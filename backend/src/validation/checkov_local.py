"""Layer 2: checkov security scanning — local Python runner."""

import logging
import os
import shutil
import tempfile

from src.models.iac import ValidationFinding

logger = logging.getLogger(__name__)

# Product-specific checks to skip (intentional security patterns — configured via config.yaml)
_DEFAULT_SKIP_CHECKS = {
    "CKV_AWS_23",  # SourceDestCheck=false may be intentional for appliance data-plane ENIs
}


def validate_checkov(
    template_str: str,
    skip_checks: list[str] | None = None,
) -> list[ValidationFinding]:
    """Run checkov CloudFormation scanner. Returns structured findings.

    Only CRITICAL and HIGH severity findings are severity="error".
    MEDIUM and LOW are severity="warning".
    """
    findings: list[ValidationFinding] = []

    try:
        from checkov.cloudformation.runner import Runner as CfnRunner
        from checkov.runner_filter import RunnerFilter
    except ImportError:
        logger.warning("checkov not installed — skipping Layer 2 validation")
        findings.append(ValidationFinding(
            layer="checkov", severity="warning", rule_id="SKIP",
            message="checkov not installed — layer skipped",
        ))
        return findings

    all_skips = list(_DEFAULT_SKIP_CHECKS | set(skip_checks or []))

    tmp_dir = None
    try:
        from src.validation.structural import detect_format_suffix

        tmp_dir = tempfile.mkdtemp(prefix="iac_checkov_")
        template_path = os.path.join(
            tmp_dir, f"template{detect_format_suffix(template_str)}"
        )
        with open(template_path, "w") as f:
            f.write(template_str)

        runner_filter = RunnerFilter(
            framework=["cloudformation"],
            skip_checks=all_skips,
        )

        runner = CfnRunner()
        report = runner.run(
            root_folder=None,
            files=[template_path],
            runner_filter=runner_filter,
        )

        # Process failed checks
        for check_result in report.failed_checks:
            severity = _classify_severity(check_result)
            findings.append(ValidationFinding(
                layer="checkov",
                severity=severity,
                rule_id=check_result.check_id,
                message=check_result.check.name if hasattr(check_result, "check") else str(check_result.check_id),
                resource=check_result.resource if hasattr(check_result, "resource") else None,
                line=_extract_line(check_result),
            ))

    except Exception as exc:
        logger.exception("checkov execution failed")
        findings.append(ValidationFinding(
            layer="checkov", severity="warning", rule_id="EXEC_ERROR",
            message=f"checkov execution failed: {exc}",
        ))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return findings


def _classify_severity(check_result) -> str:
    """Map checkov severity to our model. CRITICAL/HIGH -> error, rest -> warning."""
    try:
        severity = getattr(check_result, "severity", None)
        if severity and hasattr(severity, "name"):
            name = severity.name.upper()
            if name in ("CRITICAL", "HIGH"):
                return "error"
    except Exception:
        pass
    return "warning"


def _extract_line(check_result) -> int | None:
    """Extract line number from checkov result."""
    try:
        file_line_range = getattr(check_result, "file_line_range", None)
        if file_line_range and len(file_line_range) >= 1:
            return file_line_range[0]
    except Exception:
        pass
    return None
