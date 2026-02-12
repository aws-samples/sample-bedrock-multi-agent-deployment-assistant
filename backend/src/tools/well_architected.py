"""AWS Well-Architected offline assessment for FortiGate deployments.

Provides pillar-by-pillar evaluation of FortiGate architecture designs
without requiring an AWS Well-Architected Tool workload.
"""

import logging

from strands import tool

logger = logging.getLogger(__name__)


@tool
def evaluate_design_against_wa(
    design_summary: str,
    use_case: str,
    ha_mode: str = "active-passive",
) -> str:
    """Evaluate a FortiGate design against Well-Architected pillars (offline analysis).

    This performs a local assessment without creating a WA workload.
    Useful for quick feedback during design generation.

    Args:
        design_summary: Architecture summary from the design agent.
        use_case: Deployment use case (sd-wan, east-west, egress).
        ha_mode: HA mode (active-passive, single).

    Returns:
        Markdown assessment against the 6 WA pillars.
    """
    # Pillar-specific checks based on FortiGate deployment patterns
    checks = {
        "Security": [],
        "Reliability": [],
        "Performance Efficiency": [],
        "Cost Optimization": [],
        "Operational Excellence": [],
        "Sustainability": [],
    }

    summary_lower = design_summary.lower()

    # Security
    has_encryption = "encryption" in summary_lower or "tls" in summary_lower
    checks["Security"].append(
        "PASS: Data encryption mentioned" if has_encryption
        else "REVIEW: Ensure data-in-transit encryption (IPSec/TLS)"
    )
    checks["Security"].append(
        "PASS: HA provides security service continuity" if ha_mode == "active-passive"
        else "RISK: Single instance — no failover for security inspection"
    )
    has_sg = "security group" in summary_lower or "sg" in summary_lower
    checks["Security"].append(
        "PASS: Security groups configured" if has_sg
        else "REVIEW: Define security group rules for least-privilege"
    )

    # Reliability
    checks["Reliability"].append(
        "PASS: Active-passive HA for automatic failover" if ha_mode == "active-passive"
        else "HIGH RISK: No HA — single point of failure"
    )
    multi_az = any(x in summary_lower for x in ["multi-az", "dual az", "2 az"])
    checks["Reliability"].append(
        "PASS: Multi-AZ deployment" if multi_az
        else "REVIEW: Consider multi-AZ for AZ failure resilience"
    )

    # Performance
    if "gwlb" in summary_lower:
        checks["Performance Efficiency"].append("PASS: GWLB provides horizontal scaling")
    elif use_case == "east-west":
        checks["Performance Efficiency"].append("REVIEW: Consider GWLB for scalable east-west inspection")
    else:
        checks["Performance Efficiency"].append("PASS: Architecture appropriate for use case")

    # Cost
    has_reserved = "reserved" in summary_lower or "savings plan" in summary_lower
    checks["Cost Optimization"].append(
        "PASS: Reserved capacity considered" if has_reserved
        else "REVIEW: Evaluate reserved instances for FortiGate VMs"
    )
    if ha_mode == "active-passive":
        checks["Cost Optimization"].append("INFO: Passive instance incurs cost — consider BYOL licensing")

    # Operational Excellence
    checks["Operational Excellence"].append(
        "REVIEW: Ensure FortiManager integration for centralized management"
    )
    checks["Operational Excellence"].append(
        "REVIEW: Configure FortiAnalyzer or CloudWatch for logging"
    )

    # Sustainability
    checks["Sustainability"].append(
        "INFO: Right-size FortiGate VM instance type to match traffic volume"
    )

    # Format output
    lines = [f"## Well-Architected Assessment: {use_case.upper()} Deployment\n"]
    for pillar, items in checks.items():
        lines.append(f"### {pillar}")
        for item in items:
            icon = "+" if item.startswith("PASS") else "-" if "RISK" in item else "~"
            lines.append(f"  {icon} {item}")
        lines.append("")

    return "\n".join(lines)
