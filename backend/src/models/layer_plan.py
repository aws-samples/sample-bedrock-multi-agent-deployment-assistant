"""Layer decomposition models for multi-layer IaC generation.

Defines the contract-based layer system where complex templates are
split into Foundation, Security, Compute, HA, and Integration layers,
each generating an independent ResourcePlan.

The Architecture Planner (LLM or predefined) produces a ``LayerPlan``
with import/export contracts.  Each layer generates a small
``ResourcePlan`` (5-15 resources) referencing imports as CFN
Parameters.  The deterministic merger wires imports to exports via
Ref/GetAtt and concatenates all layers into a single merged
``ResourcePlan`` for JSON assembly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Layer names
# ---------------------------------------------------------------------------


class LayerName:
    """Standard layer name constants for template decomposition.

    These are the common names used across patterns. Custom patterns
    can use any string as a layer name (e.g., 'scaling', 'storage').
    """

    FOUNDATION = "foundation"    # VPC, Subnets, IGW, NAT, Route Tables
    SECURITY = "security"        # Security Groups, NACLs, IAM Roles
    COMPUTE = "compute"          # Appliance instances, ENIs, EIPs
    HA = "ha"                    # HA-sync subnets, heartbeat config, failover
    INTEGRATION = "integration"  # TGW, GWLB, VPN, endpoints


# ---------------------------------------------------------------------------
# Import / export contracts
# ---------------------------------------------------------------------------


class LayerExport(BaseModel):
    """A value a layer exports for other layers to consume."""

    name: str = Field(description="Export name (e.g., 'VpcId', 'PublicSubnet1Id')")
    resource_logical_id: str = Field(
        description="Resource logical ID that produces this value"
    )
    attribute: str | None = Field(
        default=None,
        description="GetAtt attribute name (None means use Ref)",
    )
    description: str = ""


class LayerImport(BaseModel):
    """A value a layer needs from another layer."""

    name: str = Field(
        description="Import name — must match an export name from source_layer"
    )
    source_layer: str
    parameter_name: str = Field(
        description="CFN Parameter name in this layer's ResourcePlan "
        "that receives the value during generation"
    )
    description: str = ""


# ---------------------------------------------------------------------------
# Layer specification
# ---------------------------------------------------------------------------


class LayerSpec(BaseModel):
    """Specification for a single layer in the decomposition."""

    name: str
    description: str
    resource_types: list[str] = Field(
        description="AWS resource type prefixes this layer is responsible for "
        "(e.g., ['AWS::EC2::VPC', 'AWS::EC2::Subnet'])",
    )
    imports: list[LayerImport] = Field(default_factory=list)
    exports: list[LayerExport] = Field(default_factory=list)
    prompt_context: str = Field(
        default="",
        description="Additional context appended to the layer generation prompt",
    )


# ---------------------------------------------------------------------------
# Top-level plan
# ---------------------------------------------------------------------------


class LayerPlan(BaseModel):
    """Complete decomposition plan for a multi-layer template.

    Defines all layers and their import/export contracts.  For common
    patterns this is predefined (zero LLM).  For novel deployments,
    the Architecture Planner LLM generates it.
    """

    pattern_name: str
    description: str
    layers: list[LayerSpec] = Field(min_length=1)

    def get_layer(self, name: str) -> LayerSpec | None:
        """Look up a layer by name."""
        for layer in self.layers:
            if layer.name == name:
                return layer
        return None

    def parallelizable_groups(self) -> list[list[LayerSpec]]:
        """Return groups of layers that can be generated concurrently.

        Uses topological sort based on import dependencies.  Layers in
        the same group have all their import sources already resolved by
        prior groups, so they can run in parallel via ``asyncio.gather()``.

        Returns:
            Ordered list of groups.  Each group is a list of ``LayerSpec``
            whose imports are satisfied by prior groups.
        """
        remaining = list(self.layers)
        groups: list[list[LayerSpec]] = []
        resolved: set[str] = set()

        while remaining:
            group = [
                layer
                for layer in remaining
                if all(imp.source_layer in resolved for imp in layer.imports)
            ]
            if not group:
                # Circular dependency fallback — add all remaining
                groups.append(remaining)
                break
            groups.append(group)
            for layer in group:
                resolved.add(layer.name)
                remaining.remove(layer)

        return groups
