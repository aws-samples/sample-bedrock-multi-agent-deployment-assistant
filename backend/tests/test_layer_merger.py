"""Unit tests for the deterministic layer merger.

Tests cover:
1. Two-layer merge with import→export wiring (Ref)
2. Three-layer chain with transitive dependencies
3. Import parameters removed, non-import parameters preserved
4. GetAtt-style exports
5. Duplicate resource logical ID detection
6. ValueError on unresolved imports
7. All section types merge (mappings, conditions, outputs)
8. Deduplication of parameters, outputs, mappings across layers
"""

import pytest

from src.models.layer_plan import (
    LayerExport,
    LayerImport,
    LayerName,
    LayerPlan,
    LayerSpec,
)
from src.models.resource_plan import (
    CfnCondition,
    CfnMapping,
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)
from src.services.layer_merger import merge_layers


# ===================================================================
# Helpers
# ===================================================================


def _foundation_plan() -> ResourcePlan:
    """Foundation layer: VPC + Subnet."""
    return ResourcePlan(
        description="Foundation layer",
        parameters=[
            CfnParameter(logical_id="VpcCidr", type="String", default="10.0.0.0/16"),
        ],
        resources=[
            CfnResource(
                logical_id="VPC",
                type="AWS::EC2::VPC",
                properties={"CidrBlock": {"ref": "VpcCidr"}},
            ),
            CfnResource(
                logical_id="PublicSubnet1",
                type="AWS::EC2::Subnet",
                properties={
                    "VpcId": {"ref": "VPC"},
                    "CidrBlock": "10.0.1.0/24",
                },
            ),
        ],
        outputs=[
            CfnOutput(logical_id="VpcIdOutput", value={"ref": "VPC"}),
        ],
    )


def _security_plan() -> ResourcePlan:
    """Security layer: SG that imports VpcId as a parameter."""
    return ResourcePlan(
        description="Security layer",
        parameters=[
            CfnParameter(logical_id="VpcIdParam", type="String", description="Imported VPC ID"),
        ],
        resources=[
            CfnResource(
                logical_id="MgmtSG",
                type="AWS::EC2::SecurityGroup",
                properties={
                    "GroupDescription": "Management SG",
                    "VpcId": {"ref": "VpcIdParam"},
                },
            ),
        ],
    )


def _compute_plan() -> ResourcePlan:
    """Compute layer: Appliance instance importing SubnetId and SGId."""
    return ResourcePlan(
        description="Compute layer",
        parameters=[
            CfnParameter(logical_id="SubnetIdParam", type="String"),
            CfnParameter(logical_id="SGIdParam", type="String"),
            CfnParameter(logical_id="InstanceType", type="String", default="c5.xlarge"),
        ],
        resources=[
            CfnResource(
                logical_id="Appliance",
                type="AWS::EC2::Instance",
                properties={
                    "InstanceType": {"ref": "InstanceType"},
                    "SubnetId": {"ref": "SubnetIdParam"},
                    "SecurityGroupIds": [{"ref": "SGIdParam"}],
                },
            ),
        ],
        outputs=[
            CfnOutput(
                logical_id="ApplianceId",
                value={"ref": "Appliance"},
                description="Appliance Instance ID",
            ),
        ],
    )


# ===================================================================
# Two-layer merge (Foundation → Security)
# ===================================================================


class TestTwoLayerMerge:
    """Foundation exports VpcId → Security imports it."""

    @pytest.fixture
    def layer_plan(self) -> LayerPlan:
        return LayerPlan(
            pattern_name="test-two-layer",
            description="Two layer test",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC layer",
                    resource_types=["AWS::EC2::VPC", "AWS::EC2::Subnet"],
                    exports=[
                        LayerExport(name="VpcId", resource_logical_id="VPC"),
                    ],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG layer",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(
                            name="VpcId",
                            source_layer=LayerName.FOUNDATION,
                            parameter_name="VpcIdParam",
                        ),
                    ],
                ),
            ],
        )

    @pytest.fixture
    def resource_plans(self) -> dict[LayerName, ResourcePlan]:
        return {
            LayerName.FOUNDATION: _foundation_plan(),
            LayerName.SECURITY: _security_plan(),
        }

    def test_import_parameter_replaced_with_ref(self, layer_plan, resource_plans):
        """VpcIdParam in Security SG should be replaced with Ref to VPC."""
        merged = merge_layers(layer_plan, resource_plans)
        sg = next(r for r in merged.resources if r.logical_id == "MgmtSG")
        assert sg.properties["VpcId"] == {"ref": "VPC"}

    def test_import_parameter_removed(self, layer_plan, resource_plans):
        """VpcIdParam should not appear in merged parameters."""
        merged = merge_layers(layer_plan, resource_plans)
        param_ids = {p.logical_id for p in merged.parameters}
        assert "VpcIdParam" not in param_ids

    def test_non_import_parameter_preserved(self, layer_plan, resource_plans):
        """VpcCidr (not an import) should remain in merged parameters."""
        merged = merge_layers(layer_plan, resource_plans)
        param_ids = {p.logical_id for p in merged.parameters}
        assert "VpcCidr" in param_ids

    def test_all_resources_present(self, layer_plan, resource_plans):
        """All resources from both layers should be in the merged plan."""
        merged = merge_layers(layer_plan, resource_plans)
        resource_ids = {r.logical_id for r in merged.resources}
        assert resource_ids == {"VPC", "PublicSubnet1", "MgmtSG"}

    def test_outputs_merged(self, layer_plan, resource_plans):
        """Outputs from both layers should be concatenated."""
        merged = merge_layers(layer_plan, resource_plans)
        output_ids = {o.logical_id for o in merged.outputs}
        assert "VpcIdOutput" in output_ids

    def test_originals_not_mutated(self, layer_plan, resource_plans):
        """Merge should not mutate the original ResourcePlans."""
        original_sg_props = resource_plans[LayerName.SECURITY].resources[0].properties.copy()
        merge_layers(layer_plan, resource_plans)
        assert resource_plans[LayerName.SECURITY].resources[0].properties == original_sg_props


# ===================================================================
# Three-layer chain (Foundation → Security → Compute)
# ===================================================================


class TestThreeLayerChain:
    """Foundation → Security → Compute with transitive dependencies."""

    @pytest.fixture
    def layer_plan(self) -> LayerPlan:
        return LayerPlan(
            pattern_name="test-three-layer",
            description="Three layer chain",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC layer",
                    resource_types=["AWS::EC2::VPC", "AWS::EC2::Subnet"],
                    exports=[
                        LayerExport(name="VpcId", resource_logical_id="VPC"),
                        LayerExport(name="PublicSubnet1Id", resource_logical_id="PublicSubnet1"),
                    ],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG layer",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                    exports=[
                        LayerExport(name="MgmtSGId", resource_logical_id="MgmtSG"),
                    ],
                ),
                LayerSpec(
                    name=LayerName.COMPUTE,
                    description="Appliance",
                    resource_types=["AWS::EC2::Instance"],
                    imports=[
                        LayerImport(name="PublicSubnet1Id", source_layer=LayerName.FOUNDATION, parameter_name="SubnetIdParam"),
                        LayerImport(name="MgmtSGId", source_layer=LayerName.SECURITY, parameter_name="SGIdParam"),
                    ],
                ),
            ],
        )

    @pytest.fixture
    def resource_plans(self) -> dict[LayerName, ResourcePlan]:
        return {
            LayerName.FOUNDATION: _foundation_plan(),
            LayerName.SECURITY: _security_plan(),
            LayerName.COMPUTE: _compute_plan(),
        }

    def test_security_vpc_ref_wired(self, layer_plan, resource_plans):
        """Security's VpcIdParam should be replaced with Ref to VPC."""
        merged = merge_layers(layer_plan, resource_plans)
        sg = next(r for r in merged.resources if r.logical_id == "MgmtSG")
        assert sg.properties["VpcId"] == {"ref": "VPC"}

    def test_compute_subnet_ref_wired(self, layer_plan, resource_plans):
        """Compute's SubnetIdParam should be replaced with Ref to PublicSubnet1."""
        merged = merge_layers(layer_plan, resource_plans)
        fgt = next(r for r in merged.resources if r.logical_id == "Appliance")
        assert fgt.properties["SubnetId"] == {"ref": "PublicSubnet1"}

    def test_compute_sg_ref_wired(self, layer_plan, resource_plans):
        """Compute's SGIdParam in list should be replaced with Ref to MgmtSG."""
        merged = merge_layers(layer_plan, resource_plans)
        fgt = next(r for r in merged.resources if r.logical_id == "Appliance")
        assert fgt.properties["SecurityGroupIds"] == [{"ref": "MgmtSG"}]

    def test_all_import_params_removed(self, layer_plan, resource_plans):
        """All import parameters should be removed from merged output."""
        merged = merge_layers(layer_plan, resource_plans)
        param_ids = {p.logical_id for p in merged.parameters}
        assert "VpcIdParam" not in param_ids
        assert "SubnetIdParam" not in param_ids
        assert "SGIdParam" not in param_ids

    def test_non_import_params_preserved(self, layer_plan, resource_plans):
        """VpcCidr and InstanceType should remain."""
        merged = merge_layers(layer_plan, resource_plans)
        param_ids = {p.logical_id for p in merged.parameters}
        assert "VpcCidr" in param_ids
        assert "InstanceType" in param_ids

    def test_all_resources_present(self, layer_plan, resource_plans):
        """All 4 resources across 3 layers should be in merged output."""
        merged = merge_layers(layer_plan, resource_plans)
        resource_ids = {r.logical_id for r in merged.resources}
        assert resource_ids == {"VPC", "PublicSubnet1", "MgmtSG", "Appliance"}


# ===================================================================
# GetAtt exports
# ===================================================================


class TestGetAttExport:
    """Test that exports with attribute produce GetAtt references."""

    def test_get_att_wiring(self):
        plan = LayerPlan(
            pattern_name="test-getatt",
            description="GetAtt test",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC",
                    resource_types=["AWS::EC2::VPC"],
                    exports=[
                        LayerExport(
                            name="VpcCidr",
                            resource_logical_id="VPC",
                            attribute="CidrBlock",
                        ),
                    ],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(
                            name="VpcCidr",
                            source_layer=LayerName.FOUNDATION,
                            parameter_name="VpcCidrParam",
                        ),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[
                    CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"}),
                ],
            ),
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcCidrParam", type="String")],
                resources=[
                    CfnResource(
                        logical_id="SG",
                        type="AWS::EC2::SecurityGroup",
                        properties={"GroupDescription": {"ref": "VpcCidrParam"}},
                    ),
                ],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        sg = next(r for r in merged.resources if r.logical_id == "SG")
        assert sg.properties["GroupDescription"] == {"get_att": ["VPC", "CidrBlock"]}


# ===================================================================
# Duplicate resource detection
# ===================================================================


class TestDuplicateResources:
    """Duplicate resource logical IDs across layers should raise ValueError."""

    def test_duplicate_resource_raises(self):
        plan = LayerPlan(
            pattern_name="test-dupe",
            description="Dupe test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="MyResource", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                resources=[CfnResource(logical_id="MyResource", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "dup"})],
            ),
        }
        with pytest.raises(ValueError, match="Duplicate resource logical ID"):
            merge_layers(plan, resource_plans)


# ===================================================================
# Unresolved import
# ===================================================================


class TestUnresolvedImport:
    """Import referencing a non-existent export should raise ValueError."""

    def test_missing_export_raises(self):
        plan = LayerPlan(
            pattern_name="test-missing",
            description="Missing export",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC",
                    resource_types=["AWS::EC2::VPC"],
                    exports=[],  # No exports!
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcIdParam", type="String")],
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
            ),
        }
        with pytest.raises(ValueError, match="not found in exports"):
            merge_layers(plan, resource_plans)

    def test_missing_source_layer_raises(self):
        plan = LayerPlan(
            pattern_name="test-missing-layer",
            description="Missing source layer",
            layers=[
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcIdParam", type="String")],
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
            ),
        }
        with pytest.raises(ValueError, match="does not exist"):
            merge_layers(plan, resource_plans)


# ===================================================================
# All section types merge
# ===================================================================


class TestAllSectionsMerge:
    """Mappings, conditions, outputs from all layers should merge."""

    def test_mappings_merged(self):
        plan = LayerPlan(
            pattern_name="test-sections",
            description="Sections test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
                mappings=[CfnMapping(logical_id="RegionMap", mapping={"us-east-1": {"AMI": "ami-123"}})],
            ),
            LayerName.SECURITY: ResourcePlan(
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
                mappings=[CfnMapping(logical_id="InstanceMap", mapping={"small": {"Type": "c5.xlarge"}})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        mapping_ids = {m.logical_id for m in merged.mappings}
        assert mapping_ids == {"RegionMap", "InstanceMap"}

    def test_conditions_merged(self):
        plan = LayerPlan(
            pattern_name="test-conditions",
            description="Conditions test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.COMPUTE, description="b", resource_types=["AWS::EC2::Instance"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
                conditions=[CfnCondition(logical_id="HasVpc", condition={"equals": [{"ref": "VpcCidr"}, ""]})],
            ),
            LayerName.COMPUTE: ResourcePlan(
                resources=[CfnResource(logical_id="FGT", type="AWS::EC2::Instance", properties={"InstanceType": "c5.xlarge"})],
                conditions=[CfnCondition(logical_id="IsHA", condition={"equals": [{"ref": "HAMode"}, "yes"]})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        cond_ids = {c.logical_id for c in merged.conditions}
        assert cond_ids == {"HasVpc", "IsHA"}

    def test_outputs_merged(self):
        plan = LayerPlan(
            pattern_name="test-outputs",
            description="Outputs test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.COMPUTE, description="b", resource_types=["AWS::EC2::Instance"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
                outputs=[CfnOutput(logical_id="VpcId", value={"ref": "VPC"})],
            ),
            LayerName.COMPUTE: ResourcePlan(
                resources=[CfnResource(logical_id="FGT", type="AWS::EC2::Instance", properties={"InstanceType": "c5.xlarge"})],
                outputs=[CfnOutput(logical_id="InstanceId", value={"ref": "FGT"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        output_ids = {o.logical_id for o in merged.outputs}
        assert output_ids == {"VpcId", "InstanceId"}


# ===================================================================
# Deduplication
# ===================================================================


class TestDeduplication:
    """Duplicate parameters, outputs, and mappings should keep first occurrence."""

    def test_duplicate_parameters_deduped(self):
        """Same parameter in two layers — keep only the first."""
        plan = LayerPlan(
            pattern_name="test-dedup",
            description="Dedup test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                parameters=[CfnParameter(logical_id="Env", type="String", default="prod")],
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="Env", type="String", default="staging")],
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        env_params = [p for p in merged.parameters if p.logical_id == "Env"]
        assert len(env_params) == 1
        assert env_params[0].default == "prod"  # First occurrence wins

    def test_duplicate_outputs_deduped(self):
        plan = LayerPlan(
            pattern_name="test-dedup-out",
            description="Dedup outputs",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
                outputs=[CfnOutput(logical_id="StackName", value={"ref": "AWS::StackName"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
                outputs=[CfnOutput(logical_id="StackName", value={"ref": "AWS::StackId"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        stack_outputs = [o for o in merged.outputs if o.logical_id == "StackName"]
        assert len(stack_outputs) == 1

    def test_duplicate_mappings_deduped(self):
        plan = LayerPlan(
            pattern_name="test-dedup-map",
            description="Dedup mappings",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.COMPUTE, description="b", resource_types=["AWS::EC2::Instance"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
                mappings=[CfnMapping(logical_id="RegionMap", mapping={"us-east-1": {"AMI": "ami-aaa"}})],
            ),
            LayerName.COMPUTE: ResourcePlan(
                resources=[CfnResource(logical_id="FGT", type="AWS::EC2::Instance", properties={"InstanceType": "c5.xlarge"})],
                mappings=[CfnMapping(logical_id="RegionMap", mapping={"us-east-1": {"AMI": "ami-bbb"}})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        region_maps = [m for m in merged.mappings if m.logical_id == "RegionMap"]
        assert len(region_maps) == 1
        assert region_maps[0].mapping["us-east-1"]["AMI"] == "ami-aaa"  # First wins


# ===================================================================
# Deeply nested ref replacement
# ===================================================================


class TestDeepRefReplacement:
    """Refs to import params should be replaced even in deeply nested structures."""

    def test_ref_in_nested_list(self):
        plan = LayerPlan(
            pattern_name="test-nested",
            description="Nested ref",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC",
                    resource_types=["AWS::EC2::VPC"],
                    exports=[LayerExport(name="VpcId", resource_logical_id="VPC")],
                ),
                LayerSpec(
                    name=LayerName.COMPUTE,
                    description="Instance",
                    resource_types=["AWS::EC2::Instance"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.COMPUTE: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcIdParam", type="String")],
                resources=[
                    CfnResource(
                        logical_id="FGT",
                        type="AWS::EC2::Instance",
                        properties={
                            "Tags": [
                                {"Key": "VpcId", "Value": {"ref": "VpcIdParam"}},
                                {"Key": "Name", "Value": "Appliance"},
                            ],
                            "NetworkInterfaces": [
                                {
                                    "SubnetId": "static-subnet",
                                    "Groups": [{"ref": "VpcIdParam"}],
                                }
                            ],
                        },
                    ),
                ],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        fgt = next(r for r in merged.resources if r.logical_id == "FGT")
        # Ref in nested tag value
        assert fgt.properties["Tags"][0]["Value"] == {"ref": "VPC"}
        # Ref in nested list within dict
        assert fgt.properties["NetworkInterfaces"][0]["Groups"] == [{"ref": "VPC"}]

    def test_ref_in_output_value_replaced(self):
        """Import param refs in output values should also be replaced."""
        plan = LayerPlan(
            pattern_name="test-output-ref",
            description="Output ref",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC",
                    resource_types=["AWS::EC2::VPC"],
                    exports=[LayerExport(name="VpcId", resource_logical_id="VPC")],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcIdParam", type="String")],
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
                outputs=[
                    CfnOutput(
                        logical_id="SGVpcId",
                        value={"ref": "VpcIdParam"},
                        description="VPC ID used by SG",
                    ),
                ],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        out = next(o for o in merged.outputs if o.logical_id == "SGVpcId")
        assert out.value == {"ref": "VPC"}

    def test_ref_in_condition_replaced(self):
        """Import param refs in conditions should also be replaced."""
        plan = LayerPlan(
            pattern_name="test-cond-ref",
            description="Condition ref",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="VPC",
                    resource_types=["AWS::EC2::VPC"],
                    exports=[LayerExport(name="VpcId", resource_logical_id="VPC")],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="SG",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcIdParam"),
                    ],
                ),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                parameters=[CfnParameter(logical_id="VpcIdParam", type="String")],
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
                conditions=[
                    CfnCondition(
                        logical_id="HasVpc",
                        condition={"not": [{"equals": [{"ref": "VpcIdParam"}, ""]}]},
                    ),
                ],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        cond = next(c for c in merged.conditions if c.logical_id == "HasVpc")
        assert cond.condition == {"not": [{"equals": [{"ref": "VPC"}, ""]}]}


# ===================================================================
# Description merging
# ===================================================================


class TestDescriptionMerge:
    """Layer plan description takes precedence; falls back to joining layer descriptions."""

    def test_plan_description_used(self):
        plan = LayerPlan(
            pattern_name="test-desc",
            description="My custom description",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                description="Foundation desc",
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        assert merged.description == "My custom description"

    def test_fallback_to_joined_descriptions(self):
        plan = LayerPlan(
            pattern_name="test-desc-fallback",
            description="",  # Empty — triggers fallback
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                description="Foundation desc",
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
            LayerName.SECURITY: ResourcePlan(
                description="Security desc",
                resources=[CfnResource(logical_id="SG", type="AWS::EC2::SecurityGroup", properties={"GroupDescription": "test"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        assert "Foundation desc" in merged.description
        assert "Security desc" in merged.description


# ===================================================================
# Missing layer in resource plans (graceful skip)
# ===================================================================


class TestMissingLayerSkip:
    """If a layer spec exists but no ResourcePlan was generated, skip it."""

    def test_missing_layer_skipped(self):
        plan = LayerPlan(
            pattern_name="test-skip",
            description="Skip test",
            layers=[
                LayerSpec(name=LayerName.FOUNDATION, description="a", resource_types=["AWS::EC2::VPC"]),
                LayerSpec(name=LayerName.SECURITY, description="b", resource_types=["AWS::EC2::SecurityGroup"]),
            ],
        )
        # Only provide Foundation, not Security
        resource_plans = {
            LayerName.FOUNDATION: ResourcePlan(
                resources=[CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
            ),
        }
        merged = merge_layers(plan, resource_plans)
        assert len(merged.resources) == 1
        assert merged.resources[0].logical_id == "VPC"
