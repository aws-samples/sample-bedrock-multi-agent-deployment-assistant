# NovaMind Real-Time Inference Configuration

## Serving Runtime Options

### PyTorch Serve (TorchServe)
- Native PyTorch model support
- Dynamic batching with configurable max batch size and timeout
- Multi-model serving on single instance
- Custom handlers for pre/post processing
- Best for: PyTorch models, rapid prototyping

### TensorRT
- NVIDIA's high-performance inference optimizer
- INT8/FP16 quantization with calibration
- Kernel auto-tuning for specific GPU architecture
- 2-5x speedup over native PyTorch
- Best for: Production latency-critical workloads

### ONNX Runtime
- Framework-agnostic model execution
- Supports models from PyTorch, TensorFlow, scikit-learn
- GPU acceleration via CUDA and TensorRT execution providers
- Best for: Multi-framework environments

### Triton Inference Server
- Multi-framework, multi-model serving
- Dynamic batching across concurrent requests
- Model ensemble pipelines (preprocessing → inference → postprocessing)
- Prometheus metrics built-in
- Best for: Production multi-model deployments

## Quantization Options

| Level | Precision | Memory Reduction | Latency Improvement | Accuracy Impact |
|-------|-----------|-----------------|--------------------|-----------------| 
| None | FP32 | Baseline | Baseline | None |
| FP16 | Half precision | 50% | 30-50% faster | Negligible (<0.1%) |
| INT8 | 8-bit integer | 75% | 2-3x faster | Minor (0.1-1%) |
| INT4 | 4-bit integer | 87.5% | 3-4x faster | Moderate (1-3%) |

### Quantization Recommendations
- **Development/testing**: FP32 (no quantization) — maximum accuracy
- **Production (latency-sensitive)**: FP16 — best accuracy/performance tradeoff
- **Production (cost-sensitive)**: INT8 — significant savings with calibration
- **Edge/constrained**: INT4 — maximum compression, requires quality validation

## Health Check Configuration

```yaml
health_check:
  path: /v1/health
  interval: 10s
  timeout: 5s
  healthy_threshold: 2
  unhealthy_threshold: 3
  
  # GPU-aware health check
  gpu_memory_threshold: 90%  # Unhealthy if GPU memory > 90%
  inference_timeout: 30s     # Unhealthy if single inference > 30s
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| NOVAMIND_MODEL_PATH | /models/default | Local path to model artifacts |
| NOVAMIND_BATCH_SIZE | 1 | Default inference batch size |
| NOVAMIND_MAX_BATCH_SIZE | 32 | Maximum dynamic batch size |
| NOVAMIND_BATCH_TIMEOUT_MS | 50 | Wait time for batch accumulation |
| NOVAMIND_NUM_WORKERS | 4 | Data preprocessing workers |
| NOVAMIND_GPU_MEMORY_FRACTION | 0.9 | Max GPU memory allocation |
