# Single-Node Training — Sizing Guide

## GPU Memory Requirements by Model Size

| Model Size | Parameters | FP32 Memory | FP16/BF16 Memory | Optimizer States (AdamW) | Total Training Memory |
|------------|-----------|-------------|-------------------|--------------------------|----------------------|
| Small | <7B | 28 GB | 14 GB | 56 GB | 98 GB (FP32) / 84 GB (FP16) |
| Medium | 7-13B | 28-52 GB | 14-26 GB | 56-104 GB | 98-182 GB |
| Large | 13-30B | 52-120 GB | 26-60 GB | 104-240 GB | 182-420 GB |
| Very-Large | 30B+ | 120+ GB | 60+ GB | 240+ GB | 420+ GB |

### Memory Breakdown Formula

```
Total GPU Memory = Model Weights + Gradients + Optimizer States + Activations

Where:
  Model Weights (FP16) = parameters × 2 bytes
  Gradients = parameters × 2 bytes (same precision as weights)
  Optimizer States (AdamW) = parameters × 8 bytes (momentum + variance in FP32)
  Activations = batch_size × sequence_length × hidden_dim × num_layers × 2 bytes
```

## Instance Type Selection

### Fine-Tuning (LoRA/QLoRA)

| Model Size | Recommended Instance | GPU Memory Used | Training Time (1 epoch, 10K samples) |
|------------|---------------------|-----------------|--------------------------------------|
| Small (<7B) | g5.2xlarge | 12-18 GB / 24 GB | 1-3 hours |
| Medium (7-13B) | g5.12xlarge | 60-80 GB / 96 GB | 3-8 hours |
| Large (13-30B) | g5.48xlarge | 120-160 GB / 192 GB | 8-20 hours |
| Very-Large (30B+) | p4d.24xlarge | 200-280 GB / 320 GB | 20-48 hours |

### Full Training / Full Fine-Tuning

| Model Size | Recommended Instance | GPU Memory Used | Training Time (1 epoch, 100K samples) |
|------------|---------------------|-----------------|---------------------------------------|
| Small (<7B) | g5.12xlarge | 80-96 GB / 96 GB | 6-12 hours |
| Medium (7-13B) | g5.48xlarge | 140-180 GB / 192 GB | 12-36 hours |
| Large (13-30B) | p4d.24xlarge | 250-310 GB / 320 GB | 36-96 hours |
| Very-Large (30B+) | p5.48xlarge | 400-600 GB / 640 GB | 72-240 hours |

## Batch Size Recommendations

### Per GPU Type

| GPU | Memory | Max Batch Size (7B, FP16) | Max Batch Size (13B, FP16) | Optimal Batch Size |
|-----|--------|---------------------------|----------------------------|--------------------|
| A10G (24 GB) | 24 GB | 4 | 1-2 (LoRA only) | 2-4 |
| A10G × 4 (96 GB) | 96 GB | 16 | 8 | 8-16 |
| A100 × 8 (320 GB) | 320 GB | 64 | 32 | 32-64 |
| H100 × 8 (640 GB) | 640 GB | 128 | 64 | 64-128 |

### Gradient Accumulation

When batch size is limited by GPU memory, use gradient accumulation:

```
effective_batch_size = micro_batch_size × gradient_accumulation_steps

Example: micro_batch=4, accumulation=8 → effective_batch=32
```

| Effective Batch Size | Gradient Accumulation Steps | Impact on Training Time |
|---------------------|----------------------------|------------------------|
| 32 | 8 (micro=4) | ~1.1× slower than native batch=32 |
| 64 | 16 (micro=4) | ~1.2× slower |
| 128 | 32 (micro=4) | ~1.3× slower |

## Storage Requirements

### EBS Volume Sizing

| Component | Size Formula | Example (13B model, 50GB dataset) |
|-----------|-------------|-----------------------------------|
| Model weights | parameters × 4 bytes × 1.5 (headroom) | 78 GB |
| Dataset | raw_size × 2 (tokenized + original) | 100 GB |
| Checkpoints | model_size × num_checkpoints | 260 GB (5 checkpoints) |
| Training logs | ~1 GB per 100 hours | 5 GB |
| System + packages | 30-50 GB | 40 GB |
| **Total recommended** | Sum × 1.3 (safety margin) | **~630 GB** |

### Recommended EBS Configuration

| Training Duration | Volume Type | Size | IOPS | Throughput |
|-------------------|-------------|------|------|------------|
| < 24 hours | gp3 | 500 GB | 3000 | 125 MB/s |
| 24-72 hours | gp3 | 1 TB | 6000 | 250 MB/s |
| > 72 hours | io2 | 1-2 TB | 10000 | 500 MB/s |

## Training Time Estimates

### Fine-Tuning (LoRA, rank=16)

| Model Size | Dataset Size | g5.2xlarge | g5.12xlarge | p4d.24xlarge |
|------------|-------------|------------|-------------|--------------|
| Small (7B) | 10K samples | 1.5 hrs | 0.5 hrs | 0.2 hrs |
| Small (7B) | 100K samples | 15 hrs | 4 hrs | 1.5 hrs |
| Medium (13B) | 10K samples | N/A (OOM) | 2 hrs | 0.7 hrs |
| Medium (13B) | 100K samples | N/A | 18 hrs | 6 hrs |
| Large (30B) | 10K samples | N/A | N/A | 3 hrs |
| Large (30B) | 100K samples | N/A | N/A | 28 hrs |

### Full Fine-Tuning (all parameters)

| Model Size | Dataset Size | g5.12xlarge | g5.48xlarge | p4d.24xlarge | p5.48xlarge |
|------------|-------------|-------------|-------------|--------------|-------------|
| Small (7B) | 10K | 3 hrs | 1.5 hrs | 0.5 hrs | 0.3 hrs |
| Small (7B) | 100K | 28 hrs | 14 hrs | 5 hrs | 2.5 hrs |
| Medium (13B) | 10K | N/A | 4 hrs | 1.5 hrs | 0.7 hrs |
| Medium (13B) | 100K | N/A | 36 hrs | 12 hrs | 6 hrs |
| Large (30B) | 100K | N/A | N/A | 48 hrs | 20 hrs |

## Cost Estimates (us-east-1)

### On-Demand Pricing

| Instance | Hourly Cost | Fine-Tune 7B (10K) | Fine-Tune 13B (100K) | Full Train 7B (100K) |
|----------|-------------|--------------------|-----------------------|---------------------|
| g5.2xlarge | $1.21 | $1.82 | N/A | N/A |
| g5.12xlarge | $5.67 | $2.84 | $102 | $159 |
| g5.48xlarge | $16.29 | N/A | N/A | $228 |
| p4d.24xlarge | $32.77 | $6.55 | $197 | $164 |
| p5.48xlarge | $65.39 | N/A | N/A | $163 |

### Spot Instance Savings

| Instance | On-Demand | Spot (avg) | Savings | Interruption Rate |
|----------|-----------|------------|---------|-------------------|
| g5.2xlarge | $1.21/hr | $0.48/hr | 60% | Low (5-10%) |
| g5.12xlarge | $5.67/hr | $2.27/hr | 60% | Low (5-10%) |
| p4d.24xlarge | $32.77/hr | $13.11/hr | 60% | Medium (10-20%) |
| p5.48xlarge | $65.39/hr | $26.16/hr | 60% | Medium (10-20%) |

## Memory Optimization Techniques

| Technique | Memory Reduction | Training Speed Impact | When to Use |
|-----------|-----------------|----------------------|-------------|
| Mixed Precision (FP16/BF16) | 30-40% | +10-20% faster | Always (modern GPUs) |
| Gradient Checkpointing | 40-60% | -20-30% slower | Large models on limited memory |
| LoRA (rank=16) | 70-90% | +50-100% faster | Fine-tuning only |
| QLoRA (4-bit) | 85-95% | +30-60% faster | Fine-tuning on small GPUs |
| DeepSpeed ZeRO Stage 2 | 50-60% | -5-10% slower | Multi-GPU on single node |
| Flash Attention 2 | 20-30% (activations) | +15-25% faster | Always (supported GPUs) |
