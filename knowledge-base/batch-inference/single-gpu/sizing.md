# NovaMind Batch Inference Sizing Guide

## Instance Selection for Batch Workloads

Batch inference prioritizes throughput over latency. Larger batch sizes improve GPU utilization.

### Recommended Instance Types

| Instance Type | GPU | GPU Memory | Best For | Throughput (batch=16) |
|--------------|-----|------------|----------|----------------------|
| g5.xlarge | 1x A10G | 24 GB | Small models, high volume | ~800 samples/s |
| g5.2xlarge | 1x A10G | 24 GB | Medium models + heavy preprocessing | ~800 samples/s |
| g5.12xlarge | 4x A10G | 96 GB | Large models or multi-model parallel | ~2400 samples/s |
| p4d.24xlarge | 8x A100 | 320 GB | Very large models (30B+) | ~1500 samples/s |

### Sizing by Dataset

| Dataset Size | Model Size | Recommended | Est. Duration | Est. Cost |
|-------------|-----------|-------------|---------------|-----------|
| 10K samples | <7B | g5.xlarge | ~15 min | ~$0.25 |
| 100K samples | <7B | g5.xlarge | ~2 hours | ~$2.00 |
| 1M samples | <7B | g5.12xlarge | ~7 hours | ~$40.00 |
| 100K samples | 7-30B | g5.12xlarge | ~12 hours | ~$68.00 |
| 1M samples | 30B+ | p4d.24xlarge | ~18 hours | ~$590.00 |

## Cost Optimization with Spot

Spot pricing (typical discount):
- g5.xlarge: ~$0.35/hour (65% savings)
- g5.12xlarge: ~$2.00/hour (65% savings)
- p4d.24xlarge: ~$12.00/hour (63% savings)

**Important**: Always enable checkpointing when using Spot instances.
Spot interruption rate for g5 family: ~5% (low, but not zero).

## Memory Planning

```
Required GPU Memory = model_size + (batch_size × input_tensor_size) + runtime_overhead
- model_size: parameters × 2 bytes (FP16) or × 1 byte (INT8)
- input_tensor_size: varies by model (typically 1-10 MB per sample)
- runtime_overhead: ~2 GB (CUDA context + framework)
```

## Disk Planning

Local NVMe scratch space needed:
```
scratch_space = dataset_size + output_size + 2 × (batch_size × sample_size)
```

g5.xlarge provides 250 GB NVMe — sufficient for most batch jobs.
For datasets >200 GB, stream from S3 instead of staging locally.
