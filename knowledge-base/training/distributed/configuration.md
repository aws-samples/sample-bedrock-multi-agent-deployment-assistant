# Distributed Training — Configuration Guide

## Cluster Configuration

### Placement Group Setup

```yaml
TrainingPlacementGroup:
  Type: AWS::EC2::PlacementGroup
  Properties:
    GroupName: novamind-training-cluster
    Strategy: cluster  # Required for EFA — all instances in same rack/spine
    SpreadLevel: rack   # Maximum network locality
```

### EFA Network Interface Configuration

```yaml
NetworkInterfaces:
  - DeviceIndex: 0
    SubnetId: !Ref PrivateSubnet
    Groups:
      - !Ref TrainingClusterSG
    InterfaceType: efa  # Enables Elastic Fabric Adapter
    Description: "EFA interface for distributed training"
```

### Multi-Interface Configuration (p5.48xlarge)

| Interface | Type | Purpose |
|-----------|------|---------|
| eth0 | Standard ENI | Management, SSH, S3 access |
| efa0-efa31 | EFA | NCCL all-reduce, gradient sync |

## FSx Lustre Setup

### Capacity and Throughput Planning

| Workload | Capacity | Throughput | Deployment Type |
|----------|----------|-----------|-----------------|
| Small dataset (<100 GB) | 1.2 TB | PERSISTENT_1 (200 MB/s/TiB) | PERSISTENT_1 |
| Medium dataset (100 GB-1 TB) | 2.4 TB | PERSISTENT_2 (1000 MB/s/TiB) | PERSISTENT_2 |
| Large dataset (1-10 TB) | 12 TB | PERSISTENT_2 (1000 MB/s/TiB) | PERSISTENT_2 |
| Very large (>10 TB) | 24+ TB | PERSISTENT_2 (1000 MB/s/TiB) | PERSISTENT_2 |

### CloudFormation FSx Configuration

```yaml
TrainingFileSystem:
  Type: AWS::FSx::FileSystem
  Properties:
    FileSystemType: LUSTRE
    StorageCapacity: 2400  # GB (must be multiple of 1200)
    SubnetIds:
      - !Ref PrivateSubnet
    SecurityGroupIds:
      - !Ref FSxSecurityGroup
    LustreConfiguration:
      DeploymentType: PERSISTENT_2
      PerUnitStorageThroughput: 1000  # MB/s/TiB
      DataCompressionType: LZ4
      AutoImportPolicy: NEW_CHANGED_DELETED
      ImportPath: !Sub "s3://${DatasetBucket}"
      ExportPath: !Sub "s3://${DatasetBucket}/export"
    Tags:
      - Key: Purpose
        Value: distributed-training
```

### Mount Configuration (per training node)

```bash
#!/bin/bash
# mount-fsx.sh — run on each training node

FSX_DNS="fs-0123456789.fsx.us-east-1.amazonaws.com"
FSX_MOUNT="/fsx"

# Install Lustre client
amazon-linux-extras install -y lustre
# OR for Ubuntu:
# apt-get install -y lustre-client-modules-$(uname -r) lustre-utils

mkdir -p $FSX_MOUNT
mount -t lustre -o noatime,flock ${FSX_DNS}@tcp:/${MOUNT_NAME} $FSX_MOUNT

# Verify mount and performance
lfs df -h $FSX_MOUNT
dd if=/dev/zero of=$FSX_MOUNT/benchmark bs=1M count=1024 oflag=direct
```

## MPI/NCCL Configuration

### NCCL Environment Variables

```bash
# Optimal NCCL configuration for EFA clusters
export NCCL_DEBUG=WARN
export NCCL_PROTO=simple           # Best for EFA
export NCCL_ALGO=Ring              # Ring topology for large messages
export NCCL_TREE_THRESHOLD=0       # Use tree for small messages
export NCCL_IB_DISABLE=1           # Disable InfiniBand (use EFA)
export NCCL_SOCKET_IFNAME=eth0     # Control plane interface
export NCCL_NET_GDR_LEVEL=PHB      # GPU Direct RDMA level
export FI_PROVIDER=efa             # Libfabric provider
export FI_EFA_USE_DEVICE_RDMA=1    # Enable GPU-direct with EFA
export FI_EFA_FORK_SAFE=1          # Safe forking with EFA

# Performance tuning
export NCCL_BUFFSIZE=8388608       # 8MB buffer for large models
export NCCL_P2P_LEVEL=NVL          # NVLink for intra-node
export NCCL_CROSS_NIC=0            # Use single NIC per GPU
```

### Multi-Node Launch (torchrun)

```bash
#!/bin/bash
# launch-distributed.sh — run from head node

NNODES=4
NPROC_PER_NODE=8  # GPUs per node
MASTER_ADDR=$(hostname -I | awk '{print $1}')
MASTER_PORT=29500

# Generate hostfile
cat > /tmp/hostfile <<EOF
node-0:8
node-1:8
node-2:8
node-3:8
EOF

# Launch on all nodes via SSH
for i in $(seq 0 $((NNODES-1))); do
    ssh node-$i "
        cd /fsx/training &&
        torchrun \
            --nnodes=$NNODES \
            --nproc_per_node=$NPROC_PER_NODE \
            --node_rank=$i \
            --master_addr=$MASTER_ADDR \
            --master_port=$MASTER_PORT \
            train.py \
            --config /fsx/configs/training_config.yaml
    " &
done
wait
```

## Distributed Training Framework Configuration

### PyTorch DDP (Data Distributed Parallel)

```python
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def setup_ddp():
    dist.init_process_group(
        backend="nccl",
        init_method="env://",
    )
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    return local_rank

def train():
    local_rank = setup_ddp()
    model = MyModel().to(local_rank)
    model = DDP(model, device_ids=[local_rank])
    # Training loop...
```

### DeepSpeed ZeRO-3 Configuration

```json
{
  "bf16": {"enabled": true},
  "zero_optimization": {
    "stage": 3,
    "offload_optimizer": {"device": "none"},
    "offload_param": {"device": "none"},
    "overlap_comm": true,
    "contiguous_gradients": true,
    "reduce_bucket_size": 5e8,
    "stage3_prefetch_bucket_size": 5e8,
    "stage3_param_persistence_threshold": 1e6,
    "stage3_gather_16bit_weights_on_model_save": true
  },
  "gradient_accumulation_steps": 4,
  "gradient_clipping": 1.0,
  "steps_per_print": 100,
  "communication_data_type": "bf16",
  "comms_logger": {"enabled": true, "verbose": false}
}
```

### FSDP Configuration (PyTorch 2.x)

```python
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    ShardingStrategy,
)
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy

fsdp_config = {
    "sharding_strategy": ShardingStrategy.FULL_SHARD,
    "mixed_precision": MixedPrecision(
        param_dtype=torch.bfloat16,
        reduce_dtype=torch.bfloat16,
        buffer_dtype=torch.bfloat16,
    ),
    "auto_wrap_policy": transformer_auto_wrap_policy,
    "backward_prefetch": BackwardPrefetch.BACKWARD_PRE,
    "forward_prefetch": True,
    "use_orig_params": True,  # Required for torch.compile
    "limit_all_gathers": True,  # Memory-efficient
}

model = FSDP(model, **fsdp_config)
```

## Fault Tolerance

### Node Health Monitoring

```python
import torch.distributed as dist
import signal
import sys

class HealthMonitor:
    def __init__(self, checkpoint_dir):
        self.checkpoint_dir = checkpoint_dir
        signal.signal(signal.SIGUSR1, self._handle_checkpoint_signal)
        signal.signal(signal.SIGTERM, self._handle_termination)

    def _handle_checkpoint_signal(self, signum, frame):
        """Emergency checkpoint on Spot interruption signal."""
        self.save_checkpoint(emergency=True)

    def _handle_termination(self, signum, frame):
        """Graceful shutdown — save and cleanup."""
        self.save_checkpoint(emergency=True)
        dist.destroy_process_group()
        sys.exit(0)

    def check_peer_health(self):
        """Verify all nodes are still responsive."""
        try:
            dist.barrier(timeout=timedelta(seconds=60))
            return True
        except Exception:
            return False
```

### Automatic Resume After Node Failure

```bash
#!/bin/bash
# auto-resume.sh — orchestrator script

MAX_RETRIES=5
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    echo "Training attempt $((RETRY_COUNT+1))/$MAX_RETRIES"

    # Check all nodes are healthy
    for node in "${NODES[@]}"; do
        ssh -o ConnectTimeout=10 $node "nvidia-smi" || {
            echo "Node $node unhealthy, replacing..."
            replace_node $node
            sleep 120  # Wait for replacement
        }
    done

    # Launch training with checkpoint resume
    ./launch-distributed.sh --resume-from-checkpoint
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "Training completed successfully"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT+1))
    echo "Training failed (exit $EXIT_CODE), retrying in 60s..."
    sleep 60
done
```

## Security Group Rules

### Training Cluster Security Group

| Direction | Protocol | Port Range | Source/Dest | Purpose |
|-----------|----------|-----------|-------------|---------|
| Inbound | TCP | 29500-29600 | Self (SG) | PyTorch distributed (NCCL) |
| Inbound | TCP | 0-65535 | Self (SG) | EFA traffic (all ports) |
| Inbound | UDP | 0-65535 | Self (SG) | EFA traffic (all ports) |
| Inbound | TCP | 22 | Bastion SG | SSH management |
| Inbound | TCP | 6006 | VPN CIDR | TensorBoard |
| Outbound | TCP | 443 | S3 prefix list | Dataset + checkpoint access |
| Outbound | TCP | 443 | CloudWatch | Metrics publication |
| Outbound | TCP | 988 | FSx SG | Lustre client traffic |
| Outbound | TCP | 1021-1023 | FSx SG | Lustre client traffic |

### FSx Security Group

| Direction | Protocol | Port Range | Source | Purpose |
|-----------|----------|-----------|--------|---------|
| Inbound | TCP | 988 | Training Cluster SG | Lustre server |
| Inbound | TCP | 1021-1023 | Training Cluster SG | Lustre server |

## Orchestration with Step Functions

```json
{
  "StartAt": "ProvisionCluster",
  "States": {
    "ProvisionCluster": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:...:provision-training-cluster",
      "Next": "WaitForNodes",
      "Retry": [{"ErrorEquals": ["InsufficientCapacity"], "MaxAttempts": 3, "BackoffRate": 2}]
    },
    "WaitForNodes": {
      "Type": "Wait",
      "Seconds": 300,
      "Next": "LaunchTraining"
    },
    "LaunchTraining": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:...:launch-distributed-training",
      "TimeoutSeconds": 604800,
      "Next": "UploadArtifacts",
      "Catch": [{"ErrorEquals": ["NodeFailure"], "Next": "HandleFailure"}]
    },
    "HandleFailure": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:...:replace-failed-node",
      "Next": "LaunchTraining"
    },
    "UploadArtifacts": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:...:upload-final-model",
      "Next": "Cleanup"
    },
    "Cleanup": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:...:terminate-cluster",
      "End": true
    }
  }
}
```
