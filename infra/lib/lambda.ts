import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface LambdaConstructProps {
  table: dynamodb.Table;
  designQueue: sqs.Queue;
  iacQueue: sqs.Queue;
  docsQueue: sqs.Queue;
  artifactsBucket: s3.Bucket;
  knowledgeBaseBucket: s3.Bucket;
  encryptionKey: kms.IKey;
}

export class LambdaConstruct extends Construct {
  public readonly designWorker: lambda.Function;
  public readonly iacWorker: lambda.Function;
  public readonly docsWorker: lambda.Function;
  public readonly wsConnect: lambda.Function;
  public readonly wsDisconnect: lambda.Function;
  public readonly wsSubscribe: lambda.Function;
  public readonly notificationBridge: lambda.Function;
  public readonly wsHeartbeat: lambda.Function;

  constructor(scope: Construct, id: string, props: LambdaConstructProps) {
    super(scope, id);

    // ---------------------------------------------------------------------------
    // Design Worker Lambda — processes design tasks from SQS FIFO queue
    // ---------------------------------------------------------------------------

    const designWorkerLogGroup = new logs.LogGroup(this, "DesignWorkerLogGroup", {
      logGroupName: "/ai-lcm/lambda/design-worker",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    const designWorkerImage = new ecr_assets.DockerImageAsset(this, "DesignWorkerImage", {
      directory: path.join(__dirname, "..", "..", "backend"),
      file: "Dockerfile.lambda",
    });

    this.designWorker = new lambda.DockerImageFunction(this, "DesignWorker", {
      functionName: "ai-lcm-design-worker",
      code: lambda.DockerImageCode.fromEcr(designWorkerImage.repository, {
        tagOrDigest: designWorkerImage.imageTag,
      }),
      memorySize: 2048,
      timeout: cdk.Duration.minutes(5),
      environment: {
        AI_LCM_DYNAMODB_TABLE: props.table.tableName,
        AI_LCM_S3_ARTIFACTS_BUCKET: props.artifactsBucket.bucketName,
        AI_LCM_S3_KNOWLEDGE_BASE_BUCKET: props.knowledgeBaseBucket.bucketName,
        AI_LCM_STORAGE_BACKEND: "aws",
      },
      logGroup: designWorkerLogGroup,
    });

    // SQS event source — process one message at a time for design tasks
    this.designWorker.addEventSource(
      new lambdaEventSources.SqsEventSource(props.designQueue, {
        batchSize: 1,
      }),
    );

    // Bedrock model invocation — scoped to current region and Anthropic Claude models
    this.designWorker.addToRolePolicy(
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

    // CloudWatch custom metrics (AI-LCM namespace)
    this.designWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchPutMetrics",
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "AI-LCM" },
        },
      }),
    );

    // DynamoDB + S3 access for design worker
    props.table.grantReadWriteData(this.designWorker);
    props.artifactsBucket.grantRead(this.designWorker);
    props.knowledgeBaseBucket.grantRead(this.designWorker);

    // cdk-nag suppressions for design worker role
    NagSuppressions.addResourceSuppressions(
      this.designWorker,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Bedrock foundation model ARN uses anthropic.claude-* wildcard to support Sonnet + Haiku model variants. " +
            "Region-scoped to deployment region. " +
            "DynamoDB and S3 wildcards are scoped to specific resources. " +
            "SQS permissions are auto-granted by CDK event source mapping. " +
            "CloudWatch PutMetricData requires Resource::* but is scoped by namespace condition.",
          appliesTo: [
            {
              regex: "/^Resource::arn:aws:bedrock:.+::foundation-model/anthropic\\.claude-\\*$/",
            },
            "Resource::*",
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.table.node.defaultChild as cdk.CfnElement)}.Arn>/index/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.artifactsBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.knowledgeBaseBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            "Action::s3:GetBucket*",
            "Action::s3:GetObject*",
            "Action::s3:List*",
            "Action::kms:GenerateDataKey*",
            "Action::kms:ReEncrypt*",
          ],
        },
        {
          id: "AwsSolutions-IAM4",
          reason:
            "Lambda basic execution role (AWSLambdaBasicExecutionRole) is required for CloudWatch Logs. " +
            "This is a CDK-managed policy attached automatically.",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
        },
      ],
      true,
    );

    // ---------------------------------------------------------------------------
    // IaC Worker Lambda — processes IaC tasks from SQS FIFO queue
    // ---------------------------------------------------------------------------

    const iacWorkerLogGroup = new logs.LogGroup(this, "IaCWorkerLogGroup", {
      logGroupName: "/ai-lcm/lambda/iac-worker",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    const iacWorkerImage = new ecr_assets.DockerImageAsset(this, "IaCWorkerImage", {
      directory: path.join(__dirname, "..", "..", "backend"),
      file: "Dockerfile.lambda",
    });

    this.iacWorker = new lambda.DockerImageFunction(this, "IaCWorker", {
      functionName: "ai-lcm-iac-worker",
      code: lambda.DockerImageCode.fromEcr(iacWorkerImage.repository, {
        tagOrDigest: iacWorkerImage.imageTag,
      }),
      memorySize: 2048,
      timeout: cdk.Duration.minutes(15),
      environment: {
        AI_LCM_DYNAMODB_TABLE: props.table.tableName,
        AI_LCM_S3_ARTIFACTS_BUCKET: props.artifactsBucket.bucketName,
        AI_LCM_S3_KNOWLEDGE_BASE_BUCKET: props.knowledgeBaseBucket.bucketName,
        AI_LCM_STORAGE_BACKEND: "aws",
        AI_LCM_CFN_GUARD_BINARY: "/usr/local/bin/cfn-guard",
      },
      logGroup: iacWorkerLogGroup,
    });

    // SQS event source — process one message at a time for IaC tasks
    this.iacWorker.addEventSource(
      new lambdaEventSources.SqsEventSource(props.iacQueue, {
        batchSize: 1,
      }),
    );

    // Bedrock model invocation — scoped to current region and Anthropic Claude models
    this.iacWorker.addToRolePolicy(
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

    // CloudWatch custom metrics (AI-LCM namespace)
    this.iacWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchPutMetrics",
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "AI-LCM" },
        },
      }),
    );

    // DynamoDB + S3 access for IaC worker
    props.table.grantReadWriteData(this.iacWorker);
    props.artifactsBucket.grantReadWrite(this.iacWorker);
    props.knowledgeBaseBucket.grantRead(this.iacWorker);

    // cdk-nag suppressions for IaC worker role
    NagSuppressions.addResourceSuppressions(
      this.iacWorker,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Bedrock foundation model ARN uses anthropic.claude-* wildcard to support Sonnet + Haiku model variants. " +
            "Region-scoped to deployment region. " +
            "DynamoDB and S3 wildcards are scoped to specific resources. " +
            "SQS permissions are auto-granted by CDK event source mapping. " +
            "CloudWatch PutMetricData requires Resource::* but is scoped by namespace condition.",
          appliesTo: [
            {
              regex: "/^Resource::arn:aws:bedrock:.+::foundation-model/anthropic\\.claude-\\*$/",
            },
            "Resource::*",
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.table.node.defaultChild as cdk.CfnElement)}.Arn>/index/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.artifactsBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.knowledgeBaseBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            "Action::s3:GetBucket*",
            "Action::s3:GetObject*",
            "Action::s3:List*",
            "Action::s3:Abort*",
            "Action::s3:DeleteObject*",
            "Action::kms:GenerateDataKey*",
            "Action::kms:ReEncrypt*",
          ],
        },
        {
          id: "AwsSolutions-IAM4",
          reason:
            "Lambda basic execution role (AWSLambdaBasicExecutionRole) is required for CloudWatch Logs. " +
            "This is a CDK-managed policy attached automatically.",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
        },
      ],
      true,
    );

    // ---------------------------------------------------------------------------
    // Docs Worker Lambda — processes documentation tasks from SQS FIFO queue
    // ---------------------------------------------------------------------------

    const docsWorkerLogGroup = new logs.LogGroup(this, "DocsWorkerLogGroup", {
      logGroupName: "/ai-lcm/lambda/docs-worker",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    const docsWorkerImage = new ecr_assets.DockerImageAsset(this, "DocsWorkerImage", {
      directory: path.join(__dirname, "..", "..", "backend"),
      file: "Dockerfile.lambda",
    });

    this.docsWorker = new lambda.DockerImageFunction(this, "DocsWorker", {
      functionName: "ai-lcm-docs-worker",
      code: lambda.DockerImageCode.fromEcr(docsWorkerImage.repository, {
        tagOrDigest: docsWorkerImage.imageTag,
        cmd: ["src.workers.docs_worker.handler"],
      }),
      memorySize: 2048,
      timeout: cdk.Duration.minutes(10),
      environment: {
        AI_LCM_DYNAMODB_TABLE: props.table.tableName,
        AI_LCM_S3_ARTIFACTS_BUCKET: props.artifactsBucket.bucketName,
        AI_LCM_S3_KNOWLEDGE_BASE_BUCKET: props.knowledgeBaseBucket.bucketName,
        AI_LCM_STORAGE_BACKEND: "aws",
      },
      logGroup: docsWorkerLogGroup,
    });

    // SQS event source — process one message at a time for docs tasks
    this.docsWorker.addEventSource(
      new lambdaEventSources.SqsEventSource(props.docsQueue, {
        batchSize: 1,
      }),
    );

    // Bedrock model invocation — scoped to current region and Anthropic Claude models
    this.docsWorker.addToRolePolicy(
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

    // CloudWatch custom metrics (AI-LCM namespace)
    this.docsWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchPutMetrics",
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "AI-LCM" },
        },
      }),
    );

    // DynamoDB + S3 access for docs worker
    props.table.grantReadWriteData(this.docsWorker);
    props.artifactsBucket.grantRead(this.docsWorker);
    props.knowledgeBaseBucket.grantRead(this.docsWorker);

    // cdk-nag suppressions for docs worker role
    NagSuppressions.addResourceSuppressions(
      this.docsWorker,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Bedrock foundation model ARN uses anthropic.claude-* wildcard to support Sonnet + Haiku model variants. " +
            "Region-scoped to deployment region. " +
            "DynamoDB and S3 wildcards are scoped to specific resources. " +
            "SQS permissions are auto-granted by CDK event source mapping. " +
            "CloudWatch PutMetricData requires Resource::* but is scoped by namespace condition.",
          appliesTo: [
            {
              regex: "/^Resource::arn:aws:bedrock:.+::foundation-model/anthropic\\.claude-\\*$/",
            },
            "Resource::*",
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.table.node.defaultChild as cdk.CfnElement)}.Arn>/index/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.artifactsBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.knowledgeBaseBucket.node.defaultChild as cdk.CfnElement)}.Arn>/*`,
            "Action::s3:GetBucket*",
            "Action::s3:GetObject*",
            "Action::s3:List*",
            "Action::kms:GenerateDataKey*",
            "Action::kms:ReEncrypt*",
          ],
        },
        {
          id: "AwsSolutions-IAM4",
          reason:
            "Lambda basic execution role (AWSLambdaBasicExecutionRole) is required for CloudWatch Logs. " +
            "This is a CDK-managed policy attached automatically.",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
        },
      ],
      true,
    );

    // ---------------------------------------------------------------------------
    // WebSocket Lambda handlers — connect, disconnect, subscribe
    // ---------------------------------------------------------------------------

    const wsHandlersPath = path.join(__dirname, "..", "..", "backend", "lambdas", "ws");

    const wsLambdaDefaults = {
      runtime: lambda.Runtime.PYTHON_3_12,
      memorySize: 256,
      timeout: cdk.Duration.seconds(10),
      environment: {
        DYNAMODB_TABLE: props.table.tableName,
      },
    };

    const wsConnectLogGroup = new logs.LogGroup(this, "WsConnectLogGroup", {
      logGroupName: "/ai-lcm/lambda/ws-connect",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.wsConnect = new lambda.Function(this, "WsConnect", {
      functionName: "ai-lcm-ws-connect",
      ...wsLambdaDefaults,
      handler: "ws_connect.handler",
      code: lambda.Code.fromAsset(wsHandlersPath),
      logGroup: wsConnectLogGroup,
    });

    const wsDisconnectLogGroup = new logs.LogGroup(this, "WsDisconnectLogGroup", {
      logGroupName: "/ai-lcm/lambda/ws-disconnect",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.wsDisconnect = new lambda.Function(this, "WsDisconnect", {
      functionName: "ai-lcm-ws-disconnect",
      ...wsLambdaDefaults,
      handler: "ws_disconnect.handler",
      code: lambda.Code.fromAsset(wsHandlersPath),
      logGroup: wsDisconnectLogGroup,
    });

    const wsSubscribeLogGroup = new logs.LogGroup(this, "WsSubscribeLogGroup", {
      logGroupName: "/ai-lcm/lambda/ws-subscribe",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.wsSubscribe = new lambda.Function(this, "WsSubscribe", {
      functionName: "ai-lcm-ws-subscribe",
      ...wsLambdaDefaults,
      handler: "ws_subscribe.handler",
      code: lambda.Code.fromAsset(wsHandlersPath),
      logGroup: wsSubscribeLogGroup,
    });

    // Grant DynamoDB read/write to all WebSocket handlers
    props.table.grantReadWriteData(this.wsConnect);
    props.table.grantReadWriteData(this.wsDisconnect);
    props.table.grantReadWriteData(this.wsSubscribe);

    // ---------------------------------------------------------------------------
    // Notification Bridge Lambda — EventBridge Pipe → WS fan-out
    // ---------------------------------------------------------------------------

    const notificationBridgeLogGroup = new logs.LogGroup(this, "NotificationBridgeLogGroup", {
      logGroupName: "/ai-lcm/lambda/ws-notification-bridge",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.notificationBridge = new lambda.Function(this, "NotificationBridge", {
      functionName: "ai-lcm-ws-notification-bridge",
      runtime: lambda.Runtime.PYTHON_3_12,
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      handler: "ws_notification_bridge.handler",
      environment: {
        DYNAMODB_TABLE: props.table.tableName,
      },
      code: lambda.Code.fromAsset(wsHandlersPath),
      logGroup: notificationBridgeLogGroup,
    });

    props.table.grantReadWriteData(this.notificationBridge);

    // ---------------------------------------------------------------------------
    // Heartbeat Lambda — scheduled every 5 min to clean stale WS connections
    // ---------------------------------------------------------------------------

    const heartbeatLogGroup = new logs.LogGroup(this, "HeartbeatLogGroup", {
      logGroupName: "/ai-lcm/lambda/ws-heartbeat",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.wsHeartbeat = new lambda.Function(this, "WsHeartbeat", {
      functionName: "ai-lcm-ws-heartbeat",
      runtime: lambda.Runtime.PYTHON_3_12,
      memorySize: 256,
      timeout: cdk.Duration.minutes(2),
      handler: "ws_heartbeat.handler",
      environment: {
        DYNAMODB_TABLE: props.table.tableName,
      },
      code: lambda.Code.fromAsset(wsHandlersPath),
      logGroup: heartbeatLogGroup,
    });

    props.table.grantReadWriteData(this.wsHeartbeat);

    // CloudWatch custom metrics for heartbeat
    this.wsHeartbeat.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchPutMetrics",
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "AI-LCM" },
        },
      }),
    );

    // cdk-nag suppressions for all WebSocket Lambda roles (including new ones)
    const wsLambdas = [
      this.wsConnect,
      this.wsDisconnect,
      this.wsSubscribe,
      this.notificationBridge,
      this.wsHeartbeat,
    ];
    NagSuppressions.addResourceSuppressions(
      wsLambdas,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "DynamoDB wildcards are scoped to the specific AI-LCM table and its indexes. " +
            "These are auto-generated by CDK grantReadWriteData(). " +
            "KMS wildcards (GenerateDataKey*, ReEncrypt*) are auto-generated by CDK for KMS-encrypted table access. " +
            "CloudWatch PutMetricData requires Resource::* but is scoped by namespace condition. " +
            "API Gateway Management API execute-api wildcard is required for posting to arbitrary WebSocket connections.",
          appliesTo: [
            `Resource::<${cdk.Stack.of(this).getLogicalId(props.table.node.defaultChild as cdk.CfnElement)}.Arn>/index/*`,
            "Resource::*",
            "Action::kms:GenerateDataKey*",
            "Action::kms:ReEncrypt*",
            {
              regex: "/^Resource::arn:aws:execute-api:.+/@connections/\\*$/",
            },
          ],
        },
        {
          id: "AwsSolutions-IAM4",
          reason:
            "Lambda basic execution role (AWSLambdaBasicExecutionRole) is required for CloudWatch Logs. " +
            "This is a CDK-managed policy attached automatically.",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
        },
        {
          id: "AwsSolutions-L1",
          reason:
            "Python 3.12 is the latest fully supported runtime in the Lambda Python ecosystem. " +
            "Will upgrade to 3.13 when all dependencies are verified compatible.",
        },
      ],
      true,
    );
  }
}
