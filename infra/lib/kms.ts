import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export class KmsConstruct extends Construct {
  public readonly key: kms.Key;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.key = new kms.Key(this, "AiDeployKey", {
      enableKeyRotation: true,
      description: "AI-Deploy encryption key for DynamoDB and S3",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      alias: "alias/ai-deploy",
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
            "kms:EncryptionContext:aws:logs:arn": `arn:aws:logs:${stack.region}:${stack.account}:log-group:/ai-deploy/*`,
          },
        },
      }),
    );

    new ssm.StringParameter(this, "KeyArnParam", {
      parameterName: "/ai-deploy/kms-key-arn",
      stringValue: this.key.keyArn,
      description: "KMS key ARN for AI-Deploy encryption",
    });
  }
}
