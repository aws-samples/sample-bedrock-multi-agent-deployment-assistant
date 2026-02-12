# Bedrock Knowledge Base — Structure, Metadata & Ingestion Guide

## Date: 2026-02-24

---

## 1. S3 Bucket Layout

The knowledge base bucket (`AI_LCM_S3_KNOWLEDGE_BASE_BUCKET`) should follow this structure:

```
s3://ai-lcm-knowledge-base/
│
├── sd-wan/
│   ├── hub-spoke/
│   │   ├── architecture.md
│   │   ├── architecture.md.metadata.json       ← metadata sidecar
│   │   ├── configuration.md
│   │   ├── configuration.md.metadata.json
│   │   ├── components.md
│   │   ├── components.md.metadata.json
│   │   └── sizing.md
│   │       sizing.md.metadata.json
│   │
│   ├── dual-hub/
│   │   ├── architecture.md
│   │   ├── architecture.md.metadata.json
│   │   ├── configuration.md
│   │   ├── configuration.md.metadata.json
│   │   └── ...
│   │
│   └── spoke-only/
│       └── ...
│
├── egress/
│   ├── single-az/
│   │   └── ...
│   └── multi-az/
│       └── ...
│
├── ingress/
│   ├── nlb-sandwich/
│   │   └── ...
│   └── gwlb/
│       └── ...
│
└── inspection/
    ├── centralized/
    │   └── ...
    └── distributed/
        └── ...
```

### Document Types per Leaf Folder

Each `{use_case}/{deployment_type}/` folder should contain these documents:

| File | Content | Purpose |
|---|---|---|
| `architecture.md` | Reference architecture overview, VPC layout, traffic flows, network diagram description | Planning phase: auto-fill fields, understand standard patterns |
| `components.md` | AWS services used, FortiGate VM sizes, license types, security features required | Planning phase: populate component-related fields |
| `configuration.md` | FortiGate configuration details, routing protocols, overlay strategies, HA setup | Execution phase: per-question KB context for technical fields |
| `sizing.md` | Instance sizing, bandwidth thresholds, cost estimates, performance characteristics | Execution phase: per-question KB context for sizing/perf fields |
| `best-practices.md` (optional) | Well-Architected alignment, compliance considerations, security posture | Execution phase: per-question KB context for compliance/security fields |

---

## 2. Metadata Schema

Every source document needs a **metadata sidecar file** that Bedrock KB uses during indexing. The sidecar file name is `{document_filename}.metadata.json` and lives alongside the source document.

### Metadata Attributes

| Attribute | Type | Values | Purpose |
|---|---|---|---|
| `use_case` | STRING | `sd-wan`, `egress`, `ingress`, `inspection` | Level-1 filtering — narrow search to use case |
| `deployment_type` | STRING | Varies per use case (see table below) | Level-2 filtering — narrow to specific pattern |
| `document_type` | STRING | `architecture`, `components`, `configuration`, `sizing`, `best-practices` | Content-type filtering — get specific kinds of info |

### Deployment Types by Use Case

| Use Case | Deployment Types |
|---|---|
| `sd-wan` | `hub-spoke`, `dual-hub`, `spoke-only` |
| `egress` | `single-az`, `multi-az` |
| `ingress` | `nlb-sandwich`, `gwlb` |
| `inspection` | `centralized`, `distributed` |

> **Note**: These are examples. Add deployment types as your KB grows. The interview agent's hierarchical search adapts automatically — it filters by whatever `deployment_type` values exist.

### Example Sidecar File

For `s3://ai-lcm-knowledge-base/sd-wan/hub-spoke/architecture.md`:

**File**: `s3://ai-lcm-knowledge-base/sd-wan/hub-spoke/architecture.md.metadata.json`

```json
{
  "metadataAttributes": {
    "use_case": {
      "value": "sd-wan",
      "type": "STRING"
    },
    "deployment_type": {
      "value": "hub-spoke",
      "type": "STRING"
    },
    "document_type": {
      "value": "architecture",
      "type": "STRING"
    }
  }
}
```

---

## 3. Creating the Bedrock Knowledge Base

### 3.1 Prerequisites

- S3 bucket with documents + metadata sidecars uploaded
- IAM role with Bedrock + S3 permissions

### 3.2 Via AWS Console

1. **Navigate**: Amazon Bedrock → Knowledge bases → Create knowledge base
2. **Name**: `ai-lcm-knowledge-base`
3. **IAM role**: Create new or select existing (needs `s3:GetObject`, `bedrock:*`)
4. **Data source**:
   - Type: **Amazon S3**
   - S3 URI: `s3://ai-lcm-knowledge-base/`
   - Metadata: **Enabled** (this tells Bedrock to look for `.metadata.json` sidecar files)
5. **Chunking strategy**: **Fixed-size chunking**
   - Chunk size: **500 tokens** (good for technical docs — keeps context focused)
   - Overlap: **50 tokens** (prevents losing context at chunk boundaries)
6. **Embedding model**: **Titan Embeddings V2** (`amazon.titan-embed-text-v2:0`)
   - 1024 dimensions, good balance of quality and cost
7. **Vector store**: **Amazon OpenSearch Serverless** (managed, no infra to maintain)
   - Or: **Amazon Aurora PostgreSQL** with pgvector (if already using Aurora)
8. **Metadata filtering**: Ensure "Filterable metadata attributes" includes:
   - `use_case` (String, Filterable)
   - `deployment_type` (String, Filterable)
   - `document_type` (String, Filterable)
9. **Create** → Note the Knowledge Base ID → Set as `AI_LCM_KNOWLEDGE_BASE_ID` env var

### 3.3 Via AWS CLI

```bash
# Step 1: Create the knowledge base
aws bedrock-agent create-knowledge-base \
  --name "ai-lcm-knowledge-base" \
  --role-arn "arn:aws:iam::ACCOUNT:role/BedrockKBRole" \
  --knowledge-base-configuration '{
    "type": "VECTOR",
    "vectorKnowledgeBaseConfiguration": {
      "embeddingModelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }' \
  --storage-configuration '{
    "type": "OPENSEARCH_SERVERLESS",
    "opensearchServerlessConfiguration": {
      "collectionArn": "arn:aws:aoss:us-east-1:ACCOUNT:collection/COLLECTION_ID",
      "vectorIndexName": "ai-lcm-kb-index",
      "fieldMapping": {
        "vectorField": "embedding",
        "textField": "text",
        "metadataField": "metadata"
      }
    }
  }'

# Step 2: Create the S3 data source
aws bedrock-agent create-data-source \
  --knowledge-base-id "KB_ID_FROM_STEP_1" \
  --name "s3-docs" \
  --data-source-configuration '{
    "type": "S3",
    "s3Configuration": {
      "bucketArn": "arn:aws:s3:::ai-lcm-knowledge-base"
    }
  }' \
  --vector-ingestion-configuration '{
    "chunkingConfiguration": {
      "chunkingStrategy": "FIXED_SIZE",
      "fixedSizeChunkingConfiguration": {
        "maxTokens": 500,
        "overlapPercentage": 10
      }
    }
  }'

# Step 3: Sync (ingest) the data source
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "KB_ID" \
  --data-source-id "DS_ID_FROM_STEP_2"
```

### 3.4 Via CDK (if using infra/)

Add to your CDK stack:

```typescript
import * as bedrock from 'aws-cdk-lib/aws-bedrock';

const kb = new bedrock.CfnKnowledgeBase(this, 'AiLcmKB', {
  name: 'ai-lcm-knowledge-base',
  roleArn: kbRole.roleArn,
  knowledgeBaseConfiguration: {
    type: 'VECTOR',
    vectorKnowledgeBaseConfiguration: {
      embeddingModelArn: `arn:aws:bedrock:${region}::foundation-model/amazon.titan-embed-text-v2:0`,
    },
  },
  storageConfiguration: {
    type: 'OPENSEARCH_SERVERLESS',
    opensearchServerlessConfiguration: { /* ... */ },
  },
});
```

---

## 4. Syncing / Re-indexing

After adding or updating documents:

```bash
# Re-sync the data source (re-indexes all documents)
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "YOUR_KB_ID" \
  --data-source-id "YOUR_DS_ID"

# Check sync status
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "YOUR_KB_ID" \
  --data-source-id "YOUR_DS_ID" \
  --ingestion-job-id "JOB_ID"
```

**Important**: After uploading new documents with `.metadata.json` sidecars, you MUST run a sync job. Bedrock doesn't auto-detect S3 changes.

---

## 5. Verifying Metadata Filtering Works

Test from Python:

```python
import boto3

client = boto3.client("bedrock-agent-runtime", region_name="us-east-1")

# Level-1: Filter by use case only
response = client.retrieve(
    knowledgeBaseId="YOUR_KB_ID",
    retrievalQuery={"text": "FortiGate SD-WAN architecture"},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 5,
            "filter": {
                "equals": {"key": "use_case", "value": "sd-wan"}
            }
        }
    },
)
print(f"Results: {len(response['retrievalResults'])}")
for r in response["retrievalResults"]:
    print(f"  Source: {r['location']['s3Location']['uri']}")
    print(f"  Score: {r.get('score', 0):.2f}")

# Level-2: Filter by use case + deployment type
response = client.retrieve(
    knowledgeBaseId="YOUR_KB_ID",
    retrievalQuery={"text": "FortiGate SD-WAN hub-spoke overlay configuration"},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 5,
            "filter": {
                "andAll": [
                    {"equals": {"key": "use_case", "value": "sd-wan"}},
                    {"equals": {"key": "deployment_type", "value": "hub-spoke"}}
                ]
            }
        }
    },
)

# Level-3: Filter by use case + document type (get architecture docs only)
response = client.retrieve(
    knowledgeBaseId="YOUR_KB_ID",
    retrievalQuery={"text": "VPC layout traffic flow"},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 3,
            "filter": {
                "andAll": [
                    {"equals": {"key": "use_case", "value": "sd-wan"}},
                    {"equals": {"key": "document_type", "value": "architecture"}}
                ]
            }
        }
    },
)
```

---

## 6. Adding New Documents

To add a new document to the KB:

1. **Create the document** (Markdown or PDF) in the correct folder path
2. **Create the metadata sidecar** alongside it:
   ```bash
   # Example: adding a new config doc for egress/multi-az
   aws s3 cp new-config.md s3://ai-lcm-knowledge-base/egress/multi-az/configuration.md
   aws s3 cp - s3://ai-lcm-knowledge-base/egress/multi-az/configuration.md.metadata.json <<'EOF'
   {
     "metadataAttributes": {
       "use_case": {"value": "egress", "type": "STRING"},
       "deployment_type": {"value": "multi-az", "type": "STRING"},
       "document_type": {"value": "configuration", "type": "STRING"}
     }
   }
   EOF
   ```
3. **Trigger re-sync**:
   ```bash
   aws bedrock-agent start-ingestion-job \
     --knowledge-base-id "YOUR_KB_ID" \
     --data-source-id "YOUR_DS_ID"
   ```

---

## 7. Document Writing Guidelines

For best search relevance:

1. **Use specific headers** that match search queries:
   - Good: `## SD-WAN Hub-Spoke VPC Layout`
   - Bad: `## Architecture Overview`

2. **Include key terms early** in each section — the chunking strategy splits at ~500 tokens, so important terms should appear in the first paragraph of each section.

3. **Be explicit about field values** the interview agent needs:
   - Mention specific routing protocols: "This architecture uses **BGP** for dynamic routing between the FortiGate hub and AWS Transit Gateway."
   - Mention resilience patterns: "Deploy in **ha-single-region-dual-zone** configuration for standard AWS multi-AZ resilience."
   - Mention component specifics: "FortiGate VM size: **c5.xlarge** for up to 5 Gbps throughput."

4. **Cross-reference related deployment types**: "For dual-hub configurations, see `/sd-wan/dual-hub/architecture.md`."

5. **Keep each document focused** on its `document_type`. Don't mix architecture descriptions with configuration details in the same file.

---

## 8. Automation Script for Bulk Metadata Generation

If you have existing documents without sidecars, use this script to generate them:

```bash
#!/bin/bash
# generate-metadata.sh — Creates .metadata.json sidecars from folder structure
# Usage: ./generate-metadata.sh s3://ai-lcm-knowledge-base

BUCKET_PREFIX="${1:?Usage: $0 s3://bucket-name}"

# Map of file patterns to document types
declare -A DOC_TYPES=(
  ["architecture"]="architecture"
  ["components"]="components"
  ["configuration"]="configuration"
  ["config"]="configuration"
  ["sizing"]="sizing"
  ["best-practices"]="best-practices"
  ["best_practices"]="best-practices"
)

aws s3 ls "$BUCKET_PREFIX" --recursive | while read -r line; do
  file=$(echo "$line" | awk '{print $4}')

  # Skip metadata files and non-documents
  [[ "$file" == *.metadata.json ]] && continue
  [[ "$file" != *.md && "$file" != *.pdf && "$file" != *.txt ]] && continue

  # Extract hierarchy from path: use_case/deployment_type/filename
  IFS='/' read -r use_case deployment_type filename <<< "$file"
  [ -z "$deployment_type" ] && continue
  [ -z "$filename" ] && continue

  # Determine document type from filename
  basename="${filename%.*}"
  doc_type="${DOC_TYPES[$basename]:-other}"

  # Generate metadata JSON
  metadata=$(cat <<EOF
{
  "metadataAttributes": {
    "use_case": {"value": "$use_case", "type": "STRING"},
    "deployment_type": {"value": "$deployment_type", "type": "STRING"},
    "document_type": {"value": "$doc_type", "type": "STRING"}
  }
}
EOF
)

  echo "Creating metadata for: $file (use_case=$use_case, deployment_type=$deployment_type, doc_type=$doc_type)"
  echo "$metadata" | aws s3 cp - "$BUCKET_PREFIX/$file.metadata.json"
done

echo "Done. Run 'aws bedrock-agent start-ingestion-job' to re-index."
```
