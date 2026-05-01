"""Unit tests for JSON CloudFormation assembly (Path 3).

Tests focus on:
1. Intrinsic function conversion (lowercase dict -> CFN JSON long-form)
2. Template assembly (ResourcePlan -> valid JSON)
3. Round-trip integrity (assemble_json -> json.loads -> verify structure)
4. Regression: assembled JSON passes the structural validator
"""

import json

from src.models.resource_plan import (
    CfnCondition,
    CfnMapping,
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)
from src.services.cfn_assembler import (
    _convert_intrinsics_json,
    assemble_json,
)


# ===================================================================
# _convert_intrinsics_json
# ===================================================================


class TestConvertIntrinsicsJson:
    """Tests for intrinsic function dict -> CFN JSON long-form conversion."""

    def test_ref(self):
        result = _convert_intrinsics_json({"ref": "MyVPC"})
        assert result == {"Ref": "MyVPC"}

    def test_sub(self):
        result = _convert_intrinsics_json({"sub": "${AWS::StackName}-vpc"})
        assert result == {"Fn::Sub": "${AWS::StackName}-vpc"}

    def test_get_att_keeps_list_form(self):
        """GetAtt in JSON keeps the list form (no dot-joining like YAML)."""
        result = _convert_intrinsics_json({"get_att": ["MyInstance", "PublicIp"]})
        assert result == {"Fn::GetAtt": ["MyInstance", "PublicIp"]}

    def test_select(self):
        result = _convert_intrinsics_json({"select": [0, {"get_azs": ""}]})
        assert result == {"Fn::Select": [0, {"Fn::GetAZs": ""}]}

    def test_join_with_refs(self):
        result = _convert_intrinsics_json(
            {"join": [",", [{"ref": "A"}, {"ref": "B"}]]}
        )
        assert result == {"Fn::Join": [",", [{"Ref": "A"}, {"Ref": "B"}]]}

    def test_if_condition(self):
        result = _convert_intrinsics_json(
            {"if": ["IsHA", {"ref": "Active"}, {"ref": "AWS::NoValue"}]}
        )
        assert result == {
            "Fn::If": ["IsHA", {"Ref": "Active"}, {"Ref": "AWS::NoValue"}]
        }

    def test_find_in_map(self):
        result = _convert_intrinsics_json(
            {"find_in_map": ["RegionMap", {"ref": "AWS::Region"}, "AMI"]}
        )
        assert result == {
            "Fn::FindInMap": ["RegionMap", {"Ref": "AWS::Region"}, "AMI"]
        }

    def test_base64_nested(self):
        result = _convert_intrinsics_json(
            {"base64": {"sub": "#!/bin/bash\necho hello"}}
        )
        assert result == {"Fn::Base64": {"Fn::Sub": "#!/bin/bash\necho hello"}}

    def test_equals(self):
        result = _convert_intrinsics_json({"equals": ["a", "b"]})
        assert result == {"Fn::Equals": ["a", "b"]}

    def test_condition(self):
        result = _convert_intrinsics_json({"condition": "IsHA"})
        assert result == {"Condition": "IsHA"}

    def test_not(self):
        result = _convert_intrinsics_json({"not": [{"equals": ["a", "b"]}]})
        assert result == {"Fn::Not": [{"Fn::Equals": ["a", "b"]}]}

    def test_and(self):
        result = _convert_intrinsics_json(
            {"and": [{"condition": "A"}, {"condition": "B"}]}
        )
        assert result == {"Fn::And": [{"Condition": "A"}, {"Condition": "B"}]}

    def test_or(self):
        result = _convert_intrinsics_json(
            {"or": [{"condition": "A"}, {"condition": "B"}]}
        )
        assert result == {"Fn::Or": [{"Condition": "A"}, {"Condition": "B"}]}

    def test_import_value(self):
        result = _convert_intrinsics_json({"import_value": "SharedStack-VPCID"})
        assert result == {"Fn::ImportValue": "SharedStack-VPCID"}

    def test_split(self):
        result = _convert_intrinsics_json({"split": [",", {"ref": "Param"}]})
        assert result == {"Fn::Split": [",", {"Ref": "Param"}]}

    def test_cidr(self):
        result = _convert_intrinsics_json({"cidr": ["10.0.0.0/16", 6, 8]})
        assert result == {"Fn::Cidr": ["10.0.0.0/16", 6, 8]}

    def test_get_azs(self):
        result = _convert_intrinsics_json({"get_azs": ""})
        assert result == {"Fn::GetAZs": ""}

    def test_plain_string_passthrough(self):
        assert _convert_intrinsics_json("hello") == "hello"

    def test_plain_int_passthrough(self):
        assert _convert_intrinsics_json(42) == 42

    def test_plain_bool_passthrough(self):
        assert _convert_intrinsics_json(True) is True

    def test_none_passthrough(self):
        assert _convert_intrinsics_json(None) is None

    def test_regular_dict_recursion(self):
        """Non-intrinsic dicts should have their values recursed."""
        result = _convert_intrinsics_json({
            "CidrBlock": {"ref": "VpcCidr"},
            "EnableDnsSupport": True,
        })
        assert result == {
            "CidrBlock": {"Ref": "VpcCidr"},
            "EnableDnsSupport": True,
        }

    def test_list_recursion(self):
        result = _convert_intrinsics_json([{"ref": "A"}, "plain", 1])
        assert result == [{"Ref": "A"}, "plain", 1]

    def test_deeply_nested(self):
        """Three-level nesting: base64 -> join -> ref."""
        result = _convert_intrinsics_json(
            {"base64": {"join": ["\n", ["line1", {"ref": "Param"}]]}}
        )
        assert result == {
            "Fn::Base64": {"Fn::Join": ["\n", ["line1", {"Ref": "Param"}]]}
        }

    def test_multi_key_dict_not_intrinsic(self):
        """A dict with multiple keys is NOT an intrinsic function."""
        result = _convert_intrinsics_json({"ref": "A", "extra": "B"})
        assert isinstance(result, dict)
        assert "ref" in result
        assert "extra" in result


# ===================================================================
# assemble_json()
# ===================================================================


class TestAssembleJson:
    """Tests for ResourcePlan -> CloudFormation JSON assembly."""

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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["AWSTemplateFormatVersion"] == "2010-09-09"
        assert "MyVPC" in parsed["Resources"]
        assert parsed["Resources"]["MyVPC"]["Type"] == "AWS::EC2::VPC"

    def test_valid_json(self):
        """Output must be valid JSON (no exceptions from json.loads)."""
        plan = self._minimal_plan()
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_intrinsic_functions_in_json(self):
        """Intrinsic functions should use long-form in JSON output."""
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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Resources"]["VPC"]["Properties"]["CidrBlock"] == {"Ref": "VpcCidr"}
        assert "VpcCidr" in parsed["Parameters"]

    def test_description_included(self):
        plan = self._minimal_plan(description="My Inference Stack")
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Description"] == "My Inference Stack"

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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert "VpcCidr" in parsed["Parameters"]
        assert "InstanceType" in parsed["Parameters"]
        assert parsed["Parameters"]["InstanceType"]["AllowedValues"] == ["c5.xlarge", "c5.2xlarge"]

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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Outputs"]["VpcId"]["Value"] == {"Ref": "MyVPC"}

    def test_mappings_section(self):
        plan = self._minimal_plan(
            mappings=[
                CfnMapping(
                    logical_id="RegionMap",
                    mapping={"us-east-1": {"AMI": "ami-12345"}},
                ),
            ],
        )
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Mappings"]["RegionMap"]["us-east-1"]["AMI"] == "ami-12345"

    def test_conditions_section(self):
        plan = self._minimal_plan(
            conditions=[
                CfnCondition(
                    logical_id="IsHA",
                    condition={"equals": [{"ref": "HAMode"}, "active-passive"]},
                ),
            ],
        )
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Conditions"]["IsHA"] == {
            "Fn::Equals": [{"Ref": "HAMode"}, "active-passive"]
        }

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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Resources"]["Subnet"]["DependsOn"] == ["VPC"]

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
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Resources"]["PassiveInstance"]["Condition"] == "IsHA"

    def test_no_empty_sections(self):
        """Empty optional sections should not appear in output."""
        plan = self._minimal_plan()
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert "Parameters" not in parsed
        assert "Outputs" not in parsed
        assert "Mappings" not in parsed
        assert "Conditions" not in parsed

    def test_nested_intrinsics_in_json(self):
        """Appliance UserData pattern: base64 -> sub."""
        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="FGT",
                    type="AWS::EC2::Instance",
                    properties={
                        "InstanceType": "c5.xlarge",
                        "UserData": {"base64": {"sub": "config system interface\nedit port1\nend"}},
                    },
                ),
            ],
        )
        result = assemble_json(plan)
        parsed = json.loads(result)
        user_data = parsed["Resources"]["FGT"]["Properties"]["UserData"]
        assert user_data == {
            "Fn::Base64": {"Fn::Sub": "config system interface\nedit port1\nend"}
        }

    def test_output_with_export(self):
        plan = self._minimal_plan(
            outputs=[
                CfnOutput(
                    logical_id="VpcId",
                    value={"ref": "MyVPC"},
                    export_name={"sub": "${AWS::StackName}-VpcId"},
                ),
            ],
        )
        result = assemble_json(plan)
        parsed = json.loads(result)
        assert parsed["Outputs"]["VpcId"]["Export"] == {
            "Name": {"Fn::Sub": "${AWS::StackName}-VpcId"}
        }

    def test_passes_structural_validation(self):
        """Regression test: assembled JSON must pass the structural validator.

        This is the critical test — the original bug was that cfn_dump()
        YAML output with !Ref tags failed yaml.safe_load() in the
        structural validator.  JSON output must parse cleanly.
        """
        from src.validation.structural import validate_structural

        plan = self._minimal_plan(
            resources=[
                CfnResource(
                    logical_id="VPC",
                    type="AWS::EC2::VPC",
                    properties={"CidrBlock": {"ref": "VpcCidr"}},
                ),
            ],
            parameters=[
                CfnParameter(logical_id="VpcCidr", type="String", default="10.0.0.0/16"),
            ],
        )
        result = assemble_json(plan)
        findings = validate_structural(result)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"Structural errors: {[f.message for f in errors]}"

    def test_complex_template_passes_structural(self):
        """Complex template with all section types passes structural validation."""
        from src.validation.structural import validate_structural

        plan = ResourcePlan(
            description="Complex Appliance HA stack",
            parameters=[
                CfnParameter(logical_id="VpcCidr", type="String", default="10.0.0.0/16"),
                CfnParameter(logical_id="HAMode", type="String", default="active-passive"),
            ],
            mappings=[
                CfnMapping(
                    logical_id="RegionMap",
                    mapping={"us-east-1": {"AMI": "ami-12345"}},
                ),
            ],
            conditions=[
                CfnCondition(
                    logical_id="IsHA",
                    condition={"equals": [{"ref": "HAMode"}, "active-passive"]},
                ),
            ],
            resources=[
                CfnResource(
                    logical_id="VPC",
                    type="AWS::EC2::VPC",
                    properties={
                        "CidrBlock": {"ref": "VpcCidr"},
                        "EnableDnsSupport": True,
                        "Tags": [{"Key": "Name", "Value": {"sub": "${AWS::StackName}-vpc"}}],
                    },
                ),
                CfnResource(
                    logical_id="Appliance",
                    type="AWS::EC2::Instance",
                    properties={
                        "InstanceType": "c5.xlarge",
                        "ImageId": {"find_in_map": ["RegionMap", {"ref": "AWS::Region"}, "AMI"]},
                        "UserData": {"base64": {"sub": "config system global\nset hostname FGT\nend"}},
                    },
                    depends_on=["VPC"],
                ),
            ],
            outputs=[
                CfnOutput(
                    logical_id="VpcId",
                    value={"ref": "VPC"},
                    description="VPC ID",
                ),
                CfnOutput(
                    logical_id="ApplianceId",
                    value={"ref": "Appliance"},
                    condition="IsHA",
                ),
            ],
        )
        result = assemble_json(plan)
        findings = validate_structural(result)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"Structural errors: {[f.message for f in errors]}"

        # Verify round-trip
        parsed = json.loads(result)
        assert len(parsed["Resources"]) == 2
        assert len(parsed["Parameters"]) == 2
        assert "Mappings" in parsed
        assert "Conditions" in parsed
        assert len(parsed["Outputs"]) == 2


# ===================================================================
# detect_format_suffix
# ===================================================================


class TestDetectFormatSuffix:
    """Tests for format detection helper."""

    def test_json_detected(self):
        from src.validation.structural import detect_format_suffix
        assert detect_format_suffix('{"AWSTemplateFormatVersion": "2010-09-09"}') == ".json"

    def test_json_with_whitespace(self):
        from src.validation.structural import detect_format_suffix
        assert detect_format_suffix('  \n  {"key": "value"}') == ".json"

    def test_yaml_detected(self):
        from src.validation.structural import detect_format_suffix
        assert detect_format_suffix("AWSTemplateFormatVersion: '2010-09-09'") == ".yaml"

    def test_yaml_with_comment(self):
        from src.validation.structural import detect_format_suffix
        assert detect_format_suffix("# CloudFormation\nResources:") == ".yaml"
