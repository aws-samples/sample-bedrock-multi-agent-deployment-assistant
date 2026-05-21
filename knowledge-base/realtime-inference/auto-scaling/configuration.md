# Auto-Scaling Inference Fleet — Configuration Guide

## Auto Scaling Group Configuration

### Core ASG Parameters

| Parameter | Recommended Value | Description |
|-----------|------------------|-------------|
| MinSize | 2 | Minimum for HA (multi-AZ) |
| MaxSize | 20 | Budget cap (adjust per workload) |
| DesiredCapacity | 2-4 | Starting fleet size |
| HealthCheckType | ELB | ALB-based health checks |
| HealthCheckGracePeriod | 600 | Model loading time (seconds) |
| DefaultCooldown | 300 | Prevent rapid scaling oscillation |
| NewInstancesProtectedFromScaleIn | true | Protect warming instances |

### Launch Template Configuration

```yaml
LaunchTemplate:
  InstanceType: g5.12xlarge
  ImageId: ami-deep-learning-gpu  # Deep Learning AMI (Ubuntu)
  BlockDeviceMappings:
    - DeviceName: /dev/sda1
      Ebs:
        VolumeSize: 500
        VolumeType: gp3
        Iops: 6000
        Throughput: 250
        Encrypted: true
  NetworkInterfaces:
    - DeviceIndex: 0
      SubnetId: !Ref PrivateSubnet
      Groups:
        - !Ref InferenceSecurityGroup
  UserData:
    # Model download + server startup script
    Fn::Base64: !Sub |
      #!/bin/bash
      aws s3 cp s3://${ModelBucket}/models/${ModelName}/ /opt/model/ --recursive
      cd /opt/inference-server
      ./start-server.sh --model-path /opt/model --port 8080
```

## Scaling Policies

### Target Tracking — GPU Utilization

```yaml
GPUScalingPolicy:
  Type: AWS::AutoScaling::ScalingPolicy
  Properties:
    AutoScalingGroupName: !Ref ASG
    PolicyType: TargetTrackingScaling
    TargetTrackingConfiguration:
      CustomizedMetricSpecification:
        MetricName: GPUUtilization
        Namespace: NovaMind/Inference
        Statistic: Average
        Unit: Percent
      TargetValue: 70
      ScaleInCooldown: 300
      ScaleOutCooldown: 60
```

### Target Tracking — Request Latency

```yaml
LatencyScalingPolicy:
  Type: AWS::AutoScaling::ScalingPolicy
  Properties:
    AutoScalingGroupName: !Ref ASG
    PolicyType: TargetTrackingScaling
    TargetTrackingConfiguration:
      PredefinedMetricSpecification:
        PredefinedMetricType: ALBRequestCountPerTarget
        ResourceLabel: !Sub "${ALB.FullName}/${TargetGroup.FullName}"
      TargetValue: 50  # requests per target per minute
      ScaleInCooldown: 300
      ScaleOutCooldown: 60
```

### Step Scaling — Queue Depth (Burst Response)

```yaml
QueueDepthScaleOut:
  Type: AWS::AutoScaling::ScalingPolicy
  Properties:
    PolicyType: StepScaling
    AdjustmentType: ChangeInCapacity
    StepAdjustments:
      - MetricIntervalLowerBound: 0
        MetricIntervalUpperBound: 100
        ScalingAdjustment: 2
      - MetricIntervalLowerBound: 100
        MetricIntervalUpperBound: 500
        ScalingAdjustment: 5
      - MetricIntervalLowerBound: 500
        ScalingAdjustment: 10
```

### Scheduled Scaling (Predictable Patterns)

```yaml
BusinessHoursScaleUp:
  Type: AWS::AutoScaling::ScheduledAction
  Properties:
    AutoScalingGroupName: !Ref ASG
    MinSize: 4
    DesiredCapacity: 6
    Recurrence: "0 8 * * MON-FRI"  # 8 AM weekdays

NightScaleDown:
  Type: AWS::AutoScaling::ScheduledAction
  Properties:
    AutoScalingGroupName: !Ref ASG
    MinSize: 2
    DesiredCapacity: 2
    Recurrence: "0 22 * * *"  # 10 PM daily
```

## ALB Configuration

### Target Group Settings

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Protocol | HTTP | TLS terminates at ALB |
| Port | 8080 | Inference server port |
| HealthCheckPath | /health | Lightweight GPU-aware check |
| HealthCheckInterval | 30s | Balance detection speed vs load |
| HealthyThreshold | 2 | Fast registration after model loads |
| UnhealthyThreshold | 3 | Tolerate transient GPU hiccups |
| DeregistrationDelay | 120s | Drain in-flight requests before removal |
| SlowStart | 300s | Gradual traffic ramp after registration |

### Health Check Implementation

The `/health` endpoint should verify:
1. HTTP server is responding (basic)
2. GPU is accessible (nvidia-smi check)
3. Model is loaded into GPU memory
4. Inference warmup request completes in < 5s

### Stickiness Configuration

| Workload | Stickiness | Duration | Rationale |
|----------|-----------|----------|-----------|
| Stateless inference | Disabled | — | Even distribution |
| Session-based (chat) | Enabled | 3600s | KV cache per connection |
| Batch upload + query | Enabled | 300s | Data locality |

## Warm Pool Configuration

```yaml
WarmPool:
  Type: AWS::AutoScaling::WarmPool
  Properties:
    AutoScalingGroupName: !Ref ASG
    PoolState: Stopped  # or Running for fastest recovery
    MinSize: 1
    MaxGroupPreparedCapacity: 3
    InstanceReusePolicy:
      ReuseOnScaleIn: true
```

### Lifecycle Hooks

| Hook | Timeout | Action |
|------|---------|--------|
| Launch (warm → InService) | 600s | Download model, load to GPU, run warmup |
| Terminate (InService → terminate) | 120s | Drain requests, upload metrics, cleanup |

```yaml
ModelLoadHook:
  Type: AWS::AutoScaling::LifecycleHook
  Properties:
    AutoScalingGroupName: !Ref ASG
    LifecycleTransition: autoscaling:EC2_INSTANCE_LAUNCHING
    HeartbeatTimeout: 600
    DefaultResult: ABANDON  # Kill instance if model load fails
```

## CloudWatch Alarms

### Scale-In Protection Composite Alarm

```yaml
ScaleInProtection:
  Type: AWS::CloudWatch::CompositeAlarm
  Properties:
    AlarmRule: |
      ALARM(HighGPUUtilization) OR
      ALARM(HighRequestRate) OR
      ALARM(HighLatency)
    ActionsEnabled: true
    # When active, prevents ASG from scaling in
```

### Key Monitoring Alarms

| Alarm | Metric | Threshold | Action |
|-------|--------|-----------|--------|
| HighGPU | GPUUtilization > 85% for 5 min | Scale out | Add 2 instances |
| LowGPU | GPUUtilization < 20% for 15 min | Scale in | Remove 1 instance |
| HighLatency | P99 > target × 2 for 3 min | Scale out | Add 3 instances |
| ErrorRate | 5xx > 5% for 2 min | Alert + scale out | Page on-call |
| QueueDepth | Pending > 200 for 1 min | Scale out | Add 5 instances |
| OOMKills | GPUOOMCount > 0 | Alert | Switch to larger instance |

## Security Group Rules

### Inference Security Group

| Direction | Protocol | Port | Source | Purpose |
|-----------|----------|------|--------|---------|
| Inbound | TCP | 8080 | ALB SG | Inference requests |
| Inbound | TCP | 9090 | Monitoring SG | Prometheus metrics |
| Inbound | TCP | 22 | Bastion SG | SSH (emergency debug) |
| Outbound | TCP | 443 | 0.0.0.0/0 | S3 model download, CloudWatch |
| Outbound | TCP | 2049 | EFS SG | Shared model storage (optional) |

## Deployment Strategy

### Rolling Update Configuration

```yaml
UpdatePolicy:
  AutoScalingRollingUpdate:
    MinInstancesInService: 2
    MaxBatchSize: 2
    PauseTime: PT10M
    SuspendProcesses:
      - HealthCheck
      - ReplaceUnhealthy
      - AZRebalance
    WaitOnResourceSignals: true
```

### Canary Deployment (Weight-Based)

| Phase | New Version % | Duration | Rollback Trigger |
|-------|--------------|----------|-----------------|
| 1 | 5% | 15 minutes | Error rate > 1% or P99 > 2× baseline |
| 2 | 25% | 30 minutes | Error rate > 0.5% or P99 > 1.5× baseline |
| 3 | 50% | 30 minutes | Error rate > 0.5% |
| 4 | 100% | — | Manual verification complete |
