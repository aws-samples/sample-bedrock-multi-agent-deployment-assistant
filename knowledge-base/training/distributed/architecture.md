# NovaMind Distributed Training Architecture

## Overview

Multi-node distributed training using NovaMind's training runtime across multiple GPU instances. Supports data parallelism and model parallelism for training models that don't fit on a single GPU.

## Architecture Components

- **Training Cluster**: Multiple p4d/p5 instances in a placement group
- **EFA (Elastic Fabric Adapter)**: High-bandwidth, low-latency inter-node communication
- **FSx for Lustre**: High-performance shared filesystem for training data
- **S3**: Training datasets (cold storage) and checkpoint storage
- **SageMaker (optional)**: Managed training job orchestration

## Cluster Design

### Network Configuration
- **Placement Group**: Cluster strategy for lowest latency between nodes
- **EFA**: 400 Gbps network throughput (p4d) or 3.2 Tbps (p5e)
- **Security Group**: Allow all traffic within training cluster SG
- **No public access**: Training nodes are fully private

### Storage Hierarchy
| Tier | Storage | Use | Bandwidth |
|------|---------|-----|-----------|
| L1 | GPU HBM | Active tensors | 2 TB/s |
| L2 | Local NVMe | Data loader cache | 7 GB/s |
| L3 | FSx Lustre | Shared dataset | 100+ GB/s aggregate |
| L4 | S3 | Checkpoints, cold data | Unlimited (parallel) |

## Training Strategies

### Data Parallelism
- Each node holds full model copy
- Gradients synchronized via AllReduce (NCCL over EFA)
- Linear scaling up to 8-16 nodes for most architectures
- Recommended for models that fit in single-GPU memory

### Model Parallelism (Tensor)
- Model layers split across GPUs
- Required for models exceeding single-GPU memory
- Higher communication overhead — benefits from EFA
- Typically combined with data parallelism (hybrid)

### Pipeline Parallelism
- Model split into sequential stages across nodes
- Micro-batching hides pipeline bubbles
- Best for very deep models with uniform layer sizes

## Checkpointing

- Full checkpoint every N steps (configurable, default: every 1000 steps)
- Async checkpoint to S3 (doesn't block training)
- Resume from any checkpoint on cluster resize or failure
- Checkpoint size = model_params × 12 bytes (params + optimizer state + gradients)

## Fault Tolerance

- Node failure detected in <30s via NCCL timeout
- Training resumes from last checkpoint on replacement node
- EC2 Auto Scaling handles node replacement
- EFA connection re-establishment: ~10s after new node joins
