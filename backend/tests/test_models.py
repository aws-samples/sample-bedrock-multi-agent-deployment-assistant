from src.models.requirements import InterviewOutput, UseCases, WorkloadResilience
from src.models.design import (
    DesignOption,
    DesignRecommendation,
    VPCBlueprint,
    ApplianceBlueprint,
    InterfaceBlueprint,
    KBReference,
    DeploymentParameters,
    DesignTaskStatus,
    compute_requirements_hash,
)
from src.models.project import Project, ProjectStatus

import pytest


def test_interview_output():
    doc = InterviewOutput(
        use_cases=["realtime-inference", "training"],
        gpu_budget="high",
        availability_requirement="production-multi-az",
        data_sensitivity="confidential",
        compliance=["soc2"],
        solution_description="Deploy real-time inference with training pipeline",
    )
    assert "realtime-inference" in doc.use_cases
    assert doc.gpu_budget == "high"


def _make_design_option(**overrides) -> DesignOption:
    """Helper to create a valid DesignOption with all required fields."""
    defaults = dict(
        name="Standard HA",
        description="Active-Passive GPU inference cluster",
        architecture_summary="Two GPU instances in HA across dual AZ",
        pros=["High availability", "Simple failover"],
        cons=["Higher cost", "Passive instance idle"],
        estimated_monthly_cost_usd=1500.0,
        security_posture_rating=4,
        complexity_rating=2,
        deployment_pattern="ha-dual-az",
        use_case="realtime-inference",
        ha_mode="active-passive",
        appliance_instance_type="g5.xlarge",
        aws_services=["VPC", "EC2", "ELB"],
        vpc_topology=[
            VPCBlueprint(
                role="security",
                subnet_roles=["public", "private", "ha-sync"],
                availability_zones=2,
            ),
        ],
        appliance_topology=[
            ApplianceBlueprint(
                role="active",
                vpc_role="security",
                interfaces=[
                    InterfaceBlueprint(port_name="port1", subnet_role="public", description="WAN"),
                    InterfaceBlueprint(port_name="port2", subnet_role="private", description="LAN"),
                    InterfaceBlueprint(port_name="port3", subnet_role="ha-sync", description="HA heartbeat"),
                ],
            ),
        ],
        kb_references=[
            KBReference(source_uri="s3://kb/doc1.md", excerpt="HA deployment guide", relevance_score=0.95),
        ],
    )
    defaults.update(overrides)
    return DesignOption(**defaults)


def test_design_option():
    option = _make_design_option()
    assert option.security_posture_rating == 4
    assert option.deployment_pattern == "ha-dual-az"
    assert len(option.vpc_topology) == 1
    assert len(option.appliance_topology) == 1


def test_design_option_interface_validation():
    """Interface subnet_role must exist in a VPC's subnet_roles."""
    with pytest.raises(ValueError, match="subnet_role"):
        _make_design_option(
            appliance_topology=[
                ApplianceBlueprint(
                    role="active",
                    vpc_role="security",
                    interfaces=[
                        InterfaceBlueprint(
                            port_name="port1",
                            subnet_role="nonexistent",
                            description="Bad ref",
                        ),
                    ],
                ),
            ],
        )


def test_design_option_template_consistency():
    """has_code_template=True requires template_s3_prefix."""
    with pytest.raises(ValueError, match="template_s3_prefix"):
        _make_design_option(has_code_template=True, template_s3_prefix=None)


def test_design_recommendation():
    options = [
        _make_design_option(name="Cost Optimized", complexity_rating=1),
        _make_design_option(name="Balanced", complexity_rating=3),
        _make_design_option(name="Enterprise", complexity_rating=5),
    ]
    rec = DesignRecommendation(
        options=options,
        recommended_option_index=1,
        rationale="Balanced option fits most use cases",
        requirements_summary="Real-time inference with training, dual-AZ HA",
    )
    assert rec.recommended_option_index == 1
    assert len(rec.options) == 3


def test_deployment_parameters_cidr_validation():
    params = DeploymentParameters(
        aws_region="us-east-1",
        vpc_cidr="10.0.0.0/16",
        project_name="test",
    )
    assert params.vpc_cidr == "10.0.0.0/16"

    with pytest.raises(ValueError, match="CIDR"):
        DeploymentParameters(
            aws_region="us-east-1",
            vpc_cidr="not-a-cidr",
            project_name="test",
        )


def test_deployment_parameters_region_validation():
    with pytest.raises(ValueError, match="region"):
        DeploymentParameters(
            aws_region="invalid",
            vpc_cidr="10.0.0.0/16",
            project_name="test",
        )


def test_design_task_status():
    assert DesignTaskStatus.QUEUED.value == "queued"
    assert DesignTaskStatus.COMPLETED.value == "completed"


def test_requirements_hash_stability():
    reqs = {"use_cases": ["realtime-inference"], "gpu_budget": "high"}
    h1 = compute_requirements_hash(reqs)
    h2 = compute_requirements_hash(reqs)
    assert h1 == h2
    assert len(h1) == 16


def test_project():
    project = Project(
        tenant_id="tenant-123",
        project_id="proj-456",
        name="Test Project",
    )
    assert project.status == ProjectStatus.REQUIREMENTS
