# Distributed Training — Components Reference

## AWS Infrastructure Components

| Component | Purpose | Key Configuration | Cost Factor |
|-----------|---------|-------------------|-------------|
| EC2 GPU Instances (p4d/p5) | Training compute nodes | Instance type, placement group, EFA | Primary cost (85-95%) |
| Elastic Fabric Adapter (EFA) | High-bandwidth inter-node communication | Interface type, security group rules | Included in instance cost |
| FSx for Lustre | Shared high-performance filesystem | Capacity, throughput tier, compression | Per-GB + throughput tier |
| S3 | Dataset source, checkpoint storage, final model | Lifecycle rules, versioning | Per-GB + requests |
| Placement Group (Cluster) | Co-locate instances for lowest latency | Strategy: cluster, single AZ | Free |
| VPC + Private Subnets | Network isolation, EFA traffic containment | Large CIDR (EFA uses many IPs) | NAT gateway for S3 access |
| Security Groups | EFA inter-node traffic + management access | All ports intra-SG, SSH from bastion | Free |
| CloudWatch | Training metrics, GPU utilization, loss curves | Custom namespace, metric math | Per-metric + ingestion |
| Step Functions | Training job orchestration, failure recovery | State machine, timeouts, retry | Per-state-transition |
| IAM Roles | Node access to S3/FSx/CloudWatch/SSM | Least privilege, instance profile | Free |
| KMS | Encryption for FSx, S3, EBS, CloudWatch Logs | Multi-service key policy | Per-key + requests |
| SSM Parameter Store | Cluster configuration, hyperparameters | Versioned parameters | Free tier |
| EventBridge | Training completion events, scheduled cleanup | Rules, targets | Per-event |
| SNS | Training completion/failure notifications | Topics, email/Lambda subscriptions | Per-publish |

## Training Frameworks

| Framework | Parallelism | Key Features | Best For |
|-----------|------------|--------------|----------|
| PyTorch DDP | Data parallel | Built-in, simple API, good scaling | Standard distributed training |
| PyTorch FSDP | Data + model sharding | ZeRO-3 equivalent, native PyTorch | Large models, PyTorch-native |
| DeepSpeed | Data + model + pipeline | ZeRO 1-3, offloading, MoE support | Very large models, memory optimization |
| Megatron-LM | Tensor + pipeline + data | Maximum throughput, custom kernels | Pre-training 100B+ models |
| FairScale | Data + model + pipeline | Modular, research-friendly | Research and experimentation |
| Colossal-AI | All parallelism types | Auto-parallelism, heterogeneous | Automated parallelization |

## Component Interactions

```
Step Functions (orchestration)
    → EC2 Instances (N nodes in placement group)
        ← → EFA (gradient sync, all-reduce)
        ← → FSx Lustre (shared dataset, checkpoints)
        → S3 (final model upload, periodic checkpoints)
        → CloudWatch (training metrics, GPU stats)
    → EventBridge (completion event)
    → SNS (notification)
```

## Network Architecture

| Layer | Component | Bandwidth | Latency | Purpose |
|-------|-----------|-----------|---------|---------|
| Intra-GPU | NVLink/NVSwitch | 600-900 GB/s | <1 μs | Tensor parallelism within node |
| Intra-Node PCIe | PCIe Gen5 | 64 GB/s | ~1 μs | GPU ↔ CPU data transfer |
| Inter-Node | EFA (p4d: 400 Gbps, p5: 3200 Gbps) | 50-400 GB/s | 5-15 μs | Data parallelism across nodes |
| Storage | FSx Lustre | 1-12 GB/s | 1-5 ms | Dataset reads, checkpoint writes |
| Management | Standard ENI (25 Gbps) | ~3 GB/s | <1 ms | SSH, S3 access, CloudWatch |

## Storage Hierarchy

| Level | Component | Capacity | Bandwidth | Use |
|-------|-----------|----------|-----------|-----|
| L1 | GPU HBM (VRAM) | 80 GB/GPU | 3.35 TB/s (H100) | Model weights, activations |
| L2 | NVMe Instance Store | 8-30 TB | 7-14 GB/s | Dataset cache, scratch |
| L3 | FSx Lustre | 1.2-50 TB | 1-12 GB/s | Shared dataset, checkpoints |
| L4 | S3 | Unlimited | 50-100 Gbps (multi-stream) | Cold storage, final artifacts |

## Fault Tolerance Components

| Component | Failure Handled | Recovery Mechanism | Recovery Time |
|-----------|----------------|-------------------|---------------|
| NCCL Watchdog | Communication timeout | Abort + restart from checkpoint | 5-10 minutes |
| Checkpoint Manager | Node failure, Spot interruption | Resume from last checkpoint | 10-30 minutes |
| Node Health Monitor | GPU errors, OOM, thermal shutdown | Replace node, rejoin ring | 5-15 minutes |
| EFA Connection Manager | Network partition, link flap | Reconnect + barrier sync | 1-5 minutes |
| Step Functions Retry | Any training failure | Re-provision + resume | 10-30 minutes |
| Spot Rebalancing | Capacity reclaim warning | Emergency checkpoint + migrate | 2-5 minutes |

## Monitoring Stack

| Metric | Source | Update Frequency | Alert Threshold |
|--------|--------|-----------------|-----------------|
| Training Loss | Application | Every step | NaN or 10× spike |
| Learning Rate | Application | Every step | Unexpected zero |
| Throughput (tokens/sec) | Application | Every 100 steps | Drop > 30% |
| GPU Utilization | DCGM/nvidia-smi | Every 10s | < 50% sustained |
| GPU Memory | DCGM/nvidia-smi | Every 10s | > 95% (OOM risk) |
| GPU Temperature | DCGM/nvidia-smi | Every 10s | > 83°C |
| EFA Tx/Rx Bytes | CloudWatch (VPC) | Every 1 min | Drop > 50% (communication failure) |
| FSx Throughput | CloudWatch (FSx) | Every 1 min | Sustained > 80% of provisioned |
| Node Count | ASG/Custom | Every 1 min | < expected (node loss) |
| Checkpoint Age | Application | Every checkpoint | > 2× checkpoint interval |

## Cost Breakdown (Typical 8-Node p4d Training Job, 1 Week)

| Component | Cost | Percentage |
|-----------|------|-----------|
| EC2 Instances (8× p4d.24xlarge × 168 hrs) | $44,050 | 92% |
| FSx Lustre (2.4 TB, PERSISTENT_2) | $350 | 0.7% |
| S3 (checkpoints + dataset) | $50 | 0.1% |
| NAT Gateway (data transfer) | $200 | 0.4% |
| CloudWatch (metrics + logs) | $100 | 0.2% |
| Other (KMS, SSM, etc.) | $20 | <0.1% |
| **Total** | **~$44,770** | **100%** |
