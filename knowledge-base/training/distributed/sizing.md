# Distributed Training — Sizing Guide

## Multi-Node Scaling by Model Size

| Model Size | Parameters | Min Nodes (p4d) | Recommended Nodes (p4d) | Min Nodes (p5) | Recommended Nodes (p5) |
|------------|-----------|-----------------|------------------------|----------------|----------------------|
| Medium | 7-13B | 1 | 2 | 1 | 1 |
| Large | 13-30B | 2 | 4 | 1 | 2 |
| Very-Large | 30-70B | 4 | 8 | 2 | 4 |
| Extreme | 70B+ | 8 | 16-32 | 4 | 8-16 |

## Node Count Decision Matrix

| Factor | Fewer Nodes | More Nodes |
|--------|------------|------------|
| Wall-clock time constraint | — | ✓ Scales sub-linearly |
| Cost constraint | ✓ Less communication overhead | — |
| Model fits in single node memory | ✓ No tensor parallelism needed | — |
| Model exceeds single node memory | — | ✓ Required for tensor/pipeline parallelism |
| Dataset > 1TB | — | ✓ Faster data loading with more I/O bandwidth |
| Training duration > 1 week | — | ✓ Reduces wall-clock despite overhead |

## Communication Overhead

### EFA Bandwidth Requirements

| Cluster Size | Gradient Sync Volume (13B) | Gradient Sync Volume (70B) | Required Bandwidth | EFA Meets? |
|-------------|---------------------------|---------------------------|-------------------|------------|
| 2 nodes | 26 GB per step | 140 GB per step | 50 Gbps | ✓ (400 Gbps on p4d) |
| 4 nodes | 26 GB per step | 140 GB per step | 100 Gbps | ✓ |
| 8 nodes | 26 GB per step | 140 GB per step | 200 Gbps | ✓ |
| 16 nodes | 26 GB per step | 140 GB per step | 400 Gbps | ✓ (3200 Gbps on p5) |
| 32 nodes | 26 GB per step | 140 GB per step | 800 Gbps | ✓ (p5 only) |

### Communication Time per Training Step

| Cluster Size | Ring AllReduce (13B) | Ring AllReduce (70B) | Tree AllReduce (70B) |
|-------------|---------------------|---------------------|---------------------|
| 2 nodes (p4d) | 0.5 sec | 2.8 sec | 2.5 sec |
| 4 nodes (p4d) | 0.9 sec | 5.0 sec | 3.5 sec |
| 8 nodes (p4d) | 1.6 sec | 8.8 sec | 5.0 sec |
| 16 nodes (p4d) | 2.8 sec | 15.4 sec | 7.5 sec |
| 8 nodes (p5) | 0.2 sec | 1.1 sec | 0.8 sec |
| 16 nodes (p5) | 0.4 sec | 2.0 sec | 1.2 sec |

## Scaling Efficiency

### Data Parallelism (DDP) Efficiency

| Nodes | Ideal Speedup | Actual Speedup (13B) | Actual Speedup (70B) | Efficiency |
|-------|--------------|---------------------|---------------------|------------|
| 1 | 1.0× | 1.0× | 1.0× | 100% |
| 2 | 2.0× | 1.85× | 1.80× | 92% / 90% |
| 4 | 4.0× | 3.5× | 3.3× | 88% / 82% |
| 8 | 8.0× | 6.4× | 5.8× | 80% / 73% |
| 16 | 16.0× | 11.2× | 9.6× | 70% / 60% |
| 32 | 32.0× | 19.2× | 15.4× | 60% / 48% |

### Model Parallelism (Tensor + Pipeline) Efficiency

| Parallelism | Nodes | Efficiency (30B) | Efficiency (70B) | Best For |
|-------------|-------|-----------------|-----------------|----------|
| Tensor (TP=8) | 1 | 95% | 93% | Single-node multi-GPU |
| Pipeline (PP=2) | 2 | 85% | 88% | Cross-node, low bandwidth |
| TP=8, PP=2 | 2 | 80% | 85% | Large models, 2 nodes |
| TP=8, PP=4 | 4 | 72% | 78% | Very-large models |
| TP=8, PP=4, DP=2 | 8 | 65% | 70% | Maximum throughput |

## Instance Selection

### p4d.24xlarge (8× A100 80GB, 400 Gbps EFA)

| Metric | Value |
|--------|-------|
| GPU memory per node | 640 GB (8 × 80 GB) |
| Inter-node bandwidth | 400 Gbps (4× EFA adapters) |
| Intra-node bandwidth | 600 GB/s (NVLink) |
| Hourly cost (On-Demand) | $32.77 |
| Hourly cost (Spot) | $13.11 |
| Best for | 13-70B models, cost-sensitive training |

### p5.48xlarge (8× H100 80GB, 3200 Gbps EFA)

| Metric | Value |
|--------|-------|
| GPU memory per node | 640 GB (8 × 80 GB) |
| Inter-node bandwidth | 3200 Gbps (32× EFA adapters) |
| Intra-node bandwidth | 900 GB/s (NVSwitch) |
| Hourly cost (On-Demand) | $65.39 |
| Hourly cost (Spot) | $26.16 |
| Best for | 70B+ models, time-sensitive training, maximum scaling efficiency |

## Storage Sizing (FSx Lustre)

### Capacity Planning

| Dataset Size | Checkpoint Size (70B) | Recommended FSx Capacity | Throughput Mode |
|-------------|----------------------|--------------------------|-----------------|
| < 100 GB | 140 GB × 5 saves | 1.2 TB | 125 MB/s/TiB |
| 100 GB - 1 TB | 140 GB × 5 saves | 2.4 TB | 250 MB/s/TiB |
| 1 TB - 10 TB | 140 GB × 5 saves | 12 TB | 500 MB/s/TiB |
| > 10 TB | 140 GB × 5 saves | 24+ TB | 1000 MB/s/TiB |

### Data Loading Throughput Requirements

| Nodes | Batch Size (global) | Seq Length | Data Read Rate | FSx Throughput Needed |
|-------|--------------------|-----------|--------------|--------------------|
| 2 | 64 | 2048 | 2 GB/step | 500 MB/s |
| 4 | 128 | 2048 | 4 GB/step | 1 GB/s |
| 8 | 256 | 2048 | 8 GB/step | 2 GB/s |
| 16 | 512 | 2048 | 16 GB/step | 4 GB/s |

### FSx Lustre Pricing (us-east-1)

| Capacity | Throughput | Monthly Cost | Per-GB Cost |
|----------|-----------|-------------|-------------|
| 1.2 TB | 150 MB/s | $175 | $0.145/GB |
| 2.4 TB | 600 MB/s | $350 | $0.145/GB |
| 12 TB | 6 GB/s | $1,740 | $0.145/GB |

## Network Topology

### Placement Group Requirements

| Cluster Size | Placement Strategy | Rationale |
|-------------|-------------------|-----------|
| 2-8 nodes | Cluster (single AZ) | Maximum inter-node bandwidth, lowest latency |
| 8-16 nodes | Cluster (single AZ) | EFA requires cluster placement group |
| 16-32 nodes | Cluster (may hit AZ limits) | Request capacity reservation in advance |

### EFA Configuration per Instance

| Instance | EFA Adapters | Bandwidth per Adapter | Total Bandwidth |
|----------|-------------|----------------------|-----------------|
| p4d.24xlarge | 4 | 100 Gbps | 400 Gbps |
| p5.48xlarge | 32 | 100 Gbps | 3200 Gbps |

## Cost Estimates

### Training Cost Formula

```
Total Cost = (instance_hourly × nodes × training_hours) + storage_monthly + data_transfer

Where:
  training_hours = (dataset_tokens / throughput_tokens_per_second) / 3600
  throughput = single_node_throughput × nodes × scaling_efficiency
```

### Example: Full Training of 70B Model

| Configuration | Nodes | Training Time | Instance Cost | Storage Cost | Total |
|--------------|-------|--------------|--------------|-------------|-------|
| p4d.24xlarge × 8 | 8 | 14 days | $88,150 | $350 | $88,500 |
| p4d.24xlarge × 16 | 16 | 9 days | $113,300 | $350 | $113,650 |
| p5.48xlarge × 4 | 4 | 10 days | $62,770 | $350 | $63,120 |
| p5.48xlarge × 8 | 8 | 6 days | $75,324 | $350 | $75,674 |

### Spot Instance Strategy for Training

| Training Duration | Spot Viable? | Strategy | Checkpoint Frequency |
|-------------------|-------------|----------|---------------------|
| < 4 hours | Yes | Full Spot | Every 30 minutes |
| 4-24 hours | Yes (with risk) | Spot + checkpointing | Every 15 minutes |
| 1-7 days | Partially | Spot with fallback to On-Demand | Every 10 minutes |
| > 7 days | No | On-Demand or Savings Plan | Every 30 minutes |

## Memory Distribution Across Nodes

### DeepSpeed ZeRO Stage 3

| Component | Distribution | Per-Node Memory (70B, 8 nodes) |
|-----------|-------------|-------------------------------|
| Model parameters | Sharded across all nodes | 17.5 GB (140 GB / 8) |
| Gradients | Sharded across all nodes | 17.5 GB (140 GB / 8) |
| Optimizer states | Sharded across all nodes | 70 GB (560 GB / 8) |
| Activations | Per-node (not shared) | 40-80 GB (batch dependent) |
| **Total per node** | — | **145-185 GB** |

### FSDP (Fully Sharded Data Parallel)

| Sharding Strategy | Memory per Node (70B, 4 nodes) | Communication Overhead |
|-------------------|-------------------------------|----------------------|
| FULL_SHARD | 200 GB | High (all-gather + reduce-scatter) |
| SHARD_GRAD_OP | 350 GB | Medium (reduce-scatter only) |
| NO_SHARD | 640 GB (won't fit) | Low (all-reduce only) |
