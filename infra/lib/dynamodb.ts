import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as kms from "aws-cdk-lib/aws-kms";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export interface DynamoDbConstructProps {
  environment: string;
  encryptionKey: kms.Key;
}

export class DynamoDbConstruct extends Construct {
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DynamoDbConstructProps) {
    super(scope, id);

    this.table = new dynamodb.Table(this, "AiDeployTable", {
      tableName: `ai-deploy-table-${props.environment}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.encryptionKey,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      timeToLiveAttribute: "ttl",
    });

    this.table.addGlobalSecondaryIndex({
      indexName: "GSI1",
      partitionKey: { name: "gsi1pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi1sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: "GSI2",
      partitionKey: { name: "gsi2pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi2sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });


    new cdk.CfnOutput(this, "TableName", {
      value: this.table.tableName,
      description: "DynamoDB table name",
    });

    new ssm.StringParameter(this, "TableArnParam", {
      parameterName: "/ai-deploy/dynamodb-table-arn",
      stringValue: this.table.tableArn,
      description: "DynamoDB table ARN for AI-Deploy",
    });
  }
}
