import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as scheduler_targets from "aws-cdk-lib/aws-scheduler-targets";
import { Construct } from "constructs";
import { DynamoDbConstruct } from "./dynamodb";
import { S3Construct } from "./s3";
import { CognitoConstruct } from "./cognito";
import { VpcConstruct } from "./vpc";
import { EcsConstruct } from "./ecs";
import { AlarmsConstruct } from "./alarms";
import { KmsConstruct } from "./kms";
import { SqsConstruct } from "./sqs";
import { LambdaConstruct } from "./lambda";
import { WebSocketConstruct } from "./websocket";
import { EventBridgePipeConstruct } from "./eventbridge-pipe";
import { CloudFrontConstruct } from "./cloudfront";

/**
 * AI-Deploy infrastructure stack: ECS Fargate + ALB + VPC with shared
 * resources (DynamoDB, S3, Cognito, KMS, Alarms) and async design + IaC
 * processing (SQS, Lambda, WebSocket API Gateway).
 */
export class AiDeployStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const environment = this.node.tryGetContext("environment") ?? "dev";
    const notificationEmail = this.node.tryGetContext("notificationEmail");
    const natGateways = this.node.tryGetContext("natGateways");
    const certificateArn = this.node.tryGetContext("certificateArn");

    // Shared resources
    const kmsKey = new KmsConstruct(this, "Kms");
    const dynamo = new DynamoDbConstruct(this, "DynamoDB", {
      environment,
      encryptionKey: kmsKey.key,
    });
    const s3 = new S3Construct(this, "S3", {
      encryptionKey: kmsKey.key,
    });
    const cognito = new CognitoConstruct(this, "Cognito");

    // SQS FIFO queues for async design + IaC task processing
    const sqsConstruct = new SqsConstruct(this, "Sqs", {
      encryptionKey: kmsKey.key,
    });

    // ---------------------------------------------------------------------------
    // VPC (created early so worker Lambdas can use it)
    // ---------------------------------------------------------------------------

    const vpc = new VpcConstruct(this, "Vpc", { natGateways, encryptionKey: kmsKey.key });

    // ---------------------------------------------------------------------------
    // WebSocket API + Lambda handlers for real-time design task updates
    // ---------------------------------------------------------------------------

    const lambdaConstruct = new LambdaConstruct(this, "Lambda", {
      table: dynamo.table,
      designQueue: sqsConstruct.designQueue,
      iacQueue: sqsConstruct.iacQueue,
      docsQueue: sqsConstruct.docsQueue,
      artifactsBucket: s3.artifactsBucket,
      knowledgeBaseBucket: s3.knowledgeBaseBucket,
      encryptionKey: kmsKey.key,
      vpc: vpc.vpc,
    });

    const websocket = new WebSocketConstruct(this, "WebSocket", {
      connectHandler: lambdaConstruct.wsConnect,
      disconnectHandler: lambdaConstruct.wsDisconnect,
      subscribeHandler: lambdaConstruct.wsSubscribe,
      authorizer: lambdaConstruct.wsAuthorizer,
      encryptionKey: kmsKey.key,
    });

    // Wire Cognito config into the WS authorizer
    lambdaConstruct.wsAuthorizer.addEnvironment(
      "COGNITO_USER_POOL_ID",
      cognito.userPool.userPoolId,
    );
    lambdaConstruct.wsAuthorizer.addEnvironment(
      "COGNITO_CLIENT_ID",
      cognito.userPoolClient.userPoolClientId,
    );

    // Wire the WebSocket callback URL into notification bridge + heartbeat
    lambdaConstruct.notificationBridge.addEnvironment(
      "WEBSOCKET_CALLBACK_URL",
      websocket.callbackUrl,
    );
    lambdaConstruct.wsHeartbeat.addEnvironment(
      "WEBSOCKET_CALLBACK_URL",
      websocket.callbackUrl,
    );

    // Grant WS Management API access to notification bridge + heartbeat
    websocket.stage.grantManagementApiAccess(lambdaConstruct.notificationBridge);
    websocket.stage.grantManagementApiAccess(lambdaConstruct.wsHeartbeat);

    // EventBridge Pipe: DynamoDB stream → notification bridge Lambda
    new EventBridgePipeConstruct(this, "TaskNotificationPipe", {
      table: dynamo.table,
      notificationBridgeLambda: lambdaConstruct.notificationBridge,
      encryptionKey: kmsKey.key,
    });

    // EventBridge Scheduler: heartbeat every 5 minutes
    new scheduler.CfnSchedule(this, "HeartbeatSchedule", {
      name: "ai-deploy-ws-heartbeat",
      scheduleExpression: "rate(5 minutes)",
      flexibleTimeWindow: { mode: "OFF" },
      target: {
        arn: lambdaConstruct.wsHeartbeat.functionArn,
        roleArn: new iam.Role(this, "HeartbeatSchedulerRole", {
          assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
          inlinePolicies: {
            InvokeLambda: new iam.PolicyDocument({
              statements: [
                new iam.PolicyStatement({
                  actions: ["lambda:InvokeFunction"],
                  resources: [lambdaConstruct.wsHeartbeat.functionArn],
                }),
              ],
            }),
          },
        }).roleArn,
      },
    });

    // ---------------------------------------------------------------------------
    // ECS Fargate + ALB
    // ---------------------------------------------------------------------------

    const ecsConstruct = new EcsConstruct(this, "Ecs", {
      vpc: vpc.vpc,
      table: dynamo.table,
      artifactsBucket: s3.artifactsBucket,
      knowledgeBaseBucket: s3.knowledgeBaseBucket,
      accessLogsBucket: s3.accessLogsBucket,
      encryptionKey: kmsKey.key,
      certificateArn,
    });

    // Pass SQS queue URL and WebSocket URL to ECS backend environment
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_SQS_DESIGN_QUEUE_URL",
      sqsConstruct.designQueue.queueUrl,
    );
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_WEBSOCKET_URL",
      websocket.stage.url,
    );
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_WEBSOCKET_CALLBACK_URL",
      websocket.callbackUrl,
    );

    // Pass IaC queue URL to ECS backend environment
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_SQS_IAC_QUEUE_URL",
      sqsConstruct.iacQueue.queueUrl,
    );

    // Pass docs queue URL to ECS backend environment
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_SQS_DOCS_QUEUE_URL",
      sqsConstruct.docsQueue.queueUrl,
    );

    // Cognito auth config
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_COGNITO_USER_POOL_ID",
      cognito.userPool.userPoolId,
    );
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_COGNITO_CLIENT_ID",
      cognito.userPoolClient.userPoolClientId,
    );

    // Knowledge base bucket, region, and operational settings
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_S3_KNOWLEDGE_BASE_BUCKET",
      s3.knowledgeBaseBucket.bucketName,
    );
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_AWS_REGION",
      cdk.Stack.of(this).region,
    );
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_TRUSTED_PROXY",
      "true",
    );

    // Grant ECS task role permission to send messages to all task queues
    sqsConstruct.designQueue.grantSendMessages(ecsConstruct.service.taskDefinition.taskRole);
    sqsConstruct.iacQueue.grantSendMessages(ecsConstruct.service.taskDefinition.taskRole);
    sqsConstruct.docsQueue.grantSendMessages(ecsConstruct.service.taskDefinition.taskRole);

    // ---------------------------------------------------------------------------
    // CloudFront + S3 for static frontend
    // ---------------------------------------------------------------------------
    const frontend = new CloudFrontConstruct(this, "Frontend", {
      accessLogsBucket: s3.accessLogsBucket,
    });

    // Set CORS to allow CloudFront domain
    ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
      "AI_DEPLOY_CORS_ORIGINS",
      `["https://${frontend.distribution.distributionDomainName}"]`,
    );

    // Observability
    new AlarmsConstruct(this, "Alarms", {
      table: dynamo.table,
      service: ecsConstruct.service,
      alb: ecsConstruct.alb,
      designDlq: sqsConstruct.designDlq,
      iacDlq: sqsConstruct.iacDlq,
      docsDlq: sqsConstruct.docsDlq,
      notificationEmail,
      encryptionKey: kmsKey.key,
    });

    // Tag all resources
    const tags = {
      Project: "ai-deploy",
      Environment: environment,
      CostCenter: this.node.tryGetContext("costCenter") ?? "engineering",
      Owner: this.node.tryGetContext("owner") ?? "platform-team",
      ManagedBy: "cdk",
    };

    Object.entries(tags).forEach(([key, value]) => {
      cdk.Tags.of(this).add(key, value);
    });
  }
}
