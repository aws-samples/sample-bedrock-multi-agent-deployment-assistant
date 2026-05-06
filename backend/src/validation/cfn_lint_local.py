"""Layer 1: cfn-lint validation — local Python API, no network needed."""

import logging
import os
import tempfile

from src.models.iac import ValidationFinding

logger = logging.getLogger(__name__)


def validate_cfn_lint(template_str: str, region: str = "us-east-1") -> list[ValidationFinding]:
    """Run cfn-lint on template string. Returns structured findings.

    Only E-prefix (errors) are severity="error". W-prefix and I-prefix are "warning"/"info".
    """
    findings: list[ValidationFinding] = []

    try:
        import cfnlint  # noqa: F401
        from cfnlint import decode, runner
        from cfnlint.config import ConfigMixIn
    except ImportError:
        logger.warning("cfn-lint not installed — skipping Layer 1 validation")
        findings.append(ValidationFinding(
            layer="cfn-lint", severity="warning", rule_id="SKIP",
            message="cfn-lint not installed — layer skipped",
        ))
        return findings

    # Write template to temp file (cfn-lint needs a file path)
    tmp_path = None
    try:
        from src.validation.structural import detect_format_suffix

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=detect_format_suffix(template_str),
            delete=False, prefix="iac_validate_",
        ) as tmp:
            tmp.write(template_str)
            tmp.flush()
            tmp_path = tmp.name

        # Run cfn-lint using its Python API
        # cfn-lint >=1.44 moved `filename` into ConfigMixIn as `templates`.
        config = ConfigMixIn(
            [],
            include_checks=[],
            configure_rules={},
            regions=[region],
            include_experimental=False,
            ignore_checks=[],
            templates=[tmp_path],
        )

        (template, matches) = decode.decode(tmp_path)

        if matches:
            # Decode errors
            for match in matches:
                severity = _classify_severity(str(match.rule.id))
                findings.append(ValidationFinding(
                    layer="cfn-lint",
                    severity=severity,
                    rule_id=str(match.rule.id),
                    message=str(match.message),
                    resource=str(match.filename) if hasattr(match, "filename") else None,
                    line=match.linenumber if hasattr(match, "linenumber") else None,
                ))
        else:
            # Run the full lint
            lint_runner = runner.Runner(config)
            lint_matches = list(lint_runner.run())

            for match in lint_matches:
                severity = _classify_severity(str(match.rule.id))
                findings.append(ValidationFinding(
                    layer="cfn-lint",
                    severity=severity,
                    rule_id=str(match.rule.id),
                    message=str(match.message),
                    resource=_extract_resource(match),
                    line=match.linenumber if hasattr(match, "linenumber") else None,
                ))

    except Exception as exc:
        logger.exception("cfn-lint execution failed")
        findings.append(ValidationFinding(
            layer="cfn-lint", severity="warning", rule_id="EXEC_ERROR",
            message=f"cfn-lint execution failed: {exc}",
        ))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return findings


def _classify_severity(rule_id: str) -> str:
    """Classify cfn-lint rule ID into severity."""
    if rule_id.startswith("E"):
        return "error"
    if rule_id.startswith("W"):
        return "warning"
    return "info"


def _extract_resource(match) -> str | None:
    """Extract logical resource ID from a cfn-lint match."""
    try:
        if hasattr(match, "resource") and match.resource:
            return str(match.resource)
        if hasattr(match, "path") and match.path:
            parts = list(match.path)
            if len(parts) >= 2 and parts[0] == "Resources":
                return str(parts[1])
    except Exception:  # nosec B110 - cfn-lint match schema varies across versions; resource ID is optional metadata
        pass
    return None
