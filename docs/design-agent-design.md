# Design Agent — Async KB-Grounded Architecture Design

---

## 1. Problem Statement

The design agent needs to take the requirements and then generate design options and collect information for the IaC agent to generate code.

### Goal

A production-ready design agent that:
- Generates 3 KB-grounded design options **asynchronously** (SQS + Lambda)
- Notifies the frontend via **WebSocket** when designs are ready (scaling to 500-10K concurrent connections)
- Collects **deployment parameters** after design selection
- Outputs a **machine-actionable schema** that the IaC agent can map directly to CloudFormation parameters
- Maintains **zero hallucination tolerance** by grounding every design decision in KB documents

---

## 2. Architecture Overview

### 2.1 Four-Phase Flow

```
Phase 1: DESIGN GENERATION (Async)
┌────────────────────────────────────────────────────────────────────┐
│                                                                    │
│  Frontend                    Backend API              SQS + Lambda │
│  ────────                    ───────────              ──────────── │
│                                                                    │
│  POST /api/design/submit ──► Create task record ──► Enqueue SQS    │
│  ◄── HTTP 202 + task_id      (DynamoDB: QUEUED)     FIFO message   │
│                                                          │         │
│  ┌─ WebSocket ──────────┐                                ▼         │
│  │ Subscribe to project │    DynamoDB Stream ◄── Lambda Worker     │
│  └──────────────────────┘    ──► EventBridge          │            │
│                              ──► Notification         │ Sonnet     │
│  ◄── WS push: "completed"        Lambda ──► WS push   │ + KB       │
│                                                       │ + WA eval  │
│  GET /api/design/task/{id}                            │            │
│  ◄── 3 DesignOption                                   ▼            │
│                                                   Store result     │
│                                                   (DynamoDB)       │
└────────────────────────────────────────────────────────────────────┘

Phase 2: USER SELECTION
┌────────────────────────────────────────────────────────────────────┐
│  User reviews 3 options (with KB citations + WA scores)            │
│  Selects one option OR requests redesign (async, back to Phase 1)  │
│  POST /api/design/select {option_index}                            │
└────────────────────────────────────────────────────────────────────┘

Phase 3: REFINEMENT (Synchronous)
┌────────────────────────────────────────────────────────────────────┐
│  Based on selected deployment_pattern:                             │
│  1. Query KB for pattern-specific configuration docs               │
│  2. LLM (Haiku) analyzes KB → identifies required params + defaults│
│  3. Frontend shows dynamic form pre-populated with KB defaults     │
│  4. User fills deployment parameters (CIDRs, region, license...)   │
│  POST /api/design/refine {deployment_parameters}                   │
└────────────────────────────────────────────────────────────────────┘

Phase 4: PARAMETER RESOLUTION (Deterministic)
┌────────────────────────────────────────────────────────────────────┐
│  Deterministic code computes all IaC parameters:                   │
│  - Subnet CIDRs from VPC CIDR + AZ count                           │
│  - product interface IPs from subnet CIDRs                       │
│  - AMI ID from region + product version                          │
│  - Security group rules from use case + deployment pattern         │
│  - Route table entries, IAM roles, bootstrap config                │
│  Output: ResolvedIaCParameters → passed to IaC agent               │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 Why These Choices

| Decision | Rationale |
|---|---|
| **Lambda container** (not ECS worker) | 30-120s processing fits Lambda's 15-min limit. Instant scaling, zero ops overhead. Cold starts (3-8s) acceptable for async tasks. |
| **SQS FIFO** (not standard SQS) | MessageGroupId per project prevents duplicate concurrent designs. Content-based deduplication prevents double-submit. |
| **WebSocket from day 1** (not polling) | Sub-second notification. API Gateway WebSocket is fully managed, decoupled from ECS. ~$4/mo at scale. |
| **Hybrid schema** (LLM picks pattern, code computes rest) | LLM is good at selecting deployment patterns and high-level params. Deterministic code is reliable for subnet math, IP assignment. Zero hallucination risk on networking details. |
| **KB-first, no hardcoded enums** | Deployment patterns, subnet roles, interface mappings all come from KB. Adding a new pattern = upload docs to S3. Zero code changes. Models enforce shape, KB provides vocabulary. |
| **Template-first, generate-fallback** | Check if a code template exists in KB for the pattern. If yes, parameterize it. If no, generate novel code from KB architecture docs + component blocks. |
| **Haiku for refinement analysis** (not Sonnet) | Refinement is a focused single-turn task: "given this KB content, what parameters are needed?" Haiku handles this well at lower cost/latency. |
| **1-2 KBs** (flexible) | Option A: Single KB with `document_type` metadata to separate prose from code. Option B: Design KB (prose, vector search) + Code KB (templates, larger chunks). |

---

## 3. Data Models

### 3.0 Design Principle: Schema Enforces Grammar, KB Provides Vocabulary

**No hardcoded enums for business values.** The models below enforce *shape* (a design has
VPCs, VPCs have subnets, products have interfaces) but never *enumerate* which deployment
patterns, subnet roles, or interface mappings are valid. Those come from the KB.

Adding a new deployment pattern = uploading documents to S3 + re-syncing KB. Zero code changes.

The only code-level enums are for truly fixed infrastructure concepts (task status).

### 3.1 Topology Blueprints — Structural Contracts

These models define the **grammar** of a product deployment. The LLM fills them with
values sourced from KB architecture docs. The parameter resolver reads them to know
exactly what to compute.

```python
# backend/src/models/design.py

class VPCBlueprint(BaseModel):
    """A VPC needed in the design. Shape is code-enforced; values are KB-sourced."""
    role: str = Field(description="VPC purpose — KB-sourced (e.g., 'security', 'inspection', 'spoke')")
    subnet_roles: list[str] = Field(
        description="Subnet types per AZ — KB-sourced (e.g., ['public', 'private', 'ha-sync', 'ha-mgmt'])"
    )
    availability_zones: int = Field(ge=1, le=2)


class InterfaceBlueprint(BaseModel):
    """A product network interface — maps port to subnet role."""
    port_name: str = Field(description="product port (e.g., 'port1', 'port2')")
    subnet_role: str = Field(description="Which subnet role this port connects to")
    description: str = Field(description="Interface purpose (e.g., 'External/WAN', 'HA heartbeat')")


class productBlueprint(BaseModel):
    """A product instance's placement and interface layout."""
    role: str = Field(description="Instance role — KB-sourced (e.g., 'active', 'passive', 'target')")
    vpc_role: str = Field(description="Which VPC (by role) this product belongs to")
    interfaces: list[InterfaceBlueprint]


class KBReference(BaseModel):
    """Citation from knowledge base grounding a design decision."""
    source_uri: str = Field(description="S3 URI of the KB document")
    excerpt: str = Field(description="Relevant excerpt (max 500 chars)")
    relevance_score: float = Field(ge=0.0, le=1.0)
```

### 3.2 DesignOption — LLM-Generated

```python
class DesignOption(BaseModel):
    """A single architecture design option.

    Shape is enforced by code. Values are sourced from KB.
    No enum constraints on deployment_pattern, ha_mode, etc. — the KB
    defines what patterns exist, and the LLM selects from KB content.
    """

    # --- Human-readable (displayed in UI) ---
    name: str
    description: str
    architecture_summary: str
    pros: list[str] = Field(min_length=2)
    cons: list[str] = Field(min_length=2)
    estimated_monthly_cost_usd: float
    security_posture_rating: int = Field(ge=1, le=5)
    complexity_rating: int = Field(ge=1, le=5)

    # --- Machine-actionable (KB-sourced values, code-enforced shape) ---
    deployment_pattern: str = Field(
        description="KB-sourced pattern name (e.g., 'hub-spoke', 'gwlb-transit', 'ha-dual-az'). "
        "NOT an enum — valid values come from KB architecture docs."
    )
    use_case: str = Field(description="Primary use case (e.g., 'realtime-inference', 'batch-inference', 'training')")
    ha_mode: str = Field(description="HA configuration (e.g., 'active-passive', 'active-active', 'standalone')")
    product_instance_type: str = Field(description="EC2 instance type from KB sizing (e.g., 'g5.xlarge', 'p4d.24xlarge')")
    aws_services: list[str] = Field(description="All AWS services used")

    # --- Topology blueprints (the key structural output) ---
    vpc_topology: list[VPCBlueprint] = Field(
        description="VPCs needed, with subnet roles per VPC. Drives parameter resolver."
    )
    product_topology: list[productBlueprint] = Field(
        description="product instances with interface-to-subnet mappings. Drives IP assignment."
    )

    # --- Code template match ---
    has_code_template: bool = Field(
        default=False,
        description="True if a matching code template was found in KB/S3"
    )
    template_s3_prefix: str | None = Field(
        default=None,
        description="S3 prefix of the matching code template (e.g., 'sd-wan/hub-spoke/code/')"
    )

    # --- KB grounding (mandatory) ---
    kb_references: list[KBReference] = Field(
        min_length=1,
        description="KB documents that informed this design. At least 1 required."
    )
    well_architected_assessment: dict[str, str] | None = Field(
        default=None,
        description="Per-pillar WA scores: {'security': 'PASS: ...', 'reliability': 'REVIEW: ...', ...}"
    )


class DesignRecommendation(BaseModel):
    """3 design options with a recommendation."""

    options: list[DesignOption] = Field(min_length=3, max_length=3)
    recommended_option_index: int = Field(ge=0, le=2)
    rationale: str
    requirements_summary: str

    # Available templates discovered during generation
    available_templates: list[str] = Field(
        default_factory=list,
        description="S3 prefixes of all code templates found for these use cases"
    )
```

### 3.3 DeploymentParameters — User-Provided (after selection)

The base fields are universally required for any AWS deployment.
Everything else is **dynamic** — driven by KB configuration docs for the selected pattern.
No hardcoded pattern-specific fields (no `existing_tgw_id`, no `customer_vpc_cidrs` in the model).

```python
class DeploymentParameters(BaseModel):
    """Deployment parameters collected from user after design selection.

    Base fields are always required. Pattern-specific fields live in
    additional_parameters — their names and types are determined at runtime
    by the RefinementPlan (which is generated from KB configuration docs).
    """

    # --- Always required (universal to any AWS deployment) ---
    aws_region: str = Field(description="AWS region (e.g., us-east-1)")
    vpc_cidr: str = Field(description="Primary VPC CIDR (e.g., 10.0.0.0/16)")
    environment: str = Field(default="production", description="dev, staging, production")
    project_name: str = Field(description="Project name for resource naming and tagging")

    # --- Pattern-specific (KB-driven, collected via RefinementPlan) ---
    additional_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Pattern-specific parameters. Keys and types determined by RefinementPlan "
        "which is generated from KB configuration docs. Examples: existing_tgw_id, "
        "customer_vpc_cidrs, tgw_asn, license_type, product_version, admin_cidr."
    )
```

### 3.4 RefinementPlan — LLM-Generated (identifies what to collect)

Generated by Haiku analyzing KB configuration docs + the selected design's template
Parameters section (if a code template exists). This is entirely KB-driven — no hardcoded
`PATTERN_REQUIRED_PARAMS` mapping.

```python
class RefinementField(BaseModel):
    """A single parameter to collect during refinement."""
    field_name: str = Field(description="Key for DeploymentParameters.additional_parameters")
    label: str = Field(description="Human-readable form label")
    description: str = Field(description="Help text from KB configuration docs")
    required: bool = True
    default_value: str | None = Field(default=None, description="KB-derived default")
    default_rationale: str | None = Field(default=None, description="Why this default (from KB)")
    input_type: str = Field(default="text", description="text, select, cidr, number")
    options: list[str] | None = Field(default=None, description="For 'select' type")
    validation_pattern: str | None = Field(default=None, description="Regex for validation")


class RefinementPlan(BaseModel):
    """Plan for collecting deployment parameters.

    Generated by Haiku + KB analysis. If a code template exists, Haiku also
    reads the template's Parameters section to identify required CloudFormation
    parameters that need user input.
    """
    fields: list[RefinementField]
    kb_configuration_notes: str = Field(
        description="Configuration guidance summary from KB"
    )
    template_parameters_found: list[str] = Field(
        default_factory=list,
        description="CloudFormation parameter names found in the template's Parameters section"
    )
    kb_references: list[KBReference] = Field(default_factory=list)
```

### 3.5 ResolvedIaCParameters — Deterministic Output (to IaC agent)

Structural models for the resolved output. The resolver reads `vpc_topology` and
`product_topology` from the design to know exactly what to compute.
Pattern-specific extras (TGW config, GWLB config) go in `additional_resolved`.

```python
class SubnetSpec(BaseModel):
    """A resolved subnet with computed CIDR."""
    name: str                           # "public-az1", "ha-sync-az1"
    role: str                           # From VPCBlueprint.subnet_roles
    cidr: str                           # Computed (e.g., "10.0.1.0/24")
    availability_zone: str              # "us-east-1a"


class ResolvedVPC(BaseModel):
    """A fully resolved VPC."""
    name: str                           # "{project}-{role}-vpc"
    role: str                           # From VPCBlueprint.role
    cidr: str                           # User-provided or computed
    subnets: list[SubnetSpec]


class ResolvedInterface(BaseModel):
    """A product interface with assigned IP."""
    port_name: str                      # "port1"
    subnet_name: str                    # References SubnetSpec.name
    private_ip: str                     # Computed (e.g., "10.0.1.11")
    description: str                    # From InterfaceBlueprint
    source_dest_check: bool = False


class Resolvedproduct(BaseModel):
    """A fully resolved product instance."""
    name: str                           # "{project}-fgt-active"
    role: str                           # From productBlueprint.role
    instance_type: str
    availability_zone: str
    interfaces: list[ResolvedInterface]


class ResolvedIaCParameters(BaseModel):
    """Complete, resolved parameters for IaC generation.

    The IaC agent receives this + optionally the code template from S3.
    If a template exists: parameterize it with these values.
    If no template: generate code from KB architecture docs using these as constraints.
    """

    # --- Identity ---
    project_name: str
    environment: str
    region: str
    availability_zones: list[str]

    # --- Network ---
    vpcs: list[ResolvedVPC]

    # --- product ---
    product_instances: list[Resolvedproduct]

    # --- Code template ---
    code_template_s3_prefix: str | None = None   # If template match exists
    code_template_files: dict[str, str] | None = None  # Fetched template content

    # --- Pattern-specific extras (KB-driven, not hardcoded) ---
    additional_resolved: dict[str, Any] = Field(
        default_factory=dict,
        description="Pattern-specific resolved values. Examples: tgw_asn, gwlb_cross_zone, "
        "license_type, product_version, admin_cidr. Keys come from RefinementPlan fields."
    )

    # --- Tags ---
    tags: dict[str, str] = Field(default_factory=dict)

    # --- Traceability ---
    design_option_name: str
    deployment_pattern: str
    requirements_hash: str
```

### 3.6 DesignTask — Async Task Record (DynamoDB)

```python
# backend/src/models/design.py

class DesignTaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DesignTask(BaseModel):
    """Async design generation task tracked in DynamoDB.

    DynamoDB key schema:
      PK: TENANT#{tenant_id}
      SK: TASK#{task_id}
    """
    task_id: str
    tenant_id: str
    project_id: str
    task_type: str = "design"           # or "redesign"
    status: DesignTaskStatus = DesignTaskStatus.QUEUED
    submitted_at: str                   # ISO timestamp
    started_at: str | None = None
    completed_at: str | None = None

    # Input (stored for Lambda to read)
    requirements_json: str = Field(description="Serialized InterviewOutput")
    feedback: str | None = None         # For redesign tasks
    previous_options_json: str | None = None  # For redesign tasks

    # Output (populated by Lambda worker)
    result: dict | None = None          # DesignRecommendation dict
    error_message: str | None = None    # If status == FAILED

    # TTL for automatic cleanup
    ttl: int | None = None              # Epoch timestamp, 7 days from submission
```

---

## 4. Async Processing Architecture

### 4.1 SQS FIFO Queue

```
Queue Name:          ai-deploy-design-tasks.fifo
Message Group ID:    {tenant_id}#{project_id}     ← one design per project at a time
Deduplication:       Content-based                 ← prevents double-submit
Visibility Timeout:  300 seconds                   ← 2.5× max processing time
Message Retention:   4 days
DLQ:                 ai-deploy-design-dlq.fifo
  Max Receive Count: 3                             ← after 3 failures, send to DLQ
```

**Message Body:**
```json
{
  "task_id": "uuid",
  "tenant_id": "tenant-123",
  "project_id": "project-456",
  "task_type": "design",
  "requirements": { ... },
  "feedback": null,
  "previous_options": null
}
```

### 4.2 Lambda Worker

```
Function Name:       ai-deploy-design-worker
Runtime:             Container image (from backend Dockerfile.lambda)
Handler:             src.workers.design_worker.handler
Memory:              2048 MB (I/O-bound Bedrock calls, module-level client init)
Timeout:             300 seconds
Reserved Concurrency: 10 (prevent Bedrock throttling)
Event Source:        SQS FIFO queue (batch size: 1)
Environment:         Same as ECS task (AI_DEPLOY_* variables)
```

**Worker Flow:**

The worker calls `process_design_task()` from `backend/src/services/design_processing.py`.
This is the **same function** used by both the Lambda worker and the local dev worker.
WebSocket notification is **not** the worker's responsibility — the DynamoDB status write
triggers the EventBridge Pipe → notification bridge pipeline (§5.3).

```python
# backend/src/workers/design_worker.py
def handler(event, context):
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        task_id = body.get("task_id", "?")
        logger.info("Received SQS message for task %s", task_id)
        try:
            process_design_task(body, notify_fn=None)
        except Exception:
            logger.exception("Failed to process design task %s", task_id)
            try:
                mark_task_failed(body, notify_fn=None)
            except Exception:
                logger.exception(
                    "Failed to mark task %s as FAILED — allowing SQS retry", task_id
                )
                raise  # Re-raise so SQS retries (FAILED status not persisted)
```

**Shared Processing Pipeline** (`backend/src/services/design_processing.py`):
```python
def process_design_task(body: dict, notify_fn: Callable | None = None):
    """Shared by Lambda worker (AWS) and local worker (dev).

    Steps:
    1. Update task status → PROCESSING
    2. Reconstruct InterviewOutput from body["requirements"]
    3. Discover available code templates via discover_template_summary()
    4. Build agent prompt via build_agent_prompt()
    5. Invoke design_agent() with template summary + circuit breaker
    6. Extract DesignRecommendation via extract_recommendation()
    7. Save result to DynamoDB (status=COMPLETED)
    8. Save design to project storage
    9. Clear project.active_design_task_id
    10. Call notify_fn() if provided (local dev mode only)
    """
```

### 4.3 Task Lifecycle

```
Client POST /api/design/submit
  │
  ▼
┌──────────────────────┐
│  QUEUED              │ ← Task record in DynamoDB, message in SQS (or local queue)
│                      │   Project.active_design_task_id = task_id
└──────────┬───────────┘
           │ Lambda (or local worker) picks up message
           ▼
┌──────────────────────┐
│  PROCESSING          │ ← Worker running Sonnet + KB search
└──────────┬───────────┘
           │ Success or failure
           ▼
┌──────────────────────┐     ┌──────────────────────┐
│  COMPLETED           │ OR  │  FAILED              │
│  (result stored)     │     │  (error stored)      │
│  (design saved to    │     │  → retried up to 3×  │
│  project storage)    │     │  → then DLQ          │
│  (active_task_id     │     └──────────────────────┘
│   cleared)           │
└──────────────────────┘
```

### 4.4 Design Result Persistence & Hydration

Design results are persisted in **two locations** for different access patterns:

```
On COMPLETED:
  1. Task record (DynamoDB):
     PK: TENANT#{tenant_id}, SK: TASK#{task_id}
     result: DesignRecommendation dict        ← for GET /api/design/task/{id} polling

  2. Project step storage (DynamoDB inline):
     PK: TENANT#{tenant_id}, SK: PROJECT#{project_id}
     design_json: serialized DesignRecommendation  ← for page reload hydration

  3. Project metadata:
     active_design_task_id: null (cleared)     ← signals "no in-flight task"
```

**Why two storage locations?**
- The **task record** is ephemeral (7-day TTL) and keyed by task_id — used for polling during generation
- The **project step** is durable and keyed by project_id — used for hydration when users return later

**Frontend hydration on page reload** (`useWizardState.ts`):
```
1. GET /api/projects/{id}/state → returns { design: DesignRecommendation | null, ... }
2. If design exists:     → HYDRATE with recommendation → show DesignReview (skip generation)
3. If design is null AND active_design_task_id exists:
   → HYDRATE with designTaskId → resume polling (task still in-flight)
4. If design is null AND no active_task_id:
   → HYDRATE with no design → user must trigger generation
```

**This ensures users can navigate away at any point and return without re-running the design agent.**

**Local dev gap:** If the backend process restarts while a local worker task is in-flight,
the in-memory `queue.Queue` task is lost. The frontend will poll for 3 minutes then timeout.
In AWS (SQS/Lambda), tasks are durable in the queue — Lambda picks up where it left off.

---

## 5. WebSocket Notification Architecture

### 5.0 Scaling Target

**Single-region, 500–10,000 concurrent WebSocket connections.**

API Gateway WebSocket API is the right choice at this scale. Key limits to address:

| Limit | Default | Action |
|---|---|---|
| Concurrent connections per API | 500 | Request increase to 10,000 via AWS Service Quotas |
| Idle connection timeout | 10 min | Heartbeat Lambda pings every 5 min |
| Message payload size | 128 KB | Sufficient (task_status messages are <1 KB) |
| New connections per second | 500/s | Sufficient (connection bursts are rare) |
| Route integration timeout | 29 sec | Sufficient (handlers are <1s) |

### 5.1 API Gateway WebSocket API

```
Endpoint:            wss://{api-id}.execute-api.{region}.amazonaws.com/{stage}
Routes:
  $connect    → Lambda: ws-connect     (store connection + tenant mapping)
  $disconnect → Lambda: ws-disconnect  (remove connection record)
  subscribe   → Lambda: ws-subscribe   (subscribe to project events)
```

**Implementation files:**
- `backend/lambdas/ws/ws_connect.py`
- `backend/lambdas/ws/ws_disconnect.py`
- `backend/lambdas/ws/ws_subscribe.py`
- `backend/lambdas/ws/ws_heartbeat.py`
- `backend/lambdas/ws/ws_notification_bridge.py`
- `infra/lib/websocket.ts`

### 5.2 Connection Tracking (DynamoDB)

Uses the existing `ai-deploy-table` with two item patterns:

```
# Connection record (created on $connect)
PK: WS#{connection_id}
SK: CONNECTION
  connection_id: str
  connected_at: ISO timestamp
  ttl: epoch + 2h (auto-cleanup stale connections)

# Subscription record (created on subscribe action)
PK: WS#{connection_id}
SK: SUB#{tenant_id}#{project_id}
  connection_id: str
  project_id: str
  tenant_id: str
  gsi2pk: SUB#{tenant_id}#{project_id}
  gsi2sk: WS#{connection_id}
  subscribed_at: ISO timestamp
  ttl: epoch + 2h
```

The `WS#{connection_id}` partition key groups all items for a single connection,
enabling efficient disconnect cleanup via `query(pk=WS#{connection_id})`.

**GSI2 for subscription fan-out** (REQUIRED for 500-10K scale):
```
GSI: GSI2
  PK: gsi2pk = SUB#{tenant_id}#{project_id}   ← find all connections for a project
  SK: gsi2sk = WS#{connection_id}
  Projection: ALL
```

This GSI enables O(1) lookup of all connections subscribed to a specific project,
instead of scanning the table. Critical for fan-out when multiple users watch the same project.

### 5.3 Notification Pipeline (EventBridge Pipe)

The notification pipeline is **decoupled** from the design worker. The worker writes
to DynamoDB; the notification fires automatically via DynamoDB Stream → EventBridge Pipe.

**Why EventBridge Pipe (not worker-direct)?**
- If worker Lambda crashes after DynamoDB write but before WS push, notification still fires
- Any future status change source (admin console, another service) gets free notifications
- Separates "write result" concern from "notify subscribers" concern
- EventBridge Pipe has built-in filtering, batching, and error handling with DLQ

```
DynamoDB Stream (table: ai-deploy-table)
  │
  │ Stream view type: NEW_AND_OLD_IMAGES (required for status change detection)
  │
  ▼
EventBridge Pipe (ai-deploy-design-notification-pipe)
  │
  │ Source filter (DynamoDB Stream):
  │   eventName: MODIFY
  │   dynamodb.Keys.sk.S prefix: "TASK#"
  │   dynamodb.NewImage.status.S IN ["completed", "failed"]
  │   dynamodb.OldImage.status.S != dynamodb.NewImage.status.S
  │
  │ Enrichment: None (Lambda target receives filtered stream record directly)
  │
  ▼
Lambda: ws-notification-bridge (ai-deploy-ws-notification-bridge)
  │
  │ 1. Extract task_id, tenant_id, project_id, status from stream NewImage
  │ 2. Skip if NewImage.status == OldImage.status (no actual change)
  │ 3. Query GSI2: gsi2pk = SUB#{tenant_id}#{project_id}
  │ 4. For each subscribed connection_id:
  │    a. POST to API Gateway Management API (@connections/{connectionId})
  │    b. If GoneException (stale connection):
  │       - Query all items with pk=WS#{connectionId}
  │       - Batch-delete connection + all subscription records
  │       - Log warning, continue to next connection
  │
  ▼
WebSocket push to frontend
```

**WebSocket Message Format:**
```json
{
  "type": "design_status",
  "task_id": "abc-123",
  "project_id": "project-456",
  "tenant_id": "default",
  "status": "completed",
  "timestamp": "2026-02-25T15:30:00Z"
}
```

### 5.4 Heartbeat Lambda (Stale Connection Management)

API Gateway drops WebSocket connections after **10 minutes of inactivity**.
A scheduled Lambda proactively pings all connections and cleans up stale ones.

```
Function Name:       ai-deploy-ws-heartbeat
Implementation:      backend/lambdas/ws/ws_heartbeat.py
Trigger:             EventBridge Scheduler — every 5 minutes
Memory:              256 MB
Timeout:             2 minutes

Flow:
1. Scan DynamoDB: filter pk begins_with("WS#") AND sk = "CONNECTION" (paginated)
2. For each connection_id:
   a. POST heartbeat ping to API Gateway Management API (@connections/{connectionId})
   b. If GoneException: query all pk=WS#{connectionId} items, batch-delete all
   c. If success: connection is alive, no action
3. Record CloudWatch metrics:
   - WsActiveConnections (count of alive connections)
   - WsStaleConnectionsCleaned (count of removed connections)
```

**Why not rely on TTL alone?**
- TTL can be delayed up to 48 hours after expiration (DynamoDB best-effort)
- Stale connections cause `GoneException` on every notification attempt
- Proactive cleanup keeps the subscription index clean → faster fan-out queries
- Heartbeat also serves as a keep-alive to prevent API Gateway idle timeout

### 5.5 Frontend WebSocket Integration

**Implementation file:** `frontend/src/hooks/useWebSocket.ts`

The frontend maintains a WebSocket connection with:
- Automatic reconnection with exponential backoff (1s, 2s, 4s, 8s, max 30s)
- Server-side heartbeat every 5 min (via heartbeat Lambda) keeps connection alive
- Polling fallback: if `NEXT_PUBLIC_WEBSOCKET_URL` is not set or WebSocket fails,
  fall back to `GET /api/design/task/{id}` every 3s (DESIGN_POLL_INTERVAL_MS)
- Max polling attempts: 60 (3 min timeout before giving up)
- Subscribe on entering design step with `{action: "subscribe", project_id, tenant_id}`
- WebSocket takes priority: when WS connects, polling timer is cancelled
- On WS disconnect, polling resumes automatically

**Connection lifecycle:**
```
Mount → connect WS → send subscribe → wait for task_status messages
  │                                          │
  │ WS disconnects                           │ Received "completed"/"failed"
  ▼                                          ▼
Start polling GET /task/{id}        Dispatch DESIGN_TASK_UPDATE → load result
```

---

## 6. Knowledge Base Architecture

### 6.0 Core Principle: KB Is the Source of Truth

The KB contains **both prose and code**:
- **Prose docs** (architecture, sizing, configuration, best-practices) → discovered via vector search
- **Code templates** (CloudFormation YAML templates) → discovered via S3 listing, fetched via direct S3 read

Adding a new deployment pattern = uploading docs + templates to S3 + re-syncing KB.

### 6.1 KB S3 Naming Convention

```
s3://ai-deploy-knowledge-base/
│
├── {use_case}/                           # sd-wan, egress, ingress, inspection
│   ├── {deployment_type}/                # hub-spoke, dual-hub, single-az, gwlb, etc.
│   │   │
│   │   ├── architecture.md               # Reference architecture (prose)
│   │   ├── architecture.md.metadata.json # Sidecar: use_case, deployment_type, document_type
│   │   ├── configuration.md              # Configuration guidance + required params
│   │   ├── configuration.md.metadata.json
│   │   ├── sizing.md                     # Instance sizing, cost estimates
│   │   ├── sizing.md.metadata.json
│   │   ├── best-practices.md             # WA alignment, compliance (optional)
│   │   │
│   │   └── code/                         # CloudFormation templates (NOT in KB vector index)
│   │       ├── template.yaml             # THE MAIN TEMPLATE — includes Parameters section
│   │       ├── networking.yaml           # Nested stack or snippet (optional)
│   │       ├── product.yaml            # Nested stack or snippet (optional)
│   │       └── security-groups.yaml      # Nested stack or snippet (optional)
│   │
│   └── {deployment_type_2}/
│       └── ...
│
└── components/                           # Reusable CloudFormation building blocks
    ├── vpc/
    │   └── snippet.yaml
    ├── product-ha/
    │   └── snippet.yaml
    ├── gwlb/
    │   └── snippet.yaml
    └── transit-gateway/
        └── snippet.yaml
```

**Metadata sidecar format** (for Bedrock KB filtering):
```json
{
  "metadataAttributes": {
    "use_case": {"value": "sd-wan", "type": "STRING"},
    "deployment_type": {"value": "hub-spoke", "type": "STRING"},
    "document_type": {"value": "architecture", "type": "STRING"}
  }
}
```

**Important**: Code templates in `code/` subfolders are **excluded from KB vector indexing**
(via S3 data source path filter or separate bucket). They're retrieved via direct S3 read,
not vector search, to avoid chunking CloudFormation templates mid-resource.

**Option**: Use 2 KBs — one for prose (vector search), one for CloudFormation templates (direct S3 or larger
chunk size). This is a deployment-time choice, not an architecture change.

### 6.2 Template Discovery: Template-First, Generate-Fallback

Before the design agent runs, a pre-processing step discovers available templates:

```python
# backend/src/tools/template_discovery.py

def discover_templates(use_cases: list[str], s3_bucket: str) -> dict[str, list[TemplateInfo]]:
    """List available CloudFormation templates for given use cases.

    Returns: {use_case: [TemplateInfo(deployment_type, s3_prefix, has_parameters), ...]}

    S3 listing: s3://{bucket}/{use_case}/*/code/template.yaml
    If template.yaml exists in a deployment_type's code/ folder → template available.
    """

class TemplateInfo(BaseModel):
    use_case: str
    deployment_type: str
    s3_prefix: str            # "sd-wan/hub-spoke/code/"
    template_files: list[str] # ["template.yaml", "networking.yaml", "product.yaml", ...]
```

**Template discovery is injected into the design agent's prompt:**
```
Available CloudFormation templates for your use cases:
- sd-wan/hub-spoke/code/ (template.yaml, networking.yaml, product.yaml, security-groups.yaml)
- sd-wan/dual-hub/code/ (template.yaml, networking.yaml, product.yaml)
- inspection/centralized/code/ (template.yaml, product.yaml, gwlb.yaml)

If a template matches your design, set has_code_template=true and template_s3_prefix accordingly.
If no template matches (novel pattern), set has_code_template=false. The IaC agent will generate
CloudFormation code from KB architecture docs + component building blocks.
```

### 6.3 Design Generation — Hierarchical KB Search

```python
# Pre-processing (before agent invocation):

# Step 1: Discover available code templates
available = discover_templates(requirements.use_cases, kb_bucket)

# Step 2: Hierarchical KB search per use case
kb_context = {}
for uc in requirements.use_cases:
    kb_context[uc] = {
        "architecture": kb_search_filtered(
            query=f"product {uc} deployment architecture AWS",
            use_case=uc, document_type="architecture", max_results=5,
        ),
        "sizing": kb_search_filtered(
            query=f"product {uc} instance sizing {requirements.bandwidth}Mbps",
            use_case=uc, document_type="sizing", max_results=3,
        ),
        "best_practices": kb_search_filtered(
            query=f"product {uc} AWS best practices {requirements.resilience}",
            use_case=uc, document_type="best-practices", max_results=3,
        ),
    }

# Step 3: Inject both into design agent prompt
prompt = build_design_prompt(requirements, kb_context, available)
```

The structured output schema requires `kb_references: list[KBReference]` with `min_length=1`
per option, forcing the LLM to cite KB sources.

### 6.4 Refinement — KB-Driven Parameter Discovery

After design selection, parameter requirements are discovered from KB (not hardcoded):

```python
# Step 1: If code template exists → read Parameters section from template.yaml
if selected_design.has_code_template:
    template_yaml = s3_read(f"{selected_design.template_s3_prefix}template.yaml")
    # Parse YAML → extract Parameters section (names, types, defaults, descriptions)

# Step 2: KB search for configuration docs
config_results = kb_search_filtered(
    query=f"product {selected_design.deployment_pattern} configuration parameters",
    use_case=selected_design.use_case,
    deployment_type=selected_design.deployment_pattern,
    document_type="configuration",
    max_results=5,
)

# Step 3: Haiku generates RefinementPlan from:
#   - CloudFormation Parameters section (if template exists)
#   - KB configuration docs
#   - Selected design's topology (vpc_topology, product_topology)
```

### 6.5 Validation

Post-generation validation (no enum checking, structural + grounding checks):
- Every `DesignOption` has at least 1 `kb_reference` (non-empty citations)
- Every `product_topology[].interfaces[].subnet_role` references a role in `vpc_topology[].subnet_roles` (cross-referential consistency)
- `has_code_template=true` ⟹ `template_s3_prefix` is non-null and S3 path exists
- `vpc_topology` is non-empty (every design needs at least one VPC)

---

## 7. Refinement Phase (Detailed)

### 7.1 Flow

```
User selects design option (e.g., option index 1: "GWLB-Transit")
  │
  ▼
POST /api/design/select
  │ Store: approved_design_index = 1
  │
  ▼
Backend: generate_refinement_plan()
  │
  │ 1. If has_code_template → read template.yaml from S3
  │    (Parse YAML → extract Parameters section: names, types, defaults, descriptions)
  │
  │ 2. Query KB for pattern-specific configuration docs
  │    kb_search_filtered(deployment_type=pattern, document_type="configuration")
  │
  │ 3. Haiku analyzes BOTH sources → generates RefinementPlan
  │    - Template Parameters section tells us what the CloudFormation stack needs
  │    - KB configuration docs provide defaults and guidance
  │    - Base fields (region, vpc_cidr, environment, project_name) always included
  │
  ▼
Return RefinementPlan to frontend
  │
  ▼
Frontend: DeploymentParametersForm
  │ Dynamic form rendered from RefinementPlan.fields
  │ Pre-populated with KB defaults
  │ Client-side CIDR/region validation
  │
  ▼
User fills form and submits
  │
  ▼
POST /api/design/refine {deployment_parameters}
  │
  ▼
Backend: ParameterResolver.resolve()
  │ Reads vpc_topology + product_topology from design
  │ Computes subnets, IPs, tags from user params + topology
  │ If template: fetches all template files from S3
  │ Store resolved params in DynamoDB
  │
  ▼
Frontend: proceed to IaC generation step
```

### 7.2 Parameter Discovery — KB-Driven, Not Hardcoded

**No `PATTERN_REQUIRED_PARAMS` mapping.** The required parameters for each pattern are
discovered at runtime from two sources:

**Source A: Template Parameters section** (if code template exists)
```yaml
# Example: sd-wan/hub-spoke/code/template.yaml — Parameters section
Parameters:
  Region:
    Type: String
    Description: AWS region for deployment
  Project:
    Type: String
    Description: Project name for resource naming
  Environment:
    Type: String
    Default: production
  VPCCIDR:
    Type: String
    Default: "10.0.0.0/16"
    Description: Primary VPC CIDR block
  LicenseType:
    Type: String
    Default: payg
    AllowedValues: [payg, byol]
  InstanceType:
    Type: String
    Default: c5.xlarge
  AdminPort:
    Type: Number
    Default: 443
  KeyName:
    Type: AWS::EC2::KeyPair::KeyName
  TGWASN:
    Type: Number
    Default: 64512
```
→ Haiku reads the Parameters section and generates RefinementField entries for each parameter.

**Source B: KB configuration docs** (always available)
```markdown
# Example: sd-wan/hub-spoke/configuration.md
## Deployment Parameters
- **VPC CIDR**: Recommended /16 for hub deployments (e.g., 10.0.0.0/16)
- **Transit Gateway ASN**: Default 64512, must not conflict with on-prem BGP ASN
- **License Type**: BYOL recommended for production (cost savings with 1+ year commitment)
- **Admin CIDR**: Restrict to corporate IP range for security
```
→ Haiku extracts defaults and rationale from prose.

**Haiku merges both** into a `RefinementPlan` with KB-grounded defaults.

---

## 8. Parameter Resolver (Deterministic)

### 8.1 Responsibility

The `ParameterResolver` reads the **topology blueprints** from the design option to know
what to compute. It doesn't need to know what "gwlb-transit" means — it just follows the
blueprint's VPC/subnet/interface specifications.

```
Inputs:
  DesignOption.vpc_topology         — What VPCs and subnet roles to create
  DesignOption.product_topology   — What interfaces to assign IPs to
  DeploymentParameters                — User-provided CIDR, region, extras
  InterviewOutput                     — Original requirements (for tagging)

Output:
  ResolvedIaCParameters               — Computed subnets, IPs, and template files
```

### 8.2 Blueprint-Driven Computation

The resolver is **pattern-agnostic**. It doesn't branch on deployment_pattern.
Instead, it iterates over the structural blueprints:

```python
class ParameterResolver:
    def resolve(self, design: DesignOption, params: DeploymentParameters,
                requirements: InterviewOutput) -> ResolvedIaCParameters:

        azs = self._resolve_azs(params.aws_region, design.vpc_topology[0].availability_zones)

        # Resolve each VPC from its blueprint
        vpcs = []
        for vpc_bp in design.vpc_topology:
            subnets = self._compute_subnets(params.vpc_cidr, azs, vpc_bp.subnet_roles)
            vpcs.append(ResolvedVPC(
                name=f"{params.project_name}-{vpc_bp.role}-vpc",
                role=vpc_bp.role,
                cidr=params.vpc_cidr,
                subnets=subnets,
            ))

        # Resolve each product from its blueprint
        fgts = []
        for i, fgt_bp in enumerate(design.product_topology):
            vpc = next(v for v in vpcs if v.role == fgt_bp.vpc_role)
            interfaces = self._assign_interfaces(vpc.subnets, fgt_bp.interfaces, fgt_index=i)
            fgts.append(Resolvedproduct(
                name=f"{params.project_name}-fgt-{fgt_bp.role}",
                role=fgt_bp.role,
                instance_type=design.product_instance_type,
                availability_zone=azs[i % len(azs)],
                interfaces=interfaces,
            ))

        # Fetch code template if available
        template_files = None
        if design.has_code_template and design.template_s3_prefix:
            template_files = self._fetch_template(design.template_s3_prefix)

        return ResolvedIaCParameters(
            project_name=params.project_name,
            environment=params.environment,
            region=params.aws_region,
            availability_zones=azs,
            vpcs=vpcs,
            product_instances=fgts,
            code_template_s3_prefix=design.template_s3_prefix,
            code_template_files=template_files,
            additional_resolved=params.additional_parameters,  # Pass-through KB-driven extras
            tags={"Project": params.project_name, "Environment": params.environment,
                  "ManagedBy": "ai-deploy", "DeploymentPattern": design.deployment_pattern},
            design_option_name=design.name,
            deployment_pattern=design.deployment_pattern,
            requirements_hash=hashlib.sha256(requirements.model_dump_json().encode()).hexdigest()[:12],
        )
```

### 8.3 Key Computations (Pattern-Agnostic)

**Subnet CIDR Math** — works for ANY set of subnet roles:
```python
def _compute_subnets(self, vpc_cidr: str, azs: list[str], subnet_roles: list[str]) -> list[SubnetSpec]:
    """Divide VPC CIDR into /24 subnets: one per (role × AZ).

    The subnet_roles list comes from VPCBlueprint — could be
    ["public", "private"] or ["public", "private", "ha-sync", "ha-mgmt", "tgw-attach"]
    or anything else the KB defines. The resolver doesn't care what the roles mean.
    """
    network = ipaddress.ip_network(vpc_cidr)
    subnets_iter = network.subnets(new_prefix=24)
    next(subnets_iter)  # Skip .0.0/24 (reserved)

    result = []
    for role in subnet_roles:
        for az in azs:
            subnet = next(subnets_iter)
            result.append(SubnetSpec(
                name=f"{role}-{az[-2:]}",  # "public-1a"
                role=role,
                cidr=str(subnet),
                availability_zone=az,
            ))
    return result
```

**Interface IP Assignment** — reads from InterfaceBlueprint, not hardcoded port mappings:
```python
def _assign_interfaces(self, subnets: list[SubnetSpec],
                        interface_bps: list[InterfaceBlueprint],
                        fgt_index: int) -> list[ResolvedInterface]:
    """Assign IPs to product interfaces based on blueprint.

    Convention: .11 for first FGT (active), .12 for second (passive), .13 for third, etc.
    The blueprint tells us which port connects to which subnet role.
    """
    ip_offset = 11 + fgt_index
    result = []
    for bp in interface_bps:
        subnet = next(s for s in subnets if s.role == bp.subnet_role)
        network = ipaddress.ip_network(subnet.cidr)
        ip = str(network.network_address + ip_offset)
        result.append(ResolvedInterface(
            port_name=bp.port_name,
            subnet_name=subnet.name,
            private_ip=ip,
            description=bp.description,
        ))
    return result
```

**No hardcoded AMI maps, security group templates, or pattern-specific routing.**
These are either:
- In the code template (if template match) → IaC agent uses the template directly
- In KB configuration docs (if no template) → IaC agent generates from KB guidance
- In `additional_resolved` dict → passed through from user's deployment parameters

---

## 9. API Endpoints

### 9.1 Design Endpoints (Implemented)

```
POST   /api/design/submit
  Body: { requirements: InterviewOutput, project_id: str, feedback?: str, previous_options?: list }
  Response: HTTP 202 { task_id: str, status: "queued" }
  Rate limit: 5/minute
  Description: Submit async design generation task.
               Also handles redesign — pass feedback + previous_options for redesign mode.
               No separate /api/design/redesign endpoint; this single endpoint handles both.

GET    /api/design/task/{task_id}
  Query: tenant_id
  Response: { task_id, status, result?, error_message?, submitted_at, completed_at }
  Rate limit: 30/minute
  Description: Poll task status (fallback when WebSocket unavailable)

POST   /api/design/select
  Body: { project_id: str, option_index: int }
  Response: { selected_option: DesignOption, refinement_plan: RefinementPlan }
  Rate limit: 10/minute
  Description: Select a design option and get refinement parameters

POST   /api/design/refine
  Body: { project_id: str, aws_region, vpc_cidr, environment, project_name, additional_parameters }
  Response: { resolved_parameters: ResolvedIaCParameters }
  Rate limit: 10/minute
  Description: Submit deployment parameters, get resolved IaC parameters
```

### 9.2 Related Endpoints

```
GET    /api/projects/{project_id}/state
  Query: tenant_id
  Response: { project, requirements, design, iac, docs }
  Description: Load full project state for hydration on page reload.
               design field contains saved DesignRecommendation (null if not yet generated).

POST   /api/iac/invoke
  Description: IaC generation (SSE streaming). Uses resolved design + requirements.
               MCP tool servers (IaC + Terraform) provide validation during generation.

WebSocket: wss://{host}/ws (local) or wss://{api-id}.execute-api.{region}.amazonaws.com/prod (AWS)
  Routes: $connect, $disconnect, subscribe
  Description: Real-time task status notifications (§5)
```

### 9.3 IaC Agent Contract

The IaC agent receives resolved parameters + optionally a code template:
```python
# Template-first approach:
if resolved_params.code_template_files:
    # TEMPLATE PATH: Parameterize existing CloudFormation template
    context = (
        f"## CloudFormation Template (from KB)\n"
        f"Customize this template with the resolved parameters below.\n\n"
        f"### Template Files:\n{json.dumps(resolved_params.code_template_files, indent=2)}\n\n"
        f"### Resolved Parameters:\n{resolved_params.model_dump_json(indent=2)}"
    )
else:
    # GENERATE PATH: Build from KB architecture docs + component blocks
    context = (
        f"## No code template available for pattern: {resolved_params.deployment_pattern}\n"
        f"Generate CloudFormation YAML from the KB architecture docs and component building blocks.\n\n"
        f"### Resolved Parameters:\n{resolved_params.model_dump_json(indent=2)}"
    )
```

---

## 10. Design Agent Prompt

The new system prompt enforces KB-grounded design generation:

1. **Always search KB first** — before generating any design option
2. **Cite KB sources** — every option must include `kb_references` (min 1)
3. **Use KB-sourced values** — `deployment_pattern`, `ha_mode` must come from KB architecture docs.
   Do NOT invent pattern names. If the KB describes it, use the KB's terminology.
4. **Check available templates** — reference the template discovery list in the prompt.
   Prefer designs that match existing templates (better IaC quality).
5. **Exactly 3 options** — covering different cost/complexity tradeoffs:
   - Option 1: Cost-optimized (simpler, fewer services)
   - Option 2: Balanced (recommended for most scenarios)
   - Option 3: Enterprise-grade (maximum resilience/features)
6. **Build topology blueprints** — every option must include `vpc_topology` and `product_topology`
   that accurately reflect the KB reference architecture.
7. **Run WA evaluation** — for each option before finalizing
8. **Realistic cost estimates** — based on KB sizing docs

See `backend/src/prompts/design.txt` (implemented).

---

## 11. Frontend

### 11.1 State Machine Actions

The `useWizardState` reducer manages these design-specific transitions:

| Action | Trigger | Effect |
|---|---|---|
| `PROCEED_TO_DESIGN_START` | User completes interview | Set loading, call submitDesign() |
| `DESIGN_SUBMITTED` | API returns task_id | Store taskId, start polling + WS subscription |
| `DESIGN_TASK_UPDATE` | Poll or WS message | Update taskStatus; if completed → load result |
| `PROCEED_TO_DESIGN_SUCCESS` | Result loaded | Store recommendation, show DesignReview |
| `DESIGN_SELECT_SUCCESS` | User approves option | Store refinementPlan, show DeploymentParametersForm |
| `REFINE_SUCCESS` | User submits params | Store resolvedParameters, advance to IaC step |
| `REDESIGN_START` | User requests redesign | Clear recommendation, call submitDesign() with feedback |

**Polling configuration:**
- `DESIGN_POLL_INTERVAL_MS = 3000` (3 seconds between polls)
- `DESIGN_POLL_MAX_ATTEMPTS = 60` (3-minute timeout)
- Polling auto-stops when WebSocket connects
- Polling resumes on WebSocket disconnect

### 11.2 Wizard Step Flow

```
requirements → design (async generate → select → refine) → iac → documentation
                 │
                 ├── DesignLoading (async, WebSocket + polling fallback)
                 ├── DesignReview (3 options with KB citations, WA scores)
                 ├── Select option OR request redesign (feedback → async regenerate)
                 ├── DeploymentParametersForm (dynamic form, KB defaults)
                 └── Proceed to IaC (with ResolvedIaCParameters)
```

---

## 12. Local Development Parity

### Principle: Same Code, Different Backends

The local development environment MUST mirror the AWS production architecture as closely as
possible. The same business logic code (`process_design_task`, `ws_manager`, state machine)
runs in both environments. Only the **transport/infrastructure layer** differs.

**Key abstraction:** The backend uses **protocol-based dispatch** controlled by environment
variables. When `AI_DEPLOY_SQS_DESIGN_QUEUE_URL` is set → AWS mode. When unset → local mode.

### 12.1 Component Mapping

| AWS Component | Local Equivalent | Abstraction Boundary |
|---|---|---|
| SQS FIFO Queue | `queue.Queue[dict \| None]` (thread-safe) | `enqueue_design_task()` in `design.py` service |
| Lambda Design Worker | Daemon thread in `local_worker.py` | `process_design_task()` shared function |
| DynamoDB Stream → EventBridge Pipe | `notify_fn` callback parameter | `process_design_task(body, notify_fn=...)` |
| WS Notification Bridge Lambda | `ws_manager.notify()` direct call | `NotificationBridge` protocol |
| API Gateway WebSocket API | FastAPI native WebSocket at `/ws` | Same message format, same subscription model |
| DynamoDB + S3 Storage | Floci-emulated DynamoDB + S3 (same `DynamoS3ProjectStore`) | `ProjectStore` protocol (`storage/protocol.py`) |
| Cognito JWT Auth | Query parameter `?tenant_id=default` | `get_tenant_id()` in `config/auth.py` |
| CloudWatch Metrics | Disabled (no-op) | `AI_DEPLOY_METRICS_ENABLED=false` |

### 12.2 Local Worker Architecture

**Implementation file:** `backend/src/workers/local_worker.py`

```
FastAPI Lifespan
  │
  │ startup()
  ▼
┌─────────────────────────────────────────────────┐
│  Local Worker                                   │
│                                                 │
│  queue.Queue ◄── enqueue_design_task()          │
│       │                                         │
│       │ Daemon thread: _worker_loop()           │
│       ▼                                         │
│  process_design_task(body, notify_fn=ws_notify) │
│       │                                         │
│       │ On completion:                          │
│       ▼                                         │
│  notify_fn() → ws_manager.notify()              │
│       │         (run_coroutine_threadsafe)      │
│       ▼                                         │
│  FastAPI /ws endpoint pushes to subscribers     │
│                                                 │
│  shutdown() → enqueue(None) sentinel → join()   │
└─────────────────────────────────────────────────┘
```

**Why a daemon thread (not asyncio)?**
The design agent uses synchronous Bedrock SDK calls via the Strands framework.
Running in a thread avoids blocking the FastAPI event loop while keeping the
same synchronous `process_design_task()` code path as Lambda.

### 12.3 Local WebSocket

**Implementation file:** `backend/src/services/ws_manager.py`

The FastAPI `/ws` endpoint provides the same subscription model as API Gateway WebSocket:

```python
# Same message format as API Gateway WebSocket (§5.3):
{
  "type": "task_status",
  "task_id": "abc-123",
  "project_id": "project-456",
  "task_type": "design",
  "status": "completed",
  "timestamp": "2026-02-25T15:30:00Z"
}
```

**Parity features:**
- Connection tracking in-memory (mirrors DynamoDB WS# records)
- Subscribe action with project_id (mirrors API GW subscribe route)
- `notify()` method mirrors notification bridge Lambda fan-out
- `run_coroutine_threadsafe()` bridges the sync worker thread → async WS send

### 12.4 Local Notification Pipeline

In AWS, the notification pipeline is: DynamoDB write → Stream → EventBridge Pipe → Lambda → WS push.

Locally, the `notify_fn` callback in `process_design_task()` collapses this into a single
synchronous call chain:

```
process_design_task()
  └── update_task_status(COMPLETED) → Floci DynamoDB
  └── notify_fn(task_id, project_id, status)
        └── ws_manager.notify(project_id, message)
              └── for each subscribed WS connection:
                    connection.send_json(message)
```

This preserves the **same decoupling principle** — the worker doesn't know HOW notifications
happen, it just calls `notify_fn`. In AWS, `notify_fn` is `None` (EventBridge Pipe handles it).
Locally, `notify_fn` is `ws_manager.notify`.

### 12.5 Environment Variables (Local vs AWS)

```bash
# .env (local development — auto-generated by scripts/setup-local.sh)
AI_DEPLOY_AWS_ENDPOINT_URL=http://localhost:4566
AI_DEPLOY_DEBUG=true
AI_DEPLOY_CORS_ORIGINS=["http://localhost:3000"]
AI_DEPLOY_DYNAMODB_TABLE=ai-deploy-table
AI_DEPLOY_S3_ARTIFACTS_BUCKET=ai-deploy-artifacts
# AI_DEPLOY_SQS_DESIGN_QUEUE_URL is set to Floci URL → local worker polls Floci SQS
# AI_DEPLOY_COGNITO_USER_POOL_ID is set to Floci pool → local Cognito auth
# AI_DEPLOY_WEBSOCKET_URL is NOT set → local WS mode

# AWS (production) — set by CDK via ECS task definition
AI_DEPLOY_DEBUG=false
AI_DEPLOY_DYNAMODB_TABLE=ai-deploy-table-prod
AI_DEPLOY_S3_ARTIFACTS_BUCKET=ai-deploy-artifacts-prod
AI_DEPLOY_S3_KNOWLEDGE_BASE_BUCKET=ai-deploy-knowledge-base-prod
AI_DEPLOY_SQS_DESIGN_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123/ai-deploy-design-tasks.fifo
AI_DEPLOY_WEBSOCKET_URL=wss://xxx.execute-api.us-east-1.amazonaws.com/prod
AI_DEPLOY_COGNITO_USER_POOL_ID=us-east-1_xxx
AI_DEPLOY_COGNITO_CLIENT_ID=xxx
AI_DEPLOY_KNOWLEDGE_BASE_ID=xxx
AI_DEPLOY_METRICS_ENABLED=true
```

### 12.6 What's NOT Parity (Acceptable Gaps)

| Gap | Why Acceptable |
|---|---|
| Local SQS via Floci (no real FIFO deduplication guarantees) | Floci emulates SQS semantics; double-submit unlikely in single-user dev |
| No DynamoDB Stream → EventBridge Pipe | notify_fn callback achieves the same result synchronously |
| No EventBridge Pipe filtering | notify_fn is only called on terminal states (same filter semantics) |
| No heartbeat Lambda | Local /ws connections are in-memory; no stale connection issue |
| Local Cognito via Floci (no real MFA) | Auth flow is tested; MFA enforcement tested in staging AWS |
| No CloudWatch metrics | Structured JSON logs to stdout provide equivalent observability |

---

## 13. Error Handling & Resilience

### 13.1 Failure Modes

| Failure | Handling |
|---|---|
| Bedrock throttling | Lambda retries via SQS (3 attempts, exponential backoff via visibility timeout) |
| Bedrock timeout | Lambda 300s timeout → task marked FAILED → DLQ after 3 retries |
| KB not configured | Graceful degradation: designs use built-in prompt knowledge only, `kb_references` empty with warning |
| WebSocket disconnection | Frontend falls back to polling `GET /api/design/task/{id}` every 3s |
| Lambda cold start | 3-8s cold start acceptable for async tasks (user already waiting) |
| DynamoDB Stream lag | <1s typical; EventBridge Pipe has built-in error handling + DLQ |
| Double-submit | SQS FIFO content-based deduplication (5-min window) |
| Stale task records | TTL-based auto-cleanup (7 days) |
| Stale WS connections | Heartbeat Lambda proactive cleanup every 5 min (§5.4) + GoneException handling in notification bridge |
| Local worker task loss | If backend restarts mid-task, in-memory queue is lost. Frontend polls 60× then timeouts. User retries. |
| EventBridge Pipe failure | Pipe has built-in retry + DLQ. Worker's DynamoDB write is durable regardless. |

### 13.2 Circuit Breaker Integration

The Lambda worker reuses the existing `bedrock_breaker` circuit breaker
(`backend/src/config/circuit_breaker.py`):

- **Three states**: CLOSED (normal) → OPEN (5 consecutive failures) → HALF_OPEN (recovery test)
- **Recovery timeout**: 30 seconds in OPEN state before allowing a test call
- If Bedrock is in OPEN state → fail fast (don't waste Lambda execution time)
- SQS will retry when visibility timeout expires (breaker may have recovered)
- HTTP 503 response with `Retry-After` header when circuit is open

### 13.3 Input Validation

Implemented in `backend/src/utils/validation.py`:
- Prompt injection detection (regex patterns for "ignore previous instructions")
- MAX_FIELD_LENGTH: 10,000 chars per field
- MAX_TOTAL_PAYLOAD: 50,000 chars total
- Safe ID pattern: `[a-zA-Z0-9_-]+` for tenant_id, project_id (prevents path traversal)
- `sanitize_text()` and `sanitize_requirements()` strip prompt injection patterns from user-provided feedback and `additional_parameters` before passing them to LLM agents
- `_get_client_ip()` only trusts `X-Forwarded-For` when `settings.trusted_proxy` is enabled (prevents IP spoofing in direct-access mode)
- Request body size limit: 1 MB (middleware)
