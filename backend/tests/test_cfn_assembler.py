"""Unit tests for the CFN Assembler — converts ResourcePlan to CloudFormation YAML.

Tests focus on:
1. Intrinsic function conversion (JSON dict -> CfnTag)
2. Template assembly (ResourcePlan -> valid YAML)
3. Parameter default injection
4. Round-trip integrity (assemble -> cfn_load -> verify structure)
"""

import yaml

from src.models.resource_plan import (
    CfnCondition,
    CfnMapping,
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)
from src.services.cfn_assembler import (
    _convert_intrinsics,
    assemble,
    inject_parameter_defaults,
)
from src.utils.cfn_yaml import CfnTag, cfn_load


# ===================================================================
# _convert_intrinsics
# ===================================================================


class TestConvertIntrinsics:
    """Tests for intrinsic function dict -> CfnTag conversion."""

    def test_ref(self):
        result = _convert_intrinsics({"ref": "MyVPC"})
        assert isinstance(result, CfnTag)
        assert result.tag == "!Ref"
        assert result.value == "MyVPC"

    def test_sub(self):
        result = _convert_intrinsics({"sub": "${AWS::StackName}-vpc"})
        assert isinstance(result, CfnTag)
        assert result.tag == "!Sub"
        assert result.value == "${AWS::StackName}-vpc"

    def test_get_att_list_to_dotted(self):
        result = _convert_intrinsics({"get_att": ["MyInstance", "PublicIp"]})
        assert isinstance(result, CfnTag)
        assert result.tag == "!GetAtt"
        assert result.value == "MyInstance.PublicIp"

    def test_select(self):
        result = _convert_intrinsics({"select": [0, {"get_azs": ""}]})
        assert isinstance(result, CfnTag)
        assert result.tag == "!Select"
        inner = result.value
        assert isinstance(inner, list)
        assert inner[0] == 0
        assert isinstance(inner[1], CfnTag)
        assert inner[1].tag == "!GetAZs"

    def test_join_with_refs(self):
        result = _convert_intrinsics(
            {"join": [",", [{"ref": "A"}, {"ref": "B"}]]}
        )
        assert isinstance(result, CfnTag)
        assert result.tag == "!Join"
        assert result.value[0] == ","
        assert isinstance(result.value[1], list)
        assert all(isinstance(r, CfnTag) for r in result.value[1])

    def test_if_condition(self):
        result = _convert_intrinsics(
            {"if": ["IsHA", {"ref": "Active"}, {"ref": "AWS::NoValue"}]}
        )
        assert isinstance(result, CfnTag)
        assert result.tag == "!If"

    def test_find_in_map(self):
        result = _convert_intrinsics(
            {"find_in_map": ["RegionMap", {"ref": "AWS::Region"}, "AMI"]}
        )
        assert isinstance(result, CfnTag)
        assert result.tag == "!FindInMap"

    def test_base64_nested(self):
        result = _convert_intrinsics({"base64": {"sub": "#!/bin/bash\necho hello"}})
        assert isinstance(result, CfnTag)
        assert result.tag == "!Base64"
        assert isinstance(result.value, CfnTag)
        assert result.value.tag == "!Sub"

    def test_equals(self):
        result = _convert_intrinsics({"equals": ["a", "b"]})
        assert isinstance(result, CfnTag)
        assert result.tag == "!Equals"

    def test_plain_string_passthrough(self):
        assert _convert_intrinsics("hello") == "hello"

    def test_plain_int_passthrough(self):
        assert _convert_intrinsics(42) == 42

    def test_plain_bool_passthrough(self):
        assert _convert_intrinsics(True) is True

    def test_none_passthrough(self):
        assert _convert_intrinsics(None) is None

    def test_regular_dict_recursion(self):
        """Non-intrinsic dicts should have their values recursed."""
        result = _convert_intrinsics({
            "CidrBlock": {"ref": "VpcCidr"},
            "EnableDnsSupport": True,
        })
        assert isinstance(result, dict)
        assert isinstance(result["CidrBlock"], CfnTag)
        assert result["EnableDnsSupport"] is True

    def test_list_recursion(self):
        result = _convert_intrinsics([{"ref": "A"}, "plain", 1])
        assert isinstance(result, list)
        assert isinstance(result[0], CfnTag)
        assert result[1] == "plain"
        assert result[2] == 1

    def test_deeply_nested(self):
        """Three-level nesting: base64 -> sub -> ref inside the sub."""
        result = _convert_intrinsics(
            {"base64": {"join": ["\n", ["line1", {"ref": "Param"}]]}}
        )
        assert isinstance(result, CfnTag)
        assert result.tag == "!Base64"
        assert isinstance(result.value, CfnTag)
        assert result.value.tag == "!Join"

    def test_multi_key_dict_not_intrinsic(self):
        """A dict with multiple keys is NOT an intrinsic function."""
        result = _convert_intrinsics({"ref": "A", "extra": "B"})
        assert isinstance(result, dict)
        assert "ref" in result
        assert "extra" in result


# ===================================================================
# assemble()
# ===================================================================


class TestAssemble:
    """Tests for ResourcePlan -> CloudFormation YAML assembly."""

    def _minimal_plan(self, **kwargs) -> ResourcePlan:
        """Create a minimal valid ResourcePlan."""
        defaults = {
            "resources": [
                CfnResource(
                    logical_id="MyVPC",
                    type="AWS::EC2::VPC",
                    properties={"CidrBlock": "10.0.0.0/16"},
                )
            ],
        }
        defaults.update(kwargs)
        return ResourcePlan(**defaults)

    def test_minimal_template(self):
        plan = self._minimal_plan()
        result = assemble(plan)
        assert "AWSTemplateFormatVersion" in result
        assert "Resources" in result
        assert "MyVPC" in result
        assert "AWS::EC2::VPC" in result

    def test_template_parseable_by_yaml(self):
        plan = self._minimal_plan()
        result = assemble(plan)
        parsed = yaml.safe_load(result)
        assert isinstance(parsed, dict)
        assert "AWSTemplateFormatVersion" in parsed

    def test_template_parseable_by_cfn_load(self):
        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="VPC",
                    type="AWS::EC2::VPC",
                    properties={"CidrBlock": {"ref": "VpcCidr"}},
                )
            ],
            parameters=[
                CfnParameter(logical_id="VpcCidr", type="String", default="10.0.0.0/16"),
            ],
        )
        result = assemble(plan)
        parsed = cfn_load(result)
        assert "Resources" in parsed
        assert "Parameters" in parsed

    def test_description_included(self):
        plan = self._minimal_plan(description="My Inference Stack")
        result = assemble(plan)
        assert "My Inference Stack" in result

    def test_parameters_section(self):
        plan = self._minimal_plan(
            parameters=[
                CfnParameter(
                    logical_id="VpcCidr",
                    type="String",
                    default="10.0.0.0/16",
                    description="VPC CIDR block",
                ),
                CfnParameter(
                    logical_id="InstanceType",
                    type="String",
                    default="c5.xlarge",
                    allowed_values=["c5.xlarge", "c5.2xlarge"],
                ),
            ],
        )
        result = assemble(plan)
        assert "VpcCidr" in result
        assert "InstanceType" in result
        assert "AllowedValues" in result

    def test_outputs_section(self):
        plan = self._minimal_plan(
            outputs=[
                CfnOutput(
                    logical_id="VpcId",
                    value={"ref": "MyVPC"},
                    description="VPC ID",
                ),
            ],
        )
        result = assemble(plan)
        assert "Outputs" in result
        assert "VpcId" in result

    def test_mappings_section(self):
        plan = self._minimal_plan(
            mappings=[
                CfnMapping(
                    logical_id="RegionMap",
                    mapping={"us-east-1": {"AMI": "ami-12345"}},
                ),
            ],
        )
        result = assemble(plan)
        assert "Mappings" in result
        assert "RegionMap" in result

    def test_conditions_section(self):
        plan = self._minimal_plan(
            conditions=[
                CfnCondition(
                    logical_id="IsHA",
                    condition={"equals": [{"ref": "HAMode"}, "active-passive"]},
                ),
            ],
        )
        result = assemble(plan)
        assert "Conditions" in result
        assert "IsHA" in result

    def test_resource_with_depends_on(self):
        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="VPC",
                    type="AWS::EC2::VPC",
                    properties={"CidrBlock": "10.0.0.0/16"},
                ),
                CfnResource(
                    logical_id="Subnet",
                    type="AWS::EC2::Subnet",
                    properties={"VpcId": {"ref": "VPC"}, "CidrBlock": "10.0.1.0/24"},
                    depends_on=["VPC"],
                ),
            ],
        )
        result = assemble(plan)
        assert "DependsOn" in result

    def test_resource_with_condition(self):
        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="PassiveInstance",
                    type="AWS::EC2::Instance",
                    properties={"InstanceType": "c5.xlarge"},
                    condition="IsHA",
                ),
            ],
        )
        result = assemble(plan)
        parsed = cfn_load(result)
        assert parsed["Resources"]["PassiveInstance"]["Condition"] == "IsHA"

    def test_intrinsic_functions_in_properties(self):
        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="ENI",
                    type="AWS::EC2::NetworkInterface",
                    properties={
                        "SubnetId": {"ref": "PublicSubnet"},
                        "GroupSet": [{"ref": "MgmtSG"}],
                        "SourceDestCheck": False,
                    },
                ),
            ],
        )
        result = assemble(plan)
        # Should produce valid YAML with !Ref tags
        parsed = cfn_load(result)
        assert "ENI" in parsed["Resources"]

    def test_no_empty_sections(self):
        """Empty optional sections should not appear in output."""
        plan = self._minimal_plan()
        result = assemble(plan)
        assert "Parameters" not in result
        assert "Outputs" not in result
        assert "Mappings" not in result
        assert "Conditions" not in result

    def test_passes_structural_validation(self):
        """Assembled template should pass the structural validator."""
        from src.validation.structural import validate_structural

        plan = self._minimal_plan()
        result = assemble(plan)
        findings = validate_structural(result)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"Structural errors: {[f.message for f in errors]}"


# ===================================================================
# inject_parameter_defaults()
# ===================================================================


class TestInjectParameterDefaults:
    """Tests for programmatic parameter default injection."""

    _TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  VpcCidr:
    Type: String
    Default: 10.0.0.0/16
  InstanceType:
    Type: String
    Default: c5.large
Resources:
  VPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: !Ref VpcCidr
"""

    def test_updates_matching_defaults(self):
        result = inject_parameter_defaults(
            self._TEMPLATE, {"VpcCidr": "172.16.0.0/16"}
        )
        parsed = cfn_load(result)
        assert parsed["Parameters"]["VpcCidr"]["Default"] == "172.16.0.0/16"

    def test_leaves_unmatched_defaults(self):
        result = inject_parameter_defaults(
            self._TEMPLATE, {"VpcCidr": "172.16.0.0/16"}
        )
        parsed = cfn_load(result)
        assert parsed["Parameters"]["InstanceType"]["Default"] == "c5.large"

    def test_ignores_nonexistent_parameters(self):
        result = inject_parameter_defaults(
            self._TEMPLATE, {"NonExistent": "value"}
        )
        parsed = cfn_load(result)
        assert "NonExistent" not in parsed["Parameters"]

    def test_updates_multiple_defaults(self):
        result = inject_parameter_defaults(
            self._TEMPLATE,
            {"VpcCidr": "172.16.0.0/16", "InstanceType": "c5.2xlarge"},
        )
        parsed = cfn_load(result)
        assert parsed["Parameters"]["VpcCidr"]["Default"] == "172.16.0.0/16"
        assert parsed["Parameters"]["InstanceType"]["Default"] == "c5.2xlarge"

    def test_preserves_resources(self):
        result = inject_parameter_defaults(
            self._TEMPLATE, {"VpcCidr": "172.16.0.0/16"}
        )
        parsed = cfn_load(result)
        assert "VPC" in parsed["Resources"]
        assert parsed["Resources"]["VPC"]["Type"] == "AWS::EC2::VPC"

    def test_raises_on_invalid_template(self):
        import pytest

        with pytest.raises(ValueError, match="not a mapping"):
            inject_parameter_defaults("not a template", {})

    def test_empty_defaults_returns_unchanged(self):
        result = inject_parameter_defaults(self._TEMPLATE, {})
        parsed = cfn_load(result)
        assert parsed["Parameters"]["VpcCidr"]["Default"] == "10.0.0.0/16"
