# NovaMind Batch Inference — Single GPU Architecture

## Overview

Batch inference processes large datasets offline using a single NovaMind Inference Server instance. Optimized for throughput over latency — processes thousands of predictions per job with efficient GPU batching.

## Architecture Components

- **NovaMind Inference Server**: GPU instance optimized for batch throughput
- **S3 Input/Output**: Source datasets and prediction results stored in S3
- **Step Functions**: Orchestrates batch job lifecycle
- **SQS Job Queue**: Manages batch job submission and priority
- **DynamoDB**: Job status tracking and metadata

## Batch Processing Flow

1. User submits batch job (S3 input path + model version + parameters)
2. Step Functions launches NovaMind instance from AMI
3. Instance pulls input data from S3 in streaming chunks
4. GPU processes predictions in optimal batch sizes (auto-tuned)
5. Results written incrementally to S3 output path
6. Instance terminates on completion (cost optimization)

## Instance Configuration

- **Spot Instances**: Recommended for batch (interruptible with checkpointing)
- **Local NVMe**: Used as scratch space for input data staging
- **EBS**: Not needed — all persistent data in S3
- **No ALB**: Direct S3 I/O, no network serving

## Job Management

### Priority Levels
| Priority | SLA | Instance Strategy |
|----------|-----|-------------------|
| Critical | <1 hour | On-Demand, immediate launch |
| Standard | <4 hours | Spot with On-Demand fallback |
| Economy | <24 hours | Spot only, flexible start time |

### Checkpointing
- Progress saved to S3 every 1000 predictions
- On Spot interruption: job resumes from last checkpoint
- Checkpoint metadata in DynamoDB (job_id, last_offset, timestamp)

## Optimization

- Dynamic batching: auto-tunes batch size based on available GPU memory
- Input pre-processing: parallel CPU threads prepare next batch while GPU processes current
- Output buffering: aggregate predictions before S3 write (reduce API calls)
- Model quantization: INT8 quantization for 2x throughput with <1% accuracy loss
