"""Layer 3: cfn-guard custom rules — subprocess invocation."""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from src.models.iac import ValidationFinding

logger = logging.getLogger(__name__)

# Path to custom rules file (product-specific, configured via config.yaml)
_RULES_DIR = Path(__file__).parent
_RULES_FILE = _RULES_DIR / "appliance_rules.guard"


def validate_cfn_guard(
    template_str: str,
    guard_binary: str = "cfn-guard",
    rules_file: Path | None = None,
) -> list[ValidationFinding]:
    """Run cfn-guard with custom rules. Returns structured findings."""
    findings: list[ValidationFinding] = []

    rules_path = rules_file or _RULES_FILE
    if not rules_path.exists():
        logger.warning("cfn-guard rules file not found: %s — skipping Layer 3", rules_path)
        findings.append(ValidationFinding(
            layer="cfn-guard", severity="warning", rule_id="SKIP",
            message=f"Rules file not found: {rules_path} — layer skipped",
        ))
        return findings

    tmp_path = None
    try:
        from src.validation.structural import detect_format_suffix

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=detect_format_suffix(template_str),
            delete=False, prefix="iac_guard_",
        ) as tmp:
            tmp.write(template_str)
            tmp.flush()
            tmp_path = tmp.name

        result = subprocess.run(  # nosec B603 - argv is a list (no shell), guard_binary is internal-only
            [
                guard_binary, "validate",
                "--data", tmp_path,
                "--rules", str(rules_path),
                "--output-format", "json",
                "--show-summary", "none",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            # All rules passed
            return findings

        # Parse JSON output for failures
        try:
            output = json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            output = None

        if output and isinstance(output, dict):
            _parse_guard_output(output, findings)
        elif result.stdout.strip():
            # Fallback: parse text output line by line
            _parse_guard_text(result.stdout, findings)
        elif result.stderr.strip():
            findings.append(ValidationFinding(
                layer="cfn-guard", severity="warning", rule_id="EXEC_WARN",
                message=f"cfn-guard stderr: {result.stderr[:500]}",
            ))

    except FileNotFoundError:
        logger.warning("cfn-guard binary not found at '%s' — skipping Layer 3", guard_binary)
        findings.append(ValidationFinding(
            layer="cfn-guard", severity="warning", rule_id="SKIP",
            message=f"cfn-guard binary not found: {guard_binary} — layer skipped",
        ))
    except subprocess.TimeoutExpired:
        findings.append(ValidationFinding(
            layer="cfn-guard", severity="warning", rule_id="TIMEOUT",
            message="cfn-guard execution timed out (15s)",
        ))
    except Exception as exc:
        logger.exception("cfn-guard execution failed")
        findings.append(ValidationFinding(
            layer="cfn-guard", severity="warning", rule_id="EXEC_ERROR",
            message=f"cfn-guard execution failed: {exc}",
        ))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return findings


def _parse_guard_output(output: dict, findings: list[ValidationFinding]) -> None:
    """Parse cfn-guard JSON output into findings."""
    not_compliant = output.get("not_compliant", [])
    if isinstance(not_compliant, list):
        for item in not_compliant:
            rule_name = item.get("Rule", item.get("rule", "UNKNOWN"))
            message = item.get("Message", item.get("message", "Rule failed"))
            resource = item.get("Resource", item.get("resource"))
            findings.append(ValidationFinding(
                layer="cfn-guard",
                severity="error",
                rule_id=str(rule_name),
                message=str(message),
                resource=str(resource) if resource else None,
            ))


def _parse_guard_text(text: str, findings: list[ValidationFinding]) -> None:
    """Fallback: parse cfn-guard text output."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "FAIL" in line.upper() or "failed" in line.lower():
            findings.append(ValidationFinding(
                layer="cfn-guard",
                severity="error",
                rule_id="GUARD_FAIL",
                message=line[:500],
            ))
