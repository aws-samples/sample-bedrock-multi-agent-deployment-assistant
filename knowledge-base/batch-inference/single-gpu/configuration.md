# NovaMind Batch Inference Configuration

## Job Submission Format

```json
{
  "job_id": "batch-20260430-001",
  "model": "s3://models/sentiment-v2/",
  "input": "s3://data/input/reviews-2026-04.parquet",
  "output": "s3://data/output/predictions-2026-04/",
  "parameters": {
    "batch_size": 16,
    "quantization": "int8",
    "max_concurrent_batches": 4
  },
  "priority": "standard",
  "spot_enabled": true,
  "checkpoint_interval": 1000
}
```

## Checkpointing Configuration

Checkpointing is critical for Spot Instance workloads. NovaMind saves progress to S3 at configurable intervals.

### Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| checkpoint_interval | 1000 | Save progress every N samples |
| checkpoint_path | s3://{output}/checkpoints/ | S3 path for checkpoint files |
| resume_from_checkpoint | true | Auto-resume on Spot interruption |
| max_checkpoint_age_hours | 48 | Discard stale checkpoints |

### Checkpoint Contents
- Last processed offset in input dataset
- Model state (if fine-tuning during batch)
- Accumulated metrics (loss, accuracy)
- Partial output buffer (unflushed predictions)

## Spot Instance Strategy

### Interruption Handling
1. EC2 sends 2-minute warning via instance metadata
2. NovaMind catches SIGTERM signal
3. Current batch completes (timeout: 30s)
4. Emergency checkpoint written to S3
5. Instance terminates gracefully

### Fallback Strategy
```yaml
spot_config:
  allocation_strategy: capacity-optimized
  instance_types:
    - g5.xlarge      # Primary choice
    - g5.2xlarge     # Fallback (more availability)
    - g4dn.xlarge    # Budget fallback (older GPU)
  on_demand_fallback: true  # Use on-demand if no Spot after 5 min
  max_spot_price: "auto"    # Up to on-demand price
```

## Input/Output Formats

### Supported Input Formats
- Parquet (recommended — columnar, efficient)
- JSON Lines (one JSON object per line)
- CSV (with header row)
- TFRecord (TensorFlow native)

### Output Format
- JSON Lines: `{"index": 0, "prediction": [...], "confidence": 0.95}`
- One prediction per line, matching input order
- Failed predictions: `{"index": 5, "error": "input_too_large"}`
