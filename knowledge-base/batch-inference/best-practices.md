# Batch Inference — Best Practices

## Cost Optimization

### Do

- Use Spot Instances for all non-SLA-bound batch jobs (60-70% savings)
- Diversify across multiple instance types and AZs to reduce Spot interruption
- Set aggressive checkpointing (every 10-15 min) to minimize rework on interruption
- Use S3 Intelligent-Tiering for output data that may not be accessed immediately
- Schedule batch jobs during off-peak hours (lower Spot prices, better availability)
- Compress intermediate data (LZ4 for speed, ZSTD for ratio)
- Delete temporary files and scratch data after job completion

### Don't

- Run batch jobs on On-Demand without evaluating Spot viability
- Checkpoint too frequently (< 5 min) — S3 PUT costs and I/O overhead add up
- Keep idle instances running between job batches
- Store intermediate results in DynamoDB (expensive for large payloads; use S3)
- Use provisioned IOPS EBS for sequential batch reads (gp3 throughput is sufficient)

### Cost-per-Item Optimization

| Lever | Typical Impact | Trade-off |
|-------|---------------|-----------|
| Larger batch sizes | -20% cost/item | Higher memory, longer per-batch latency |
| INT8 quantization | -50% cost/item | Possible quality degradation |
| Spot instances | -65% cost/item | Need checkpointing, possible delays |
| Data prefetching | -10% cost/item | More complex implementation |
| NVMe scratch disk | -15% I/O cost | Data lost on termination |

## Reliability

### Idempotent Processing

| Principle | Implementation |
|-----------|---------------|
| Unique item IDs | Every input record has a deterministic ID |
| Deduplication on output | Check output bucket before writing (or use conditional PUT) |
| Stateless inference | No shared state between items within a batch |
| Deterministic ordering | Process items in sorted order for reproducibility |
| Retry safety | Re-processing an item produces the same result |

### Checkpoint Strategy

| Checkpoint Frequency | Storage Cost | Max Rework on Failure | Best For |
|---------------------|-------------|----------------------|----------|
| Every 100 items | Low | Up to 100 items | Small items, fast inference |
| Every 1000 items | Very low | Up to 1000 items | Large batches, stable Spot |
| Every 10 minutes | Medium | 10 min of work | Time-based, variable item size |
| Every 5 minutes | Higher | 5 min of work | Expensive inference, high Spot interruption |

### Dead Letter Queue (DLQ) Design

- Send permanently failed items to DLQ after 3 retries
- Include in DLQ message: original input, error type, stack trace, attempt count
- Alert when DLQ depth > 0
- DLQ retention: 14 days (enough for manual investigation)
- Process DLQ items separately with enhanced logging

### Retry Strategy

| Error Type | Retry? | Max Attempts | Backoff |
|-----------|--------|-------------|---------|
| GPU OOM | Yes (reduce batch) | 2 | Immediate (smaller batch) |
| Model load failure | Yes | 3 | 60s exponential |
| S3 throttle | Yes | 5 | Jittered exponential |
| Input format error | No | — | Send to DLQ |
| Inference timeout | Yes | 2 | 30s |
| Spot interruption | Yes (new instance) | 3 | 120s |

## Performance

### Data Loading Optimization

| Technique | Speedup | When to Use |
|-----------|---------|-------------|
| Prefetch next batch while inferring | 1.5-2× | Always (overlap I/O and compute) |
| NVMe scratch for dataset cache | 2-3× vs EBS | Large datasets, repeated access |
| Parallel S3 downloads (multipart) | 3-5× vs sequential | Files > 100 MB |
| Memory-mapped files | 1.5× | Datasets that fit in RAM |
| Columnar format (Parquet) | 2× vs CSV | Structured data with column selection |
| Pre-tokenized input | 1.3× | NLP models (skip tokenization at inference) |

### Batch Size Tuning

| GPU Memory Available | Recommended Batch Size | Rationale |
|---------------------|----------------------|-----------|
| < 25% free after model load | 1-4 | Memory constrained |
| 25-50% free | 4-16 | Balanced throughput/memory |
| 50-75% free | 16-64 | Maximize GPU utilization |
| > 75% free | 64-256 | Fully utilize available memory |

### Pipeline Parallelism for Batch Jobs

```
Stage 1 (CPU): Download + preprocess → Queue A
Stage 2 (GPU): Inference → Queue B
Stage 3 (CPU): Postprocess + upload

Run all 3 stages concurrently with bounded queues.
Throughput = min(stage_1_rate, stage_2_rate, stage_3_rate)
Optimize the bottleneck stage.
```

## Security

### Data Protection

| Control | Implementation |
|---------|---------------|
| Encryption at rest | S3 SSE-KMS for input/output, EBS encryption |
| Encryption in transit | VPC endpoints for S3 (no internet traversal) |
| Data isolation | Per-job IAM role with scoped S3 prefix access |
| Audit trail | CloudTrail for all S3 access, job metadata in DynamoDB |
| Data retention | Lifecycle rules: delete intermediate data after 7 days |
| Input validation | Schema validation before processing (reject malformed early) |

### Network Security

- Process in private subnets with S3 VPC endpoint (gateway type, free)
- No internet access required (all AWS services via VPC endpoints)
- Security group: outbound to S3/DynamoDB/CloudWatch endpoints only
- No SSH access to batch instances (use SSM Session Manager if needed)

## Monitoring

### Job-Level Metrics

| Metric | Purpose | Alert When |
|--------|---------|-----------|
| Items processed | Progress tracking | Stalled for > 10 min |
| Items failed | Quality monitoring | > 5% failure rate |
| Processing rate (items/sec) | Performance baseline | < 50% of expected |
| Time to completion (ETA) | SLA monitoring | ETA > deadline |
| Spot interruptions | Cost/reliability | > 3 per job |
| GPU utilization | Efficiency | < 40% (underutilized) |
| Queue depth (pending items) | Backlog size | Growing despite active workers |

### Operational Dashboard

Include: job timeline (Gantt chart), throughput sparkline, error rate pie chart, cost accumulator, Spot interruption timeline, per-item latency histogram.
