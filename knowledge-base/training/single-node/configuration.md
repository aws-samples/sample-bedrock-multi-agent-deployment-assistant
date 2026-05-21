# Single-Node Training — Configuration Guide

## Instance Launch Configuration

### Deep Learning AMI Selection

| AMI | Framework | CUDA | Use Case |
|-----|-----------|------|----------|
| Deep Learning AMI (Ubuntu 22.04) | PyTorch 2.x, TF 2.x | 12.x | General training |
| Deep Learning Base AMI (Ubuntu 22.04) | None (install your own) | 12.x | Custom frameworks |
| Neuron DL AMI | PyTorch + Neuron SDK | N/A | Trainium/Inferentia |

### EBS Volume Configuration

```yaml
BlockDeviceMappings:
  - DeviceName: /dev/sda1
    Ebs:
      VolumeSize: 200
      VolumeType: gp3
      Iops: 3000
      Throughput: 125
      Encrypted: true
      KmsKeyId: !Ref TrainingKmsKey
  - DeviceName: /dev/sdf
    Ebs:
      VolumeSize: 1000        # Training data + checkpoints
      VolumeType: gp3
      Iops: 6000
      Throughput: 250
      Encrypted: true
      KmsKeyId: !Ref TrainingKmsKey
      DeleteOnTermination: false  # Preserve checkpoints
```

### Instance Store (NVMe) for Scratch

| Instance | NVMe Storage | Use For |
|----------|-------------|---------|
| g5.12xlarge | 3.8 TB | Dataset cache, intermediate activations |
| g5.48xlarge | 7.6 TB | Large datasets, all checkpoints |
| p4d.24xlarge | 8 TB | Maximum I/O performance |
| p5.48xlarge | 30.4 TB | Entire training pipeline on local storage |

## Training Environment Setup

### CUDA and Framework Versions

| Component | Recommended Version | Compatibility Note |
|-----------|--------------------|--------------------|
| CUDA | 12.4+ | Required for H100 support |
| cuDNN | 9.0+ | Flash Attention 2 support |
| NCCL | 2.20+ | Multi-GPU communication |
| PyTorch | 2.3+ | FSDP2, torch.compile |
| Transformers | 4.40+ | Latest model support |
| DeepSpeed | 0.14+ | ZeRO-3 offload improvements |
| Flash Attention | 2.5+ | Sliding window, GQA support |

### Environment Setup Script

```bash
#!/bin/bash
# setup-training-env.sh

# Mount data volume
mkfs.ext4 /dev/sdf
mount /dev/sdf /data
mkdir -p /data/{datasets,checkpoints,logs}

# Format and mount NVMe instance store for scratch
mdadm --create /dev/md0 --level=0 --raid-devices=4 /dev/nvme*n1
mkfs.ext4 /dev/md0
mount /dev/md0 /scratch

# Install training dependencies
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install transformers datasets accelerate deepspeed flash-attn
pip install wandb tensorboard boto3

# Verify GPU access
nvidia-smi
python -c "import torch; print(f'GPUs: {torch.cuda.device_count()}')"
```

## Job Configuration

### Hyperparameter Defaults by Training Type

| Parameter | Fine-Tuning (LoRA) | Full Fine-Tuning | Pre-Training |
|-----------|-------------------|------------------|--------------|
| Learning Rate | 2e-4 | 2e-5 | 1e-4 |
| LR Schedule | cosine | cosine | cosine with warmup |
| Warmup Steps | 100 | 500 | 2000 |
| Weight Decay | 0.01 | 0.01 | 0.1 |
| Batch Size | 4-16 | 2-8 | 32-128 |
| Gradient Accumulation | 4-8 | 8-16 | 1-4 |
| Max Gradient Norm | 1.0 | 1.0 | 1.0 |
| Epochs | 3-5 | 1-3 | 1 |
| FP16/BF16 | BF16 | BF16 | BF16 |

### LoRA Configuration

```json
{
  "lora_r": 16,
  "lora_alpha": 32,
  "lora_dropout": 0.05,
  "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
  "bias": "none",
  "task_type": "CAUSAL_LM"
}
```

### DeepSpeed ZeRO-2 Config (Single Node, Multi-GPU)

```json
{
  "bf16": {"enabled": true},
  "zero_optimization": {
    "stage": 2,
    "offload_optimizer": {"device": "none"},
    "contiguous_gradients": true,
    "overlap_comm": true,
    "reduce_scatter": true
  },
  "gradient_accumulation_steps": 8,
  "gradient_clipping": 1.0,
  "train_batch_size": "auto",
  "train_micro_batch_size_per_gpu": "auto"
}
```

## Monitoring Setup

### Key GPU Metrics

| Metric | Source | Alert Threshold | Action |
|--------|--------|----------------|--------|
| GPU Utilization | nvidia-smi | < 50% sustained | Increase batch size |
| GPU Memory Used | nvidia-smi | > 95% | Enable gradient checkpointing |
| GPU Temperature | nvidia-smi | > 83°C | Check cooling, reduce power limit |
| Training Loss | Application | NaN or spike > 10× | Reduce LR, check data |
| Learning Rate | Application | — | Verify schedule is correct |
| Throughput (samples/sec) | Application | Drop > 30% | Check I/O bottleneck |

### CloudWatch Custom Metrics Publication

```python
import boto3
import time

cloudwatch = boto3.client("cloudwatch")

def publish_training_metrics(epoch, step, loss, lr, throughput, gpu_util):
    cloudwatch.put_metric_data(
        Namespace="NovaMind/Training",
        MetricData=[
            {"MetricName": "TrainingLoss", "Value": loss, "Unit": "None",
             "Dimensions": [{"Name": "JobId", "Value": job_id}]},
            {"MetricName": "LearningRate", "Value": lr, "Unit": "None",
             "Dimensions": [{"Name": "JobId", "Value": job_id}]},
            {"MetricName": "Throughput", "Value": throughput, "Unit": "Count/Second",
             "Dimensions": [{"Name": "JobId", "Value": job_id}]},
            {"MetricName": "GPUUtilization", "Value": gpu_util, "Unit": "Percent",
             "Dimensions": [{"Name": "JobId", "Value": job_id}]},
        ],
    )
```

## Checkpoint Configuration

### Checkpoint Strategy

| Training Duration | Checkpoint Frequency | Keep Last N | Total Storage |
|-------------------|---------------------|-------------|---------------|
| < 6 hours | Every 1 hour | 3 | model_size × 3 |
| 6-24 hours | Every 2 hours | 5 | model_size × 5 |
| 24-72 hours | Every 4 hours | 5 | model_size × 5 |
| > 72 hours | Every 6 hours | 3 + best | model_size × 4 |

### S3 Sync Configuration

```python
# Async checkpoint upload to S3
import subprocess
import threading

def async_checkpoint_upload(local_path, s3_path):
    """Upload checkpoint to S3 in background without blocking training."""
    def _upload():
        subprocess.run([
            "aws", "s3", "sync", local_path, s3_path,
            "--exclude", "*.tmp",
            "--storage-class", "STANDARD_IA",
        ], check=True)
    thread = threading.Thread(target=_upload, daemon=True)
    thread.start()
    return thread
```

### Resume-from-Checkpoint

```python
from transformers import TrainingArguments

training_args = TrainingArguments(
    output_dir="/data/checkpoints",
    resume_from_checkpoint=True,  # Auto-detect latest checkpoint
    save_strategy="steps",
    save_steps=500,
    save_total_limit=5,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
)
```

## Spot Instance Configuration

### Interruption Handling

```bash
#!/bin/bash
# spot-interruption-handler.sh — runs as systemd service

METADATA_URL="http://169.254.169.254/latest/meta-data/spot/instance-action"

while true; do
    response=$(curl -s -o /dev/null -w "%{http_code}" $METADATA_URL)
    if [ "$response" == "200" ]; then
        echo "Spot interruption detected! Saving checkpoint..."
        # Signal training process to save immediately
        kill -USR1 $(cat /tmp/training.pid)
        # Wait for checkpoint save (max 60s)
        sleep 60
        # Upload final checkpoint
        aws s3 sync /data/checkpoints/ s3://$CHECKPOINT_BUCKET/$JOB_ID/
        echo "Checkpoint saved. Shutting down gracefully."
        exit 0
    fi
    sleep 5
done
```

### CloudFormation Spot Configuration

```yaml
SpotOptions:
  SpotInstanceType: persistent
  InstanceInterruptionBehavior: stop  # Preserves EBS on interruption
  MaxPrice: "20.00"  # Cap at ~60% of On-Demand
```

## Security Configuration

### IAM Role for Training Instance

```yaml
TrainingInstanceRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Statement:
        - Effect: Allow
          Principal:
            Service: ec2.amazonaws.com
          Action: sts:AssumeRole
    Policies:
      - PolicyName: TrainingAccess
        PolicyDocument:
          Statement:
            - Effect: Allow
              Action:
                - s3:GetObject
                - s3:ListBucket
              Resource:
                - !Sub "arn:aws:s3:::${DatasetBucket}"
                - !Sub "arn:aws:s3:::${DatasetBucket}/*"
            - Effect: Allow
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource:
                - !Sub "arn:aws:s3:::${CheckpointBucket}/${JobId}/*"
            - Effect: Allow
              Action:
                - cloudwatch:PutMetricData
              Resource: "*"
              Condition:
                StringEquals:
                  cloudwatch:namespace: "NovaMind/Training"
            - Effect: Allow
              Action:
                - logs:CreateLogGroup
                - logs:CreateLogStream
                - logs:PutLogEvents
              Resource: !Sub "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/novamind/training/*"
```

### Security Group

| Direction | Protocol | Port | Source | Purpose |
|-----------|----------|------|--------|---------|
| Inbound | TCP | 22 | Bastion SG | SSH access |
| Inbound | TCP | 6006 | VPN CIDR | TensorBoard |
| Outbound | TCP | 443 | S3 prefix list | Data + checkpoint access |
| Outbound | TCP | 443 | CloudWatch endpoint | Metrics + logs |
