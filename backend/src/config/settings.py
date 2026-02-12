from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # AWS
    aws_region: str = "us-east-1"

    # Bedrock Models
    primary_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    lightweight_model_id: str = "us.anthropic.claude-haiku-3-20250310-v1:0"

    # Bedrock Token Limits
    max_tokens: int = 16384          # Generation agents (design, docs)
    interview_max_tokens: int = 4096  # Interview/validation (shorter outputs)
    interview_executor_max_tokens: int = 1024  # Interview executor (Haiku single-shot turns)
    bedrock_max_concurrency: int = 10  # Max concurrent Bedrock API calls (single user peaks ~3-5; throttles abusive concurrency)

    # IaC generation token limits (per-path overrides of max_tokens)
    iac_compose_max_tokens: int = 32768       # Path 2: snippet composition
    iac_fix_max_tokens: int = 32768           # Paths 1 & 2: YAML-level fix fallback

    # Layered IaC generation (Path 3: decomposed pipeline)
    iac_layer_plan_max_tokens: int = 16384    # Architecture planner — LayerPlan JSON grows with layers/imports/exports
    iac_layer_generate_max_tokens: int = 16384  # Per-layer generation (5-15 resources)
    iac_layer_fix_max_tokens: int = 16384     # Per-layer fix calls

    # Bedrock Knowledge Base
    knowledge_base_id: Optional[str] = None

    # Bedrock Guardrails
    guardrail_id: Optional[str] = None
    guardrail_version: str = "DRAFT"

    # DynamoDB
    dynamodb_table: str = "ai-lcm-table"

    # S3
    s3_artifacts_bucket: str = "ai-lcm-artifacts"
    s3_knowledge_base_bucket: str = "ai-lcm-knowledge-base"

    # Cognito
    cognito_user_pool_id: Optional[str] = None
    cognito_client_id: Optional[str] = None

    # Metrics
    metrics_enabled: bool = True  # Publish CloudWatch custom metrics (AI-LCM namespace)

    # Interview Plan
    interview_plan_cache_ttl_minutes: int = 30

    # Design async processing
    sqs_design_queue_url: Optional[str] = None  # If None, run synchronously (local dev)
    websocket_url: Optional[str] = None  # API Gateway WebSocket URL for frontend

    # IaC async processing
    sqs_iac_queue_url: Optional[str] = None  # If None, use local worker (same as design)

    # Documentation async processing
    sqs_docs_queue_url: Optional[str] = None  # If None, use local worker

    # Documentation agent token limits
    docs_diagram_max_tokens: int = 16384         # Architecture diagram generation
    docs_diagram_fix_max_tokens: int = 16384     # Diagram fix attempts
    docs_user_guide_max_tokens: int = 32768       # User guide (~3000 words with tables)
    docs_threat_model_max_tokens: int = 32768     # STRIDE threat model (~3000 words with tables)
    docs_diagram_max_fix_attempts: int = 3       # Max diagram validation-fix iterations

    # Validation settings
    checkov_skip_checks: list[str] = []  # Checkov check IDs to skip (e.g., ["CKV_AWS_23"])
    cfn_guard_binary: str = "cfn-guard"  # Path to cfn-guard CLI binary

    # Storage
    storage_backend: str = "local"  # "local" or "aws"
    session_storage_dir: str = ".local-data/sessions"  # FileSessionManager storage

    # Server
    host: str = "0.0.0.0"  # nosec B104 -- required for Docker/container environments
    port: int = 8000
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]
    trusted_proxy: bool = False  # Trust X-Forwarded-For header (set True when behind ALB/CloudFront)

    model_config = {"env_prefix": "AI_LCM_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
