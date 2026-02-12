import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface VpcConstructProps {
  /** Number of NAT Gateways (default 2 for production resilience). */
  natGateways?: number;
  /** KMS key for encrypting flow log CloudWatch log group. */
  encryptionKey?: kms.IKey;
}

export class VpcConstruct extends Construct {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props?: VpcConstructProps) {
    super(scope, id);

    this.vpc = new ec2.Vpc(this, "AiLcmVpc", {
      vpcName: "ai-lcm-vpc",
      maxAzs: 2,
      natGateways: props?.natGateways ?? 2,
      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
    });

    const flowLogGroup = new logs.LogGroup(this, "FlowLogGroup", {
      logGroupName: "/ai-lcm/vpc-flow-logs",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props?.encryptionKey,
    });

    this.vpc.addFlowLog("FlowLog", {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
      trafficType: ec2.FlowLogTrafficType.ALL,
    });

    const interfaceEndpoints = [
      { name: "BedrockEndpoint", service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME },
      { name: "BedrockControlPlaneEndpoint", service: ec2.InterfaceVpcEndpointAwsService.BEDROCK },
      { name: "CloudWatchLogsEndpoint", service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
    ];

    const endpoints = interfaceEndpoints.map(({ name, service }) =>
      this.vpc.addInterfaceEndpoint(name, { service }),
    );

    this.vpc.addGatewayEndpoint("DynamoDbEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    endpoints.forEach((endpoint) => {
      NagSuppressions.addResourceSuppressions(
        endpoint,
        [
          {
            id: "CdkNagValidationFailure",
            reason:
              "VPC endpoint security group ingress CIDR references intrinsic VPC CIDR block. " +
              "Endpoint is restricted to VPC-internal traffic only.",
          },
        ],
        true,
      );
    });

    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      description: "VPC ID",
    });
  }
}
