#!/usr/bin/env bash
# setup-bedrock-kb.sh — Complete Bedrock Knowledge Base setup
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - S3 bucket created (by CDK stack or manually)
#   - Bedrock model access enabled in the target region
#
# Usage:
#   ./scripts/setup-bedrock-kb.sh <S3_BUCKET_NAME> <REGION>
#
# Example:
#   ./scripts/setup-bedrock-kb.sh ai-deploy-knowledge-base-prod us-east-1

set -euo pipefail

BUCKET="${1:-}"
REGION="${2:-us-east-1}"
KB_DIR="knowledge-base"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$BUCKET" ]; then
  echo "Usage: $0 <S3_BUCKET_NAME> [REGION]"
  echo ""
  echo "Example: $0 ai-deploy-knowledge-base-prod us-east-1"
  exit 1
fi

echo "==> Bedrock Knowledge Base Setup"
echo "    Bucket: s3://$BUCKET"
echo "    Region: $REGION"
echo "    KB Dir: $KB_DIR"
echo ""

# Step 1: Generate metadata sidecar files
echo "==> Step 1: Generating metadata sidecar files..."
"$SCRIPT_DIR/generate-kb-metadata.sh" "$KB_DIR"
echo ""

# Step 2: Upload to S3
echo "==> Step 2: Uploading knowledge base documents to S3..."
aws s3 sync "$KB_DIR/" "s3://$BUCKET/" \
  --region "$REGION" \
  --delete \
  --exclude "*.DS_Store" \
  --exclude "__pycache__/*"

echo "    Upload complete. $(find "$KB_DIR" -type f | wc -l | tr -d ' ') files synced."
echo ""

# Step 3: Verify upload
echo "==> Step 3: Verifying S3 contents..."
DOC_COUNT=$(aws s3 ls "s3://$BUCKET/" --recursive --region "$REGION" | grep -c "\.md$" || true)
META_COUNT=$(aws s3 ls "s3://$BUCKET/" --recursive --region "$REGION" | grep -c "\.metadata\.json$" || true)
echo "    Documents: $DOC_COUNT"
echo "    Metadata files: $META_COUNT"

if [ "$DOC_COUNT" -ne "$META_COUNT" ]; then
  echo "    WARNING: Document count does not match metadata count!"
  echo "    Every .md file should have a corresponding .metadata.json"
fi
echo ""

# Step 4: Instructions for Bedrock KB creation
echo "==> Step 4: Create Bedrock Knowledge Base (manual steps)"
echo ""
echo "   In the AWS Console → Amazon Bedrock → Knowledge bases → Create:"
echo ""
echo "   1. Name: ai-deploy-knowledge-base"
echo "      Description: NovaMind Inference Server deployment documentation"
echo ""
echo "   2. IAM Role: Create a new service role (or use existing)"
echo "      Permissions needed: s3:GetObject, s3:ListBucket on s3://$BUCKET"
echo ""
echo "   3. Data source:"
echo "      - Source: Amazon S3"
echo "      - S3 URI: s3://$BUCKET/"
echo "      - Metadata field mapping: Use file-level metadata (.metadata.json)"
echo ""
echo "   4. Embedding model: Amazon Titan Text Embeddings V2"
echo "      (or Cohere Embed English V3 for multilingual)"
echo ""
echo "   5. Vector store: Quick create (OpenSearch Serverless)"
echo "      OR choose existing OpenSearch/Aurora/Pinecone"
echo ""
echo "   6. Chunking strategy:"
echo "      - Strategy: Fixed-size chunking"
echo "      - Max tokens: 500"
echo "      - Overlap: 50 tokens"
echo ""
echo "   7. After creation, click 'Sync' to index all documents."
echo ""
echo "   8. Note the Knowledge Base ID (format: XXXXXXXXXX)"
echo "      Set it in your environment: AI_DEPLOY_KNOWLEDGE_BASE_ID=<KB_ID>"
echo ""
echo "==> Alternatively, create via CLI:"
echo ""
cat <<'CLIEOF'
   # Create the KB (replace ROLE_ARN with your Bedrock KB service role)
   aws bedrock-agent create-knowledge-base \
     --name "ai-deploy-knowledge-base" \
     --role-arn "arn:aws:iam::ACCOUNT:role/AmazonBedrockExecutionRoleForKnowledgeBase" \
     --knowledge-base-configuration '{
       "type": "VECTOR",
       "vectorKnowledgeBaseConfiguration": {
         "embeddingModelArn": "arn:aws:bedrock:REGION::foundation-model/amazon.titan-embed-text-v2:0"
       }
     }' \
     --storage-configuration '{
       "type": "OPENSEARCH_SERVERLESS",
       "opensearchServerlessConfiguration": {
         "collectionArn": "YOUR_COLLECTION_ARN",
         "fieldMapping": {
           "metadataField": "metadata",
           "textField": "text",
           "vectorField": "vector"
         },
         "vectorIndexName": "bedrock-knowledge-base-default-index"
       }
     }' \
     --region REGION

   # Add S3 data source
   aws bedrock-agent create-data-source \
     --knowledge-base-id "YOUR_KB_ID" \
     --name "s3-knowledge-base" \
     --data-source-configuration '{
       "type": "S3",
       "s3Configuration": {
         "bucketArn": "arn:aws:s3:::BUCKET_NAME"
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
     }' \
     --region REGION

   # Sync the data source
   aws bedrock-agent start-ingestion-job \
     --knowledge-base-id "YOUR_KB_ID" \
     --data-source-id "YOUR_DATA_SOURCE_ID" \
     --region REGION
CLIEOF

echo ""
echo "==> Step 5: Verify KB works"
echo ""
echo "   # Test retrieval:"
echo "   aws bedrock-agent-runtime retrieve \\"
echo "     --knowledge-base-id YOUR_KB_ID \\"
echo "     --retrieval-query '{\"text\": \"auto scaling GPU inference architecture\"}' \\"
echo "     --retrieval-configuration '{\"vectorSearchConfiguration\": {\"numberOfResults\": 3}}' \\"
echo "     --region $REGION"
echo ""
echo "   # Test filtered retrieval:"
echo "   aws bedrock-agent-runtime retrieve \\"
echo "     --knowledge-base-id YOUR_KB_ID \\"
echo "     --retrieval-query '{\"text\": \"sizing\"}' \\"
echo "     --retrieval-configuration '{\"vectorSearchConfiguration\": {\"numberOfResults\": 3, \"filter\": {\"equals\": {\"key\": \"use_case\", \"value\": \"realtime-inference\"}}}}' \\"
echo "     --region $REGION"
echo ""
echo "==> Done! Next: set AI_DEPLOY_KNOWLEDGE_BASE_ID in your ECS task environment."
