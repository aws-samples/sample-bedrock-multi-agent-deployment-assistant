import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as s3 from "aws-cdk-lib/aws-s3";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface S3ConstructProps {
  encryptionKey: kms.Key;
}

export class S3Construct extends Construct {
  public readonly knowledgeBaseBucket: s3.Bucket;
  public readonly artifactsBucket: s3.Bucket;
  public readonly accessLogsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: S3ConstructProps) {
    super(scope, id);

    // Access logging bucket for S3 server access logs (AwsSolutions-S1)
    this.accessLogsBucket = new s3.Bucket(this, "AccessLogsBucket", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: "expire-access-logs",
          expiration: cdk.Duration.days(365),
        },
      ],
    });

    NagSuppressions.addResourceSuppressions(this.accessLogsBucket, [
      {
        id: "AwsSolutions-S1",
        reason:
          "Access logs bucket does not need its own access logging to avoid infinite loop.",
      },
    ]);

    this.knowledgeBaseBucket = new s3.Bucket(this, "KnowledgeBaseBucket", {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: props.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      enforceSSL: true,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: "knowledge-base/",
    });

    this.artifactsBucket = new s3.Bucket(this, "ArtifactsBucket", {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: props.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      enforceSSL: true,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: "artifacts/",
      lifecycleRules: [
        {
          id: "expire-draft-artifacts",
          prefix: "drafts/",
          expiration: cdk.Duration.days(90),
        },
        {
          id: "expire-noncurrent-versions",
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
      ],
    });

    new cdk.CfnOutput(this, "KnowledgeBaseBucketName", {
      value: this.knowledgeBaseBucket.bucketName,
      description: "S3 bucket for Bedrock Knowledge Base source documents",
    });

    new cdk.CfnOutput(this, "ArtifactsBucketName", {
      value: this.artifactsBucket.bucketName,
      description: "S3 bucket for generated IaC and documentation artifacts",
    });
  }
}
