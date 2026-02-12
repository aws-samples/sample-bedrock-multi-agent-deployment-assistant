"""Unit tests for the Parameter Mapper — maps ResolvedIaCParameters to CFN defaults."""

from src.models.design import (
    ResolvedFortiGate,
    ResolvedIaCParameters,
    ResolvedInterface,
    ResolvedVPC,
    SubnetSpec,
)
from src.services.parameter_mapper import build_parameter_defaults


def _make_params(**overrides) -> ResolvedIaCParameters:
    """Create a ResolvedIaCParameters with sensible defaults."""
    defaults = dict(
        project_name="myproject",
        environment="production",
        region="us-east-1",
        availability_zones=["us-east-1a", "us-east-1b"],
        vpcs=[
            ResolvedVPC(
                name="inspection-vpc",
                role="inspection",
                cidr="10.0.0.0/16",
                subnets=[
                    SubnetSpec(name="public-1", role="public", cidr="10.0.1.0/24", availability_zone="us-east-1a"),
                    SubnetSpec(name="private-1", role="private", cidr="10.0.2.0/24", availability_zone="us-east-1a"),
                ],
            ),
        ],
        fortigate_instances=[
            ResolvedFortiGate(
                name="fgt-active",
                role="active",
                instance_type="c5.xlarge",
                availability_zone="us-east-1a",
                interfaces=[
                    ResolvedInterface(port_name="port1", subnet_name="public-1", private_ip="10.0.1.10", description="mgmt"),
                    ResolvedInterface(port_name="port2", subnet_name="private-1", private_ip="10.0.2.10", description="data"),
                ],
            ),
        ],
        design_option_name="Option A",
        deployment_pattern="ha-dual-az",
        requirements_hash="abc123",
        tags={"Project": "myproject", "Environment": "production"},
    )
    defaults.update(overrides)
    return ResolvedIaCParameters(**defaults)


_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  AWSRegion:
    Type: String
    Default: us-west-2
  AZ1:
    Type: String
    Default: us-west-2a
  AZ2:
    Type: String
    Default: us-west-2b
  VPCCidr:
    Type: String
    Default: 10.0.0.0/16
  Public1SubnetCidr:
    Type: String
    Default: 10.0.0.0/24
  InstanceType:
    Type: String
    Default: c5.large
  ProjectName:
    Type: String
    Default: default
  Environment:
    Type: String
    Default: dev
  TgwAsn:
    Type: String
    Default: '64512'
Resources:
  VPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: !Ref VPCCidr
"""


class TestBuildParameterDefaults:
    """Tests for build_parameter_defaults()."""

    def test_maps_region(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["AWSRegion"] == "us-east-1"

    def test_maps_az_by_index(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["AZ1"] == "us-east-1a"
        assert defaults["AZ2"] == "us-east-1b"

    def test_maps_vpc_cidr(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["VPCCidr"] == "10.0.0.0/16"

    def test_maps_subnet_cidr(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["Public1SubnetCidr"] == "10.0.1.0/24"

    def test_maps_instance_type(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["InstanceType"] == "c5.xlarge"

    def test_maps_project_name(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["ProjectName"] == "myproject"

    def test_maps_environment(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["Environment"] == "production"

    def test_maps_additional_resolved(self):
        params = _make_params(additional_resolved={"tgw_asn": "65000"})
        defaults = build_parameter_defaults(params, _TEMPLATE)
        assert defaults["TgwAsn"] == "65000"

    def test_empty_template_returns_empty(self):
        params = _make_params()
        defaults = build_parameter_defaults(params, "not a template")
        assert defaults == {}

    def test_no_parameters_section(self):
        params = _make_params()
        template = "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  VPC:\n    Type: AWS::EC2::VPC\n"
        defaults = build_parameter_defaults(params, template)
        assert defaults == {}

    def test_unmatched_parameters_excluded(self):
        params = _make_params()
        template = """\
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  SomeRandomParam:
    Type: String
    Default: foo
Resources:
  VPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: '10.0.0.0/16'
"""
        defaults = build_parameter_defaults(params, template)
        assert "SomeRandomParam" not in defaults
