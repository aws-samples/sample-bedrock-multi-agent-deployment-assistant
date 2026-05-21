#!/usr/bin/env bash
# Provision Floci resources for local development.
# Idempotent — safe to run multiple times.
#
# Optional env vars (for real Bedrock KB integration):
#   BEDROCK_KB_ID          — existing Knowledge Base ID
#   BEDROCK_KB_BUCKET      — real S3 bucket backing the KB
#   BEDROCK_KB_DATA_SOURCE — data source ID for ingestion sync
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ENDPOINT="http://localhost:4566"
REGION="us-west-2"
TABLE_NAME="ai-deploy-table"
ARTIFACTS_BUCKET="ai-deploy-artifacts"
KB_BUCKET="ai-deploy-knowledge-base"

# Suspend AWS_PROFILE for Floci commands (avoids SSO credential resolution against local endpoint)
_SAVED_AWS_PROFILE="${AWS_PROFILE:-}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"

AWS="aws --endpoint-url $ENDPOINT --region $REGION --no-cli-pager"

echo "==> Provisioning Floci resources at $ENDPOINT"

# ---------------------------------------------------------------------------
# DynamoDB Table
# ---------------------------------------------------------------------------
if $AWS dynamodb describe-table --table-name "$TABLE_NAME" >/dev/null 2>&1; then
  echo "    DynamoDB table '$TABLE_NAME' already exists"
else
  echo "    Creating DynamoDB table '$TABLE_NAME'..."
  $AWS dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions \
      AttributeName=pk,AttributeType=S \
      AttributeName=sk,AttributeType=S \
      AttributeName=gsi1pk,AttributeType=S \
      AttributeName=gsi1sk,AttributeType=S \
      AttributeName=gsi2pk,AttributeType=S \
      AttributeName=gsi2sk,AttributeType=S \
    --key-schema \
      AttributeName=pk,KeyType=HASH \
      AttributeName=sk,KeyType=RANGE \
    --global-secondary-indexes \
      '[
        {"IndexName":"GSI1","KeySchema":[{"AttributeName":"gsi1pk","KeyType":"HASH"},{"AttributeName":"gsi1sk","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}},
        {"IndexName":"GSI2","KeySchema":[{"AttributeName":"gsi2pk","KeyType":"HASH"},{"AttributeName":"gsi2sk","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}
      ]' \
    --billing-mode PAY_PER_REQUEST \
    --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \
    >/dev/null

  $AWS dynamodb update-time-to-live \
    --table-name "$TABLE_NAME" \
    --time-to-live-specification Enabled=true,AttributeName=ttl \
    >/dev/null 2>&1 || true

  echo "    Created DynamoDB table with streams + TTL"
fi

# ---------------------------------------------------------------------------
# S3 Buckets
# ---------------------------------------------------------------------------
for BUCKET in "$ARTIFACTS_BUCKET" "$KB_BUCKET"; do
  if $AWS s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    echo "    S3 bucket '$BUCKET' already exists"
  else
    $AWS s3 mb "s3://$BUCKET" >/dev/null
    echo "    Created S3 bucket '$BUCKET'"
  fi
done

# ---------------------------------------------------------------------------
# SQS FIFO Queues
# ---------------------------------------------------------------------------
QUEUES=(
  "ai-deploy-design-dlq.fifo"
  "ai-deploy-design-tasks.fifo"
  "ai-deploy-iac-dlq.fifo"
  "ai-deploy-iac-tasks.fifo"
  "ai-deploy-docs-dlq.fifo"
  "ai-deploy-docs-tasks.fifo"
)

for QUEUE in "${QUEUES[@]}"; do
  if $AWS sqs get-queue-url --queue-name "$QUEUE" >/dev/null 2>&1; then
    echo "    SQS queue '$QUEUE' already exists"
  else
    ATTRS='{"FifoQueue":"true","ContentBasedDeduplication":"true"}'
    $AWS sqs create-queue --queue-name "$QUEUE" --attributes "$ATTRS" >/dev/null
    echo "    Created SQS queue '$QUEUE'"
  fi
done

# ---------------------------------------------------------------------------
# Cognito User Pool + Client + Test User
# ---------------------------------------------------------------------------
POOL_NAME="ai-deploy-local"
POOL_ID=$($AWS cognito-idp list-user-pools --max-results 10 2>/dev/null \
  | python3 -c "import sys,json; pools=json.load(sys.stdin).get('UserPools',[]); print(next((p['Id'] for p in pools if p['Name']=='$POOL_NAME'),''))" 2>/dev/null || echo "")

if [ -n "$POOL_ID" ]; then
  echo "    Cognito pool '$POOL_NAME' already exists (ID: $POOL_ID)"
else
  POOL_ID=$($AWS cognito-idp create-user-pool \
    --pool-name "$POOL_NAME" \
    --schema '[{"Name":"tenant_id","AttributeDataType":"String","Mutable":true,"Required":false}]' \
    --query 'UserPool.Id' --output text)
  echo "    Created Cognito pool '$POOL_NAME' (ID: $POOL_ID)"
fi

CLIENT_ID=$($AWS cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 5 2>/dev/null \
  | python3 -c "import sys,json; clients=json.load(sys.stdin).get('UserPoolClients',[]); print(clients[0]['ClientId'] if clients else '')" 2>/dev/null || echo "")

if [ -n "$CLIENT_ID" ]; then
  echo "    Cognito client already exists (ID: $CLIENT_ID)"
else
  CLIENT_ID=$($AWS cognito-idp create-user-pool-client \
    --user-pool-id "$POOL_ID" \
    --client-name frontend \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --query 'UserPoolClient.ClientId' --output text)
  echo "    Created Cognito client (ID: $CLIENT_ID)"
fi

# Create test user (ignore if exists)
$AWS cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "dev@local.test" \
  --temporary-password "TempPass1!" \
  --user-attributes Name=email,Value=dev@local.test Name=custom:tenant_id,Value=local-dev \
  >/dev/null 2>&1 || true

$AWS cognito-idp admin-set-user-password \
  --user-pool-id "$POOL_ID" \
  --username "dev@local.test" \
  --password "LocalDev1!" \
  --permanent \
  >/dev/null 2>&1 || true

echo "    Test user: dev@local.test / LocalDev1! (tenant: local-dev)"

# ---------------------------------------------------------------------------
# Seed Knowledge Base bucket with local docs (Floci — for S3 tool access)
# ---------------------------------------------------------------------------
KB_DIR="$ROOT/knowledge-base"
if [ -d "$KB_DIR" ]; then
  # Generate metadata sidecar files before any S3 sync
  echo "    Generating metadata sidecar files..."
  bash "$ROOT/scripts/generate-kb-metadata.sh" "$KB_DIR" 2>/dev/null || true
  echo "    Syncing knowledge-base/ → Floci s3://$KB_BUCKET/"
  $AWS s3 sync "$KB_DIR" "s3://$KB_BUCKET/" --quiet
else
  echo "    No knowledge-base/ directory found — skipping KB seed"
fi

# ---------------------------------------------------------------------------
# Real Bedrock Knowledge Base sync (optional — for production-like retrieval)
# ---------------------------------------------------------------------------
# Restore AWS_PROFILE for real AWS calls below
if [ -n "$_SAVED_AWS_PROFILE" ]; then
  export AWS_PROFILE="$_SAVED_AWS_PROFILE"
fi
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

BEDROCK_KB_ID="${BEDROCK_KB_ID:-}"
BEDROCK_KB_BUCKET="${BEDROCK_KB_BUCKET:-}"
BEDROCK_KB_DATA_SOURCE="${BEDROCK_KB_DATA_SOURCE:-}"

if [ -n "$BEDROCK_KB_ID" ] && [ -n "$BEDROCK_KB_BUCKET" ] && [ -d "$KB_DIR" ]; then
  echo ""
  echo "==> Syncing to real Bedrock Knowledge Base"
  echo "    KB ID: $BEDROCK_KB_ID"
  echo "    Bucket: s3://$BEDROCK_KB_BUCKET"

  # Sync to real AWS S3 (NOT Floci endpoint) — non-fatal if creds unavailable
  echo "    Uploading to real S3..."
  if aws s3 sync "$KB_DIR/" "s3://$BEDROCK_KB_BUCKET/" \
    --region "$REGION" \
    --delete \
    --exclude "*.DS_Store" \
    --exclude "__pycache__/*" \
    --quiet 2>&1; then

    DOC_COUNT=$(find "$KB_DIR" -name "*.md" | wc -l | tr -d ' ')
    echo "    Uploaded $DOC_COUNT documents + metadata"

    # Trigger ingestion sync if data source ID is provided
    if [ -n "$BEDROCK_KB_DATA_SOURCE" ]; then
      echo "    Triggering ingestion job..."
      JOB_ID=$(aws bedrock-agent start-ingestion-job \
        --knowledge-base-id "$BEDROCK_KB_ID" \
        --data-source-id "$BEDROCK_KB_DATA_SOURCE" \
        --region "$REGION" \
        --query 'ingestionJob.ingestionJobId' \
        --output text \
        --no-cli-pager 2>/dev/null || echo "")
      if [ -n "$JOB_ID" ]; then
        echo "    Ingestion job started: $JOB_ID"
        echo "    (runs in background — retrieval available once indexing completes)"
      else
        echo "    WARNING: Failed to start ingestion job. Sync manually in the console."
      fi
    fi
  else
    echo "    WARNING: S3 sync failed (credentials may need refresh: aws sso login --profile $AWS_PROFILE)"
    echo "    KB retrieval will still work if data was previously synced."
  fi
elif [ -n "$BEDROCK_KB_ID" ] && [ -z "$BEDROCK_KB_BUCKET" ]; then
  echo ""
  echo "    NOTE: BEDROCK_KB_ID is set but BEDROCK_KB_BUCKET is not."
  echo "    KB retrieval will work but docs won't be synced automatically."
fi

# ---------------------------------------------------------------------------
# Bedrock AgentCore Memory (real AWS — like Bedrock, not emulated in Floci)
# ---------------------------------------------------------------------------
# Idempotent: creates memory resource on first run, reuses on subsequent runs.
# Pass AGENTCORE_MEMORY_FRESH=true (or dev.sh --fresh) to delete and recreate.
AGENTCORE_MEMORY_FRESH="${AGENTCORE_MEMORY_FRESH:-false}"
AGENTCORE_MEMORY_NAME="ai_deploy_memory_dev"
AGENTCORE_MEMORY_ID=""

echo ""
echo "==> Provisioning AgentCore Memory (real AWS, region: $REGION)"

# Look for existing memory by name (list-memories only returns id, not name;
# the id format is "{name}-{random10}", so we match by prefix)
EXISTING_MEMORIES=$(aws bedrock-agentcore-control list-memories \
  --region "$REGION" \
  --no-cli-pager \
  --output json 2>/dev/null || echo '{"memories":[]}')

AGENTCORE_MEMORY_ID=$(echo "$EXISTING_MEMORIES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('memories', []):
    mid = m.get('id', '')
    if mid.startswith('${AGENTCORE_MEMORY_NAME}-'):
        print(mid)
        break
" 2>/dev/null || echo "")

if [ "$AGENTCORE_MEMORY_FRESH" = "true" ] && [ -n "$AGENTCORE_MEMORY_ID" ]; then
  echo "    --fresh: Deleting existing memory '$AGENTCORE_MEMORY_NAME' ($AGENTCORE_MEMORY_ID)..."
  aws bedrock-agentcore-control delete-memory \
    --memory-id "$AGENTCORE_MEMORY_ID" \
    --region "$REGION" \
    --no-cli-pager >/dev/null 2>&1 || true
  AGENTCORE_MEMORY_ID=""
  echo "    Deleted. Will recreate."
fi

if [ -n "$AGENTCORE_MEMORY_ID" ]; then
  echo "    AgentCore Memory already exists: $AGENTCORE_MEMORY_ID"
else
  echo "    Creating AgentCore Memory '$AGENTCORE_MEMORY_NAME'..."
  CREATE_RESULT=$(aws bedrock-agentcore-control create-memory \
    --name "$AGENTCORE_MEMORY_NAME" \
    --region "$REGION" \
    --event-expiry-duration 30 \
    --memory-strategies '[
      {"semanticMemoryStrategy": {"name": "FactExtractor", "namespaceTemplates": ["/facts/{actorId}/"]}},
      {"userPreferenceMemoryStrategy": {"name": "PreferenceTracker", "namespaceTemplates": ["/preferences/{actorId}/"]}}
    ]' \
    --no-cli-pager \
    --output json 2>&1) || true

  AGENTCORE_MEMORY_ID=$(echo "$CREATE_RESULT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('memory', {}).get('id', data.get('id', '')))
except: pass
" 2>/dev/null || echo "")

  if [ -n "$AGENTCORE_MEMORY_ID" ]; then
    echo "    Created AgentCore Memory: $AGENTCORE_MEMORY_ID"
  else
    echo "    WARNING: Failed to create AgentCore Memory. Memory features will be disabled."
    echo "    (Ensure IAM permissions for bedrock-agentcore-control:CreateMemory)"
    echo "    Output: $CREATE_RESULT"
  fi
fi

# ---------------------------------------------------------------------------
# Ensure backend .env has all required Floci settings
# ---------------------------------------------------------------------------
ENV_FILE="$ROOT/backend/.env"

# KB config: remove stale keys when switching modes
if [ -n "$BEDROCK_KB_ID" ]; then
  if [ -f "$ENV_FILE" ]; then
    sed -i.bak '/^AI_DEPLOY_KNOWLEDGE_BASE_LOCAL_PATH=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  fi
else
  if [ -f "$ENV_FILE" ]; then
    sed -i.bak '/^AI_DEPLOY_KNOWLEDGE_BASE_ID=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  fi
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "# Generated by scripts/setup-local.sh — Floci local development" > "$ENV_FILE"
  echo "    Created $ENV_FILE"
fi

# Build the list of required key=value pairs
REQUIRED_VARS="
AI_DEPLOY_AWS_REGION=$REGION
AI_DEPLOY_AWS_ENDPOINT_URL=$ENDPOINT
AI_DEPLOY_DYNAMODB_TABLE=$TABLE_NAME
AI_DEPLOY_S3_ARTIFACTS_BUCKET=$ARTIFACTS_BUCKET
AI_DEPLOY_S3_KNOWLEDGE_BASE_BUCKET=$KB_BUCKET
AI_DEPLOY_SQS_DESIGN_QUEUE_URL=$ENDPOINT/000000000000/ai-deploy-design-tasks.fifo
AI_DEPLOY_SQS_IAC_QUEUE_URL=$ENDPOINT/000000000000/ai-deploy-iac-tasks.fifo
AI_DEPLOY_SQS_DOCS_QUEUE_URL=$ENDPOINT/000000000000/ai-deploy-docs-tasks.fifo
AI_DEPLOY_METRICS_ENABLED=false
AI_DEPLOY_DEBUG=true
AI_DEPLOY_COGNITO_USER_POOL_ID=$POOL_ID
AI_DEPLOY_COGNITO_CLIENT_ID=$CLIENT_ID
AI_DEPLOY_CORS_ORIGINS=[\"http://localhost:3000\"]
"

if [ -n "${AWS_PROFILE:-}" ]; then
  REQUIRED_VARS="$REQUIRED_VARS
AI_DEPLOY_AWS_PROFILE=$AWS_PROFILE"
fi

if [ -n "$BEDROCK_KB_ID" ]; then
  REQUIRED_VARS="$REQUIRED_VARS
AI_DEPLOY_KNOWLEDGE_BASE_ID=$BEDROCK_KB_ID"
else
  REQUIRED_VARS="$REQUIRED_VARS
AI_DEPLOY_KNOWLEDGE_BASE_LOCAL_PATH=../knowledge-base"
fi

if [ -n "$AGENTCORE_MEMORY_ID" ]; then
  REQUIRED_VARS="$REQUIRED_VARS
AI_DEPLOY_AGENTCORE_MEMORY_ID=$AGENTCORE_MEMORY_ID"
fi

# Upsert each required key
UPDATED=0
echo "$REQUIRED_VARS" | while IFS= read -r line; do
  [ -z "$line" ] && continue
  KEY="${line%%=*}"
  VALUE="${line#*=}"
  if grep -q "^${KEY}=" "$ENV_FILE" 2>/dev/null; then
    CURRENT=$(grep "^${KEY}=" "$ENV_FILE" | head -1 | cut -d= -f2-)
    if [ "$CURRENT" != "$VALUE" ]; then
      sed -i.bak "s|^${KEY}=.*|${KEY}=${VALUE}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
      UPDATED=$((UPDATED + 1))
    fi
  else
    echo "${KEY}=${VALUE}" >> "$ENV_FILE"
    UPDATED=$((UPDATED + 1))
  fi
done

echo "    Backend .env updated at $ENV_FILE"

echo ""
echo "==> Provisioning complete!"
echo "    Table: $TABLE_NAME (streams enabled)"
echo "    Buckets: $ARTIFACTS_BUCKET, $KB_BUCKET"
echo "    Queues: design, iac, docs (FIFO + DLQs)"
echo "    Cognito: pool=$POOL_ID client=$CLIENT_ID"
if [ -n "$AGENTCORE_MEMORY_ID" ]; then
  echo "    AgentCore Memory: $AGENTCORE_MEMORY_ID (real AWS, $REGION)"
else
  echo "    AgentCore Memory: disabled (creation failed or not available)"
fi
