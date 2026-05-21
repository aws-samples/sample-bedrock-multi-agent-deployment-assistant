# Auto-Scaling Inference Fleet — Sizing Guide

## Instance Type Selection

| Instance | GPUs | GPU Memory | vCPUs | Network | Best For |
|----------|------|-----------|-------|---------|----------|
| g5.xlarge | 1× A10G | 24 GB | 4 | 10 Gbps | Small models (<7B), low-traffic endpoints |
| g5.2xlarge | 1× A10G | 24 GB | 8 | 10 Gbps | Small models with preprocessing overhead |
| g5.12xlarge | 4× A10G | 96 GB | 48 | 40 Gbps | Medium models (7-13B), multi-model serving |
| g5.48xlarge | 8× A10G | 192 GB | 192 | 100 Gbps | Large models (13-30B), high-throughput |
| p4d.24xlarge | 8× A100 | 320 GB | 96 | 400 Gbps | Very-large models (30B+), lowest latency |
| p5.48xlarge | 8× H100 | 640 GB | 192 | 3200 Gbps | Very-large models, maximum throughput |

## Fleet Sizing by Model Size

### Small Models (<7B parameters)

| Metric | g5.xlarge | g5.2xlarge |
|--------|-----------|------------|
| Max concurrent requests | 8-12 | 8-12 |
| Throughput (tokens/sec) | 800-1200 | 800-1200 |
| P99 latency (ms) | 45-80 | 40-70 |
| Cost per 1M tokens | $0.12 | $0.16 |
| Recommended fleet size (100 RPS) | 10-14 instances | 10-14 instances |

### Medium Models (7-13B parameters)

| Metric | g5.12xlarge | g5.48xlarge |
|--------|-------------|-------------|
| Max concurrent requests | 16-24 | 32-48 |
| Throughput (tokens/sec) | 2000-3500 | 4000-7000 |
| P99 latency (ms) | 80-150 | 60-120 |
| Cost per 1M tokens | $0.28 | $0.35 |
| Recommended fleet size (100 RPS) | 5-7 instances | 3-4 instances |

### Large Models (13-30B parameters)

| Metric | g5.48xlarge | p4d.24xlarge |
|--------|-------------|--------------|
| Max concurrent requests | 12-16 | 24-32 |
| Throughput (tokens/sec) | 1500-2500 | 4000-6000 |
| P99 latency (ms) | 150-300 | 80-150 |
| Cost per 1M tokens | $0.65 | $0.85 |
| Recommended fleet size (100 RPS) | 7-10 instances | 3-5 instances |

### Very-Large Models (30B+ parameters)

| Metric | p4d.24xlarge | p5.48xlarge |
|--------|--------------|-------------|
| Max concurrent requests | 8-12 | 16-24 |
| Throughput (tokens/sec) | 1500-2500 | 4000-8000 |
| P99 latency (ms) | 200-500 | 100-250 |
| Cost per 1M tokens | $1.50 | $1.80 |
| Recommended fleet size (100 RPS) | 10-14 instances | 5-7 instances |

## Scaling Thresholds

### Target Tracking Policies

| Metric | Scale-Out Threshold | Scale-In Threshold | Evaluation Period |
|--------|--------------------|--------------------|-------------------|
| GPU Utilization | > 70% | < 30% | 3 minutes |
| Request Queue Depth | > 50 per instance | < 10 per instance | 1 minute |
| P99 Latency | > target × 1.5 | < target × 0.5 | 5 minutes |
| CPU Utilization | > 80% | < 40% | 3 minutes |

### Scaling Behavior Configuration

| Parameter | Recommended Value | Rationale |
|-----------|------------------|-----------|
| Scale-out cooldown | 60 seconds | Fast reaction to traffic spikes |
| Scale-in cooldown | 300 seconds | Avoid flapping on traffic variance |
| Min capacity | 2 | High availability (multi-AZ) |
| Max capacity | 20 | Budget protection (adjust per workload) |
| Warm pool size | ceil(min_capacity × 0.5) | 50% of min for burst absorption |

## Warm Pool Sizing

Warm pools pre-initialize instances with the ML model loaded into GPU memory, reducing scale-out latency from 5-10 minutes to 30-60 seconds.

| Fleet Size | Warm Pool Size | Rationale |
|------------|---------------|-----------|
| 2-5 instances | 1-2 | Handles single-instance failure + small burst |
| 5-10 instances | 2-3 | Handles 20-30% traffic spike |
| 10-20 instances | 3-5 | Handles gradual ramp without cold starts |

### Model Preload Time (Cold Start)

| Model Size | Without Warm Pool | With Warm Pool |
|------------|-------------------|----------------|
| Small (<7B) | 2-4 minutes | 20-30 seconds |
| Medium (7-13B) | 4-6 minutes | 30-45 seconds |
| Large (13-30B) | 6-10 minutes | 45-90 seconds |
| Very-Large (30B+) | 10-15 minutes | 60-120 seconds |

## Capacity Planning

### Peak-to-Steady Ratios

| Workload Pattern | Peak/Steady Ratio | Recommended Buffer |
|------------------|-------------------|-------------------|
| Business hours only | 3:1 | Min capacity = steady-state need |
| Global 24/7 | 1.5:1 | Min capacity = 70% of average |
| Event-driven (unpredictable) | 10:1 | Warm pool = 30% of max |
| Gradual growth | 2:1 | Review and adjust monthly |

### Formula: Min Fleet Size

```
min_instances = ceil(target_rps / per_instance_rps) × availability_factor

Where:
  per_instance_rps = max_concurrent_requests / avg_latency_seconds
  availability_factor = 1.5 (production-multi-az) or 1.0 (development)
```

### Formula: Max Fleet Size

```
max_instances = ceil(peak_rps / per_instance_rps) × 1.2

The 1.2 factor accounts for:
  - Instance health check failures (1 in 10)
  - Uneven load distribution across AZs
```

## Cost Optimization

### Spot Instance Mix

| Availability Requirement | On-Demand % | Spot % | Savings vs Full On-Demand |
|--------------------------|-------------|--------|---------------------------|
| production-multi-az | 100% | 0% | Baseline |
| production-single-az | 70% | 30% | 15-20% |
| staging | 30% | 70% | 45-55% |
| development | 0% | 100% | 60-70% |

### Cost Comparison (us-east-1, monthly, 10-instance fleet)

| Instance | On-Demand/month | Spot/month | Savings Plan/month |
|----------|----------------|------------|-------------------|
| g5.xlarge | $11,880 | $4,750 | $7,720 |
| g5.12xlarge | $66,960 | $26,780 | $43,520 |
| p4d.24xlarge | $236,520 | $94,600 | $153,740 |
| p5.48xlarge | $472,320 | $188,930 | $307,010 |

## Horizontal vs Vertical Scaling Decision

| Criterion | Scale Horizontally | Scale Vertically |
|-----------|-------------------|-----------------|
| Latency-sensitive | ✓ More instances = shorter queues | — |
| Cost-sensitive | — | ✓ Fewer larger instances amortize overhead |
| Model fits single GPU | ✓ Simple replication | — |
| Model requires multi-GPU | — | ✓ Must use larger instance type |
| High availability required | ✓ Spread across AZs | — |
| Bursty traffic | ✓ Fast scale-out with warm pool | — |
