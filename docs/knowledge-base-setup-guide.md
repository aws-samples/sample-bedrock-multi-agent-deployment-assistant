# Knowledge Base Setup Guide

## Overview

The AI Deploy Assistant uses a Knowledge Base (KB) to ground its responses in product-specific documentation. This guide covers both production (AWS Bedrock) and development (local files) KB setup.

---

## Local Knowledge Base (Development)

For local development, place documents in the `knowledge-base/` directory at project root.

### Directory Structure

```
knowledge-base/
  {use_case}/                    # Matches catalog use case values
    {deployment_type}/           # Sub-category of the use case
      {document_type}.md         # Document type (architecture, sizing, etc.)
```

### Document Types

From `catalog.lock.yaml → knowledge_base.document_types`:

| Type | Content | Example |
|------|---------|---------|
| `architecture` | System design, component relationships, traffic flows | How components connect |
| `sizing` | Instance types, throughput, capacity planning | What size to use |
| `configuration` | Setup parameters, runtime settings, options | How to configure |
| `components` | Available features, modules, capabilities | What's available |
| `best-practices` | Operational guidance, security, compliance | How to do it right |

### Example (NovaMind Inference Server)

```
knowledge-base/
  realtime-inference/
    single-endpoint/
      architecture.md      # Single instance behind ALB
      sizing.md            # GPU instance type selection guide
      configuration.md     # Serving runtime options (TorchServe, Triton, etc.)
    auto-scaling/
      architecture.md      # ASG + ALB + CloudWatch scaling
  batch-inference/
    single-gpu/
      architecture.md      # Step Functions + Spot instances
      sizing.md            # Cost/time tradeoffs by dataset size
      configuration.md     # Job submission format, checkpointing
  training/
    distributed/
      architecture.md      # Multi-node EFA + FSx Lustre
    single-node/
      architecture.md      # Single multi-GPU training
```

### How Local Search Works

The `LocalKBProvider`:
1. Scans all `.md`, `.txt`, `.yaml`, `.json` files in the directory
2. Extracts metadata from path (`use_case/deployment_type/document_type.ext`)
3. Tokenizes document content (lowercase, alphanumeric tokens)
4. On search: scores documents using TF-IDF against query tokens
5. Applies path-based metadata filtering (same semantics as Bedrock filters)

---

## AWS Bedrock Knowledge Base (Production)

### S3 Bucket Structure

Upload documents to S3 with the same path convention:

```
s3://ai-deploy-knowledge-base/
  {use_case}/
    {deployment_type}/
      {document_type}.{ext}
```

Supported formats: PDF, Markdown, TXT, HTML, DOCX.

### Metadata Configuration

Bedrock KB supports metadata filtering. Configure metadata extraction from the S3 path:

| Metadata Key | Source | Example |
|-------------|--------|---------|
| `use_case` | First path segment | `realtime-inference` |
| `deployment_type` | Second path segment | `auto-scaling` |
| `document_type` | Filename stem | `architecture` |

### Bedrock KB Setup Steps

1. **Create S3 bucket**: `ai-deploy-knowledge-base` (or your custom name)
2. **Upload documents**: Follow the path structure above
3. **Create Bedrock KB**:
   - Data source: S3 bucket
   - Embedding model: Titan Embeddings V2
   - Vector store: OpenSearch Serverless (or Pinecone/Aurora)
4. **Configure metadata**: Set up metadata extraction from S3 paths
5. **Sync**: Run initial data source sync
6. **Set env var**: `AI_DEPLOY_KNOWLEDGE_BASE_ID=<your-kb-id>`

### Chunking Strategy

Recommended settings:
- Chunk size: 500 tokens
- Overlap: 50 tokens
- Strategy: Fixed-size chunking (simpler, works well for structured docs)

---

## KB Search Patterns

The system uses two search modes:

### Flat Search (Design Agent)

Used by the design agent via the `kb_search` tool:
```
query: "NovaMind Inference Server realtime-inference deployment AWS"
max_results: 5
```

### Filtered Search (Interview Planner)

Used by the interview planner for targeted field-specific queries:
```
query: "auto scaling fleet architecture"
use_case: "realtime-inference"
document_type: ["architecture", "components"]
max_results: 5
```

The search template is configurable in `catalog.lock.yaml`:
```yaml
knowledge_base:
  search_template: "{product_name} {use_case} deployment AWS"
```

---

## Verifying KB Setup

### Local verification

```bash
cd backend
AI_DEPLOY_KNOWLEDGE_BASE_ID="" uv run python -c "
from src.services.kb_provider import get_kb_provider, reset_kb_provider
reset_kb_provider()
provider = get_kb_provider()
print(f'Provider: {type(provider).__name__}')
print(f'Available: {provider.is_available}')
results = provider.search('inference scaling GPU', max_results=3)
for r in results:
    print(f'  [{r.score:.3f}] {r.source_uri}')
"
```

### Bedrock verification

```bash
cd backend
AI_DEPLOY_KNOWLEDGE_BASE_ID=YOUR_KB_ID uv run python -c "
from src.services.kb_provider import get_kb_provider, reset_kb_provider
reset_kb_provider()
provider = get_kb_provider()
results = provider.search('deployment architecture', max_results=3)
for r in results:
    print(f'  [{r.score:.3f}] {r.source_uri}')
"
```
