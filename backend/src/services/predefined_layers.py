"""Predefined LayerPlans for common deployment patterns.

Each function returns a ``LayerPlan`` with hardcoded layer specs,
imports, and exports.  This eliminates the Architecture Planner LLM
call for known patterns — ~5-8 seconds saved per generation.

Patterns are matched via fuzzy key lookup in ``get_predefined_plan()``.
These patterns correspond to the catalog.lock.yaml pattern definitions.
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

_FOUNDATION_EXPORTS = [
    LayerExport(
        name="VpcId",
        resource_logical_id="MainVPC",
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

_SECURITY_VPC_IMPORT = LayerImport(
    name="VpcId",
    source_layer=LayerName.FOUNDATION,
    parameter_name="VpcId",
    description="VPC ID from foundation layer",
)

_SECURITY_EXPORTS = [
    LayerExport(
        name="InferenceSecurityGroupId",
        resource_logical_id="InferenceSecurityGroup",
        attribute="GroupId",
        description="Inference traffic SG (HTTPS/gRPC from ALB)",
    ),
    LayerExport(
        name="ManagementSecurityGroupId",
        resource_logical_id="ManagementSecurityGroup",
        attribute="GroupId",
        description="Management SG (SSH restricted to AdminCIDR)",
    ),
]


# ---------------------------------------------------------------------------
# Pattern: Single Instance (dev/staging)
# ---------------------------------------------------------------------------


def single_instance_plan() -> LayerPlan:
    """Single GPU instance — simplest deployment for dev or low-traffic inference."""
    return LayerPlan(
        pattern_name="single-instance",
        description="Single GPU instance with public/private subnets for inference",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="VPC, subnets (public + private), IGW, NAT Gateway, route tables",
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
                prompt_context=(
                    "Single-AZ deployment. Create one public subnet (ALB, NAT) "
                    "and one private subnet (GPU instance). NAT Gateway for outbound "
                    "model downloads from S3."
                ),
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups for inference and management, IAM role for S3/CloudWatch access",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(
                        name="InstanceProfileArn",
                        resource_logical_id="InferenceInstanceProfile",
                        attribute="Arn",
                        description="IAM instance profile for GPU instance (S3 model access + CloudWatch)",
                    ),
                ],
                prompt_context=(
                    "Inference SG: HTTPS(443) + gRPC(8500) from ALB SG. "
                    "Management SG: SSH(22) from AdminCIDR parameter. "
                    "IAM role with S3 read (model registry), CloudWatch Logs/Metrics, ECR pull."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="GPU EC2 instance, Application Load Balancer, target group, health checks",
                resource_types=[
                    "AWS::EC2::Instance",
                    "AWS::ElasticLoadBalancingV2::LoadBalancer",
                    "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "AWS::ElasticLoadBalancingV2::Listener",
                    "AWS::EC2::SecurityGroup",
                ],
                imports=[
                    LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId"),
                    LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet1Id"),
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="InferenceSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="InferenceSecurityGroupId"),
                    LayerImport(name="InstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="InstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="InstanceId", resource_logical_id="InferenceInstance", description="GPU instance ID"),
                    LayerExport(name="ALBDnsName", resource_logical_id="InferenceALB", attribute="DNSName", description="ALB DNS for inference endpoint"),
                ],
                prompt_context=(
                    "Single GPU instance (g5.xlarge default, parameterized). "
                    "ALB in public subnet, instance in private subnet. "
                    "Target group health check on /v1/health. "
                    "UserData: pull container image from ECR, start inference server on port 8500."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: Auto-Scaling Fleet (production inference)
# ---------------------------------------------------------------------------


def auto_scaling_fleet_plan() -> LayerPlan:
    """Auto-scaling GPU fleet behind ALB for production real-time inference."""
    return LayerPlan(
        pattern_name="auto-scaling-fleet",
        description="Auto-scaling GPU fleet behind ALB with CloudWatch-driven scaling",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="VPC, dual-AZ subnets (public + private), IGW, NAT Gateways, route tables",
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
                exports=_FOUNDATION_EXPORTS_DUAL_AZ,
                prompt_context=(
                    "Dual-AZ deployment. Two public subnets (ALB) and two private subnets "
                    "(GPU instances). NAT Gateway per AZ for model downloads and telemetry."
                ),
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups for ALB, inference instances, and management. IAM role for S3/ECR/CloudWatch.",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=_SECURITY_EXPORTS + [
                    LayerExport(name="ALBSecurityGroupId", resource_logical_id="ALBSecurityGroup", attribute="GroupId"),
                    LayerExport(name="InstanceProfileArn", resource_logical_id="InferenceInstanceProfile", attribute="Arn"),
                ],
                prompt_context=(
                    "ALB SG: HTTPS(443) from 0.0.0.0/0. "
                    "Inference SG: 8500 from ALB SG only. "
                    "Management SG: SSH(22) from AdminCIDR. "
                    "IAM role: S3 read, ECR pull, CloudWatch Logs/Metrics, SSM for management."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="Launch Template, Auto Scaling Group, ALB, Target Group, Listeners",
                resource_types=[
                    "AWS::EC2::LaunchTemplate",
                    "AWS::AutoScaling::AutoScalingGroup",
                    "AWS::ElasticLoadBalancingV2::LoadBalancer",
                    "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "AWS::ElasticLoadBalancingV2::Listener",
                ],
                imports=[
                    LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId"),
                    LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet1Id"),
                    LayerImport(name="PublicSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PublicSubnet2Id"),
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="PrivateSubnet2Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet2Id"),
                    LayerImport(name="ALBSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="ALBSecurityGroupId"),
                    LayerImport(name="InferenceSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="InferenceSecurityGroupId"),
                    LayerImport(name="InstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="InstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="ALBDnsName", resource_logical_id="InferenceALB", attribute="DNSName"),
                    LayerExport(name="ASGName", resource_logical_id="InferenceASG"),
                ],
                prompt_context=(
                    "Launch template: GPU instance (parameterized type), UserData pulls ECR image. "
                    "ASG: min=2, max=parameterized, health check grace period 120s (model load time). "
                    "ALB in public subnets, target group health check on /v1/health (HTTP 8500). "
                    "Spread across both AZs for high availability."
                ),
            ),
            LayerSpec(
                name="scaling",
                description="CloudWatch alarms, scaling policies, and optional warm pool",
                resource_types=[
                    "AWS::AutoScaling::ScalingPolicy",
                    "AWS::CloudWatch::Alarm",
                    "AWS::AutoScaling::WarmPool",
                ],
                imports=[
                    LayerImport(name="ASGName", source_layer=LayerName.COMPUTE, parameter_name="ASGName"),
                ],
                exports=[],
                prompt_context=(
                    "Target tracking: scale on GPU utilization (custom metric via CloudWatch agent). "
                    "Step scaling: scale out at P99 latency > 200ms. "
                    "Scale-in cooldown: 300s. Scale-out cooldown: 60s. "
                    "Warm pool: 2 instances in Stopped state (pre-loaded model on EBS)."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: Batch Processing (offline inference)
# ---------------------------------------------------------------------------


def batch_processing_plan() -> LayerPlan:
    """Spot-based batch processing with Step Functions orchestration."""
    return LayerPlan(
        pattern_name="batch-processing",
        description="Spot GPU instances orchestrated by Step Functions for batch inference",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="VPC, private subnet, S3 VPC endpoint, NAT Gateway",
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::NatGateway",
                    "AWS::EC2::EIP",
                    "AWS::EC2::VPCEndpoint",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS + [
                    LayerExport(name="S3VpcEndpointId", resource_logical_id="S3VpcEndpoint", description="S3 Gateway endpoint"),
                ],
                prompt_context=(
                    "Private-only compute subnet (no public IPs on GPU instances). "
                    "S3 Gateway VPC endpoint for free, high-throughput model/data access. "
                    "NAT Gateway only for ECR pulls and CloudWatch telemetry."
                ),
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups, IAM roles for EC2 (S3 + CloudWatch) and Step Functions",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                    "AWS::IAM::Policy",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=[
                    LayerExport(name="BatchInstanceProfileArn", resource_logical_id="BatchInstanceProfile", attribute="Arn"),
                    LayerExport(name="StepFunctionsRoleArn", resource_logical_id="StepFunctionsRole", attribute="Arn"),
                    LayerExport(name="BatchSecurityGroupId", resource_logical_id="BatchSecurityGroup", attribute="GroupId"),
                ],
                prompt_context=(
                    "Batch SG: egress-only (S3 via endpoint, NAT for ECR/CW). No inbound needed. "
                    "EC2 IAM role: S3 read/write (input/output buckets), CloudWatch, ECR pull. "
                    "Step Functions IAM role: EC2 RunInstances, TerminateInstances, S3, DynamoDB."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="Launch template (Spot), S3 buckets (input/output), DynamoDB (job tracking)",
                resource_types=[
                    "AWS::EC2::LaunchTemplate",
                    "AWS::S3::Bucket",
                    "AWS::DynamoDB::Table",
                ],
                imports=[
                    LayerImport(name="PrivateSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="PrivateSubnet1Id"),
                    LayerImport(name="BatchSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="BatchSecurityGroupId"),
                    LayerImport(name="BatchInstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="BatchInstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="InputBucketName", resource_logical_id="InputBucket"),
                    LayerExport(name="OutputBucketName", resource_logical_id="OutputBucket"),
                    LayerExport(name="JobTableName", resource_logical_id="JobTable"),
                    LayerExport(name="LaunchTemplateId", resource_logical_id="BatchLaunchTemplate"),
                ],
                prompt_context=(
                    "Launch template: Spot instance (capacity-optimized strategy), "
                    "UserData downloads model + input from S3, runs inference, uploads output. "
                    "S3 input bucket with lifecycle policy. S3 output bucket with versioning. "
                    "DynamoDB job table: PK=job_id, tracks status/progress/checkpoints."
                ),
            ),
            LayerSpec(
                name="orchestration",
                description="Step Functions state machine, SQS job queue, EventBridge schedule",
                resource_types=[
                    "AWS::StepFunctions::StateMachine",
                    "AWS::SQS::Queue",
                    "AWS::Events::Rule",
                ],
                imports=[
                    LayerImport(name="StepFunctionsRoleArn", source_layer=LayerName.SECURITY, parameter_name="StepFunctionsRoleArn"),
                    LayerImport(name="LaunchTemplateId", source_layer=LayerName.COMPUTE, parameter_name="LaunchTemplateId"),
                    LayerImport(name="JobTableName", source_layer=LayerName.COMPUTE, parameter_name="JobTableName"),
                ],
                exports=[
                    LayerExport(name="StateMachineArn", resource_logical_id="BatchStateMachine", attribute="Arn"),
                    LayerExport(name="JobQueueUrl", resource_logical_id="JobQueue"),
                ],
                prompt_context=(
                    "Step Functions: Submit → LaunchInstance → WaitForCompletion → Cleanup. "
                    "SQS job queue for submission with DLQ for failed jobs. "
                    "State machine handles Spot interruption: check checkpoint, re-launch if needed. "
                    "EventBridge rule for scheduled batch runs (cron parameter)."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern: Distributed Training (multi-node cluster)
# ---------------------------------------------------------------------------


def distributed_training_plan() -> LayerPlan:
    """Multi-node GPU training cluster with EFA networking and shared storage."""
    return LayerPlan(
        pattern_name="distributed-training",
        description="Multi-node training cluster with EFA, placement group, and FSx for Lustre",
        layers=[
            LayerSpec(
                name=LayerName.FOUNDATION,
                description="VPC, private subnets, NAT Gateway, S3 VPC endpoint",
                resource_types=[
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                    "AWS::EC2::NatGateway",
                    "AWS::EC2::EIP",
                    "AWS::EC2::VPCEndpoint",
                ],
                imports=[],
                exports=_FOUNDATION_EXPORTS + [
                    LayerExport(name="TrainingSubnetId", resource_logical_id="TrainingSubnet", description="Dedicated training subnet (large CIDR for cluster)"),
                ],
                prompt_context=(
                    "Single-AZ for EFA (placement group requires same AZ). "
                    "Large private subnet (/20) for training cluster. "
                    "S3 Gateway endpoint for checkpoint/data access. "
                    "NAT for ECR pulls and telemetry only."
                ),
            ),
            LayerSpec(
                name=LayerName.SECURITY,
                description="Security groups (all-traffic within cluster), IAM roles",
                resource_types=[
                    "AWS::EC2::SecurityGroup",
                    "AWS::IAM::Role",
                    "AWS::IAM::InstanceProfile",
                ],
                imports=[_SECURITY_VPC_IMPORT],
                exports=[
                    LayerExport(name="ClusterSecurityGroupId", resource_logical_id="ClusterSecurityGroup", attribute="GroupId"),
                    LayerExport(name="TrainingInstanceProfileArn", resource_logical_id="TrainingInstanceProfile", attribute="Arn"),
                ],
                prompt_context=(
                    "Cluster SG: allow ALL traffic within same SG (EFA requires this). "
                    "Management SG: SSH from AdminCIDR. "
                    "IAM role: S3 full access (checkpoints + datasets), CloudWatch, ECR pull, "
                    "FSx access, EC2 describe (for node discovery)."
                ),
            ),
            LayerSpec(
                name=LayerName.COMPUTE,
                description="Placement group, Launch Template (EFA-enabled), Auto Scaling Group",
                resource_types=[
                    "AWS::EC2::PlacementGroup",
                    "AWS::EC2::LaunchTemplate",
                    "AWS::AutoScaling::AutoScalingGroup",
                ],
                imports=[
                    LayerImport(name="TrainingSubnetId", source_layer=LayerName.FOUNDATION, parameter_name="TrainingSubnetId"),
                    LayerImport(name="ClusterSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="ClusterSecurityGroupId"),
                    LayerImport(name="TrainingInstanceProfileArn", source_layer=LayerName.SECURITY, parameter_name="TrainingInstanceProfileArn"),
                ],
                exports=[
                    LayerExport(name="PlacementGroupName", resource_logical_id="TrainingPlacementGroup"),
                    LayerExport(name="ASGName", resource_logical_id="TrainingASG"),
                ],
                prompt_context=(
                    "Placement group: cluster strategy (lowest latency for NCCL). "
                    "Launch template: p4d.24xlarge (parameterized), EFA network interface, "
                    "instance store NVMe for data loader cache. "
                    "ASG: exact capacity (no scaling — fixed cluster size parameter). "
                    "UserData: configure EFA, join training cluster, start NCCL daemon."
                ),
            ),
            LayerSpec(
                name="storage",
                description="FSx for Lustre filesystem, S3 data repository association, checkpoint bucket",
                resource_types=[
                    "AWS::FSx::FileSystem",
                    "AWS::FSx::DataRepositoryAssociation",
                    "AWS::S3::Bucket",
                ],
                imports=[
                    LayerImport(name="TrainingSubnetId", source_layer=LayerName.FOUNDATION, parameter_name="TrainingSubnetId"),
                    LayerImport(name="ClusterSecurityGroupId", source_layer=LayerName.SECURITY, parameter_name="ClusterSecurityGroupId"),
                ],
                exports=[
                    LayerExport(name="FileSystemId", resource_logical_id="TrainingFileSystem"),
                    LayerExport(name="FileSystemDnsName", resource_logical_id="TrainingFileSystem", attribute="DNSName"),
                    LayerExport(name="CheckpointBucketName", resource_logical_id="CheckpointBucket"),
                ],
                prompt_context=(
                    "FSx for Lustre: PERSISTENT_2 deployment, 1200 MB/s/TiB throughput. "
                    "Data repository association: auto-import from S3 training data bucket. "
                    "Checkpoint S3 bucket with versioning enabled. "
                    "FSx mount target in training subnet."
                ),
            ),
            LayerSpec(
                name="networking",
                description="EFA-specific networking configuration and CloudWatch monitoring",
                resource_types=[
                    "AWS::CloudWatch::Alarm",
                    "AWS::CloudWatch::Dashboard",
                ],
                imports=[
                    LayerImport(name="ASGName", source_layer=LayerName.COMPUTE, parameter_name="ASGName"),
                    LayerImport(name="FileSystemId", source_layer="storage", parameter_name="FileSystemId"),
                ],
                exports=[],
                prompt_context=(
                    "CloudWatch dashboard: GPU utilization, EFA packet rate, FSx throughput, "
                    "training loss (custom metric from application). "
                    "Alarms: node failure (ASG unhealthy), FSx throughput saturation, "
                    "GPU memory OOM events. "
                    "SNS topic for training failure notifications."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Pattern registry and lookup
# ---------------------------------------------------------------------------

PREDEFINED_PATTERNS: dict[str, Callable[[], LayerPlan]] = {
    "single": single_instance_plan,
    "single-instance": single_instance_plan,
    "standalone": single_instance_plan,
    "dev": single_instance_plan,
    "auto-scaling-fleet": auto_scaling_fleet_plan,
    "fleet": auto_scaling_fleet_plan,
    "asg": auto_scaling_fleet_plan,
    "auto-scaling": auto_scaling_fleet_plan,
    "batch-processing": batch_processing_plan,
    "batch": batch_processing_plan,
    "step-functions": batch_processing_plan,
    "offline": batch_processing_plan,
    "distributed-training": distributed_training_plan,
    "training-cluster": distributed_training_plan,
    "multi-node": distributed_training_plan,
    "distributed": distributed_training_plan,
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
