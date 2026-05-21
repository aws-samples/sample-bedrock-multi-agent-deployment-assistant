# Real-Time Inference (Single Endpoint) — Components Reference

## AWS Infrastructure Components

| Component | Purpose | Key Configuration | Cost Factor |
|-----------|---------|-------------------|-------------|
| EC2 GPU Instance (g5/p4d/p5) | Hosts ML model and inference server | Instance type, AMI, EBS storage | Primary cost driver (60-80%) |
| Application Load Balancer | Routes inference requests, health checks, TLS termination | Target group, health check path, idle timeout | Low fixed cost + per-request |
| VPC + Subnets | Network isolation, private placement | CIDR, private/public subnets, NAT gateway | NAT gateway hourly + data |
| Security Groups | Firewall rules for inference traffic | Inbound port 8080, outbound S3/CloudWatch | Free |
| EBS (gp3/io2) | Boot volume + model storage | Volume size, IOPS, throughput | Per-GB + IOPS provisioned |
| S3 | Model artifact storage, input/output data | Bucket policies, versioning, lifecycle | Per-GB stored + requests |
| CloudWatch | Metrics, logs, GPU monitoring | Custom metrics namespace, log retention | Per-metric + log ingestion |
| IAM Role | Least-privilege access for instance | S3 read, CloudWatch write, KMS decrypt | Free |
| KMS | Encryption for EBS, S3, logs | Key policy, automatic rotation | Per-key + per-request |
| SSM Parameter Store | Configuration management (model version, thresholds) | SecureString for secrets, versioning | Free tier (standard) |
| ACM Certificate | TLS for ALB HTTPS listener | DNS validation, auto-renewal | Free |

## Serving Runtimes

| Runtime | Framework Support | Key Features | Best For |
|---------|------------------|--------------|----------|
| TorchServe | PyTorch | Model versioning, batching, metrics, MAR packaging | General PyTorch models |
| Triton Inference Server | PyTorch, TensorFlow, ONNX, TensorRT | Multi-model, dynamic batching, ensemble pipelines | Multi-framework, high throughput |
| vLLM | PyTorch (LLMs) | PagedAttention, continuous batching, tensor parallelism | LLM text generation |
| TensorRT-LLM | TensorRT (LLMs) | Quantization, inflight batching, KV cache optimization | Maximum LLM performance |
| ONNX Runtime | ONNX | Cross-platform, graph optimization, quantization | Framework-agnostic deployment |

## Component Interactions

```
Client → ALB (TLS termination, routing)
           → EC2 GPU Instance
               → Inference Server (TorchServe/Triton/vLLM)
                   → GPU (model loaded in VRAM)
                   → EBS (model artifacts on disk)
               → CloudWatch Agent (GPU metrics)
           → CloudWatch (dashboards, alarms)
           → S3 (model download on startup)
```

## Monitoring Stack

| Component | Metrics Collected | Alert Integration |
|-----------|------------------|-------------------|
| CloudWatch Agent | GPU utilization, memory, temperature, power | CloudWatch Alarms → SNS |
| NVIDIA DCGM | Per-GPU profiling, NVLink utilization | Custom metrics → CloudWatch |
| Application Metrics | Request latency, throughput, queue depth, batch size | Custom namespace |
| ALB Metrics | RequestCount, TargetResponseTime, HTTPCode_Target_5XX | Built-in CloudWatch |
| EBS Metrics | VolumeReadOps, VolumeThroughput, BurstBalance | Built-in CloudWatch |

## Data Flow

| Stage | Source | Destination | Protocol | Encryption |
|-------|--------|-------------|----------|------------|
| Model download | S3 | EC2 (EBS) | HTTPS | SSE-KMS + TLS |
| Inference request | Client | ALB | HTTPS | TLS 1.3 |
| ALB to instance | ALB | EC2:8080 | HTTP | VPC internal |
| Metrics publish | EC2 | CloudWatch | HTTPS | TLS + SigV4 |
| Logs | EC2 | CloudWatch Logs | HTTPS | TLS + KMS |
| Health check | ALB | EC2:/health | HTTP | VPC internal |
