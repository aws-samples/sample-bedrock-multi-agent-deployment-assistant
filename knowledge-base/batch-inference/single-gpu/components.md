# Batch Inference (Single GPU) — Components Reference

## AWS Infrastructure Components

| Component | Purpose | Key Configuration | Cost Factor |
|-----------|---------|-------------------|-------------|
| EC2 Spot Instance (g5) | Batch inference compute | Instance type, Spot price cap, interruption handling | Primary cost (60-70% savings with Spot) |
| Step Functions | Job orchestration, retry logic, state management | State machine definition, timeout, error handling | Per-state-transition |
| S3 (Input) | Source dataset storage | Bucket, prefix structure, lifecycle | Per-GB stored |
| S3 (Output) | Inference results storage | Bucket, server-side encryption, lifecycle | Per-GB stored |
| SQS | Job queue, work distribution | FIFO, visibility timeout, DLQ | Per-message |
| DynamoDB | Job metadata, progress tracking | On-demand, TTL for cleanup | Per-RCU/WCU |
| CloudWatch | Job metrics, duration tracking, error monitoring | Custom namespace, log groups | Per-metric + ingestion |
| EventBridge | Job scheduling, event-driven triggers | Rules, cron expressions | Per-event |
| IAM Role | Instance permissions for S3/SQS/DynamoDB/CloudWatch | Least privilege, session-scoped | Free |
| KMS | Encryption for all data at rest | Key rotation, cross-service grants | Per-key + requests |
| VPC + NAT Gateway | Network isolation, outbound connectivity | Private subnets, S3 VPC endpoint | NAT hourly + data |
| SNS | Job completion/failure notifications | Topic, subscriptions (email/Lambda) | Per-publish |

## Batch Processing Frameworks

| Framework | Orchestration | Key Features | Best For |
|-----------|--------------|--------------|----------|
| Custom (EC2 + SQS) | SQS polling loop | Full control, simple retry, checkpointing | Simple batch jobs |
| AWS Batch | Managed job queues | Auto-scaling, job dependencies, multi-node | Large-scale parallel jobs |
| Step Functions + Lambda/EC2 | State machine | Visual workflow, error handling, branching | Complex pipelines |
| SageMaker Processing | Managed containers | Framework-agnostic, auto-scaling, spot support | ML-specific processing |
| SageMaker Batch Transform | Managed inference | Auto-batching, model hosting, S3 I/O | Direct model inference |

## Component Interactions

```
EventBridge (schedule/trigger)
    → Step Functions (orchestration)
        → EC2 Spot (compute)
            → S3 (read input data)
            → GPU (inference)
            → S3 (write results)
            → CloudWatch (metrics)
            → DynamoDB (progress tracking)
        → SNS (completion notification)
        → DLQ (failed items)
```

## Job Lifecycle Components

| Stage | Component | Action | Failure Mode |
|-------|-----------|--------|--------------|
| Submit | API/EventBridge | Create job record in DynamoDB, enqueue to SQS | Retry 3× |
| Provision | Step Functions | Launch Spot instance, wait for ready | Fallback to On-Demand |
| Initialize | EC2 User Data | Download model, setup environment | Terminate + retry |
| Process | Inference loop | Read batch → infer → write results | Checkpoint + resume |
| Checkpoint | S3 + DynamoDB | Save progress every N items | Last checkpoint is resume point |
| Complete | Step Functions | Upload results, update DynamoDB, notify SNS | Manual review |
| Cleanup | Step Functions | Terminate instance, archive logs | Scheduled cleanup |

## Data Flow

| Stage | Source | Destination | Format | Encryption |
|-------|--------|-------------|--------|------------|
| Input | S3 input bucket | EC2 instance (NVMe scratch) | JSONL / Parquet / CSV | SSE-KMS → TLS |
| Inference | GPU memory | Local NVMe buffer | Tensors | In-memory |
| Output | NVMe buffer | S3 output bucket | JSONL / Parquet | TLS → SSE-KMS |
| Progress | EC2 | DynamoDB | JSON (item/batch count) | TLS + table encryption |
| Checkpoint | EC2 | S3 checkpoint prefix | Binary (pickle/safetensors) | SSE-KMS |
| Metrics | EC2 | CloudWatch | PutMetricData API | TLS + SigV4 |

## Spot Instance Components

| Component | Purpose | Configuration |
|-----------|---------|---------------|
| Spot Fleet / Launch Template | Request Spot capacity | MaxPrice, instance types, AZ diversification |
| Interruption Handler | Detect 2-min warning | IMDSv2 polling, USR1 signal to process |
| Checkpoint Manager | Save progress on interruption | S3 sync, DynamoDB progress marker |
| Capacity Rebalancing | Proactive instance replacement | Rebalance recommendation → new instance |
| Fallback to On-Demand | Guaranteed completion | Step Functions catch → On-Demand launch |

## Monitoring and Alerting

| Metric | Source | Alert Condition | Action |
|--------|--------|----------------|--------|
| Job Duration | Step Functions | > SLA × 1.5 | Page on-call |
| Items Processed/sec | CloudWatch custom | < baseline × 0.5 | Check GPU health |
| Error Rate | DynamoDB (failed items) | > 5% of batch | Pause + investigate |
| Spot Interruptions | CloudWatch Events | > 3 per job | Switch to On-Demand |
| Queue Depth | SQS ApproximateMessages | > 1000 for > 30 min | Scale out workers |
| DLQ Depth | SQS DLQ messages | > 0 | Alert + manual review |
