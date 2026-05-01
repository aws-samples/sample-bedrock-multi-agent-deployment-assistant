import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface SqsConstructProps {
  encryptionKey: kms.IKey;
}

export class SqsConstruct extends Construct {
  public readonly designQueue: sqs.Queue;
  public readonly designDlq: sqs.Queue;
  public readonly iacQueue: sqs.Queue;
  public readonly iacDlq: sqs.Queue;
  public readonly docsQueue: sqs.Queue;
  public readonly docsDlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: SqsConstructProps) {
    super(scope, id);

    // Dead-letter queue for failed design task messages
    this.designDlq = new sqs.Queue(this, "DesignDlq", {
      queueName: "ai-deploy-design-dlq.fifo",
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
    });

    // Main FIFO queue for design task processing
    this.designQueue = new sqs.Queue(this, "DesignQueue", {
      queueName: "ai-deploy-design-tasks.fifo",
      fifo: true,
      contentBasedDeduplication: true,
      visibilityTimeout: cdk.Duration.minutes(30),
      retentionPeriod: cdk.Duration.days(4),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
      deadLetterQueue: {
        queue: this.designDlq,
        maxReceiveCount: 3,
      },
    });

    // Dead-letter queue for failed IaC task messages
    this.iacDlq = new sqs.Queue(this, "IaCDLQ", {
      queueName: "ai-deploy-iac-dlq.fifo",
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
    });

    // Main FIFO queue for IaC task processing
    this.iacQueue = new sqs.Queue(this, "IaCQueue", {
      queueName: "ai-deploy-iac-tasks.fifo",
      fifo: true,
      contentBasedDeduplication: true,
      visibilityTimeout: cdk.Duration.minutes(90), // 6x Lambda timeout (15 min)
      retentionPeriod: cdk.Duration.days(4),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
      deadLetterQueue: {
        queue: this.iacDlq,
        maxReceiveCount: 3,
      },
    });

    // Dead-letter queue for failed docs task messages
    this.docsDlq = new sqs.Queue(this, "DocsDLQ", {
      queueName: "ai-deploy-docs-dlq.fifo",
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
    });

    // Main FIFO queue for docs task processing
    this.docsQueue = new sqs.Queue(this, "DocsQueue", {
      queueName: "ai-deploy-docs-tasks.fifo",
      fifo: true,
      contentBasedDeduplication: true,
      visibilityTimeout: cdk.Duration.minutes(60), // 6x Lambda timeout (10 min)
      retentionPeriod: cdk.Duration.days(4),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: props.encryptionKey,
      deadLetterQueue: {
        queue: this.docsDlq,
        maxReceiveCount: 3,
      },
    });

    // cdk-nag: SQS FIFO queues do not support SSE-SQS; KMS is used instead
    NagSuppressions.addResourceSuppressions(
      [this.designQueue, this.designDlq, this.iacQueue, this.iacDlq, this.docsQueue, this.docsDlq],
      [
        {
          id: "AwsSolutions-SQS3",
          reason:
            "The DLQ itself does not need a secondary DLQ. Failed messages are retained for 14 days for manual inspection.",
        },
        {
          id: "AwsSolutions-SQS4",
          reason:
            "SQS FIFO queues are encrypted with a customer-managed KMS key. " +
            "SSL enforcement is handled at the IAM policy level.",
        },
      ],
      true,
    );

    new cdk.CfnOutput(this, "DesignQueueUrl", {
      value: this.designQueue.queueUrl,
      description: "SQS FIFO queue URL for design task processing",
    });

    new cdk.CfnOutput(this, "DesignDlqUrl", {
      value: this.designDlq.queueUrl,
      description: "SQS DLQ URL for failed design tasks",
    });

    new ssm.StringParameter(this, "DesignQueueArnParam", {
      parameterName: "/ai-deploy/sqs-design-queue-arn",
      stringValue: this.designQueue.queueArn,
      description: "SQS design queue ARN for AI-Deploy",
    });

    new cdk.CfnOutput(this, "IaCQueueUrl", {
      value: this.iacQueue.queueUrl,
      description: "SQS FIFO queue URL for IaC task processing",
    });

    new cdk.CfnOutput(this, "IaCDlqUrl", {
      value: this.iacDlq.queueUrl,
      description: "SQS DLQ URL for failed IaC tasks",
    });

    new ssm.StringParameter(this, "IaCQueueArnParam", {
      parameterName: "/ai-deploy/sqs-iac-queue-arn",
      stringValue: this.iacQueue.queueArn,
      description: "SQS IaC queue ARN for AI-Deploy",
    });

    new cdk.CfnOutput(this, "DocsQueueUrl", {
      value: this.docsQueue.queueUrl,
      description: "SQS FIFO queue URL for docs task processing",
    });

    new cdk.CfnOutput(this, "DocsDlqUrl", {
      value: this.docsDlq.queueUrl,
      description: "SQS DLQ URL for failed docs tasks",
    });

    new ssm.StringParameter(this, "DocsQueueArnParam", {
      parameterName: "/ai-deploy/sqs-docs-queue-arn",
      stringValue: this.docsQueue.queueArn,
      description: "SQS docs queue ARN for AI-Deploy",
    });
  }
}
