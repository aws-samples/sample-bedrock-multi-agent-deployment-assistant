# NovaMind Single-Node Training Architecture

## Overview

Single-node training for models that fit within one instance's GPU memory. Simplest deployment — no distributed communication overhead. Suitable for fine-tuning, small model training, and experimentation.

## Architecture Components

- **Training Instance**: Single multi-GPU instance (g5.12xlarge or p4d.24xlarge)
- **S3**: Dataset storage and checkpoint persistence
- **EBS**: Optional persistent volume for large local datasets
- **CloudWatch**: Training metrics (loss, throughput, GPU utilization)

## Instance Selection

| Instance | GPUs | GPU Memory | Best For |
|----------|------|------------|----------|
| g5.12xlarge | 4x A10G | 96 GB | Fine-tuning models up to 13B params |
| g5.48xlarge | 8x A10G | 192 GB | Training models up to 20B params |
| p4d.24xlarge | 8x A100 | 320 GB | Training models up to 65B params |
| p5.48xlarge | 8x H100 | 640 GB | Training models up to 130B params |

## Training Flow

1. Launch instance from training AMI (CUDA, NovaMind runtime pre-installed)
2. Pull dataset from S3 to local NVMe (or mount FSx Lustre)
3. Load model architecture and initialize weights (random or pre-trained)
4. Training loop: forward → loss → backward → optimizer step
5. Periodic checkpoints saved to S3
6. Final model exported to S3 model registry
7. Instance terminated (Spot or on-demand based on urgency)

## Data Loading

- **NVMe staging**: Copy dataset to local NVMe for fastest I/O
- **Memory-mapped files**: Efficient random access without full RAM copy
- **Prefetch workers**: 4-8 CPU workers preparing next batches
- **Data augmentation**: Applied on-the-fly by CPU workers

## GPU Utilization Optimization

- Target: >80% GPU utilization during training
- Mixed precision (FP16/BF16): 2x throughput, half memory
- Gradient accumulation: simulate larger batch sizes without more GPU memory
- Gradient checkpointing: trade compute for memory (enables larger models)
