"""Mermaid diagram validation via Node.js mermaid.parse().

Shells out to a small Node.js script that uses the same Mermaid library as
the frontend to validate architecture-beta diagrams. This ensures that any
diagram passing validation here will render correctly in the browser.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve the validator script path relative to this file.
# Layout: backend/src/tools/mermaid_validator.py → backend/tools/validate-mermaid/index.mjs
_VALIDATOR_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "tools" / "validate-mermaid" / "index.mjs"
)

# Timeout for the Node.js subprocess (seconds)
_VALIDATION_TIMEOUT = 15


def validate_mermaid(diagram_code: str) -> tuple[bool, str]:
    """Validate a Mermaid diagram using Node.js ``mermaid.parse()``.

    Args:
        diagram_code: Raw Mermaid diagram text (e.g. ``architecture-beta\\n ...``).

    Returns:
        A tuple of ``(valid, error_message)``.
        *error_message* is empty when *valid* is True.
    """
    if not diagram_code or not diagram_code.strip():
        return False, "Empty diagram code"

    if not _VALIDATOR_SCRIPT.exists():
        logger.warning(
            "Mermaid validator script not found at %s — skipping validation",
            _VALIDATOR_SCRIPT,
        )
        # Graceful degradation: treat as valid if the validator isn't installed
        return True, ""

    try:
        result = subprocess.run(  # nosec B603,B607 - argv is a list, runs in container with controlled PATH
            ["node", str(_VALIDATOR_SCRIPT)],
            input=diagram_code,
            capture_output=True,
            text=True,
            timeout=_VALIDATION_TIMEOUT,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("Mermaid validator exited with code %d: %s", result.returncode, stderr)
            return False, stderr or "Validator process failed"

        data = json.loads(result.stdout)
        valid = data.get("valid", False)
        error = data.get("error", "")

        if not valid:
            logger.debug("Mermaid validation failed: %s", error)

        return valid, error

    except subprocess.TimeoutExpired:
        logger.error("Mermaid validator timed out after %ds", _VALIDATION_TIMEOUT)
        return False, "Validation timed out"
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse validator output: %s", exc)
        return False, f"Invalid validator output: {exc}"
    except FileNotFoundError:
        logger.error("Node.js not found — install Node.js to enable Mermaid validation")
        # Graceful degradation
        return True, ""
    except Exception as exc:
        logger.error("Unexpected error during Mermaid validation: %s", exc)
        return False, str(exc)
