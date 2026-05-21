"""Blueprint-driven parameter resolver for IaC generation.

Reads VPCBlueprint and ApplianceBlueprint from design options and
deterministically computes all networking parameters (subnet CIDRs,
interface IPs, tags, AZ assignments).

Design principles:
- Pattern-agnostic: no hardcoded deployment patterns or AMI lookup tables.
- Schema-driven: topology blueprints define the shape; KB defines the values.
- Deterministic: identical inputs always produce identical outputs.
- Passes through additional_parameters to additional_resolved unchanged.
"""

import ipaddress
import logging
import math

from botocore.exceptions import ClientError

from src.config.settings import settings
from src.models.design import (
    DeploymentParameters,
    DesignOption,
    InterfaceBlueprint,
    ResolvedAppliance,
    ResolvedIaCParameters,
    ResolvedInterface,
    ResolvedVPC,
    SubnetSpec,
    VPCBlueprint,
    compute_requirements_hash,
)
from src.models.requirements import InterviewOutput

logger = logging.getLogger(__name__)


class ParameterResolver:
    """Resolve topology blueprints + deployment parameters into concrete IaC values.

    The resolver is stateless: all computation is driven by the DesignOption
    blueprints and the user-provided DeploymentParameters.
    """

    def resolve(
        self,
        design: DesignOption,
        params: DeploymentParameters,
        requirements: InterviewOutput,
    ) -> ResolvedIaCParameters:
        """Resolve a design option into complete IaC parameters.

        Args:
            design: The selected DesignOption with topology blueprints.
            params: User-provided deployment parameters (region, CIDR, etc.).
            requirements: The original interview output for hash traceability.

        Returns:
            ResolvedIaCParameters ready for IaC generation.
        """
        logger.info(
            "Resolving parameters for design=%s pattern=%s region=%s",
            design.name,
            design.deployment_pattern,
            params.aws_region,
        )

        # Determine the maximum AZ count needed across all VPCs
        max_az_count = max(vpc.availability_zones for vpc in design.vpc_topology)
        azs = self._resolve_azs(params.aws_region, max_az_count)

        # Resolve each VPC blueprint
        resolved_vpcs: list[ResolvedVPC] = []
        # Build a lookup from (vpc_role, subnet_role, az) -> SubnetSpec
        subnet_lookup: dict[tuple[str, str, str], SubnetSpec] = {}

        for vpc_bp in design.vpc_topology:
            vpc_azs = azs[: vpc_bp.availability_zones]
            subnets = self._compute_subnets(
                params.vpc_cidr, vpc_azs, vpc_bp.subnet_roles
            )

            resolved_vpc = ResolvedVPC(
                name=f"{params.project_name}-{vpc_bp.role}-vpc",
                role=vpc_bp.role,
                cidr=params.vpc_cidr,
                subnets=subnets,
            )
            resolved_vpcs.append(resolved_vpc)

            # Index subnets for interface assignment
            for subnet in subnets:
                subnet_lookup[(vpc_bp.role, subnet.role, subnet.availability_zone)] = (
                    subnet
                )

            logger.debug(
                "Resolved VPC role=%s with %d subnets across %d AZs",
                vpc_bp.role,
                len(subnets),
                vpc_bp.availability_zones,
            )

        # Resolve each Appliance blueprint
        resolved_appliances: list[ResolvedAppliance] = []
        # Track Appliance index per VPC role for IP offset assignment
        appliance_index_by_vpc: dict[str, int] = {}

        for appliance_bp in design.appliance_topology:
            appliance_idx = appliance_index_by_vpc.get(appliance_bp.vpc_role, 0)
            appliance_index_by_vpc[appliance_bp.vpc_role] = appliance_idx + 1

            # Find the matching VPC blueprint for AZ count
            vpc_bp = self._find_vpc_blueprint(design.vpc_topology, appliance_bp.vpc_role)
            vpc_azs = azs[: vpc_bp.availability_zones]

            # Assign Appliance to an AZ based on its index within the VPC
            appliance_az = vpc_azs[appliance_idx % len(vpc_azs)]

            # Collect subnets for this VPC + AZ for interface assignment
            az_subnets = [
                subnet_lookup[(appliance_bp.vpc_role, role, appliance_az)]
                for role in vpc_bp.subnet_roles
                if (appliance_bp.vpc_role, role, appliance_az) in subnet_lookup
            ]

            interfaces = self._assign_interfaces(
                az_subnets, appliance_bp.interfaces, appliance_idx
            )

            resolved_appliance = ResolvedAppliance(
                name=f"{params.project_name}-appliance-{appliance_bp.role}",
                role=appliance_bp.role,
                instance_type=design.appliance_instance_type,
                availability_zone=appliance_az,
                interfaces=interfaces,
            )
            resolved_appliances.append(resolved_appliance)

            logger.debug(
                "Resolved Appliance role=%s az=%s with %d interfaces",
                appliance_bp.role,
                appliance_az,
                len(interfaces),
            )

        # Fetch code template files from S3 if a template exists
        template_files: dict[str, str] | None = None
        if design.has_code_template and design.template_s3_prefix:
            template_files = self._fetch_template(design.template_s3_prefix)
            if not template_files:
                logger.warning(
                    "Template S3 prefix %s declared but no files retrieved",
                    design.template_s3_prefix,
                )
                template_files = None

        # Compute requirements hash for traceability
        req_hash = compute_requirements_hash(requirements.model_dump())

        # Build tags
        tags = {
            "Project": params.project_name,
            "Environment": params.environment,
            "ManagedBy": "ai-deploy",
            "DeploymentPattern": design.deployment_pattern,
            "RequirementsHash": req_hash,
        }

        resolved = ResolvedIaCParameters(
            project_name=params.project_name,
            environment=params.environment,
            region=params.aws_region,
            availability_zones=azs,
            vpcs=resolved_vpcs,
            appliance_instances=resolved_appliances,
            code_template_s3_prefix=(
                design.template_s3_prefix if design.has_code_template else None
            ),
            code_template_files=template_files,
            additional_resolved=dict(params.additional_parameters),
            tags=tags,
            design_option_name=design.name,
            deployment_pattern=design.deployment_pattern,
            requirements_hash=req_hash,
        )

        logger.info(
            "Parameter resolution complete: %d VPCs, %d Appliances, %d AZs",
            len(resolved.vpcs),
            len(resolved.appliance_instances),
            len(resolved.availability_zones),
        )
        return resolved

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_azs(self, region: str, count: int) -> list[str]:
        """Generate AZ names for a region.

        Args:
            region: AWS region (e.g. 'us-east-1').
            count: Number of AZs needed (1 or 2).

        Returns:
            List of AZ names (e.g. ['us-east-1a', 'us-east-1b']).
        """
        suffixes = "abcdefghijklmnopqrstuvwxyz"
        azs = [f"{region}{suffixes[i]}" for i in range(count)]
        logger.debug("Resolved %d AZs for region %s: %s", count, region, azs)
        return azs

    def _compute_subnets(
        self,
        vpc_cidr: str,
        azs: list[str],
        subnet_roles: list[str],
    ) -> list[SubnetSpec]:
        """Divide a VPC CIDR into equal-sized subnets for each role and AZ.

        The total number of subnets is ``len(subnet_roles) * len(azs)``.
        The VPC network is split into the next power-of-two that can
        accommodate that many subnets.

        Args:
            vpc_cidr: VPC CIDR block (e.g. '10.0.0.0/16').
            azs: List of availability zone names.
            subnet_roles: List of subnet role names from the VPC blueprint.

        Returns:
            List of SubnetSpec with computed CIDRs.

        Raises:
            ValueError: If the VPC CIDR cannot accommodate the required subnets.
        """
        network = ipaddress.ip_network(vpc_cidr, strict=False)
        total_subnets = len(subnet_roles) * len(azs)

        # Calculate the number of additional prefix bits needed
        # to split the network into at least total_subnets equal parts
        extra_bits = math.ceil(math.log2(total_subnets)) if total_subnets > 1 else 0
        new_prefix = network.prefixlen + extra_bits

        if new_prefix > network.max_prefixlen:
            raise ValueError(
                f"Cannot split {vpc_cidr} into {total_subnets} subnets: "
                f"would require /{new_prefix} which exceeds /{network.max_prefixlen}"
            )

        all_subnets = list(network.subnets(prefixlen_diff=extra_bits))

        if len(all_subnets) < total_subnets:
            raise ValueError(
                f"Network {vpc_cidr} split into {len(all_subnets)} subnets "
                f"but {total_subnets} are required"
            )

        specs: list[SubnetSpec] = []
        subnet_index = 0
        for role in subnet_roles:
            for az_idx, az in enumerate(azs):
                az_suffix = f"az{az_idx + 1}"
                subnet_cidr = str(all_subnets[subnet_index])
                specs.append(
                    SubnetSpec(
                        name=f"{role}-{az_suffix}",
                        role=role,
                        cidr=subnet_cidr,
                        availability_zone=az,
                    )
                )
                subnet_index += 1

        logger.debug(
            "Computed %d subnets from %s (/%d -> /%d)",
            len(specs),
            vpc_cidr,
            network.prefixlen,
            new_prefix,
        )
        return specs

    def _assign_interfaces(
        self,
        subnets: list[SubnetSpec],
        interface_bps: list[InterfaceBlueprint],
        appliance_idx: int,
    ) -> list[ResolvedInterface]:
        """Assign IPs to Appliance interfaces based on subnet roles.

        Each interface blueprint references a subnet_role. The matching
        subnet is found and an IP is assigned at offset ``10 + appliance_idx``
        from the subnet's network address (so the first Appliance gets .11,
        the second .12, etc.).

        Args:
            subnets: Available subnets for this VPC and AZ.
            interface_bps: Interface blueprints from the Appliance topology.
            appliance_idx: Zero-based index of this Appliance within its VPC role.

        Returns:
            List of ResolvedInterface with assigned IPs.
        """
        # Build a lookup by subnet role for fast matching
        subnet_by_role: dict[str, SubnetSpec] = {}
        for subnet in subnets:
            subnet_by_role[subnet.role] = subnet

        resolved: list[ResolvedInterface] = []
        for iface_bp in interface_bps:
            subnet = subnet_by_role.get(iface_bp.subnet_role)
            if subnet is None:
                logger.error(
                    "No subnet found for interface %s with role '%s'. "
                    "Available roles: %s",
                    iface_bp.port_name,
                    iface_bp.subnet_role,
                    list(subnet_by_role.keys()),
                )
                raise ValueError(
                    f"Interface {iface_bp.port_name} references subnet_role "
                    f"'{iface_bp.subnet_role}' but no matching subnet was found "
                    f"in the current AZ. Available roles: "
                    f"{sorted(subnet_by_role.keys())}"
                )

            # Compute IP: subnet network address + 10 + appliance_idx
            subnet_network = ipaddress.ip_network(subnet.cidr, strict=False)
            ip_offset = 10 + appliance_idx
            private_ip = str(subnet_network.network_address + ip_offset)

            # Verify the IP falls within the subnet
            if ipaddress.ip_address(private_ip) not in subnet_network:
                raise ValueError(
                    f"Computed IP {private_ip} (offset {ip_offset}) falls outside "
                    f"subnet {subnet.cidr} for interface {iface_bp.port_name}"
                )

            resolved.append(
                ResolvedInterface(
                    port_name=iface_bp.port_name,
                    subnet_name=subnet.name,
                    private_ip=private_ip,
                    description=iface_bp.description,
                    source_dest_check=False,
                )
            )

            logger.debug(
                "Assigned %s -> subnet=%s ip=%s",
                iface_bp.port_name,
                subnet.name,
                private_ip,
            )

        return resolved

    def _fetch_template(self, s3_prefix: str) -> dict[str, str]:
        """Download code template files from S3 under the given prefix.

        Lists all objects under ``s3_prefix`` in the knowledge base bucket
        and downloads each one. Returns a dict mapping relative file paths
        to their content.

        Args:
            s3_prefix: S3 key prefix (e.g. 'sd-wan/hub-spoke/code/').

        Returns:
            Dict mapping relative file paths to file contents.
            Returns empty dict if S3 is not accessible or prefix has no objects.
        """
        bucket = settings.s3_knowledge_base_bucket
        logger.info(
            "Fetching code template from s3://%s/%s", bucket, s3_prefix
        )

        try:
            from src.config.aws import aws_client
            s3_client = aws_client("s3")
            paginator = s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=bucket, Prefix=s3_prefix)

            template_files: dict[str, str] = {}
            for page in page_iterator:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Skip directory markers
                    if key.endswith("/"):
                        continue

                    # Compute relative path from the prefix
                    relative_path = key[len(s3_prefix) :].lstrip("/")
                    if not relative_path:
                        continue

                    try:
                        response = s3_client.get_object(Bucket=bucket, Key=key)
                        content = response["Body"].read().decode("utf-8")
                        template_files[relative_path] = content
                        logger.debug("Downloaded template file: %s", relative_path)
                    except (ClientError, UnicodeDecodeError) as exc:
                        logger.warning(
                            "Failed to download template file s3://%s/%s: %s",
                            bucket,
                            key,
                            exc,
                        )

            logger.info(
                "Fetched %d template files from s3://%s/%s",
                len(template_files),
                bucket,
                s3_prefix,
            )
            return template_files

        except ClientError as exc:
            logger.warning(
                "S3 access failed for template prefix %s: %s", s3_prefix, exc
            )
            return {}
        except Exception as exc:
            logger.warning(
                "Unexpected error fetching templates from S3: %s", exc
            )
            return {}

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_vpc_blueprint(
        vpc_topology: list[VPCBlueprint], vpc_role: str
    ) -> VPCBlueprint:
        """Find a VPC blueprint by role, raising if not found."""
        for vpc_bp in vpc_topology:
            if vpc_bp.role == vpc_role:
                return vpc_bp
        raise ValueError(
            f"Appliance references vpc_role '{vpc_role}' but no VPC blueprint "
            f"has that role. Available: {[v.role for v in vpc_topology]}"
        )
