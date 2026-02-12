"""Unit tests for the spec validator — Phase 5 pre-assembly validation.

Tests cover:
  - Valid resource type passes with no findings
  - Invalid resource type is caught (SP001)
  - Invalid property names are caught (SP002)
  - Missing required properties are caught (SP003)
  - Dangling Ref targets are caught (SP004)
  - Dangling GetAtt targets are caught (SP005)
  - Custom:: resources are skipped
  - Graceful degradation when cfn-lint schemas are unavailable

All tests mock the cfn-lint schema loading so they run regardless of
whether cfn-lint is installed in the test environment.
"""

from unittest.mock import patch

import pytest

from src.models.resource_plan import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CfnResource,
    ResourcePlan,
)
from src.validation.spec_validator import (
    _collect_ref_targets,
    _walk_for_getatts,
    _walk_for_refs,
    _writable_properties,
    validate_resource_plan,
)


# ---------------------------------------------------------------------------
# Fake schemas — minimal subsets of real CloudFormation spec schemas
# ---------------------------------------------------------------------------

_FAKE_VPC_SCHEMA = {
    "typeName": "AWS::EC2::VPC",
    "properties": {
        "CidrBlock": {"type": "string"},
        "EnableDnsHostnames": {"type": "boolean"},
        "EnableDnsSupport": {"type": "boolean"},
        "InstanceTenancy": {"type": "string"},
        "Tags": {"type": "array"},
        "VpcId": {"type": "string"},
    },
    "readOnlyProperties": ["/properties/VpcId"],
}

_FAKE_SUBNET_SCHEMA = {
    "typeName": "AWS::EC2::Subnet",
    "properties": {
        "VpcId": {"type": "string"},
        "CidrBlock": {"type": "string"},
        "AvailabilityZone": {"type": "string"},
        "Tags": {"type": "array"},
        "SubnetId": {"type": "string"},
    },
    "readOnlyProperties": ["/properties/SubnetId"],
    "required": ["VpcId"],
}

_FAKE_TYPE_MAP = {
    "AWS::EC2::VPC": "fake_vpc_hash",
    "AWS::EC2::Subnet": "fake_subnet_hash",
}

_FAKE_SCHEMA_MAP = {
    "fake_vpc_hash": _FAKE_VPC_SCHEMA,
    "fake_subnet_hash": _FAKE_SUBNET_SCHEMA,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_schema_loading():
    """Mock cfn-lint schema loading for all tests in this module.

    Patches _load_region_type_map and _load_resource_schema so that
    tests work without cfn-lint installed.
    """
    with patch(
        "src.validation.spec_validator._load_region_type_map",
        return_value=_FAKE_TYPE_MAP,
    ), patch(
        "src.validation.spec_validator._load_resource_schema",
        side_effect=lambda h: _FAKE_SCHEMA_MAP.get(h),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers — minimal plan builders
# ---------------------------------------------------------------------------


def _make_plan(
    resources: list[CfnResource],
    parameters: list[CfnParameter] | None = None,
    outputs: list[CfnOutput] | None = None,
    conditions: list[CfnCondition] | None = None,
) -> ResourcePlan:
    return ResourcePlan(
        description="test plan",
        resources=resources,
        parameters=parameters or [],
        outputs=outputs or [],
        conditions=conditions or [],
    )


def _vpc_resource(logical_id: str = "MyVPC", **extra_props) -> CfnResource:
    props = {"CidrBlock": "10.0.0.0/16", **extra_props}
    return CfnResource(logical_id=logical_id, type="AWS::EC2::VPC", properties=props)


def _subnet_resource(
    logical_id: str = "MySubnet", vpc_ref: str = "MyVPC", **extra_props
) -> CfnResource:
    props = {"VpcId": {"ref": vpc_ref}, "CidrBlock": "10.0.1.0/24", **extra_props}
    return CfnResource(
        logical_id=logical_id, type="AWS::EC2::Subnet", properties=props
    )


# ===================================================================
# Valid plans — no findings expected
# ===================================================================


class TestValidPlans:
    """Plans that should pass spec validation with zero findings."""

    def test_single_vpc_passes(self):
        plan = _make_plan([_vpc_resource()])
        findings = validate_resource_plan(plan)
        assert findings == []

    def test_vpc_and_subnet_passes(self):
        plan = _make_plan([_vpc_resource(), _subnet_resource()])
        findings = validate_resource_plan(plan)
        assert findings == []

    def test_ref_to_parameter_passes(self):
        param = CfnParameter(logical_id="VpcCidr", type="String", default="10.0.0.0/16")
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={"CidrBlock": {"ref": "VpcCidr"}},
        )
        plan = _make_plan([vpc], parameters=[param])
        findings = validate_resource_plan(plan)
        assert findings == []

    def test_ref_to_pseudo_parameter_passes(self):
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={
                "CidrBlock": "10.0.0.0/16",
                "Tags": [{"Key": "StackName", "Value": {"ref": "AWS::StackName"}}],
            },
        )
        plan = _make_plan([vpc])
        findings = validate_resource_plan(plan)
        assert findings == []

    def test_custom_resource_skipped(self):
        """Custom:: resources are not validated against specs."""
        custom = CfnResource(
            logical_id="MyCustom",
            type="Custom::FortiGateBootstrap",
            properties={"Whatever": "anything goes"},
        )
        plan = _make_plan([_vpc_resource(), custom])
        findings = validate_resource_plan(plan)
        assert findings == []


# ===================================================================
# SP001 — Invalid resource type
# ===================================================================


class TestInvalidResourceType:
    """SP001: Resource types not in the CloudFormation spec."""

    def test_typo_in_resource_type(self):
        bad_resource = CfnResource(
            logical_id="BadVPC",
            type="AWS::EC2::VPX",  # typo
            properties={"CidrBlock": "10.0.0.0/16"},
        )
        plan = _make_plan([_vpc_resource(), bad_resource])
        findings = validate_resource_plan(plan)

        sp001 = [f for f in findings if f.rule_id == "SP001"]
        assert len(sp001) == 1
        assert "AWS::EC2::VPX" in sp001[0].message
        assert sp001[0].resource == "BadVPC"
        assert sp001[0].severity == "error"

    def test_completely_made_up_type(self):
        fake = CfnResource(
            logical_id="FakeResource",
            type="AWS::Nonexistent::Thing",
            properties={},
        )
        plan = _make_plan([_vpc_resource(), fake])
        findings = validate_resource_plan(plan)

        sp001 = [f for f in findings if f.rule_id == "SP001"]
        assert len(sp001) == 1
        assert sp001[0].resource == "FakeResource"


# ===================================================================
# SP002 — Invalid property name
# ===================================================================


class TestInvalidPropertyName:
    """SP002: Property names not in the resource schema."""

    def test_snake_case_property_caught(self):
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={"cidr_block": "10.0.0.0/16"},  # should be CidrBlock
        )
        plan = _make_plan([vpc])
        findings = validate_resource_plan(plan)

        sp002 = [f for f in findings if f.rule_id == "SP002"]
        assert len(sp002) == 1
        assert "cidr_block" in sp002[0].message
        assert sp002[0].resource == "MyVPC"

    def test_totally_bogus_property(self):
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={"CidrBlock": "10.0.0.0/16", "FooBarBaz": "nope"},
        )
        plan = _make_plan([vpc])
        findings = validate_resource_plan(plan)

        sp002 = [f for f in findings if f.rule_id == "SP002"]
        assert len(sp002) == 1
        assert "FooBarBaz" in sp002[0].message


# ===================================================================
# SP003 — Missing required property
# ===================================================================


class TestMissingRequiredProperty:
    """SP003: Required properties missing from a resource."""

    def test_subnet_missing_vpc_id(self):
        """AWS::EC2::Subnet requires VpcId."""
        subnet = CfnResource(
            logical_id="MySubnet",
            type="AWS::EC2::Subnet",
            properties={"CidrBlock": "10.0.1.0/24"},  # missing VpcId
        )
        plan = _make_plan([_vpc_resource(), subnet])
        findings = validate_resource_plan(plan)

        sp003 = [f for f in findings if f.rule_id == "SP003"]
        assert len(sp003) == 1
        assert "VpcId" in sp003[0].message
        assert sp003[0].resource == "MySubnet"


# ===================================================================
# SP004 — Dangling Ref targets
# ===================================================================


class TestDanglingRef:
    """SP004: Ref targets that don't exist in the plan."""

    def test_ref_to_nonexistent_resource(self):
        subnet = CfnResource(
            logical_id="MySubnet",
            type="AWS::EC2::Subnet",
            properties={
                "VpcId": {"ref": "GhostVPC"},  # doesn't exist
                "CidrBlock": "10.0.1.0/24",
            },
        )
        plan = _make_plan([subnet])
        findings = validate_resource_plan(plan)

        sp004 = [f for f in findings if f.rule_id == "SP004"]
        assert len(sp004) == 1
        assert "GhostVPC" in sp004[0].message
        assert sp004[0].severity == "error"

    def test_ref_to_nonexistent_parameter(self):
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={"CidrBlock": {"ref": "MissingParam"}},
        )
        plan = _make_plan([vpc])
        findings = validate_resource_plan(plan)

        sp004 = [f for f in findings if f.rule_id == "SP004"]
        assert len(sp004) == 1
        assert "MissingParam" in sp004[0].message

    def test_nested_ref_in_list_caught(self):
        vpc = CfnResource(
            logical_id="MyVPC",
            type="AWS::EC2::VPC",
            properties={
                "CidrBlock": "10.0.0.0/16",
                "Tags": [{"Key": "Name", "Value": {"ref": "NonexistentTag"}}],
            },
        )
        plan = _make_plan([vpc])
        findings = validate_resource_plan(plan)

        sp004 = [f for f in findings if f.rule_id == "SP004"]
        assert len(sp004) == 1
        assert "NonexistentTag" in sp004[0].message

    def test_ref_in_output_caught(self):
        vpc = _vpc_resource()
        output = CfnOutput(
            logical_id="VpcOut",
            value={"ref": "PhantomResource"},
        )
        plan = _make_plan([vpc], outputs=[output])
        findings = validate_resource_plan(plan)

        sp004 = [f for f in findings if f.rule_id == "SP004"]
        assert len(sp004) == 1
        assert "PhantomResource" in sp004[0].message

    def test_valid_ref_not_flagged(self):
        """Ref to an existing resource should not produce a finding."""
        plan = _make_plan([_vpc_resource(), _subnet_resource(vpc_ref="MyVPC")])
        findings = validate_resource_plan(plan)
        sp004 = [f for f in findings if f.rule_id == "SP004"]
        assert sp004 == []


# ===================================================================
# SP005 — Dangling GetAtt targets
# ===================================================================


class TestDanglingGetAtt:
    """SP005: GetAtt targets referencing nonexistent resources."""

    def test_getatt_to_nonexistent_resource(self):
        subnet = CfnResource(
            logical_id="MySubnet",
            type="AWS::EC2::Subnet",
            properties={
                "VpcId": {"get_att": ["GhostVPC", "VpcId"]},
                "CidrBlock": "10.0.1.0/24",
            },
        )
        plan = _make_plan([subnet])
        findings = validate_resource_plan(plan)

        sp005 = [f for f in findings if f.rule_id == "SP005"]
        assert len(sp005) == 1
        assert "GhostVPC" in sp005[0].message
        assert sp005[0].severity == "error"

    def test_getatt_to_existing_resource_passes(self):
        vpc = _vpc_resource()
        output = CfnOutput(
            logical_id="VpcIdOut",
            value={"get_att": ["MyVPC", "VpcId"]},
        )
        plan = _make_plan([vpc], outputs=[output])
        findings = validate_resource_plan(plan)
        sp005 = [f for f in findings if f.rule_id == "SP005"]
        assert sp005 == []


# ===================================================================
# Graceful degradation
# ===================================================================


class TestGracefulDegradation:
    """Validator should return empty findings if cfn-lint schemas are unavailable."""

    def test_missing_cfnlint_returns_empty(self):
        """When cfn-lint provider module import fails, return no findings."""
        plan = _make_plan([_vpc_resource()])

        # Override the autouse fixture's mock with an empty type map
        with patch(
            "src.validation.spec_validator._load_region_type_map",
            return_value={},
        ):
            findings = validate_resource_plan(plan)

        assert findings == []

    def test_empty_type_map_skips_all_checks(self):
        """Even a plan with errors should return nothing if specs unavailable."""
        bad_resource = CfnResource(
            logical_id="BadVPC",
            type="AWS::EC2::VPX",
            properties={"cidr_block": "10.0.0.0/16"},
        )
        subnet = CfnResource(
            logical_id="MySubnet",
            type="AWS::EC2::Subnet",
            properties={"VpcId": {"ref": "GhostVPC"}, "CidrBlock": "10.0.1.0/24"},
        )
        plan = _make_plan([bad_resource, subnet])

        with patch(
            "src.validation.spec_validator._load_region_type_map",
            return_value={},
        ):
            findings = validate_resource_plan(plan)

        assert findings == []


# ===================================================================
# Internal helpers — these don't need schema mocking
# ===================================================================


class TestWalkForRefs:
    """Tests for _walk_for_refs helper."""

    def test_simple_ref(self):
        assert _walk_for_refs({"ref": "MyVPC"}) == ["MyVPC"]

    def test_nested_in_list(self):
        value = [{"ref": "A"}, "plain", {"ref": "B"}]
        refs = _walk_for_refs(value)
        assert "A" in refs
        assert "B" in refs

    def test_deeply_nested(self):
        value = {"join": [",", [{"ref": "X"}, {"sub": "${Y}"}]]}
        refs = _walk_for_refs(value)
        assert "X" in refs

    def test_no_refs(self):
        assert _walk_for_refs("just a string") == []
        assert _walk_for_refs(42) == []
        assert _walk_for_refs({"key": "value"}) == []


class TestWalkForGetAtts:
    """Tests for _walk_for_getatts helper."""

    def test_simple_getatt(self):
        result = _walk_for_getatts({"get_att": ["MyVPC", "VpcId"]})
        assert result == [("MyVPC", "VpcId")]

    def test_nested_getatt(self):
        value = {"join": [",", [{"get_att": ["Res", "Attr"]}]]}
        result = _walk_for_getatts(value)
        assert ("Res", "Attr") in result

    def test_no_getatts(self):
        assert _walk_for_getatts("plain") == []
        assert _walk_for_getatts({"ref": "X"}) == []


class TestCollectRefTargets:
    """Tests for _collect_ref_targets helper."""

    def test_includes_resources_and_params(self):
        param = CfnParameter(logical_id="Cidr", type="String")
        vpc = _vpc_resource()
        plan = _make_plan([vpc], parameters=[param])
        targets = _collect_ref_targets(plan)
        assert "Cidr" in targets
        assert "MyVPC" in targets

    def test_includes_pseudo_params(self):
        plan = _make_plan([_vpc_resource()])
        targets = _collect_ref_targets(plan)
        assert "AWS::StackName" in targets
        assert "AWS::Region" in targets
        assert "AWS::AccountId" in targets


class TestWritableProperties:
    """Tests for _writable_properties helper."""

    def test_excludes_read_only(self):
        schema = {
            "properties": {"CidrBlock": {}, "VpcId": {}, "EnableDnsSupport": {}},
            "readOnlyProperties": ["/properties/VpcId"],
        }
        writable = _writable_properties(schema)
        assert "CidrBlock" in writable
        assert "EnableDnsSupport" in writable
        assert "VpcId" not in writable

    def test_no_read_only(self):
        schema = {
            "properties": {"A": {}, "B": {}},
        }
        writable = _writable_properties(schema)
        assert writable == {"A", "B"}
