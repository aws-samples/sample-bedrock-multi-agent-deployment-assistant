import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface EcsConstructProps {
  vpc: ec2.Vpc;
  table: dynamodb.Table;
  artifactsBucket: s3.Bucket;
  knowledgeBaseBucket: s3.Bucket;
  /** Environment name used as resource name prefix (e.g., 'dev', 'prod'). */
  environment: string;
  /** Optional ACM certificate ARN for HTTPS listener (TLS 1.3). */
  certificateArn?: string;
  /** Optional S3 bucket for ALB access logs. */
  accessLogsBucket?: s3.IBucket;
  /** Optional KMS key for log group encryption. */
  encryptionKey?: kms.IKey;
  /** Optional Knowledge Base ID to scope IAM permissions. */
  knowledgeBaseId?: string;
  /** Optional AgentCore Memory ID for cross-session persistent memory. */
  agentcoreMemoryId?: string;
}

export class EcsConstruct extends Construct {
  public readonly service: ecs.FargateService;
  public readonly alb: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: EcsConstructProps) {
    super(scope, id);

    const prefix = `ai-deploy-${props.environment}`;

    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `${prefix}-cluster`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });

    const taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Task role for AI-Deploy Fargate service",
    });

    // Bedrock model invocation — scoped to current region and Anthropic Claude models only
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockModelInvocation",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          `arn:aws:bedrock:${cdk.Stack.of(this).region}::foundation-model/anthropic.claude-*`,
        ],
      }),
    );

    // Bedrock model listing — required by /health endpoint
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockListModels",
        actions: ["bedrock:ListFoundationModels"],
        resources: ["*"],
      }),
    );

    // Bedrock Knowledge Base retrieval — interview and design agents run on ECS
    const kbResource = props.knowledgeBaseId
      ? `arn:aws:bedrock:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:knowledge-base/${props.knowledgeBaseId}`
      : `arn:aws:bedrock:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:knowledge-base/*`;
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockKBRetrieve",
        actions: [
          "bedrock-agent-runtime:Retrieve",
          "bedrock-agent-runtime:RetrieveAndGenerate",
        ],
        resources: [kbResource],
      }),
    );

    // CloudWatch custom metrics (AI-Deploy namespace)
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchPutMetrics",
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "AI-Deploy" },
        },
      }),
    );

    // AgentCore Memory — cross-session persistent memory (conditional)
    if (props.agentcoreMemoryId) {
      taskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "AgentCoreMemoryData",
          actions: [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:DeleteEvent",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:GetMemoryRecord",
            "bedrock-agentcore:BatchCreateMemoryRecords",
            "bedrock-agentcore:StartMemoryExtractionJob",
            "bedrock-agentcore:ListSessions",
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:memory/${props.agentcoreMemoryId}`,
          ],
        }),
      );
    }

    // DynamoDB + S3 access
    props.table.grantReadWriteData(taskRole);
    props.artifactsBucket.grantReadWrite(taskRole);
    props.knowledgeBaseBucket.grantRead(taskRole);

    // cdk-nag suppressions
    NagSuppressions.addResourceSuppressions(
      taskRole,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Bedrock foundation model ARN uses anthropic.claude-* wildcard to support Sonnet + Haiku model variants. " +
            "Bedrock knowledge-base/* wildcard scoped to account+region; deferred refactor to grant against the actual KB ARN — see README. " +
            "DynamoDB and S3 wildcards are scoped to specific resources. " +
            "kms:GenerateDataKey*/ReEncrypt* action wildcards expand within the kms: namespace and " +
            "are bound to the customer-managed key resource on the same statement.",
          appliesTo: [
            {
              regex: "/^Resource::arn:aws:bedrock:.+::foundation-model/anthropic\\.claude-\\*$/",
            },
            {
              regex: "/^Resource::arn:aws:bedrock:.+:.+:knowledge-base/\\*$/",
            },
            "Resource::*",
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.table.node.defaultChild as cdk.CfnElement)}.Arn>/index/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.artifactsBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.knowledgeBaseBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            "Action::s3:Abort*",
            "Action::s3:DeleteObject*",
            "Action::s3:GetBucket*",
            "Action::s3:GetObject*",
            "Action::s3:List*",
            "Action::kms:GenerateDataKey*",
            "Action::kms:ReEncrypt*",
          ],
        },
      ],
      true,
    );

    // ECS log group — 6 month retention, RETAIN, optional KMS encryption
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: "/ai-deploy/ecs",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    const taskDefinition = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: `${prefix}-backend`,
      cpu: 2048,
      memoryLimitMiB: 4096,
      taskRole,
    });

    const imageAsset = new ecr_assets.DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "backend"),
    });

    taskDefinition.addContainer("Backend", {
      containerName: `${prefix}-backend`,
      image: ecs.ContainerImage.fromDockerImageAsset(imageAsset),
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      environment: {
        AI_DEPLOY_DYNAMODB_TABLE: props.table.tableName,
        AI_DEPLOY_S3_ARTIFACTS_BUCKET: props.artifactsBucket.bucketName,
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "backend",
      }),
      healthCheck: {
        command: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/ping')\""],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
      },
    });

    // Suppress AwsSolutions-ECS2 for non-sensitive env vars (resource names, mode flags).
    // Sensitive config (API keys, secrets) should use ECS secrets from SSM/Secrets Manager.
    NagSuppressions.addResourceSuppressions(taskDefinition, [
      {
        id: "AwsSolutions-ECS2",
        reason:
          "Environment variables contain only non-sensitive infrastructure references " +
          "(table name, bucket name, deploy mode). No secrets or credentials are passed as env vars.",
      },
      {
        id: "AwsSolutions-IAM5",
        reason:
          "ECR GetAuthorizationToken requires Resource::* — this is CDK-managed for " +
          "DockerImageAsset pull permissions on the ECS execution role.",
        appliesTo: ["Resource::*"],
      },
    ], true);

    // Fargate service in private subnets, spread across AZs for HA
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `${prefix}-backend`,
      cluster,
      taskDefinition,
      desiredCount: 2,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      platformVersion: ecs.FargatePlatformVersion.LATEST,
      circuitBreaker: { enable: true, rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      availabilityZoneRebalancing: ecs.AvailabilityZoneRebalancing.ENABLED,
    });

    // ---------------------------------------------------------------------------
    // ALB in public subnets
    // ---------------------------------------------------------------------------
    this.alb = new elbv2.ApplicationLoadBalancer(this, "ALB", {
      loadBalancerName: `${prefix}-alb`,
      vpc: props.vpc,
      internetFacing: true,
      idleTimeout: cdk.Duration.seconds(300),
    });

    // ALB access logging
    if (props.accessLogsBucket) {
      this.alb.logAccessLogs(props.accessLogsBucket, "alb-access-logs");
    }

    const albLoggingReason = props.accessLogsBucket
      ? "ALB access logging is enabled to the provided S3 bucket."
      : "ALB access logging requires an S3 bucket with specific policy. " +
        "Enable via alb.logAccessLogs() when a logging bucket is configured.";

    NagSuppressions.addResourceSuppressions(
      this.alb,
      [
        { id: "AwsSolutions-ELB2", reason: albLoggingReason },
        {
          id: "AwsSolutions-EC23",
          reason:
            "ALB is internet-facing by design — serves the frontend API. " +
            "Inbound access is restricted by backend auth (Cognito JWT), rate limiting, and WAF.",
        },
      ],
      true,
    );

    // ---------------------------------------------------------------------------
    // HTTPS listener (if certificate provided) with HTTP redirect;
    // otherwise HTTP-only with TODO for production.
    // ---------------------------------------------------------------------------
    const targetGroupProps = {
      port: 8000,
      targets: [this.service],
      healthCheck: {
        path: "/ping",
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    };

    if (props.certificateArn) {
      this.alb.addListener("HttpListener", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: "HTTPS",
          port: "443",
          permanent: true,
        }),
      });

      const httpsListener = this.alb.addListener("HttpsListener", {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(props.certificateArn)],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
      });

      httpsListener.addTargets("EcsTarget", targetGroupProps);
    } else {
      const listener = this.alb.addListener("HttpListener", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
      });

      NagSuppressions.addResourceSuppressions(listener, [
        {
          id: "AwsSolutions-ELB2",
          reason: "HTTP listener is a placeholder. Add HTTPS with ACM certificate for production.",
        },
      ]);

      listener.addTargets("EcsTarget", targetGroupProps);
    }

    // ---------------------------------------------------------------------------
    // Auto-scaling
    // ---------------------------------------------------------------------------
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: 2,
      maxCapacity: 10,
    });

    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(300),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    scaling.scaleOnMemoryUtilization("MemoryScaling", {
      targetUtilizationPercent: 75,
      scaleInCooldown: cdk.Duration.seconds(300),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ---------------------------------------------------------------------------
    // WAF WebACL — AWS managed rule groups for common threats and known bad inputs
    // ---------------------------------------------------------------------------
    const webAcl = new wafv2.CfnWebACL(this, "WebAcl", {
      name: `${prefix}-web-acl`,
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `${prefix}-web-acl`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: "AWSManagedRulesCommonRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `${prefix}-common-rules`,
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedRulesKnownBadInputsRuleSet",
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `${prefix}-known-bad-inputs`,
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "RateLimit",
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 1000,
              aggregateKeyType: "IP",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `${prefix}-rate-limit`,
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    new wafv2.CfnWebACLAssociation(this, "WebAclAssociation", {
      resourceArn: this.alb.loadBalancerArn,
      webAclArn: webAcl.attrArn,
    });

    new cdk.CfnOutput(this, "AlbDnsName", {
      value: this.alb.loadBalancerDnsName,
      description: "ALB DNS name for the FastAPI backend",
    });

    new cdk.CfnOutput(this, "ServiceName", {
      value: this.service.serviceName,
      description: "ECS service name",
    });
  }
}
