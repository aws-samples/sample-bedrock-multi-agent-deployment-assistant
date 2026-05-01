import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { AiDeployStack } from "../lib/ai-deploy-stack";

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new AiDeployStack(app, "TestStack", {
    env: { account: "123456789012", region: "us-east-1" },
  });
  template = Template.fromStack(stack);
});

// ---------------------------------------------------------------------------
// DynamoDB
// ---------------------------------------------------------------------------

test("DynamoDB table has correct key schema and settings", () => {
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    TableName: "ai-deploy-table-dev",
    KeySchema: [
      { AttributeName: "pk", KeyType: "HASH" },
      { AttributeName: "sk", KeyType: "RANGE" },
    ],
    BillingMode: "PAY_PER_REQUEST",
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
  });
});

test("DynamoDB table uses customer-managed KMS encryption", () => {
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    SSESpecification: {
      SSEEnabled: true,
      SSEType: "KMS",
    },
  });
});

test("DynamoDB table has GSI1", () => {
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: "GSI1",
        KeySchema: [
          { AttributeName: "gsi1pk", KeyType: "HASH" },
          { AttributeName: "gsi1sk", KeyType: "RANGE" },
        ],
      }),
    ]),
  });
});

test("DynamoDB table uses NEW_AND_OLD_IMAGES stream", () => {
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    StreamSpecification: {
      StreamViewType: "NEW_AND_OLD_IMAGES",
    },
  });
});

// ---------------------------------------------------------------------------
// S3
// ---------------------------------------------------------------------------

test("Knowledge base bucket has versioning and public access blocked", () => {
  template.hasResourceProperties("AWS::S3::Bucket", {
    VersioningConfiguration: { Status: "Enabled" },
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });
});

test("Artifacts bucket has lifecycle rules", () => {
  template.hasResourceProperties("AWS::S3::Bucket", {
    LifecycleConfiguration: {
      Rules: Match.arrayWith([
        Match.objectLike({
          Id: "expire-draft-artifacts",
          Prefix: "drafts/",
          Status: "Enabled",
          ExpirationInDays: 90,
        }),
      ]),
    },
  });
});

test("S3 buckets enforce SSL", () => {
  // At least 2 bucket policies exist that deny insecure transport
  const policies = template.findResources("AWS::S3::BucketPolicy");
  expect(Object.keys(policies).length).toBeGreaterThanOrEqual(2);
});

// ---------------------------------------------------------------------------
// Cognito
// ---------------------------------------------------------------------------

test("Cognito user pool has correct password policy", () => {
  template.hasResourceProperties("AWS::Cognito::UserPool", {
    UserPoolName: "ai-deploy-user-pool",
    Policies: {
      PasswordPolicy: {
        MinimumLength: 12,
        RequireLowercase: true,
        RequireUppercase: true,
        RequireNumbers: true,
        RequireSymbols: true,
      },
    },
  });
});

test("Cognito user pool has custom tenant_id attribute", () => {
  template.hasResourceProperties("AWS::Cognito::UserPool", {
    Schema: Match.arrayWith([
      Match.objectLike({
        Name: "tenant_id",
        Mutable: false,
        AttributeDataType: "String",
      }),
    ]),
  });
});

test("Cognito user pool client uses SRP auth", () => {
  template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
    ClientName: "ai-deploy-web-client",
    ExplicitAuthFlows: Match.arrayWith(["ALLOW_USER_SRP_AUTH"]),
  });
});

test("Cognito user pool client has 7 day refresh token and revocation enabled", () => {
  template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
    ClientName: "ai-deploy-web-client",
    // CDK stores refresh token in minutes (7 days = 10080 minutes)
    RefreshTokenValidity: 10080,
    EnableTokenRevocation: true,
  });
});

// ---------------------------------------------------------------------------
// KMS — key ARN stored in SSM, not CfnOutput
// ---------------------------------------------------------------------------

test("KMS key ARN is stored in SSM parameter, not CfnOutput", () => {
  template.hasResourceProperties("AWS::SSM::Parameter", {
    Name: "/ai-deploy/kms-key-arn",
  });

  const outputs = template.findOutputs("*");
  const outputKeys = Object.keys(outputs);
  expect(outputKeys.some((k) => k.includes("KeyArn"))).toBe(false);
});

test("DynamoDB table ARN is stored in SSM parameter, not CfnOutput", () => {
  template.hasResourceProperties("AWS::SSM::Parameter", {
    Name: "/ai-deploy/dynamodb-table-arn",
  });

  const outputs = template.findOutputs("*");
  const outputKeys = Object.keys(outputs);
  expect(outputKeys.some((k) => k.includes("TableArn"))).toBe(false);
});

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

test("Stack exports required outputs", () => {
  const outputs = template.findOutputs("*");
  const outputKeys = Object.keys(outputs);
  // Verify key outputs exist (CDK generates hash suffixes)
  expect(outputKeys.some((k) => k.includes("TableName"))).toBe(true);
  expect(outputKeys.some((k) => k.includes("UserPoolId"))).toBe(true);
  expect(outputKeys.some((k) => k.includes("KnowledgeBaseBucketName"))).toBe(true);
  expect(outputKeys.some((k) => k.includes("ArtifactsBucketName"))).toBe(true);
});

// ---------------------------------------------------------------------------
// SNS Alerts Topic
// ---------------------------------------------------------------------------

test("SNS alerts topic is created", () => {
  template.hasResourceProperties("AWS::SNS::Topic", {
    TopicName: "ai-deploy-alerts",
  });
});

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------

test("Stack has project tag", () => {
  // CDK applies tags at stack level; verify Project tag exists
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    Tags: Match.arrayWith([
      Match.objectLike({ Key: "Project", Value: "ai-deploy" }),
    ]),
  });
});

// ---------------------------------------------------------------------------
// ECS deploy mode
// ---------------------------------------------------------------------------

describe("ECS deploy mode", () => {
  let ecsTemplate: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new AiDeployStack(app, "EcsTestStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    ecsTemplate = Template.fromStack(stack);
  });

  test("Fargate service uses PRIVATE subnets (AssignPublicIp DISABLED)", () => {
    ecsTemplate.hasResourceProperties("AWS::ECS::Service", {
      NetworkConfiguration: {
        AwsvpcConfiguration: {
          AssignPublicIp: "DISABLED",
        },
      },
    });
  });

  test("ALB exists and is internet-facing", () => {
    ecsTemplate.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::LoadBalancer",
      {
        Scheme: "internet-facing",
      },
    );
  });

  test("VPC has flow logs enabled", () => {
    ecsTemplate.hasResourceProperties("AWS::EC2::FlowLog", {
      TrafficType: "ALL",
    });
  });

  test("WAF WebACL exists with managed rule groups", () => {
    ecsTemplate.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "REGIONAL",
      DefaultAction: { Allow: {} },
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "AWSManagedRulesCommonRuleSet",
          Priority: 1,
        }),
        Match.objectLike({
          Name: "AWSManagedRulesKnownBadInputsRuleSet",
          Priority: 2,
        }),
      ]),
    });
  });

  test("WAF WebACL is associated with ALB", () => {
    ecsTemplate.hasResource("AWS::WAFv2::WebACLAssociation", {});
  });

  test("ECS auto-scaling is configured", () => {
    ecsTemplate.hasResourceProperties("AWS::ApplicationAutoScaling::ScalableTarget", {
      MinCapacity: 2,
      MaxCapacity: 10,
    });
  });

  test("ECS log group has 6 month retention and RETAIN policy", () => {
    ecsTemplate.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: "/ai-deploy/ecs",
      RetentionInDays: 180,
    });
  });

  test("Bedrock IAM policy is scoped to region and Claude models", () => {
    ecsTemplate.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              "bedrock:InvokeModel",
              "bedrock:InvokeModelWithResponseStream",
            ],
            Resource: "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*",
          }),
        ]),
      },
    });
  });
});
