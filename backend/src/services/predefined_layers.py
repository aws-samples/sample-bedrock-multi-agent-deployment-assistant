"""Predefined LayerPlans for common FortiGate deployment patterns.

Each function returns a ``LayerPlan`` with hardcoded layer specs,
imports, and exports.  This eliminates the Architecture Planner LLM
call for known patterns — ~5-8 seconds saved per generation.

Patterns are matched via fuzzy key lookup in ``get_predefined_plan()``.
"""

from __future__ import annotations

from typing import Callable

from src.models.layer_plan import (
    LayerExport,
    LayerImport,
    LayerName,
    LayerPlan,
    LayerSpec,
)

# ---------------------------------------------------------------------------
# Common exports/imports reused across patterns
# ---------------------------------------------------------------------------

# Foundation layer always exports these
_FOUNDATION_EXPORTS = [
    LayerExport(
        name="VpcId",
        resource_logical_id="InspectionVPC",
        attribute="VpcId",
        description="VPC ID for all downstream layers",
    ),
    LayerExport(
        name="PublicSubnet1Id",
        resource_logical_id="PublicSubnet1",
        description="Public subnet AZ-1",
    ),
    LayerExport(
        name="PrivateSubnet1Id",
        resource_logical_id="PrivateSubnet1",
        description="Private subnet AZ-1",
    ),
    LayerExport(
        name="InternetGatewayId",
        resource_logical_id="InternetGateway",
        description="IGW for public routing",
    ),
]

_FOUNDATION_EXPORTS_DUAL_AZ = _FOUNDATION_EXPORTS + [
    LayerExport(
        name="PublicSubnet2Id",
        resource_logical_id="PublicSubnet2",
        description="Public subnet AZ-2",
    ),
    LayerExport(
        name="PrivateSubnet2Id",
        resource_logical_id="PrivateSubnet2",
        description="Private subnet AZ-2",
    ),
]

# Security layer standard imports and exports
_SECURITY_VPC_IMPORT = LayerImport(
    name="VpcId",
    source_layer=LayerName.FOUNDATION,
    parameter_name="VpcId",
    description="VPC ID from foundation layer",
)

_SECURITY_EXPORTS = [
    LayerExport(
        name="MgmtSecurityGroupId",
        resource_logical_id="ManagementSecurityGroup",
        attribute="GroupId",
        description="Management SG (SSH+HTTPS restricted to AdminCIDR)",
    ),
    LayerExport(
        name="DataPlaneSecurityGroupId",
        resource_logical_id="DataPlaneSecurityGroup",
        attribute="GroupId",
        description="Data-plane SG (allow all — FortiGate inspects at app layer)",
    ),
]


# ---------------------------------------------------------------------------
# Pattern: Single FortiGate (no HA)
# ---------------------------------------------------------------------------


def single_fortigate_plan() -> LayerPlan:
    """Single FortiGate instance — simplest deployment."""
    return LayerPlan(
        pattern_name="single-fortigate",
        description="Single FortiGate instance with public/private subnets",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="VPC, subnets (public + private), IGW, route tables, and route table associations",
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::EIP",
                    "AWS::EC2::NatGateway",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS,
                prompt_context="Single-AZ deployment. Create one public and one private subnet.",
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups: management (SSH+HTTPS restricted) and data-plane (allow all)",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(
                        name="InstanceProfileArn",
                        resource_logical_id="FortiGateInstanceProfile",
                        attribute="Arn",
                        description="IAM instance profile for FortiGate (SSM + CloudWatch)",
                    ),
                ],
                prompt_context=(
                    "Management SG: SSH(22) + HTTPS(443) from AdminCIDR parameter. "
                    "Data-plane SG: allow all (FortiGate does L7 inspection). "
                    "IAM role for SSM + CloudWatch."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description=(
                    "FortiGate EC2 instance, ENIs (management + data-plane), "
                    "EIP for management, network interface attachments"
                ),
                resource_types=[
                    "AWS::EC2::Instance",
                    "AWS::EC2::NetworkInterface",
                    "AWS::EC2::NetworkInterfaceAttachment",
                    "AWS::EC2::EIP",
                    "AWS::EC2::EIPAssociation",
                ],
                imports=[
                    LayerImport(
                        name="PublicSubnet1Id",
                        source_layer=LayerName.FOUNDATION,
                        parameter_name="PublicSubnet1Id",
                    ),
                    LayerImport(
                        name="PrivateSubnet1Id",
                        source_layer=LayerName.FOUNDATION,
                        parameter_name="PrivateSubnet1Id",
                    ),
                    LayerImport(
                        name="MgmtSecurityGroupId",
                        source_layer=LayerName.SECURITY,
                        parameter_name="MgmtSecurityGroupId",
                    ),
                    LayerImport(
                        name="DataPlaneSecurityGroupId",
                        source_layer=LayerName.SECURITY,
                        parameter_name="DataPlaneSecurityGroupId",
                    ),
                    LayerImport(
                        name="InstanceProfileArn",
                        source_layer=LayerName.SECURITY,
                        parameter_name="InstanceProfileArn",
                    ),
                ],
                exports=[
                    LayerExport(
                        name="FortiGateInstanceId",
                        resource_logical_id="FortiGateInstance",
                        description="FortiGate instance ID",
                    ),
                    LayerExport(
                        name="FortiGateManagementIp",
                        resource_logical_id="FortiGateManagementEIP",
                        description="FortiGate management EIP",
                    ),
                ],
                prompt_context=(
                    "Single FortiGate. Min 2 ENIs: management (public subnet, SourceDestCheck=true) "
                    "and data-plane (private subnet, SourceDestCheck=false). "
                    "Create ENIs as separate resources, attach via NetworkInterfaceAttachment. "
                    "UserData with FortiGate CLI bootstrap via {\"base64\": {\"sub\": \"...\"}}."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: HA Active-Passive (dual AZ)
# ---------------------------------------------------------------------------


def ha_active_passive_plan() -> LayerPlan:
    """FortiGate HA Active-Passive across two Availability Zones."""
    return LayerPlan(
        pattern_name="ha-active-passive",
        description="FortiGate HA Active-Passive with dual-AZ subnets",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description=(
                    "VPC, subnets (public/private/ha-sync in 2 AZs), IGW, "
                    "route tables, NAT gateway"
                ),
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::EIP",
                    "AWS::EC2::NatGateway",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS_DUAL_AZ + [
                    LayerExport(
                        name="HASyncSubnet1Id",
                        resource_logical_id="HASyncSubnet1",
                        description="HA sync subnet AZ-1",
                    ),
                    LayerExport(
                        name="HASyncSubnet2Id",
                        resource_logical_id="HASyncSubnet2",
                        description="HA sync subnet AZ-2",
                    ),
                ],
                prompt_context=(
                    "Dual-AZ HA deployment. Create public, private, and ha-sync "
                    "subnets in each AZ (6 subnets total)."
                ),
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups: management, data-plane, HA-sync; IAM role",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(
                        name="HASyncSecurityGroupId",
                        resource_logical_id="HASyncSecurityGroup",
                        attribute="GroupId",
                        description="HA heartbeat/sync SG",
                    ),
                    LayerExport(
                        name="InstanceProfileArn",
                        resource_logical_id="FortiGateInstanceProfile",
                        attribute="Arn",
                    ),
                ],
                prompt_context=(
                    "Management SG: SSH+HTTPS from AdminCIDR. "
                    "Data-plane SG: allow all. "
                    "HA-sync SG: allow all traffic between FortiGate instances (unicast heartbeat). "
                    "IAM role for SSM + CloudWatch + HA failover (ENI reassignment)."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description=(
                    "Two FortiGate EC2 instances (active + passive), ENIs "
                    "(management, data-plane, ha-sync per instance), EIPs"
                ),
                resource_types=[
                    "AWS::EC2::Instance",
                    "AWS::EC2::NetworkInterface",
                    "AWS::EC2::NetworkInterfaceAttachment",
                    "AWS::EC2::EIP",
                    "AWS::EC2::EIPAssociation",
                ],
                imports=[
                    LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet1Id"),
                    LayerImport(name="PublicSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet2Id"),
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="PrivateSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet2Id"),
                    LayerImport(name="HASyncSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="HASyncSubnet1Id"),
                    LayerImport(name="HASyncSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="HASyncSubnet2Id"),
                    LayerImport(name="MgmtSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="MgmtSecurityGroupId"),
                    LayerImport(name="DataPlaneSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="DataPlaneSecurityGroupId"),
                    LayerImport(name="HASyncSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="HASyncSecurityGroupId"),
                    LayerImport(name="InstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="InstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="ActiveFortiGateId", resource_logical_id="ActiveFortiGate", description="Active instance ID"),
                    LayerExport(name="PassiveFortiGateId", resource_logical_id="PassiveFortiGate", description="Passive instance ID"),
                    LayerExport(name="ActiveManagementIp", resource_logical_id="ActiveManagementEIP", description="Active management EIP"),
                    LayerExport(name="PassiveManagementIp", resource_logical_id="PassiveManagementEIP", description="Passive management EIP"),
                ],
                prompt_context=(
                    "HA pair: Active in AZ-1, Passive in AZ-2. "
                    "Each instance gets 3 ENIs: management, data-plane (SourceDestCheck=false), ha-sync. "
                    "UserData includes HA config: set ha-mode a-p, set priority (active=200, passive=100)."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: GWLB Inspection
# ---------------------------------------------------------------------------


def gwlb_plan() -> LayerPlan:
    """FortiGate with Gateway Load Balancer for centralized inspection."""
    return LayerPlan(
        pattern_name="gwlb-inspection",
        description="FortiGate GWLB inspection VPC with endpoint services",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="Inspection VPC, subnets (public/private/gwlbe in 2 AZs), IGW, route tables",
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::EIP",
                    "AWS::EC2::NatGateway",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS_DUAL_AZ + [
                    LayerExport(name="GwlbeSubnet1Id", resource_logical_id="GwlbeSubnet1", description="GWLB endpoint subnet AZ-1"),
                    LayerExport(name="GwlbeSubnet2Id", resource_logical_id="GwlbeSubnet2", description="GWLB endpoint subnet AZ-2"),
                ],
                prompt_context="GWLB inspection VPC. Create public, private, and gwlbe subnets in 2 AZs.",
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups and IAM for FortiGate instances",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(name="InstanceProfileArn", resource_logical_id="FortiGateInstanceProfile", attribute="Arn"),
                ],
                prompt_context="Management SG: SSH+HTTPS from AdminCIDR. Data-plane SG: allow all (GENEVE + health checks).",
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="FortiGate instances in 2 AZs with management and data-plane ENIs",
                resource_types=[
                    "AWS::EC2::Instance",
                    "AWS::EC2::NetworkInterface",
                    "AWS::EC2::NetworkInterfaceAttachment",
                    "AWS::EC2::EIP",
                    "AWS::EC2::EIPAssociation",
                ],
                imports=[
                    LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet1Id"),
                    LayerImport(name="PublicSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet2Id"),
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="PrivateSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet2Id"),
                    LayerImport(name="MgmtSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="MgmtSecurityGroupId"),
                    LayerImport(name="DataPlaneSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="DataPlaneSecurityGroupId"),
                    LayerImport(name="InstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="InstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="FortiGate1Id", resource_logical_id="FortiGate1", description="FortiGate instance AZ-1"),
                    LayerExport(name="FortiGate2Id", resource_logical_id="FortiGate2", description="FortiGate instance AZ-2"),
                    LayerExport(name="FortiGate1DataENI", resource_logical_id="FortiGate1DataENI", description="Data ENI for GWLB target"),
                    LayerExport(name="FortiGate2DataENI", resource_logical_id="FortiGate2DataENI", description="Data ENI for GWLB target"),
                ],
                prompt_context=(
                    "One FortiGate per AZ. 2 ENIs each: management (public), data-plane (private, SourceDestCheck=false). "
                    "Data-plane ENIs are GWLB targets."
                ),
            ),
            LayerSpec(
                name=LayerName.INTEGRATION,
                description="Gateway Load Balancer, target group, GWLB endpoints, VPC endpoint service",
                resource_types=[
                    "AWS::ElasticLoadBalancingV2::LoadBalancer",
                    "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "AWS::ElasticLoadBalancingV2::Listener",
                    "AWS::EC2::VPCEndpointService",
                    "AWS::EC2::VPCEndpoint",
                ],
                imports=[
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="PrivateSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet2Id"),
                    LayerImport(name="GwlbeSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="GwlbeSubnet1Id"),
                    LayerImport(name="GwlbeSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="GwlbeSubnet2Id"),
                    LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId"),
                    LayerImport(name="FortiGate1DataENI", source_layer=LayerName.COMPUTE, parameter_name="FortiGate1DataENI"),
                    LayerImport(name="FortiGate2DataENI", source_layer=LayerName.COMPUTE, parameter_name="FortiGate2DataENI"),
                ],
                exports=[
                    LayerExport(name="GwlbArn", resource_logical_id="GatewayLoadBalancer", attribute="LoadBalancerArn", description="GWLB ARN"),
                    LayerExport(name="GwlbEndpointServiceId", resource_logical_id="GwlbEndpointService", description="GWLB endpoint service"),
                ],
                prompt_context=(
                    "GWLB in private subnets targeting FortiGate data-plane ENIs (IP target type). "
                    "GENEVE protocol, health check on port 443. "
                    "VPC endpoint service + GWLB endpoints in gwlbe subnets."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: Transit Gateway Inspection
# ---------------------------------------------------------------------------


def tgw_inspection_plan() -> LayerPlan:
    """FortiGate Transit Gateway inspection VPC."""
    return LayerPlan(
        pattern_name="tgw-inspection",
        description="FortiGate inspection VPC attached to Transit Gateway",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="Inspection VPC, subnets (public/private/tgw-attachment in 2 AZs), IGW, route tables",
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::EIP",
                    "AWS::EC2::NatGateway",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS_DUAL_AZ + [
                    LayerExport(name="TgwSubnet1Id", resource_logical_id="TgwSubnet1", description="TGW attachment subnet AZ-1"),
                    LayerExport(name="TgwSubnet2Id", resource_logical_id="TgwSubnet2", description="TGW attachment subnet AZ-2"),
                ],
                prompt_context="TGW inspection VPC. Create public, private, and tgw-attachment subnets in 2 AZs.",
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups and IAM for FortiGate instances",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(name="InstanceProfileArn", resource_logical_id="FortiGateInstanceProfile", attribute="Arn"),
                ],
                prompt_context="Management SG: SSH+HTTPS from AdminCIDR. Data-plane SG: allow all.",
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="FortiGate instances with management and data-plane ENIs",
                resource_types=[
                    "AWS::EC2::Instance",
                    "AWS::EC2::NetworkInterface",
                    "AWS::EC2::NetworkInterfaceAttachment",
                    "AWS::EC2::EIP",
                    "AWS::EC2::EIPAssociation",
                ],
                imports=[
                    LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet1Id"),
                    LayerImport(name="PublicSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet2Id"),
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="PrivateSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet2Id"),
                    LayerImport(name="MgmtSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="MgmtSecurityGroupId"),
                    LayerImport(name="DataPlaneSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="DataPlaneSecurityGroupId"),
                    LayerImport(name="InstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="InstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="FortiGate1Id", resource_logical_id="FortiGate1"),
                    LayerExport(name="FortiGate2Id", resource_logical_id="FortiGate2"),
                ],
                prompt_context="One FortiGate per AZ. Management ENI in public subnet, data-plane ENI in private (SourceDestCheck=false).",
            ),
            LayerSpec(
                name=LayerName.INTEGRATION,
                description="Transit Gateway attachment and routing",
                resource_types=[
                    "AWS::EC2::TransitGateway",
                    "AWS::EC2::TransitGatewayAttachment",
                    "AWS::EC2::TransitGatewayRouteTable",
                    "AWS::EC2::TransitGatewayRoute",
                    "AWS::EC2::TransitGatewayRouteTableAssociation",
                    "AWS::EC2::TransitGatewayRouteTablePropagation",
                ],
                imports=[
                    LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId"),
                    LayerImport(name="TgwSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="TgwSubnet1Id"),
                    LayerImport(name="TgwSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="TgwSubnet2Id"),
                ],
                exports=[
                    LayerExport(name="TransitGatewayId", resource_logical_id="InspectionTGW"),
                    LayerExport(name="TgwAttachmentId", resource_logical_id="InspectionTGWAttachment"),
                ],
                prompt_context=(
                    "Create or reference a Transit Gateway. Attach inspection VPC via tgw-attachment subnets. "
                    "Create inspection route table with default route to the attachment."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern registry and lookup
# ---------------------------------------------------------------------------

PREDEFINED_PATTERNS: dict[str, Callable[[], LayerPlan]] = {
    "single": single_fortigate_plan,
    "single-fortigate": single_fortigate_plan,
    "ha-dual-az": ha_active_passive_plan,
    "ha-active-passive": ha_active_passive_plan,
    "active-passive": ha_active_passive_plan,
    "gwlb": gwlb_plan,
    "gwlb-inspection": gwlb_plan,
    "gwlb-transit": gwlb_plan,
    "tgw-inspection": tgw_inspection_plan,
    "transit-gateway": tgw_inspection_plan,
    "tgw": tgw_inspection_plan,
}


def get_predefined_plan(deployment_pattern: str) -> LayerPlan | None:
    """Look up a predefined plan by deployment pattern name.

    Uses fuzzy matching: normalizes the pattern (lowercase, strip
    underscores/spaces) and checks if any registry key is a substring.

    Returns ``None`` if no predefined plan exists for the pattern,
    which triggers the Architecture Planner LLM fallback.
    """
    normalized = deployment_pattern.lower().replace("_", "-").replace(" ", "-").strip()

    # Exact match first
    if normalized in PREDEFINED_PATTERNS:
        return PREDEFINED_PATTERNS[normalized]()

    # Substring match
    for key, factory in PREDEFINED_PATTERNS.items():
        if key in normalized or normalized in key:
            return factory()

    return None
