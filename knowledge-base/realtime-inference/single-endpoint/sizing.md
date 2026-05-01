# NovaMind Real-Time Inference Sizing Guide

## Instance Type Selection

Choose GPU instance type based on model size, batch size, and latency requirements.

### Recommended Instance Types

| Instance Type | GPU | GPU Memory | vCPUs | Use Case | Max Throughput |
|--------------|-----|------------|-------|----------|----------------|
| g5.xlarge | 1x A10G | 24 GB | 4 | Small models (<7B params) | ~200 req/s |
| g5.2xlarge | 1x A10G | 24 GB | 8 | Medium models with preprocessing | ~200 req/s |
| g5.4xlarge | 1x A10G | 24 GB | 16 | CPU-heavy pre/post processing | ~200 req/s |
| g5.12xlarge | 4x A10G | 96 GB | 48 | Large models (7-30B params) | ~150 req/s |
| p4d.24xlarge | 8x A100 | 320 GB | 96 | Very large models (30B+ params) | ~100 req/s |

### Sizing Formula

```
Required GPU Memory = model_size_gb × 1.2 (runtime overhead)
Required instances = ceil(target_rps / instance_max_rps)
```

## Performance Benchmarks

### Latency by Model Size (single request, batch=1)
| Model Size | g5.xlarge | g5.12xlarge | p4d.24xlarge |
|-----------|-----------|-------------|--------------|
| 1B params | 15ms | 12ms | 8ms |
| 7B params | 85ms | 45ms | 25ms |
| 13B params | N/A (OOM) | 90ms | 50ms |
| 30B params | N/A | N/A (OOM) | 120ms |

### Throughput by Batch Size
| Batch Size | Latency Multiplier | Throughput Multiplier |
|-----------|-------------------|---------------------|
| 1 | 1.0x | 1.0x |
| 4 | 1.5x | 3.5x |
| 8 | 2.0x | 6.0x |
| 16 | 3.0x | 10.0x |

## Cost Estimation (us-east-1, on-demand)

- g5.xlarge: ~$1.006/hour (~$734/month)
- g5.12xlarge: ~$5.672/hour (~$4,141/month)
- p4d.24xlarge: ~$32.77/hour (~$23,922/month)

### Cost Optimization
- Use Spot Instances for non-critical workloads (60-70% savings)
- Reserved Instances for steady-state production (30-40% savings)
- Right-size based on actual GPU utilization metrics after 1 week
