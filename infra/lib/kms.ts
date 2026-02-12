import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export class KmsConstruct extends Construct {
  public readonly key: kms.Key;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.key = new kms.Key(this, "AiLcmKey", {
      enableKeyRotation: true,
      description: "AI-LCM encryption key for DynamoDB and S3",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      alias: "alias/ai-lcm",
    });

    const stack = cdk.Stack.of(this);
    this.key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: "AllowCloudWatchLogs",
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
        actions: ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
        resources: ["*"],
        conditions: {
          ArnLike: {
            "kms:EncryptionContext:aws:logs:arn": `arn:aws:logs:${stack.region}:${stack.account}:log-group:/ai-lcm/*`,
          },
        },
      }),
    );

    new ssm.StringParameter(this, "KeyArnParam", {
      parameterName: "/ai-lcm/kms-key-arn",
      stringValue: this.key.keyArn,
      description: "KMS key ARN for AI-LCM encryption",
    });
  }
}
