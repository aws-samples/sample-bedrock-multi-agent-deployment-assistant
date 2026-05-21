import * as cdk from "aws-cdk-lib";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigatewayv2_integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as apigatewayv2_authorizers from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as kms from "aws-cdk-lib/aws-kms";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface WebSocketConstructProps {
  connectHandler: lambda.Function;
  disconnectHandler: lambda.Function;
  subscribeHandler: lambda.Function;
  /** Optional Lambda authorizer for $connect route. */
  authorizer?: lambda.Function;
  /** Optional KMS key for log group encryption. */
  encryptionKey?: kms.IKey;
}

export class WebSocketConstruct extends Construct {
  public readonly api: apigatewayv2.WebSocketApi;
  public readonly stage: apigatewayv2.WebSocketStage;
  public readonly callbackUrl: string;

  constructor(scope: Construct, id: string, props: WebSocketConstructProps) {
    super(scope, id);

    // ---------------------------------------------------------------------------
    // WebSocket API with route integrations
    // ---------------------------------------------------------------------------

    const wsAuthorizer = props.authorizer
      ? new apigatewayv2_authorizers.WebSocketLambdaAuthorizer(
          "WsAuthorizer",
          props.authorizer,
          { identitySource: ["route.request.querystring.token"] },
        )
      : undefined;

    this.api = new apigatewayv2.WebSocketApi(this, "DesignWsApi", {
      apiName: "ai-deploy-design-ws",
      connectRouteOptions: {
        integration: new apigatewayv2_integrations.WebSocketLambdaIntegration(
          "ConnectIntegration",
          props.connectHandler,
        ),
        authorizer: wsAuthorizer,
      },
      disconnectRouteOptions: {
        integration: new apigatewayv2_integrations.WebSocketLambdaIntegration(
          "DisconnectIntegration",
          props.disconnectHandler,
        ),
      },
    });

    // Custom subscribe route for task subscription
    this.api.addRoute("subscribe", {
      integration: new apigatewayv2_integrations.WebSocketLambdaIntegration(
        "SubscribeIntegration",
        props.subscribeHandler,
      ),
    });

    // ---------------------------------------------------------------------------
    // Stage with access logging
    // ---------------------------------------------------------------------------

    const accessLogGroup = new logs.LogGroup(this, "WsAccessLogGroup", {
      logGroupName: "/ai-deploy/apigateway/ws-access-logs",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryptionKey: props.encryptionKey,
    });

    this.stage = new apigatewayv2.WebSocketStage(this, "ProdStage", {
      webSocketApi: this.api,
      stageName: "prod",
      autoDeploy: true,
    });

    // Wire access logging to the stage via L1 escape hatch
    const cfnStage = this.stage.node.defaultChild as apigatewayv2.CfnStage;
    cfnStage.accessLogSettings = {
      destinationArn: accessLogGroup.logGroupArn,
      format: JSON.stringify({
        requestId: "$context.requestId",
        ip: "$context.identity.sourceIp",
        connectionId: "$context.connectionId",
        eventType: "$context.eventType",
        routeKey: "$context.routeKey",
        status: "$context.status",
        requestTime: "$context.requestTime",
        integrationError: "$context.integrationErrorMessage",
      }),
    };

    // Callback URL for posting messages back to WebSocket clients
    this.callbackUrl = this.stage.callbackUrl;

    // cdk-nag suppressions
    NagSuppressions.addResourceSuppressions(
      this.stage,
      [
        {
          id: "AwsSolutions-APIG1",
          reason:
            "WebSocket API access logging is configured via the access log group. " +
            "Stage-level logging is managed separately from REST API patterns.",
        },
      ],
      true,
    );

    // APIG4 on $disconnect and subscribe routes:
    // API Gateway WebSocket APIs only support authorizers on $connect (AWS platform
    // constraint — confirmed in the apigatewayv2 docs). Identity established at
    // $connect propagates via event.requestContext.authorizer to all subsequent
    // route handlers. The subscribe handler enforces tenant isolation by reading
    // auth_tenant_id from the connection record (see backend/lambdas/ws/ws_subscribe.py
    // tenant-mismatch check). $disconnect carries no payload and only signals
    // teardown of the already-authenticated connection.
    NagSuppressions.addResourceSuppressions(
      this.api,
      [
        {
          id: "AwsSolutions-APIG4",
          reason:
            "WebSocket APIs only support authorizers on $connect (AWS platform constraint). " +
            "Auth context propagates via requestContext.authorizer; subscribe handler " +
            "validates tenant_id against the connection's auth_tenant_id.",
        },
      ],
      true,
    );

    // ---------------------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------------------

    new cdk.CfnOutput(this, "WebSocketUrl", {
      value: this.stage.url,
      description: "WebSocket URL for frontend connection",
    });

    new cdk.CfnOutput(this, "WebSocketCallbackUrl", {
      value: this.callbackUrl,
      description: "WebSocket callback URL for Lambda to send messages",
    });
  }
}
