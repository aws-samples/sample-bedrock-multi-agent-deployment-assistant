# AWS Deployment Guide

Deploy AI-LCM to AWS using CDK. This guide covers everything from initial setup to a running production environment.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS CLI | v2 | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| Node.js | 22+ | [nodejs.org](https://nodejs.org/) |
| AWS CDK CLI | latest | `npm install -g aws-cdk` |
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| pnpm | 9+ | `npm install -g pnpm` |
| Docker | latest | [docker.com](https://www.docker.com/) — required for Lambda worker images |

## Step 1: AWS Account Setup

### 1.1 Configure Credentials

```bash
aws configure
# AWS Access Key ID: <your-key>
# AWS Secret Access Key: <your-secret>
# Default region name: us-east-1    # must match your Bedrock-enabled region
```

Verify access:

```bash
aws sts get-caller-identity
```

### 1.2 Enable Bedrock Model Access

1. Open the [Bedrock console](https://console.aws.amazon.com/bedrock/) in your target region
2. Go to **Model access** → **Manage model access**
3. Request access to:
   - **Anthropic Claude Sonnet** (used by design, IaC, and interview agents)
   - **Anthropic Claude Haiku** (used by documentation agent)
4. Wait for access status to show **Access granted**

Model access is region-specific. Ensure you request access in the same region you'll deploy to.

### 1.3 Bootstrap CDK

CDK needs a one-time bootstrap in each account/region:

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1    # your target region

npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

## Step 2: Build the Frontend

The frontend must be built as a static export before deployment:

```bash
cd frontend
pnpm install
NEXT_OUTPUT=export pnpm build    # generates out/ directory
cd ..
```

This creates `frontend/out/` which CDK will deploy to S3 + CloudFront.

## Step 3: Build and Deploy Infrastructure

```bash
cd infra
npm install
npm run build
```

### 3.1 Preview Changes

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

npx cdk diff
```

### 3.2 Deploy

```bash
npx cdk deploy \
  -c environment=prod \
  -c notificationEmail=your-ops-team@example.com
```

CDK will show a summary of IAM changes and ask for confirmation. Type `y` to proceed.

First deployment takes 15-25 minutes (VPC, NAT gateways, ECS, CloudFront distribution).

### CDK Context Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `environment` | string | `dev` | Used in resource names and tags |
| `notificationEmail` | string | — | SNS subscription for CloudWatch alarms |
| `natGateways` | number | 2 | Number of NAT gateways (1 saves ~$32/mo) |
| `costCenter` | string | `engineering` | Cost allocation tag |
| `owner` | string | `platform-team` | Owner tag |

## Step 4: Note Stack Outputs

After deployment, CDK prints stack outputs. Save these values:

```
AiLcmStack.AlbDnsName           = ai-lcm-xxxx.us-east-1.elb.amazonaws.com
AiLcmStack.FrontendUrl          = https://d1234abcdef.cloudfront.net
AiLcmStack.DistributionId       = E1234ABCDEF
AiLcmStack.WebSocketUrl         = wss://abc123.execute-api.us-east-1.amazonaws.com/prod
AiLcmStack.UserPoolId           = us-east-1_XXXXXXXXX
AiLcmStack.UserPoolClientId     = xxxxxxxxxxxxxxxxxxxxxxxxxx
AiLcmStack.TableName            = ai-lcm-table-prod
AiLcmStack.KnowledgeBaseBucketName = ai-lcm-s3-knowledgebase-xxxx
AiLcmStack.ArtifactsBucketName  = ai-lcm-s3-artifacts-xxxx
```

You can also retrieve these later:

```bash
aws cloudformation describe-stacks --stack-name AiLcmStack --query 'Stacks[0].Outputs' --output table
```

## Step 5: Configure Frontend Environment

The static frontend is already deployed to CloudFront. However, it needs to know the backend URL.

Update `frontend/.env.local` with production values and rebuild:

```ini
NEXT_PUBLIC_BACKEND_URL=https://<AlbDnsName>
NEXT_PUBLIC_WEBSOCKET_URL=<WebSocketUrl>
```

Then rebuild and redeploy:

```bash
cd frontend
NEXT_OUTPUT=export pnpm build
cd ../infra
npx cdk deploy    # BucketDeployment uploads the new build
```

To invalidate CloudFront cache after redeployment:

```bash
aws cloudfront create-invalidation \
  --distribution-id <DistributionId> \
  --paths "/*"
```

## Step 6: Create First User

Cognito is configured with email sign-in and MFA. Create a user via the AWS CLI:

```bash
USER_POOL_ID=<UserPoolId from outputs>

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username user@example.com \
  --user-attributes \
    Name=email,Value=user@example.com \
    Name=email_verified,Value=true \
    Name=custom:tenant_id,Value=team-alpha \
  --temporary-password 'TempPass123!'

# Set permanent password (skip forced change)
aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username user@example.com \
  --password 'YourSecurePassword123!' \
  --permanent
```

The `custom:tenant_id` attribute controls data isolation — users in different tenants cannot see each other's projects.

## Step 7: (Optional) Knowledge Base Setup

The platform works without a Knowledge Base, but design quality improves significantly with FortiGate reference documentation.

### 7.1 Upload Documents

```bash
KB_BUCKET=<KnowledgeBaseBucketName from outputs>

aws s3 cp fortigate-admin-guide.pdf s3://$KB_BUCKET/
aws s3 cp fortigate-best-practices.pdf s3://$KB_BUCKET/
```

### 7.2 Create Knowledge Base in Bedrock

1. Open [Bedrock console](https://console.aws.amazon.com/bedrock/) → **Knowledge bases** → **Create**
2. Name: `ai-lcm-fortigate-kb`
3. Data source: S3, select the `KnowledgeBaseBucketName` bucket
4. Embedding model: **Titan Embeddings V2**
5. Vector store: let Bedrock create an OpenSearch Serverless collection (default)
6. Create and sync

### 7.3 Configure the Backend

After creation, note the Knowledge Base ID and update the ECS task environment:

```bash
# Update the ECS task definition with the KB ID
aws ecs update-service \
  --cluster ai-lcm-cluster-prod \
  --service <ServiceName> \
  --force-new-deployment \
  --task-definition $(
    aws ecs describe-services \
      --cluster ai-lcm-cluster-prod \
      --services <ServiceName> \
      --query 'services[0].taskDefinition' \
      --output text
  )
```

Or set the env var in the CDK stack by adding to `ai-lcm-stack.ts`:

```typescript
ecsConstruct.service.taskDefinition.defaultContainer?.addEnvironment(
  "AI_LCM_KNOWLEDGE_BASE_ID",
  "YOUR_KB_ID",
);
```

Then redeploy: `npx cdk deploy`

## Verification

### Health Check

```bash
ALB_DNS=<AlbDnsName>

# Basic
curl https://$ALB_DNS/ping
# {"status": "ok"}

# Deep (checks Bedrock + DynamoDB connectivity)
curl https://$ALB_DNS/health
# {"status": "healthy"}
```

### Frontend

Open the `FrontendUrl` from stack outputs in your browser. You should see the project dashboard.

### WebSocket

The WebSocket is used for real-time task updates. Verify the endpoint is reachable:

```bash
wscat -c "<WebSocketUrl>"
# Connected
```

## What Gets Deployed

| Resource | Purpose | Cost Estimate (us-east-1) |
|----------|---------|--------------------------|
| VPC (2-AZ, 2 NAT gateways) | Network isolation | ~$64/mo |
| ECS Fargate (2 vCPU, 4 GB, 2-10 tasks) | FastAPI backend | ~$70/mo |
| ALB + WAF | Load balancing + security | ~$16/mo |
| DynamoDB (on-demand) | Project metadata + task state | ~$10-50/mo |
| S3 (3 buckets) | KB docs, artifacts, access logs | ~$5/mo |
| Lambda (8 functions) | 3 workers + 5 WebSocket handlers | ~$1-5/mo |
| SQS (3 FIFO + 3 DLQ) | Async task queues | <$1/mo |
| CloudFront | Frontend CDN | ~$1-5/mo |
| Cognito | User authentication | Free tier |
| KMS | Encryption at rest | ~$1/mo |
| CloudWatch + SNS | Alarms + notifications | ~$5/mo |
| **Total** | | **~$170-220/mo** |

Set `natGateways=1` to save ~$32/mo in non-production environments.

## Security

- **Encryption at rest**: KMS customer-managed key for DynamoDB, S3, CloudWatch, SNS, SQS
- **Encryption in transit**: TLS 1.2+ on ALB, HTTPS on CloudFront, SSL enforced on S3
- **Network**: ECS in private subnets, VPC endpoints for Bedrock/S3/DynamoDB, NAT for outbound
- **WAF**: AWS managed rule groups (common + known bad inputs) + IP rate limiting (1000 req/5min)
- **Auth**: Cognito with MFA, 12-char password policy, SRP auth only
- **Headers**: HSTS, X-Frame-Options DENY, nosniff, strict referrer policy
- **Compliance**: cdk-nag (AWS Solutions) checks enabled, suppressions documented in code

## Updating

### Code Changes

```bash
# Rebuild backend Docker image + redeploy
cd infra && npx cdk deploy

# Frontend changes only
cd frontend && NEXT_OUTPUT=export pnpm build
cd ../infra && npx cdk deploy
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*"
```

### Model Updates

To change Bedrock models, update the ECS container environment via CDK or directly:

```bash
# In infra/lib/ecs.ts, update the model ID environment variables, then:
cd infra && npx cdk deploy
```

## Teardown

```bash
cd infra
npx cdk destroy
```

The Knowledge Base S3 bucket has `RETAIN` removal policy — it won't be deleted on stack destroy. Delete manually if needed:

```bash
aws s3 rb s3://<KnowledgeBaseBucketName> --force
```

## Troubleshooting

### ECS tasks keep restarting

Check container logs:

```bash
aws logs tail /ecs/ai-lcm-prod --follow
```

Common causes: missing env vars, Bedrock access not granted, incorrect region.

### "Circuit breaker open" errors

The backend circuit breaker trips after repeated Bedrock failures. Check:
1. Bedrock model access is granted in the correct region
2. The ECS task role has `bedrock:InvokeModel` permission (CDK sets this automatically)
3. Bedrock isn't experiencing an outage

### Frontend shows CORS errors

The CDK stack auto-configures `AI_LCM_CORS_ORIGINS` to the CloudFront domain. If using a custom domain, add it via CDK context or update the ECS environment variable.

### DLQ messages accumulating

Check the dead-letter queues for failed tasks:

```bash
aws sqs receive-message --queue-url <DesignDlqUrl> --max-number-of-messages 1
```

CloudWatch alarms will fire when DLQ depth exceeds thresholds.

### Lambda workers timing out

Default timeout is 15 minutes. If IaC generation consistently times out, the Bedrock model may be under heavy load. Check Lambda CloudWatch logs and consider using a faster model.
