# NovaMind Real-Time Inference — Single Endpoint Architecture

## Overview

A single NovaMind Inference Server instance deployed behind an Application Load Balancer, serving real-time ML predictions with low-latency requirements. Suitable for development, staging, or low-traffic production workloads.

## Architecture Components

- **NovaMind Inference Server**: GPU-accelerated EC2 instance running the model serving runtime
- **Application Load Balancer (ALB)**: HTTPS termination and health checking
- **S3 Model Registry**: Versioned model artifacts stored in S3
- **CloudWatch**: Inference latency metrics, GPU utilization alarms
- **ECR**: Container images for the inference runtime

## Deployment Layout

### VPC Design
- Public subnet: ALB endpoints (internet-facing)
- Private subnet: NovaMind instance with GPU (no direct internet access)
- NAT Gateway: Outbound access for model downloads and telemetry

### Instance Configuration
- **GPU interface**: Primary ENI in private subnet — handles inference traffic
- **Management interface**: Optional secondary ENI for SSH/monitoring (isolated from inference path)

## Inference Flow

1. Client sends HTTPS request to ALB
2. ALB routes to healthy NovaMind instance (target group health check)
3. Instance loads model from local cache (pre-warmed) or pulls from S3
4. GPU executes inference → returns prediction
5. Response returned through ALB to client

## Model Management

- Models stored in S3 with versioned prefixes: `s3://models/{model_name}/v{version}/`
- Instance pulls model on startup and caches locally on NVMe SSD
- Hot-swap: new model version deployed via rolling update (no downtime)
- Canary deployment: ALB weighted target groups for A/B model testing

## Monitoring

- P99 inference latency (target: <100ms for most models)
- GPU memory utilization (alert at >85%)
- Request queue depth (indicates need to scale)
- Model load time (cold start metric)
