# NovaMind Real-Time Inference — Auto-Scaling Architecture

## Overview

An auto-scaling fleet of NovaMind Inference Servers behind an Application Load Balancer. GPU instances scale based on inference queue depth and latency metrics. Suitable for production workloads with variable traffic patterns.

## Architecture Components

- **Auto Scaling Group (ASG)**: Manages NovaMind fleet size
- **Application Load Balancer**: Request distribution and health checking
- **Launch Template**: Pre-configured GPU instance with model pre-loaded in AMI
- **CloudWatch Alarms**: Scale-out/in triggers based on custom metrics
- **S3 Model Registry**: Source of truth for model artifacts
- **SQS Dead Letter Queue**: Failed inference requests for retry/debugging

## Scaling Strategy

### Scale-Out Triggers
- GPU utilization > 70% for 3 consecutive minutes
- P99 latency > 200ms for 2 consecutive minutes
- Request queue depth > 100 (ALB surge queue)

### Scale-In Triggers
- GPU utilization < 30% for 10 consecutive minutes
- No scale-in during model deployment windows
- Minimum capacity: 2 instances (one per AZ)

### Warm Pool
- Pre-initialized instances in "stopped" state
- Model already loaded on local NVMe — reduces scale-out time from 5min to 30s
- Warm pool size: 2-4 instances (based on peak prediction)

## High Availability

- Instances spread across 2+ Availability Zones
- ALB cross-zone load balancing enabled
- Health checks: HTTP inference endpoint + GPU memory check
- Unhealthy instance replaced within 2 minutes
- Grace period: 120s (model loading time on cold start)

## Traffic Management

- ALB sticky sessions: disabled (stateless inference)
- Connection draining: 30s (allow in-flight requests to complete)
- Request timeout: 60s (prevent stuck GPU jobs from blocking)
- Rate limiting: per-client via WAF rules

## Deployment Strategy

- Rolling update: replace 25% of fleet at a time
- Canary: 10% traffic to new model version for 15 minutes
- Rollback: instant via ASG instance refresh cancellation
