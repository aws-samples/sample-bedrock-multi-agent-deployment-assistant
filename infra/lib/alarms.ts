import * as cdk from "aws-cdk-lib";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as subs from "aws-cdk-lib/aws-sns-subscriptions";
import { Construct } from "constructs";

export interface AlarmsConstructProps {
  table: dynamodb.Table;
  /** ECS service — only provided in ECS deploy mode. */
  service?: ecs.FargateService;
  /** ALB — only provided in ECS deploy mode. */
  alb?: elbv2.ApplicationLoadBalancer;
  /** SQS dead-letter queues to monitor for failed task messages. */
  designDlq?: sqs.Queue;
  iacDlq?: sqs.Queue;
  docsDlq?: sqs.Queue;
  /** SQS main queues to monitor message age. */
  designQueue?: sqs.Queue;
  iacQueue?: sqs.Queue;
  docsQueue?: sqs.Queue;
  /** Worker Lambda functions to monitor for invocation errors. */
  designWorker?: lambda.Function;
  iacWorker?: lambda.Function;
  docsWorker?: lambda.Function;
  /** CloudFront distribution for 5xx monitoring. */
  distribution?: cloudfront.Distribution;
  /** Optional email for alarm notifications. */
  notificationEmail?: string;
  /** KMS key for encrypting SNS topic at rest. */
  encryptionKey: kms.IKey;
}

export class AlarmsConstruct extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;
  public readonly alertsTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: AlarmsConstructProps) {
    super(scope, id);

    // ---------------------------------------------------------------------------
    // SNS topic for alarm notifications
    // ---------------------------------------------------------------------------
    this.alertsTopic = new sns.Topic(this, "AlertsTopic", {
      topicName: "ai-deploy-alerts",
      displayName: "AI-Deploy Alarm Notifications",
      masterKey: props.encryptionKey,
    });

    if (props.notificationEmail) {
      this.alertsTopic.addSubscription(
        new subs.EmailSubscription(props.notificationEmail),
      );
    }

    const snsAction = new cw_actions.SnsAction(this.alertsTopic);

    // ---------------------------------------------------------------------------
    // DynamoDB alarms
    // ---------------------------------------------------------------------------

    const createAlarmWithActions = (
      id: string,
      name: string,
      metric: cloudwatch.IMetric,
      threshold: number,
      description: string,
      evaluationPeriods = 2,
    ): cloudwatch.Alarm => {
      const alarm = new cloudwatch.Alarm(this, id, {
        alarmName: name,
        metric,
        threshold,
        evaluationPeriods,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: description,
      });
      alarm.addAlarmAction(snsAction);
      alarm.addOkAction(snsAction);
      return alarm;
    };

    const readThrottle = createAlarmWithActions(
      "DdbReadThrottle",
      "ai-deploy-ddb-read-throttle",
      props.table.metricThrottledRequestsForOperations({
        operations: [dynamodb.Operation.GET_ITEM, dynamodb.Operation.QUERY],
        period: cdk.Duration.minutes(5),
        statistic: "Sum",
      }),
      10,
      "DynamoDB read throttling detected — consider increasing capacity",
    );

    const writeThrottle = createAlarmWithActions(
      "DdbWriteThrottle",
      "ai-deploy-ddb-write-throttle",
      props.table.metricThrottledRequestsForOperations({
        operations: [dynamodb.Operation.PUT_ITEM, dynamodb.Operation.UPDATE_ITEM],
        period: cdk.Duration.minutes(5),
        statistic: "Sum",
      }),
      10,
      "DynamoDB write throttling detected — consider increasing capacity",
    );

    // ---------------------------------------------------------------------------
    // Custom application metrics (published by backend observability module)
    // ---------------------------------------------------------------------------

    const bedrockLatency = new cloudwatch.Metric({
      namespace: "AI-Deploy",
      metricName: "BedrockInvocationLatencyMs",
      period: cdk.Duration.minutes(5),
      statistic: "p99",
    });

    const bedrockLatencyAlarm = createAlarmWithActions(
      "BedrockLatencyP99",
      "ai-deploy-bedrock-latency-p99",
      bedrockLatency,
      30000,
      "Bedrock P99 latency exceeding 30s — model may be throttled",
      3,
    );

    const rateLimitHits = new cloudwatch.Metric({
      namespace: "AI-Deploy",
      metricName: "RateLimitExceeded",
      period: cdk.Duration.minutes(5),
      statistic: "Sum",
    });

    const rateLimitAlarm = createAlarmWithActions(
      "RateLimitHits",
      "ai-deploy-rate-limit-hits",
      rateLimitHits,
      50,
      "High rate of rate-limited requests — possible abuse or undersized limits",
    );

    // ---------------------------------------------------------------------------
    // Dashboard
    // ---------------------------------------------------------------------------

    this.dashboard = new cloudwatch.Dashboard(this, "Dashboard", {
      dashboardName: "ai-deploy-operations",
    });

    // Row 1: DynamoDB
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "DynamoDB Read/Write Capacity",
        left: [
          props.table.metricConsumedReadCapacityUnits({ period: cdk.Duration.minutes(1) }),
          props.table.metricConsumedWriteCapacityUnits({ period: cdk.Duration.minutes(1) }),
        ],
        width: 12,
      }),
      new cloudwatch.AlarmWidget({
        title: "DynamoDB Throttling",
        alarm: readThrottle,
        width: 6,
      }),
      new cloudwatch.AlarmWidget({
        title: "DynamoDB Write Throttling",
        alarm: writeThrottle,
        width: 6,
      }),
    );

    // Row 2: Bedrock + Rate Limits
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Bedrock Invocation Latency (P99)",
        left: [bedrockLatency],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: "Rate Limit Hits",
        left: [rateLimitHits],
        width: 6,
      }),
      new cloudwatch.AlarmWidget({
        title: "Bedrock Latency Alarm",
        alarm: bedrockLatencyAlarm,
        width: 6,
      }),
    );

    // ---------------------------------------------------------------------------
    // SQS DLQ alarms — alert when failed tasks accumulate
    // ---------------------------------------------------------------------------

    const dlqs: { id: string; name: string; dlq: sqs.Queue; desc: string }[] = [];
    if (props.designDlq) {
      dlqs.push({
        id: "DesignDlq",
        name: "ai-deploy-design-dlq-depth",
        dlq: props.designDlq,
        desc: "Design DLQ has messages — failed design tasks require investigation",
      });
    }
    if (props.iacDlq) {
      dlqs.push({
        id: "IaCDlq",
        name: "ai-deploy-iac-dlq-depth",
        dlq: props.iacDlq,
        desc: "IaC DLQ has messages — failed IaC generation tasks require investigation",
      });
    }
    if (props.docsDlq) {
      dlqs.push({
        id: "DocsDlq",
        name: "ai-deploy-docs-dlq-depth",
        dlq: props.docsDlq,
        desc: "Docs DLQ has messages — failed documentation tasks require investigation",
      });
    }

    const dlqAlarms: cloudwatch.Alarm[] = [];
    for (const { id: dlqId, name: dlqName, dlq, desc } of dlqs) {
      const dlqAlarm = createAlarmWithActions(
        `${dlqId}Depth`,
        dlqName,
        dlq.metricApproximateNumberOfMessagesVisible({
          period: cdk.Duration.minutes(5),
          statistic: "Maximum",
        }),
        1, // Alert on any message in the DLQ
        desc,
        1, // Single evaluation period — DLQ messages are already retried
      );
      dlqAlarms.push(dlqAlarm);
    }

    if (dlqAlarms.length > 0) {
      this.dashboard.addWidgets(
        ...dlqAlarms.map(
          (alarm, i) =>
            new cloudwatch.AlarmWidget({
              title: `DLQ: ${dlqs[i].id}`,
              alarm,
              width: 8,
            }),
        ),
      );
    }

    // Row 3: ECS metrics (only in ECS mode)
    if (props.service && props.alb) {
      const cpuMetric = props.service.metricCpuUtilization({
        period: cdk.Duration.minutes(1),
      });
      const memMetric = props.service.metricMemoryUtilization({
        period: cdk.Duration.minutes(1),
      });

      const ecsCpuAlarm = createAlarmWithActions(
        "EcsCpuHigh",
        "ai-deploy-ecs-cpu-high",
        cpuMetric,
        80,
        "ECS CPU > 80% — consider scaling out",
        3,
      );

      const ecsMemAlarm = createAlarmWithActions(
        "EcsMemHigh",
        "ai-deploy-ecs-memory-high",
        memMetric,
        80,
        "ECS Memory > 80% — consider scaling out or increasing task size",
        3,
      );

      const alb5xx = props.alb.metrics.httpCodeElb(
        elbv2.HttpCodeElb.ELB_5XX_COUNT,
        { period: cdk.Duration.minutes(5), statistic: "Sum" },
      );

      const alb5xxAlarm = createAlarmWithActions(
        "Alb5xxHigh",
        "ai-deploy-alb-5xx-high",
        alb5xx,
        10,
        "ALB returning 5xx errors — backend health issue",
      );

      this.dashboard.addWidgets(
        new cloudwatch.GraphWidget({
          title: "ECS CPU & Memory",
          left: [cpuMetric],
          right: [memMetric],
          width: 12,
        }),
        new cloudwatch.GraphWidget({
          title: "ALB HTTP Errors",
          left: [alb5xx],
          width: 12,
        }),
      );
    }

    // ---------------------------------------------------------------------------
    // 6.2 — CloudFront 5xx error rate alarm
    // ---------------------------------------------------------------------------

    if (props.distribution) {
      const cf5xxMetric = new cloudwatch.Metric({
        namespace: "AWS/CloudFront",
        metricName: "5xxErrorRate",
        dimensionsMap: {
          DistributionId: props.distribution.distributionId,
          Region: "Global",
        },
        period: cdk.Duration.minutes(5),
        statistic: "Average",
      });

      const cf5xxAlarm = createAlarmWithActions(
        "CloudFront5xxRate",
        "ai-deploy-cloudfront-5xx-rate",
        cf5xxMetric,
        5,
        "CloudFront 5xx error rate > 5% — possible origin misconfiguration or edge failure",
        2,
      );

      this.dashboard.addWidgets(
        new cloudwatch.AlarmWidget({
          title: "CloudFront 5xx Rate",
          alarm: cf5xxAlarm,
          width: 8,
        }),
      );
    }

    // ---------------------------------------------------------------------------
    // 6.3 — Lambda execution error alarms
    // ---------------------------------------------------------------------------

    const workerLambdas: { id: string; name: string; fn: lambda.Function; desc: string }[] = [];
    if (props.designWorker) {
      workerLambdas.push({
        id: "DesignWorkerErrors",
        name: "ai-deploy-design-worker-errors",
        fn: props.designWorker,
        desc: "Design worker Lambda invocation errors — possible code bug or Bedrock API change",
      });
    }
    if (props.iacWorker) {
      workerLambdas.push({
        id: "IaCWorkerErrors",
        name: "ai-deploy-iac-worker-errors",
        fn: props.iacWorker,
        desc: "IaC worker Lambda invocation errors — possible code bug or Bedrock API change",
      });
    }
    if (props.docsWorker) {
      workerLambdas.push({
        id: "DocsWorkerErrors",
        name: "ai-deploy-docs-worker-errors",
        fn: props.docsWorker,
        desc: "Docs worker Lambda invocation errors — possible code bug or Bedrock API change",
      });
    }

    const lambdaErrorAlarms: cloudwatch.Alarm[] = [];
    for (const { id: lambdaId, name: lambdaName, fn, desc } of workerLambdas) {
      const errorAlarm = createAlarmWithActions(
        lambdaId,
        lambdaName,
        fn.metricErrors({ period: cdk.Duration.minutes(5), statistic: "Sum" }),
        1,
        desc,
        1,
      );
      lambdaErrorAlarms.push(errorAlarm);
    }

    if (lambdaErrorAlarms.length > 0) {
      this.dashboard.addWidgets(
        ...lambdaErrorAlarms.map(
          (alarm, i) =>
            new cloudwatch.AlarmWidget({
              title: `Lambda Errors: ${workerLambdas[i].id.replace("Errors", "")}`,
              alarm,
              width: 8,
            }),
        ),
      );
    }

    // ---------------------------------------------------------------------------
    // 6.4 — SQS message age alarms (detect stuck consumers)
    // ---------------------------------------------------------------------------

    const queuesForAge: { id: string; name: string; queue: sqs.Queue; threshold: number; desc: string }[] = [];
    if (props.designQueue) {
      queuesForAge.push({
        id: "DesignQueueAge",
        name: "ai-deploy-design-queue-age",
        queue: props.designQueue,
        threshold: 600,
        desc: "Design queue oldest message > 10 min — consumer may be stuck or concurrency exhausted",
      });
    }
    if (props.iacQueue) {
      queuesForAge.push({
        id: "IaCQueueAge",
        name: "ai-deploy-iac-queue-age",
        queue: props.iacQueue,
        threshold: 1800,
        desc: "IaC queue oldest message > 30 min — consumer may be stuck or concurrency exhausted",
      });
    }
    if (props.docsQueue) {
      queuesForAge.push({
        id: "DocsQueueAge",
        name: "ai-deploy-docs-queue-age",
        queue: props.docsQueue,
        threshold: 1200,
        desc: "Docs queue oldest message > 20 min — consumer may be stuck or concurrency exhausted",
      });
    }

    const queueAgeAlarms: cloudwatch.Alarm[] = [];
    for (const { id: queueId, name: queueName, queue, threshold, desc } of queuesForAge) {
      const ageAlarm = createAlarmWithActions(
        queueId,
        queueName,
        queue.metricApproximateAgeOfOldestMessage({
          period: cdk.Duration.minutes(5),
          statistic: "Maximum",
        }),
        threshold,
        desc,
        2,
      );
      queueAgeAlarms.push(ageAlarm);
    }

    if (queueAgeAlarms.length > 0) {
      this.dashboard.addWidgets(
        ...queueAgeAlarms.map(
          (alarm, i) =>
            new cloudwatch.AlarmWidget({
              title: `Queue Age: ${queuesForAge[i].id.replace("QueueAge", "")}`,
              alarm,
              width: 8,
            }),
        ),
      );
    }

    // ---------------------------------------------------------------------------
    // 6.6 — Bedrock throttling alarm (custom metric from backend)
    // ---------------------------------------------------------------------------

    const bedrockThrottleMetric = new cloudwatch.Metric({
      namespace: "AI-Deploy",
      metricName: "BedrockThrottleCount",
      period: cdk.Duration.minutes(5),
      statistic: "Sum",
    });

    const bedrockThrottleAlarm = createAlarmWithActions(
      "BedrockThrottling",
      "ai-deploy-bedrock-throttling",
      bedrockThrottleMetric,
      5,
      "Bedrock throttling (429s) detected — tasks silently retrying or failing. Consider requesting quota increase.",
      2,
    );

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Bedrock Throttle Count",
        left: [bedrockThrottleMetric],
        width: 12,
      }),
      new cloudwatch.AlarmWidget({
        title: "Bedrock Throttle Alarm",
        alarm: bedrockThrottleAlarm,
        width: 12,
      }),
    );
  }
}
