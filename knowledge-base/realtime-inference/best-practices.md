# Real-Time Inference — Best Practices

## Security

### Do

- Deploy inference instances in private subnets with no public IP
- Terminate TLS at the ALB; use ACM certificates with automatic renewal
- Encrypt model artifacts in S3 with KMS customer-managed keys
- Use IMDSv2 (require token) on all GPU instances
- Restrict security group inbound to ALB only (port 8080)
- Rotate model-serving credentials via IAM instance profiles (no static keys)
- Enable VPC Flow Logs for traffic audit

### Don't

- Expose inference ports directly to the internet
- Store API keys or model access tokens in environment variables
- Use default VPC or public subnets for GPU instances
- Disable encryption "for performance" — overhead is negligible on modern instances
- Grant `s3:*` when only `s3:GetObject` on the model bucket is needed

## Monitoring

### Key Metrics to Track

| Metric | Normal Range | Alert Threshold | Priority |
|--------|-------------|----------------|----------|
| P99 Latency | < target SLA | > target × 1.5 for 3 min | P1 |
| GPU Utilization | 60-85% | < 30% or > 95% for 5 min | P2 |
| Request Error Rate (5xx) | < 0.1% | > 1% for 2 min | P1 |
| Request Queue Depth | 0-10 | > 50 for 1 min | P2 |
| GPU Memory Utilization | 60-90% | > 95% (OOM imminent) | P1 |
| ALB Healthy Host Count | = desired count | < min count | P1 |
| Model Load Time | baseline ± 20% | > baseline × 2 | P3 |

### Observability Stack

- CloudWatch custom metrics for GPU stats (publish via CW Agent or DCGM exporter)
- Structured JSON logs with request_id, latency_ms, model_version, input_tokens
- X-Ray tracing for end-to-end request latency breakdown
- Dashboard: GPU util heatmap, latency percentiles, throughput time series

## Deployment

### Blue/Green Deployment

| Step | Action | Rollback |
|------|--------|----------|
| 1 | Deploy new model version to "green" target group | Delete green TG |
| 2 | Run smoke tests against green (internal ALB rule) | — |
| 3 | Shift 5% traffic to green (weighted routing) | Shift 100% back to blue |
| 4 | Monitor for 15 min, compare latency/error rates | Shift back if degraded |
| 5 | Shift 100% to green, drain blue | — |
| 6 | After 30 min stability, decommission blue | — |

### Model Versioning

- Tag S3 model artifacts with semantic version: `v{major}.{minor}.{patch}`
- Store active model version in SSM Parameter Store
- Instance startup reads version from SSM → downloads from S3
- Rollback = update SSM parameter → rolling restart ASG

### Health Check Design

```
/health (ALB health check):
  ✓ HTTP server responsive
  ✓ GPU accessible (nvidia-smi returns 0)
  ✓ Model loaded in GPU memory
  ✓ Warmup inference completes in < 5s

/ready (deep readiness):
  ✓ All above
  ✓ Queue depth < threshold
  ✓ Memory utilization < 90%
```

## Cost Optimization

### Instance Strategy by Environment

| Environment | Strategy | Cost Savings |
|-------------|----------|-------------|
| Development | Spot (single instance, tolerate interruption) | 60-70% |
| Staging | Spot + On-Demand fallback (auto-replace on interrupt) | 40-50% |
| Production | On-Demand or Savings Plan (3-year commit = 50% off) | 0-50% |
| Production (non-critical) | 70% On-Demand + 30% Spot | 15-20% |

### Right-Sizing Signals

| Signal | Action |
|--------|--------|
| GPU utilization consistently < 40% | Downsize instance type |
| GPU utilization consistently > 90% | Upsize or add instances |
| Request queue growing but GPU idle | CPU bottleneck — increase vCPUs |
| Latency high but GPU not saturated | Check batch size, I/O, preprocessing |
| Memory utilization > 90% | Enable quantization or larger instance |

### Quantization for Cost Reduction

| Quantization | Memory Saved | Latency Impact | Quality Impact |
|-------------|-------------|----------------|----------------|
| FP16 (from FP32) | 50% | -10% (faster) | Negligible |
| INT8 (dynamic) | 75% | -20% (faster) | < 1% quality loss |
| INT4 (GPTQ/AWQ) | 87% | -30% (faster) | 1-3% quality loss |

## Reliability

### Multi-AZ Deployment

- Always deploy minimum 2 instances across 2 AZs
- ALB cross-zone load balancing: enabled
- ASG AZ rebalancing: enabled (but suspend during deployments)
- Health check grace period: > model load time

### Graceful Degradation

| Failure Mode | Detection | Response |
|-------------|-----------|----------|
| Single instance failure | ALB health check (30s) | Traffic routes to healthy instances |
| AZ failure | Multiple unhealthy targets | ASG launches in surviving AZ |
| Model corruption | Inference errors spike | Rollback to previous version via SSM |
| GPU hardware error | ECC errors in nvidia-smi | Terminate instance, ASG replaces |
| Upstream dependency down | Request timeout | Return cached response or 503 with Retry-After |

### Capacity Reservation

For latency-critical production workloads:
- Use On-Demand Capacity Reservations (ODCR) in your target AZs
- Ensures instances are always available, even during capacity crunches
- Combine with Savings Plans for cost optimization
- Reserve capacity = min ASG size; burst above on standard On-Demand
