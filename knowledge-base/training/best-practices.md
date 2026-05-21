# Model Training — Best Practices

## Performance

### Mixed Precision Training

| Technique | Memory Savings | Speed Improvement | When to Use |
|-----------|---------------|-------------------|-------------|
| BF16 (BFloat16) | 50% | +15-25% | Default for all modern training (A100, H100) |
| FP16 + loss scaling | 50% | +15-25% | Older GPUs (V100) without BF16 support |
| TF32 (internal) | 0% | +10% | Automatic on Ampere+ GPUs for matmuls |

### Do

- Always use BF16 mixed precision on A100/H100 GPUs (no loss scaling needed)
- Enable Flash Attention 2 for all transformer models (saves 20-30% memory, +20% speed)
- Use `torch.compile` with `mode="reduce-overhead"` for 10-30% speedup on PyTorch 2.x
- Set `CUDA_VISIBLE_DEVICES` explicitly to avoid GPU index confusion
- Use gradient accumulation to simulate larger batch sizes without more memory
- Profile with `torch.profiler` before optimizing — find actual bottlenecks first
- Pin CPU memory (`pin_memory=True` in DataLoader) for faster GPU transfers
- Use `persistent_workers=True` in DataLoader to avoid re-spawning overhead

### Don't

- Train in FP32 on modern GPUs (wastes 50% memory for no quality gain)
- Use gradient checkpointing unless you've confirmed memory is the actual bottleneck
- Set batch size to maximum GPU capacity (leave 10% headroom for spikes)
- Ignore data loading — it's often the real bottleneck, not compute
- Skip warmup steps (causes training instability, especially at high learning rates)

### Data Loading Optimization

| Technique | Impact | Complexity |
|-----------|--------|-----------|
| Multi-worker DataLoader (num_workers=4-8) | 2-4× data throughput | Low |
| Pre-tokenized datasets (save to disk) | 1.5-2× | Low |
| Memory-mapped datasets (mmap) | 1.3× for large datasets | Low |
| WebDataset (tar-based streaming) | Best for distributed | Medium |
| NVIDIA DALI (GPU-accelerated preprocessing) | 2× for image/video | Medium |
| NVMe RAID-0 for dataset cache | 3× I/O vs single EBS | Low |

## Cost

### Spot Instance Strategy

| Training Duration | Strategy | Expected Savings | Risk Level |
|-------------------|----------|-----------------|------------|
| < 4 hours | Full Spot, checkpoint every 30 min | 60-70% | Low |
| 4-24 hours | Spot, checkpoint every 15 min, 1-2 retries budgeted | 55-65% | Medium |
| 1-7 days | Spot first 80%, switch to On-Demand for final 20% | 45-55% | Low |
| > 7 days | On-Demand or Savings Plan (3-year = 50% discount) | 40-50% | None |

### Cost Reduction Checklist

| Action | Savings | Effort |
|--------|---------|--------|
| Use Spot Instances | 60-70% | Medium (need checkpointing) |
| Right-size instance (GPU util > 60%) | 20-50% | Low (monitor, then switch) |
| Use LoRA instead of full fine-tuning | 70-90% compute reduction | Low |
| Reduce dataset size (quality > quantity) | Proportional | Analysis needed |
| Compress checkpoints (safetensors, fp16 saves) | 50% storage cost | Low |
| Schedule during off-peak (weekends, nights) | 5-15% Spot savings | Low |
| Use Savings Plans for recurring workloads | 40-50% On-Demand | Commitment |

### Budget Formula

```
Total Training Budget = compute_cost + storage_cost + data_transfer + overhead

compute_cost = instance_hourly_rate × num_instances × total_hours × (1 + retry_factor)
storage_cost = (ebs_gb × $0.08/GB) + (fsx_gb × $0.145/GB) + (s3_gb × $0.023/GB)
data_transfer = inter_az_gb × $0.01/GB (if multi-AZ, otherwise 0)
retry_factor = 0.1 (On-Demand) or 0.3 (Spot, accounts for interruption restarts)
```

## Reliability

### Checkpoint Strategy

| Training Stage | Checkpoint Frequency | Keep Last N | Rationale |
|---------------|---------------------|-------------|-----------|
| Early (first 10% of steps) | Every 500 steps | 2 | Fast iteration, detect issues |
| Mid-training | Every 1000 steps or 1 hour | 3 | Balance cost and safety |
| Late (last 10% of steps) | Every 500 steps | 5 | Preserve best model candidates |
| Best model | On eval improvement | All (or top 3) | Select best for deployment |

### Checkpoint Contents

| Item | Required? | Size (13B model) |
|------|----------|-----------------|
| Model weights (safetensors) | Yes | 26 GB |
| Optimizer state | Yes (for resume) | 78 GB |
| LR scheduler state | Yes | < 1 KB |
| RNG states (all GPUs) | Yes (reproducibility) | < 1 MB |
| Data loader state (epoch, index) | Yes (resume from exact point) | < 1 MB |
| Training arguments | Yes (configuration audit) | < 10 KB |
| Metrics history | Optional | < 10 MB |

### Automatic Resume

```python
# Pseudocode for robust checkpoint resume
def find_latest_checkpoint(checkpoint_dir):
    """Find the most recent valid checkpoint."""
    checkpoints = sorted(glob(f"{checkpoint_dir}/checkpoint-*"), key=step_number)
    for ckpt in reversed(checkpoints):
        if validate_checkpoint(ckpt):  # Check file integrity
            return ckpt
    return None  # Start from scratch

def train_with_resume(config):
    latest = find_latest_checkpoint(config.output_dir)
    if latest:
        log(f"Resuming from {latest}")
        trainer.train(resume_from_checkpoint=latest)
    else:
        log("Starting fresh training")
        trainer.train()
```

### Failure Recovery Playbook

| Failure | Detection | Recovery | Prevention |
|---------|-----------|----------|-----------|
| GPU OOM | CUDA out of memory error | Reduce batch size, enable gradient checkpointing | Monitor memory before increasing batch |
| NaN loss | Loss becomes NaN/Inf | Reduce learning rate 10×, resume from pre-NaN checkpoint | Use gradient clipping, warmup steps |
| Node crash | Training hangs, SSH fails | Replace node, resume from checkpoint | Health monitoring, auto-replacement |
| Spot interruption | 2-min warning from IMDS | Emergency checkpoint, resume on new instance | Frequent checkpointing |
| Data corruption | Unusual loss patterns | Validate dataset, restart from clean checkpoint | Checksums on dataset files |
| Slow convergence | Loss plateau for >1 epoch | Adjust LR schedule, check data mixing | Learning rate finder, curriculum learning |

## Security

### Training Data Protection

| Control | Implementation |
|---------|---------------|
| Encryption at rest | KMS for EBS, SSE-KMS for S3, FSx encryption |
| Encryption in transit | TLS for S3 access, EFA encryption (optional, 5% overhead) |
| Access control | IAM roles per training job, scoped to specific S3 prefixes |
| Data lineage | Tag checkpoints with dataset version, training config hash |
| Model protection | Encrypt final model artifacts, restrict access to deployment role |
| Audit | CloudTrail for S3 access, training job metadata in DynamoDB |

### Network Isolation

- Train in private subnets with VPC endpoints for S3, CloudWatch, SSM
- EFA traffic stays within VPC (never traverses internet)
- No public IP on training instances
- Use SSM Session Manager instead of SSH for debugging
- Separate security group for training cluster (no cross-talk with other workloads)

## Monitoring

### Training Health Dashboard

| Panel | Metric | Expected Behavior |
|-------|--------|-------------------|
| Loss curve | Training loss over steps | Smooth downward trend |
| Learning rate | LR schedule visualization | Matches configured schedule |
| GPU utilization | Per-GPU util% time series | 70-95% sustained |
| Throughput | Samples/sec or tokens/sec | Stable (±10%) after warmup |
| Gradient norm | Gradient L2 norm | Stable, no sudden spikes |
| Memory utilization | GPU memory over time | Stable after first batch |
| Communication time | % time in all-reduce | < 20% of step time (distributed) |
| Eval metrics | Validation loss, accuracy | Improving (or plateau detection) |

### Early Stopping Signals

| Signal | Action | Threshold |
|--------|--------|-----------|
| Training loss NaN | Stop immediately, investigate | Any NaN |
| Training loss spike > 10× | Pause, check data batch | Compared to running average |
| GPU utilization < 30% for 10 min | Investigate I/O bottleneck | Sustained low util |
| Gradient norm > 100× initial | Reduce LR, add clipping | Compared to first 100 steps |
| Eval loss increasing for 3+ evals | Consider early stopping | No improvement trend |
| OOM error | Reduce batch size, restart | Any CUDA OOM |
