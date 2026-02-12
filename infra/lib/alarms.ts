import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as kms from "aws-cdk-lib/aws-kms";
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
      topicName: "ai-lcm-alerts",
      displayName: "AI-LCM Alarm Notifications",
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
      "ai-lcm-ddb-read-throttle",
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
      "ai-lcm-ddb-write-throttle",
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
      namespace: "AI-LCM",
      metricName: "BedrockInvocationLatencyMs",
      period: cdk.Duration.minutes(5),
      statistic: "p99",
    });

    const bedrockLatencyAlarm = createAlarmWithActions(
      "BedrockLatencyP99",
      "ai-lcm-bedrock-latency-p99",
      bedrockLatency,
      30000,
      "Bedrock P99 latency exceeding 30s — model may be throttled",
      3,
    );

    const rateLimitHits = new cloudwatch.Metric({
      namespace: "AI-LCM",
      metricName: "RateLimitExceeded",
      period: cdk.Duration.minutes(5),
      statistic: "Sum",
    });

    const rateLimitAlarm = createAlarmWithActions(
      "RateLimitHits",
      "ai-lcm-rate-limit-hits",
      rateLimitHits,
      50,
      "High rate of rate-limited requests — possible abuse or undersized limits",
    );

    // ---------------------------------------------------------------------------
    // Dashboard
    // ---------------------------------------------------------------------------

    this.dashboard = new cloudwatch.Dashboard(this, "Dashboard", {
      dashboardName: "ai-lcm-operations",
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
        name: "ai-lcm-design-dlq-depth",
        dlq: props.designDlq,
        desc: "Design DLQ has messages — failed design tasks require investigation",
      });
    }
    if (props.iacDlq) {
      dlqs.push({
        id: "IaCDlq",
        name: "ai-lcm-iac-dlq-depth",
        dlq: props.iacDlq,
        desc: "IaC DLQ has messages — failed IaC generation tasks require investigation",
      });
    }
    if (props.docsDlq) {
      dlqs.push({
        id: "DocsDlq",
        name: "ai-lcm-docs-dlq-depth",
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
        "ai-lcm-ecs-cpu-high",
        cpuMetric,
        80,
        "ECS CPU > 80% — consider scaling out",
        3,
      );

      const ecsMemAlarm = createAlarmWithActions(
        "EcsMemHigh",
        "ai-lcm-ecs-memory-high",
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
        "ai-lcm-alb-5xx-high",
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
  }
}
