# Infrastructure

AWS CDK v2 / TypeScript — deploys the complete AI-Deploy platform to AWS.

## Quick Start

```bash
npm install                           # install deps
npm run build                         # compile TypeScript
npx cdk synth                         # emit CloudFormation template
npx cdk deploy                        # deploy to AWS
```

Requires `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION` environment variables, and a bootstrapped CDK environment (`npx cdk bootstrap`).

## Stack Architecture

All resources are deployed in a single `AiDeployStack`. The stack is composed of 12 custom constructs:

```
AiDeployStack
├── KmsConstruct          # Customer-managed encryption key
├── DynamoDbConstruct     # Project metadata + task state
├── S3Construct           # 3 buckets (knowledge base, artifacts, access logs)
├── CognitoConstruct      # User authentication (email + MFA)
├── SqsConstruct          # 3 FIFO queues + 3 DLQs (design, IaC, docs)
├── LambdaConstruct       # 8 Lambda functions (3 workers + 5 WebSocket handlers)
├── WebSocketConstruct    # API Gateway WebSocket for real-time updates
├── EventBridgePipeConstruct  # DynamoDB streams → notification Lambda
├── CloudFrontConstruct   # S3 + CloudFront for frontend static hosting
├── VpcConstruct          # 2-AZ VPC, NAT gateways, VPC endpoints
├── EcsConstruct          # Fargate service + ALB + WAF + auto-scaling
└── AlarmsConstruct       # CloudWatch alarms + dashboard + SNS
```

## Constructs Detail

| Construct | File | Key Resources |
|-----------|------|---------------|
| **KMS** | `lib/kms.ts` | Customer-managed key (auto-rotation), SSM parameter for ARN |
| **DynamoDB** | `lib/dynamodb.ts` | `ai-deploy-table-{env}`, pk/sk + 2 GSIs, PITR, streams, KMS |
| **S3** | `lib/s3.ts` | Knowledge base (versioned, RETAIN), artifacts (90-day lifecycle, DESTROY), access logs |
| **Cognito** | `lib/cognito.ts` | User pool (email, MFA required), web client (SRP auth), custom `tenant_id` attribute |
| **SQS** | `lib/sqs.ts` | 3 FIFO queues (design/IaC/docs) + 3 DLQs, KMS encrypted, content-based dedup |
| **Lambda** | `lib/lambda.ts` | 3 Docker workers (design/IaC/docs from SQS), 5 WebSocket handlers (Python 3.12) |
| **WebSocket** | `lib/websocket.ts` | API Gateway WebSocket (`$connect`, `$disconnect`, `subscribe` routes) |
| **EventBridge** | `lib/eventbridge-pipe.ts` | DynamoDB stream → Lambda, filters for TASK# status changes |
| **CloudFront** | `lib/cloudfront.ts` | S3 origin with OAC, SPA fallback, security headers, HTTPS |
| **VPC** | `lib/vpc.ts` | 2-AZ, dual NAT, flow logs, Bedrock + S3 + DynamoDB VPC endpoints |
| **ECS** | `lib/ecs.ts` | Fargate (2 vCPU, 4 GB), ALB (300s timeout), WAF, auto-scaling (2-10 tasks) |
| **Alarms** | `lib/alarms.ts` | DynamoDB throttle, Bedrock P99 latency, DLQ depth, ECS CPU/memory, ALB 5xx |

## Data Flow

```
Browser → CloudFront (static frontend)
           ↓ API calls
         ALB → ECS Fargate (FastAPI backend)
                 ↓ enqueue
               SQS FIFO queues
                 ↓ consume
               Lambda workers → Bedrock (LLM) → DynamoDB + S3
                                                    ↓ stream
                                                  EventBridge Pipe
                                                    ↓
                                                  Notification Lambda → WebSocket API → Browser
```

## CDK Context Values

Pass via `-c key=value` or set in `cdk.json`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `environment` | string | `dev` | Used in resource names and tags |
| `notificationEmail` | string | — | SNS subscription for alarms |
| `natGateways` | number | 2 | Number of NAT gateways (1 saves cost) |
| `costCenter` | string | `engineering` | Cost allocation tag |
| `owner` | string | `platform-team` | Owner tag |

Example:

```bash
npx cdk deploy -c environment=prod -c notificationEmail=ops@example.com
```

## Stack Outputs

After deployment, the stack exports:

| Output | Description |
|--------|-------------|
| `AlbDnsName` | ALB URL for the backend API |
| `FrontendUrl` | CloudFront URL for the frontend |
| `DistributionId` | CloudFront distribution ID (for cache invalidation) |
| `WebSocketUrl` | WebSocket API URL |
| `UserPoolId` | Cognito User Pool ID |
| `UserPoolClientId` | Cognito Client ID |
| `TableName` | DynamoDB table name |
| `KnowledgeBaseBucketName` | S3 bucket for KB documents |
| `ArtifactsBucketName` | S3 bucket for generated artifacts |

## Security

- **Encryption at rest**: KMS customer-managed key (DynamoDB, S3, CloudWatch, SNS, SQS)
- **Encryption in transit**: TLS 1.3 on ALB (with certificate), HTTPS on CloudFront, SSL enforced on S3
- **Network**: ECS in private subnets, VPC endpoints for AWS services, NAT for outbound
- **WAF**: AWS managed rule groups (common + known bad inputs) + IP rate limiting (1000/5min)
- **Auth**: Cognito MFA required, 12-char password policy, SRP auth only
- **Compliance**: cdk-nag (AWS Solutions) checks enabled, suppressions documented in code

## Testing

```bash
npm run build && npm run test    # 24 CDK template assertions
```

Tests validate: DynamoDB schema/encryption/GSIs, S3 versioning/lifecycle/SSL, Cognito password policy/auth flows, KMS/SSM storage, ECS private subnets/WAF/auto-scaling, Bedrock IAM scoping.

## Cost Estimate (us-east-1)

| Service | Monthly Cost |
|---------|-------------|
| NAT Gateways (2) | ~$64 |
| ECS Fargate (2 tasks, 2 vCPU, 4 GB) | ~$70 |
| ALB | ~$16 |
| DynamoDB (on-demand) | ~$10-50 |
| Lambda | ~$1-5 |
| CloudFront | ~$1-5 |
| S3 + KMS + CloudWatch | ~$5-10 |
| **Total** | **~$170-220** |

Set `natGateways=1` for dev environments to save ~$32/month.

## Commands

```bash
npm install            # install deps
npm run build          # compile TypeScript
npm run watch          # compile on change
npm run test           # Jest tests
npx cdk synth          # emit CloudFormation
npx cdk diff           # diff vs deployed
npx cdk deploy         # deploy stack
npx cdk destroy        # tear down stack
```
