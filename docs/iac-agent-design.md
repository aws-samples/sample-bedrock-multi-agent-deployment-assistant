# IaC Agent — CloudFormation-Only Async Generation

---

## 1. Problem Statement

Generate Cloudformation from DesignOptions and DeploymentParameters.

### Goal

- Generates CloudFormation templates **asynchronously** (SQS + Lambda, same pattern as design agent)
- Notifies the frontend via **DynamoDB Streams → WebSocket** when generation completes
- Uses **Structured Output + Assembly** — the LLM generates structured JSON models (`ResourcePlan`, `SnippetAssemblyPlan`, `LayerPlan`), and deterministic Python code converts them to valid CloudFormation. The LLM never produces raw YAML or raw CloudFormation JSON.
- **Prioritizes KB template matching** — uses existing validated templates when available (Path 1: zero-LLM, pure Python)
- Uses **composable CFT snippets** as building blocks when no full template match exists (Path 2: LLM-generated wiring plan)
- Applies **layered decomposed generation** for novel patterns (Path 3): an Architecture Planner LLM decomposes the deployment into layers (Foundation, Security, Compute, HA, Integration), each layer generates a small `ResourcePlan` (5-15 resources) in parallel, and a deterministic merger wires cross-layer references. Output is **CloudFormation JSON** via `json.dumps()` — mathematically incapable of producing syntax errors.
- **LLM + thread-safe cache** strategy for layer plans: the Architecture Planner generates a `LayerPlan` on first request for a deployment pattern, then caches it with a tenant-scoped key (`{tenant_id}:{pattern_key}`) under `threading.Lock` (double-checked locking) for safe concurrent reuse — zero maintenance overhead
- Runs a **local multi-tier validation pipeline**: structural → cfn-lint → checkov → cfn-guard (custom vendor rules) — all local, no external MCP dependencies. Path 3 also runs **pre-assembly spec validation** against CloudFormation resource schemas.
- Iterates through a **per-layer validation-fix loop** (up to 3 attempts) — errors are routed back to originating layers by logical ID, only broken layers are re-generated. Best-version tracking with revert-on-worse logic.
- Produces a **single CloudFormation template** — `template.json` for Path 3 (JSON), `template.yaml` for Paths 1 & 2 (YAML). Simpler, more reliable than nested stacks.
- **Works identically in both local and AWS modes** — the layered generation pipeline runs inside `generate_iac()`, which is called by both `local_worker.py` (local dev) and the Lambda worker (AWS). Task dispatch differs (SQS+Lambda vs local worker), but the generation logic is identical.

---

## 2. Architecture Overview

### 2.1 Async Flow (SQS → Lambda → EventBridge Pipe → WebSocket)

**Same pattern as the design agent.** The IaC agent reuses the existing WebSocket
infrastructure (API Gateway WS API, connection tracking, EventBridge Pipe notification
bridge) — it does NOT create separate WebSocket components.

```
Frontend                     Backend API              SQS + Lambda
────────                     ───────────              ────────────

POST /api/iac/submit ──────▶ Create IaCTask ────────▶ Enqueue SQS
◄── HTTP 202 + task_id       (DynamoDB: QUEUED)       FIFO message
                                                          │
┌─ WebSocket ──────────┐                                  ▼
│ Connect + subscribe  │     DynamoDB Stream ◄──── Lambda Worker
│ to project           │     ──▶ EventBridge Pipe      │
└──────────────────────┘     ──▶ ws-notification-      │ 1. Resolve template
                                  bridge Lambda        │ 2. Generate/parameterize
◄── WS push: "iac_status"        ──▶ WS push           │ 3. Validate (multi-tier)
                                                       │ 4. Fix loop
GET /api/iac/task/{id}                                 │ 5. Persist output
◄── IaCOutput (files +                                 ▼
     validation_report)                            Store result
                                                   (S3 + DynamoDB)
```

**Why this pattern:**
- **SQS FIFO**: Guarantees ordered processing per tenant+project. MessageGroupId: `{tenant_id}#{project_id}`. Prevents duplicate generation via content-based deduplication.
- **Lambda worker**: Stateless, auto-scales to concurrent generation demand. 15-minute max execution time is sufficient for generation + validation.
- **EventBridge Pipe** (existing): The same pipe that notifies on design task status changes also handles IaC task changes. The pipe filter is broadened to match both `TASK#` and `IAC_TASK#` SK prefixes. The `ws-notification-bridge` Lambda already handles fan-out via GSI2.
- **WebSocket via API Gateway** (existing): Same `$connect` / `$disconnect` / `subscribe` routes. Client subscribes to a project — receives both design and IaC status updates on the same connection.
- **Local dev fallback**: When SQS is not configured, enqueue to `local_worker.py` (extended to handle both design and IaC task types). Local WS notifications via the existing `ws_manager.notify()` bridge.

### 2.2 Task Lifecycle

```
QUEUED ──► PROCESSING ──► VALIDATING ──► COMPLETED
  │            │              │
  │            ▼              ▼
  │         FAILED         FAILED
  │            │              │
  ▼            ▼              ▼
(TTL: 7 days auto-expiry)
```

Each state transition triggers a DynamoDB Stream event → WebSocket notification.

### 2.3 Three-Path Template Resolution

The IaC agent follows a strict precedence for template resolution:

```
                    ResolvedIaCParameters
                           │
                           ▼
              ┌─── Has code_template_files? ───┐
              │                                │
             YES                               NO
              │                                │
              ▼                                ▼
     Path 1: PARAMETERIZE          ┌─── Has snippets for ───┐
     KB template directly          │    all resource types? │
     (pure Python, zero LLM)       │                        │
                                  YES                      NO
              │                    │                        │
              ▼                    ▼                        ▼
        Validate              Path 2: COMPOSE          Path 3: LAYERED
                              LLM → SnippetAssembly    GENERATION
                              Plan (JSON), code        ┌──────────────────┐
                              merges snippets          │ LLM → LayerPlan  │
                                   │                   │ (cached per      │
                                   ▼                   │  pattern)        │
                              Validate                 ├──────────────────┤
                                                       │ Per-layer LLM →  │
                                                       │ ResourcePlan     │
                                                       │ (parallel)       │
                                                       ├──────────────────┤
                                                       │ Spec validate    │
                                                       │ Merge + JSON     │
                                                       └────────┬─────────┘
                                                                ▼
                                                           Validate
```

---

## 3. Template Resolution Strategy (Detailed)

### 3.1 Path 1: KB Template Match → Programmatic Parameterization (Zero LLM)

**When**: `ResolvedIaCParameters.code_template_files` is not None/empty. This means the design agent identified a matching KB template during design generation, and `ParameterResolver._fetch_template()` has already downloaded the template files from S3.

**How it works** — pure Python, no Bedrock calls:

1. Extract the primary CloudFormation template from `code_template_files` (key: `template.yaml` or `template.json`).
2. `build_parameter_defaults()` (`parameter_mapper.py`) analyses the template's `Parameters` section and heuristically maps resolved values to parameter names:
   - Region, AZs (by index: `AZ1` → first AZ, `AZ2` → second AZ)
   - VPC CIDRs (by role/name similarity)
   - Subnet CIDRs (by name fuzzy matching)
   - product interface IPs
   - Instance type, project name, environment, tags
   - `additional_resolved` keys (pattern-specific extras like TGW ASN)
3. `inject_parameter_defaults()` (`cfn_assembler.py`) parses the template with `cfn_load()`, updates matching Parameter `Default` values, and re-serialises with `cfn_dump()`.
4. The output is the original KB template with only Parameter defaults changed.

**Key property**: No LLM involvement means this path **cannot fail structurally**. The template's resource section is never modified, preserving 100% fidelity to the pre-validated KB template. Unmatched parameters retain their original defaults.

**Implementation**: `_parameterize_template()` in `iac.py` (10 lines of code, no retry needed).

### 3.2 Path 2: Composable CFT Snippets

**When**: No full template match, but composable snippets exist for the required resource types.

**Snippet organization in KB (S3)**:
```
s3://ai-deploy-knowledge-base/snippets/cloudformation/
  ├── vpc/
  │   ├── vpc-2az.yaml           # VPC with 2 AZs
  │   ├── vpc-3az.yaml           # VPC with 3 AZs
  │   └── vpc-params.yaml        # Reusable parameter block
  ├── product/
  │   ├── fgt-ha-active-passive.yaml   # HA A-P pair
  │   ├── fgt-standalone.yaml          # Single FGT
  │   ├── fgt-autoscale.yaml           # Auto Scaling group
  │   ├── fgt-enis.yaml                # ENI definitions
  │   └── fgt-userdata.yaml            # Bootstrap user data
  ├── networking/
  │   ├── igw-natgw.yaml               # Internet + NAT gateways
  │   ├── tgw.yaml                     # Transit Gateway
  │   ├── gwlb.yaml                    # Gateway Load Balancer
  │   └── route-tables.yaml            # Route table patterns
  ├── security/
  │   ├── sg-product.yaml            # product security groups
  │   ├── sg-management.yaml           # Management access SG
  │   └── iam-fgt-role.yaml            # IAM role + instance profile
  └── outputs/
      ├── outputs-ha.yaml              # HA deployment outputs
      └── outputs-standalone.yaml      # Standalone outputs
```

**How it works** — LLM generates a `SnippetAssemblyPlan` (JSON), code merges deterministically:

1. Analyse the `ResolvedIaCParameters` to determine required resource types:
   - VPC count + AZ count → select VPC snippet
   - product count + roles → select FGT snippet
   - Deployment pattern → select networking snippet (TGW, GWLB, etc.)
   - Security requirements → select security snippets
2. Fetch matching snippets from S3 (via `snippet_discovery` tool), parse each with `cfn_load()`.
3. Build text summaries of each snippet — listing logical IDs, resource types, parameter names, and output names (`_summarize_snippets()`).
4. Invoke the LLM with `structured_output_model=SnippetAssemblyPlan`:
   - The LLM analyses snippet summaries and the resolved params.
   - It produces a `SnippetAssemblyPlan` JSON object specifying:
     - **Wiring**: Cross-snippet references (`SnippetWiring` — which source resource/attribute connects to which target resource property)
     - **Parameter dedup**: Which snippet's definition to keep for duplicate parameter names
     - **Output selection**: Which outputs to include in the final template
     - **Resource renames**: Logical ID renames to avoid cross-snippet conflicts
5. `_execute_assembly_plan()` deterministically executes the plan:
   - Merges Parameters, Resources, Outputs, Conditions, and Mappings from all snippets
   - Applies resource renames from the plan
   - Applies wiring: replaces property values with `!Ref` or `!GetAtt` CfnTag objects
   - Injects parameter defaults via `build_parameter_defaults()`
   - Serialises the merged template with `cfn_dump()`

**Key property**: Every resource in the final template traces back to a snippet. The LLM only specifies *how to wire and merge* — it cannot add resources that don't exist in any snippet. Structural errors are eliminated because the individual snippets are pre-validated and the merge is deterministic.

**Implementation**: `_compose_snippets()` + `_execute_assembly_plan()` in `iac.py`.

### 3.3 Path 3: Layered Decomposed Generation

**When**: No template match AND insufficient snippet coverage for the deployment pattern.

**How it works** — decomposed, JSON-native pipeline with per-layer generation:

#### Step A: KB Context (shared)
Query KB for architecture, components, and configuration docs. Fetch reference CFT sample. This context is shared across all layers.

#### Step B: Architecture Planner → LayerPlan (LLM + thread-safe cache)
1. Check in-memory cache for a `LayerPlan` matching this deployment pattern. Cache key is **tenant-scoped**: `f"{tenant_id}:{pattern_key}"` — prevents cross-tenant cache pollution.
2. On cache miss (double-checked under `_LAYER_PLAN_LOCK` — a `threading.Lock` for thread-safety):
   a. Invoke the Architecture Planner LLM with `structured_output_model=LayerPlan`.
   - The LLM decomposes the deployment into ordered layers with import/export contracts.
   - **LayerPlan** contains: `pattern_name`, `description`, `layers: list[LayerSpec]`
   - **LayerSpec** contains: `name` (foundation/security/compute/ha/integration), `description`, `resource_types`, `imports: list[LayerImport]`, `exports: list[LayerExport]`, `prompt_context`
   - Imports declare what values a layer needs from other layers (e.g., VpcId from foundation)
   - Exports declare what values a layer provides (e.g., MgmtSGId from security)
3. Cache the successful plan under the lock (double-checked locking pattern prevents redundant LLM calls under concurrent requests).

**Layer decomposition example (HA Active-Passive)**:
```
foundation → security → compute → ha
    │            │          │       │
    │  VpcId     │ MgmtSG   │ FGT   │
    │  SubnetIds │ DataSG   │ IDs   │
    │            │          │       │
    └──exports──►└─imports──►└──────►│
```

#### Step C: Per-Layer Generation (parallel within dependency groups)
1. `parallelizable_groups()` computes topological sort → concurrent batches.
2. For each batch, `asyncio.gather()` runs all layers in parallel.
3. Each layer generates a `ResourcePlan` via `_generate_layer_resources()`:
   - The LLM receives: layer scope (allowed resource types), import parameters, required exports, KB context, resolved params.
   - Import values become CFN Parameters in the layer's ResourcePlan (e.g., `VpcIdParam`).
   - The LLM only generates 5-15 resources per call (vs 30-50 in monolithic generation).
4. Each layer's `ResourcePlan` is independently validated by Pydantic.

#### Step D: Pre-Assembly Spec Validation
Before merging, each layer's `ResourcePlan` is validated against CloudFormation resource schemas (`spec_validator.py`):
- **SP001**: Invalid resource type (catches typos like `AWS::EC2::VPX`)
- **SP002**: Invalid property name (catches `cidr_block` instead of `CidrBlock`)
- **SP003**: Missing required property
- **SP004/SP005**: Dangling Ref/GetAtt targets

Layers with spec errors are fixed individually via `_fix_layer_resource_plan()` before merging.

#### Step E: Deterministic Merge → JSON Assembly
1. `merge_layers()` (`layer_merger.py`) wires cross-layer references:
   - For each import, resolves the matching export in the source layer
   - Replaces `{"ref": "VpcIdParam"}` with `{"ref": "VPC"}` (or `{"get_att": [...]}`)
   - Removes import-only parameters from the merged output
   - Concatenates all resources, parameters, outputs, mappings, conditions
   - Detects duplicate logical IDs (raises ValueError)
   - Deduplicates parameters, outputs, mappings (first occurrence wins)
2. `assemble_json()` converts the merged `ResourcePlan` to CloudFormation JSON via `json.dumps()`.

**Key properties**:
- **`json.dumps()` is mathematically incapable of producing syntax errors** — eliminates the YAML structural failure that poisoned the previous fix loop
- **Smaller per-call scope** (5-15 resources per layer) → less output truncation, fewer hallucinated properties
- **Parallel execution** within dependency groups → faster wall-clock time
- **Pre-assembly spec validation** catches errors before they become template-level cfn-lint errors
- **Per-layer fix loop** fixes only the broken layer, not the entire template

**Implementation**: `_get_or_generate_layer_plan()`, `_generate_layer_resources()`, `_generate_all_layers()` in `iac.py` + `merge_layers()` in `layer_merger.py` + `assemble_json()` in `cfn_assembler.py` + `validate_resource_plan()` in `spec_validator.py`.

**Graceful fallback**: If the layered pipeline raises `ValueError` or `TypeError` (e.g., malformed LLM output, merge conflicts), the error is caught and an `IaCOutput` with a single `GENERATION_FAILED` finding is returned rather than crashing the worker. This ensures the task transitions to FAILED cleanly instead of requiring SQS DLQ retry. Metrics (latency, path) are still recorded.

#### Local vs AWS Mode Compatibility
The layered pipeline runs entirely inside `generate_iac()`, which is called by:
- **Local mode**: `local_worker.py` calls `generate_iac()` directly in a background thread
- **AWS mode**: Lambda worker calls `generate_iac()` after dequeuing from SQS

The pipeline is identical in both modes — no mode-specific code paths. All LLM calls go through `create_bedrock_model()` which configures the Bedrock client based on `settings.aws_region` and `settings.primary_model_id`.

---

## 4. Validation Pipeline

### 4.1 Validation Architecture

```
Generated Template (YAML/JSON string)
         │
         ▼
┌─────────────────────────┐
│ Layer 0: STRUCTURAL     │  Parse YAML/JSON → verify AWSTemplateFormatVersion,
│ (No tools, pure Python) │  Resources section exists, no empty resources
└────────┬────────────────┘
         │ PASS
         ▼
┌─────────────────────────┐
│ Layer 1: cfn-lint       │  Python API: cfnlint.api.lint()
│ (Local, no AWS needed)  │  Schema validation, property types, Ref/GetAtt
│ Speed: 1-3s             │  Returns structured Match objects
└────────┬────────────────┘
         │ PASS (or warnings only)
         ▼
┌─────────────────────────┐
│ Layer 2: checkov        │  Python API: checkov Runner
│ (Local, no AWS needed)  │  Security: encryption, IAM, SGs, logging
│ Speed: 2-5s             │  Returns CheckResult objects
└────────┬────────────────┘
         │ PASS (or acceptable risk)
         ▼
┌─────────────────────────┐
│ Layer 3: cfn-guard      │  Subprocess invocation of cfn-guard CLI
│ (Custom product rules)│  product-specific: SourceDestCheck, instance types,
│ Speed: <1s              │  HA patterns, ENI counts, required tags
└────────┬────────────────┘
         │ PASS
         ▼
    VALIDATION REPORT
    (structured, per-layer)
```

### 4.2 Layer 0: Structural Pre-validation

Pure Python, no external tools. Catches malformed YAML/JSON before wasting time on downstream tools.

```python
def structural_prevalidate(template_str: str) -> ValidationResult:
    """Fast structural check before running expensive validators."""
    # 1. Parse YAML/JSON
    # 2. Verify top-level keys: AWSTemplateFormatVersion, Resources
    # 3. Verify Resources is not empty
    # 4. Verify no duplicate logical IDs
    # 5. Verify Parameters (if present) have Type fields
    # 6. Verify template size <= 1MB (CFN limit)
    # 7. Return pass/fail with error list
```

### 4.3 Layer 1: cfn-lint (Python API) — BLOCKING

Local execution via `cfnlint.api.lint()`. Fully local, no network dependency.

- **Error classification**: E-prefix (errors), W-prefix (warnings), I-prefix (informational)
- **BLOCKING layer**: cfn-lint errors prevent output delivery (same as structural)
- **Region-scoped**: Validate against the target region from `ResolvedIaCParameters.region`

### 4.4 Layer 2: checkov Security Scanning — NON-BLOCKING

Local execution via checkov's Python Runner.

- **Targeted frameworks**: `["cloudformation"]` only (not Terraform, Kubernetes, etc.)
- **NON-BLOCKING layer**: checkov findings are reported but do NOT prevent output delivery
- **Skip list**: Known acceptable patterns for product (e.g., SourceDestCheck=false on data-plane ENIs is intentional, not a misconfiguration)
- **Configurable skip checks**: `settings.checkov_skip_checks` (env: `AI_DEPLOY_CHECKOV_SKIP_CHECKS`)
- **Temporary file**: checkov requires file paths, so write template to a temp file

### 4.4.1 Error Classification: Blocking vs Non-Blocking Layers

The validation pipeline classifies errors by layer, not just severity:

| Layer | Blocking? | Rationale |
|-------|-----------|-----------|
| structural | YES | Broken YAML/JSON, missing required keys — template won't deploy |
| cfn-lint | YES | Invalid property names, types, refs — CloudFormation will reject |
| checkov | NO | Security findings reported but don't block (product patterns are intentional) |
| cfn-guard | NO | Best-practice findings reported but don't block output |

`ValidationReport` provides methods for this classification:
- `has_blocking_errors()` — any structural/cfn-lint errors?
- `blocking_error_count()` — count of blocking errors (used for best-version tracking)
- `blocking_findings()` — only structural/cfn-lint error findings
- `non_blocking_findings()` — checkov/cfn-guard findings (all severities)
- `error_count()` — total errors across all layers

### 4.5a Layer 3: cfn-guard Custom product Rules — NON-BLOCKING

Custom guard rules stored in `backend/src/validation/appliance_rules.guard`:

1. **productInstanceType**: Approved compute-optimized instance types only
2. **productMultipleENIs**: At least 2 ENIs per product (mgmt + data)
3. **productVolumeEncryption**: EBS volumes must be encrypted
4. **NoOpenSSH**: No 0.0.0.0/0 on port 22
5. **RestrictedManagementAccess**: No 0.0.0.0/0 on port 443
6. **HASyncSubnetNoPublicRoute**: HA sync subnets must be private
7. **RequiredTags**: All taggable resources must have Project, Environment, ManagedBy, UseCase tags
8. **VPCDNSEnabled**: VPC must have DNS support + hostnames enabled
9. **SourceDestCheckDisabled**: product data-plane ENIs must have SourceDestCheck=false
10. **IAMRoleLeastPrivilege**: IAM policies must not use `*` for Action (except ec2:Describe*)

### 4.6 Validation-Fix Loop (with Best-Version Tracking)

```
best_template = template
best_plan = resource_plan    # non-None for Path 3 only
best_blocking = infinity

for attempt in 1..max_attempts:
    report = run_validation_pipeline(template)
    blocking = report.blocking_error_count()

    if blocking < best_blocking:
        best_blocking = blocking
        best_template = template
        best_plan = resource_plan

    if not report.has_blocking_errors():
        break   # All blocking errors fixed

    if attempt < max_attempts:
        if layer_plan is not None and layer_resource_plans is not None:
            # Path 3: Route errors to layers, fix per-layer, re-merge
            errors_by_layer = _map_errors_to_layers(layer_plan, layer_resource_plans, report)
            for layer_name, layer_errors in errors_by_layer.items():
                layer_resource_plans[layer_name] = _fix_layer_resource_plan(
                    layer_spec, layer_resource_plans[layer_name], error_text)
            resource_plan = merge_layers(layer_plan, layer_resource_plans)
            fixed = assemble_json(resource_plan)
        else:
            # Paths 1 & 2: YAML-level fix fallback
            fixed = _fix_template_yaml(template, report)

        check_report = run_validation_pipeline(fixed)
        new_blocking = check_report.blocking_error_count()

        if new_blocking > blocking:
            template = best_template        # Revert — fix made things worse
            resource_plan = best_plan
            break
        template = fixed
        report = check_report

# Exhausted attempts — use best version seen
if report.blocking_error_count() > best_blocking:
    template = best_template
    report = run_validation_pipeline(template)
```

**Key improvements**:

1. **Per-layer fix (Path 3 — layered)**: `_map_errors_to_layers()` builds a reverse map (`resource_logical_id → LayerName`) and routes each validation error to its originating layer. Unattributed errors go to all layers. Only layers with errors are re-generated via `_fix_layer_resource_plan()`, which preserves the layer contract (imports/exports). After fixing, layers are re-merged and re-assembled.

2. **Best-version tracking with plan state**: The loop tracks both `best_template` and `best_plan` so reverting restores the correct structured state. The revert-on-worse logic prevents the fix agent from introducing more errors than it fixes.

3. **YAML fallback (Paths 1 & 2)**: `_fix_template_yaml()` still handles templates from parameterization (which rarely need fixing) and snippet composition. Even this fallback tries to produce a `ResourcePlan` for the fix, then re-assembles.

**Fix agent behavior**:
- **Path 3 (layered)**: Errors routed to layers → `_fix_layer_resource_plan()` fixes each broken layer → re-merge → re-validate. The fix prompt includes the layer contract (import parameter names, export resource IDs) to prevent breaking cross-layer references.
- **Paths 1 & 2**: Receives template YAML + resource summary + errors → returns `ResourcePlan` JSON → assembles to YAML
- Error priority header injected into fix prompt: structural > cfn-lint > cfn-guard > checkov
- For **cfn-lint errors**: Fix property names/types per CFN spec (e.g., `GroupSet` not `SecurityGroupIds` for ENIs)
- For **checkov failures**: Apply specific remediation (e.g., add `Encrypted: true`)
- For **cfn-guard failures**: Apply product-specific corrections per rule message
- For **product-intentional patterns**: SourceDestCheck=false on data-plane ENIs is NOT an error
- **NOT allowed to**: Add new resources, remove resources, change the deployment architecture, rename import parameters or export resources

---

### 4.4b Agent Names & Retry Decorators

All IaC agents use kebab-case naming consistently across Agent `name=` parameter, `@bedrock_retry()` decorator, and `TOOL_POLICIES` keys:

| Agent Name | Function | Retry Decorator |
|------------|----------|----------------|
| `iac-compose` | Path 2: Snippet assembly plan | `@bedrock_retry("iac-compose")` |
| `iac-layer-plan` | Path 3: Architecture decomposition | `@bedrock_retry("iac-layer-plan")` |
| `iac-layer-generate` | Path 3: Per-layer resource plan | `@bedrock_retry("iac-generate-layer")` |
| `iac-layer-fix` | Path 3: Per-layer fix | `@bedrock_retry("iac-fix-layer")` |
| `iac-fix` | Paths 1 & 2: Monolithic fix | `@bedrock_retry("iac-fix")` |

> **Note:** The retry decorator names for per-layer generation and per-layer fix use reversed word order compared to the agent names (`iac-generate-layer` vs `iac-layer-generate`). This is intentional — the retry metric names were established first and retained for backwards compatibility.

---

## 4.5 Single-File Output Architecture

All three generation paths produce a **single CloudFormation template**.

### Output File Structure

```
template.json   — Path 3: CloudFormation JSON (layered generation)
template.yaml   — Paths 1 & 2: CloudFormation YAML (parameterization / snippet composition)
```

The downstream consumer (`docs_processing.py`) handles both: `files.get("template.yaml", "") or files.get("template.json", "")`.

### How Each Path Produces Output (Structured Output + Assembly)

The LLM never produces raw YAML or raw CloudFormation. Each path uses a different strategy:

- **PARAMETERIZE** (`_parameterize_template()`): **Zero LLM** — pure Python. `build_parameter_defaults()` maps resolved values to parameter names, `inject_parameter_defaults()` updates the KB template. Cannot fail structurally.
- **COMPOSE** (`_compose_snippets()`): LLM generates a `SnippetAssemblyPlan` (JSON) via `structured_output_model`, then `_execute_assembly_plan()` deterministically merges pre-validated snippets. Output: YAML.
- **GENERATE** (layered pipeline): Architecture Planner LLM generates a `LayerPlan` (cached per tenant+pattern), per-layer LLMs generate `ResourcePlan` objects in parallel, `merge_layers()` wires cross-layer refs, `assemble_json()` produces CloudFormation JSON via `json.dumps()`. Output: JSON.

All dispatched via `generate_iac(params, invocation_state, feedback, feedback_validation_summary, tenant_id, project_id)` which calls `bedrock_breaker.pre_check()` at entry, resolves the path via `resolve_template_path()`, and routes to the correct generator.

### Per-Path Token Limits

Token budgets are per-path (the LLM generates structured JSON):

| Path | Setting | Default | LLM Involvement |
|------|---------|---------|-----------------|
| PARAMETERIZE | N/A | N/A | None (pure Python) |
| COMPOSE | `iac_compose_max_tokens` | 32768 | SnippetAssemblyPlan JSON |
| GENERATE (layer plan) | `iac_layer_plan_max_tokens` | 16384 | LayerPlan JSON (or predefined fast path) |
| GENERATE (per-layer) | `iac_layer_generate_max_tokens` | 16384 | Per-layer ResourcePlan JSON |
| Fix (per-layer) | `iac_layer_fix_max_tokens` | 16384 | Fixed per-layer ResourcePlan |
| Fix (monolithic fallback) | `iac_fix_max_tokens` | 32768 | Fixed ResourcePlan JSON |

Env vars: `AI_DEPLOY_IAC_LAYER_PLAN_MAX_TOKENS`, `AI_DEPLOY_IAC_LAYER_GENERATE_MAX_TOKENS`, etc.

### Fix Agent (Per-Layer for Path 3)

The fix agent operates at the **per-layer level** for Path 3:

- **Path 3 (per-layer fix)**: `_map_errors_to_layers()` builds a reverse map (`logical_id → LayerName`) and routes each validation error to its originating layer. `_fix_layer_resource_plan()` fixes each broken layer while preserving its contract (import parameter names, export resource IDs). After fixing, layers are re-merged via `merge_layers()` and re-assembled via `assemble_json()`.
- **Paths 1 & 2 (YAML-level fallback)**: `_fix_template_yaml()` — parses the template to build a resource summary, gives the LLM the template + errors, and asks for a fixed `ResourcePlan`. The fixed plan is assembled into YAML. Falls back to returning the original template on failure.

Both fix paths include error priority ordering: structural > cfn-lint > cfn-guard > checkov. Fix prompts explicitly instruct: do NOT add/remove resources, do NOT change the deployment architecture, do NOT rename import parameters or export resources, SourceDestCheck=false on data-plane ENIs is INTENTIONAL.

---

## 5. Data Models

### 5.1 IaCTask (DynamoDB)

```python
class IaCTaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"

class IaCTask(BaseModel):
    """Async IaC generation task tracked in DynamoDB.

    DynamoDB key schema:
      PK: TENANT#{tenant_id}
      SK: IAC_TASK#{task_id}
    """
    task_id: str
    tenant_id: str
    project_id: str
    status: IaCTaskStatus = IaCTaskStatus.QUEUED
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None

    template_resolution_path: str | None = None  # "parameterize" | "compose" | "generate"
    validation_attempts: int = 0
    feedback: str | None = None      # User feedback for regeneration

    result: dict | None = None       # IaCOutput on completion
    error_message: str | None = None  # On failure
    ttl: int | None = None           # 7-day auto-expiry
```

### 5.2 IaCOutput (Result Model)

```python
class ValidationFinding(BaseModel):
    """A single validation finding from any layer."""
    layer: str           # "structural" | "cfn-lint" | "checkov" | "cfn-guard"
    severity: str        # "error" | "warning" | "info"
    rule_id: str         # e.g., "E3012", "CKV_AWS_23", "productInstanceType"
    message: str
    resource: str | None = None  # Logical resource ID
    line: int | None = None
    file: str | None = None      # Source file (always "template.yaml" for single-file output)

class ValidationReport(BaseModel):
    """Structured validation results from all layers."""
    passed: bool
    findings: list[ValidationFinding]
    fix_attempts: int
    layers_executed: list[str]

class IaCOutput(BaseModel):
    """Final IaC generation output."""
    files: dict[str, str]            # {"template.yaml": "..."} — single CloudFormation template
    validation_report: ValidationReport
    template_resolution_path: str    # Which path was used
    generation_duration_ms: int
```

### 5.3 ResourcePlan & SnippetAssemblyPlan (Structured LLM Output)

These Pydantic models are the core of the Structured Output + Assembly architecture. The LLM generates instances of these models via Strands `structured_output_model`, and deterministic Python code converts them to CloudFormation YAML.

```python
# --- ResourcePlan (Path 3: GENERATE + Fix loop) ---

class CfnParameter(BaseModel):
    logical_id: str
    type: str             # CFN type: String, Number, CommaDelimitedList, etc.
    default: str | None
    description: str = ""
    allowed_values: list[str] | None = None
    no_echo: bool = False

class CfnResource(BaseModel):
    logical_id: str
    type: str             # Validated: must start with "AWS::" or "Custom::"
    properties: dict[str, Any]  # Values can be plain or intrinsic function dicts
    depends_on: list[str] | None = None
    condition: str | None = None

class CfnOutput(BaseModel):
    logical_id: str
    value: Any            # Plain string or intrinsic function dict
    description: str = ""
    export_name: Any | None = None
    condition: str | None = None

class CfnMapping(BaseModel):
    logical_id: str
    mapping: dict[str, dict[str, Any]]

class CfnCondition(BaseModel):
    logical_id: str
    condition: dict[str, Any]   # Intrinsic function dict

class ResourcePlan(BaseModel):
    """Top-level structured plan. Validated: min 1 resource, no duplicate IDs."""
    description: str = ""
    parameters: list[CfnParameter] = []
    mappings: list[CfnMapping] = []
    conditions: list[CfnCondition] = []
    resources: list[CfnResource]    # min_length=1
    outputs: list[CfnOutput] = []

# --- SnippetAssemblyPlan (Path 2: COMPOSE) ---

class SnippetWiring(BaseModel):
    source_snippet: str
    source_logical_id: str
    source_attribute: str | None = None  # None = Ref, else GetAtt attribute
    target_snippet: str
    target_resource_logical_id: str
    target_property_path: str   # Dot-delimited: "VpcId" or "SecurityGroupIds.0"

class SnippetAssemblyPlan(BaseModel):
    """LLM-generated plan for merging snippets. Code executes deterministically."""
    wiring: list[SnippetWiring] = []
    parameter_dedup_keep: dict[str, str] = {}  # param_name -> snippet to keep
    output_selection: list[str] = []            # empty = keep all
    resource_renames: dict[str, str] = {}       # old_id -> new_id
```

**Intrinsic functions as JSON dicts** — the LLM uses plain dicts with lowercase keys:
```
{"ref": "VPC"}                          → !Ref VPC
{"sub": "${AWS::StackName}-vpc"}        → !Sub ${AWS::StackName}-vpc
{"get_att": ["Instance", "PublicIp"]}   → !GetAtt Instance.PublicIp
{"select": [0, {"get_azs": ""}]}       → !Select [0, !GetAZs ""]
{"join": [",", [{"ref": "A"}]]}        → !Join [",", [!Ref A]]
{"if": ["Cond", "True", "False"]}       → !If [Cond, True, False]
{"find_in_map": ["Map", "K1", "K2"]}   → !FindInMap [Map, K1, K2]
{"base64": {"ref": "UserData"}}         → !Base64 !Ref UserData
```

The `_convert_intrinsics()` function in `cfn_assembler.py` handles the recursive conversion of these dicts to `CfnTag` objects (from `utils/cfn_yaml.py`), supporting 17 intrinsic function types including nested compositions.

### 5.4 IaCRequest (API)

```python
class IaCSubmitRequest(BaseModel):
    """Request to submit IaC generation task."""
    project_id: str
    session_id: str = ""
    feedback: str | None = Field(default=None, max_length=5000)  # For regeneration

class IaCTaskResponse(BaseModel):
    """Response from task submission or status poll."""
    task_id: str
    status: str
    submitted_at: str | None = None
    result: IaCOutput | None = None
    error: str | None = None
```

---

## 6. API Endpoints

### 6.1 Submit IaC Generation

```
POST /api/iac/submit
  Request: IaCSubmitRequest { project_id, feedback? }
  Response: HTTP 202 { task_id, status: "queued" }
  Rate Limit: 5/minute

  Preconditions:
    - Project must have status=IAC (design selected + parameters resolved)
    - ResolvedIaCParameters must exist in design step data
    - No active IaC task already running for this project

  Regeneration:
    - When `feedback` is provided, the endpoint triggers a regeneration.
    - The worker loads the previous IaC step's validation report for context.
    - Feedback + previous validation summary are injected into the agent prompt.
    - The user stays on the IaC step (no redirect to dashboard).
```

**Why simplified request**: Unlike the current `IaCRequest` which passes the full design and requirements, the new endpoint only needs `project_id`. The worker loads `ResolvedIaCParameters` from the stored design step — this is the single source of truth. For regeneration, the optional `feedback` field carries the user's change request (max 5000 chars).

### 6.2 Poll IaC Task Status

```
GET /api/iac/task/{task_id}
  Response: IaCTaskResponse
  Rate Limit: 30/minute

  Used as fallback when WebSocket is unavailable.
```

### 6.3 Download IaC Bundle

```
GET /api/export/{project_id}/iac.zip
  Response: StreamingResponse (application/zip)
  Rate Limit: 5/minute

  Same as current, but only contains CloudFormation files.
```

### 6.4 WebSocket Notifications (Reuses Existing Infrastructure)

The IaC agent does NOT create its own WebSocket endpoint. It reuses the existing
API Gateway WebSocket API deployed for the design agent. The client subscribes to
a project via the existing `subscribe` action — IaC status updates are delivered
on the same connection as design status updates, differentiated by `type`.

```
Existing WSS endpoint (same as design agent):
  wss://{api-id}.execute-api.{region}.amazonaws.com/{stage}

Client subscribes:
  { "action": "subscribe", "project_id": "...", "tenant_id": "..." }

IaC status messages pushed to client (type changes for terminal states):
  { "type": "iac_status",   "task_id": "...", "status": "processing", ... }
  { "type": "iac_status",   "task_id": "...", "status": "validating", ... }
  { "type": "iac_complete", "task_id": "...", "status": "completed", "result": {...}, ... }
  { "type": "iac_failed",   "task_id": "...", "status": "failed", "error": "...", ... }
```

**Message format consistency with design agent:**
The `ws-notification-bridge` Lambda maps terminal statuses to distinct message types:
- `completed` → `{domain}_complete` (with `result` payload from DynamoDB GetItem)
- `failed` → `{domain}_failed` (with `error` from stream record)
- Other → `{domain}_status` (intermediate status updates)

Where `domain` is `"iac"` or `"design"` based on the DynamoDB SK prefix.
The frontend routes messages by `type` (`iac_complete`, `iac_failed`, `iac_status`, etc.).

### 6.5 IaC Regeneration with Feedback

Mirrors the design agent's "regenerate with feedback" flow. After IaC generation
completes, the user can review the template, and if unsatisfied, submit feedback
to regenerate with specific changes.

**Frontend flow:**
```
IaCView (completed state)
  → "Not what you expected? Regenerate with feedback" toggle link
    → Textarea for user feedback (max 5000 chars)
      → "Regenerate IaC" button (indigo-themed)
        → useWizardState.regenerateIaC(feedback)
          → submitIaCTask(projectId, tenantId, feedback)
            → POST /api/iac/submit { project_id, feedback }
```

**Backend processing:**
```
process_iac_task(body)
  1. Extract feedback from message body
  2. If feedback present, load previous IaC step from store
  3. Build previous_validation_summary from stored validation report
  4. Pass feedback + previous_validation_summary to generate_iac()
  5. generate_iac() builds a feedback section and appends it to prompts:

     ## User Feedback on Previous Generation
     {user's feedback text}

     ## Previous Validation Report
     Passed: false, Fix attempts: 2, Errors: E3012: invalid ref; W2001: unused param
```

**Feedback injection per generation path:**

| Path | Injection Point | Reasoning |
|------|----------------|-----------|
| PARAMETERIZE | Not applicable | Pure Python, no LLM — feedback has no injection point |
| COMPOSE | Appended to user prompt for `SnippetAssemblyPlan` generation | LLM can adjust wiring, renames, and output selection based on feedback |
| GENERATE | Appended to user prompt for `ResourcePlan` generation | LLM can adjust resources, properties, and parameters based on feedback |
| Fix agent | Not injected | Fix loop has its own feedback mechanism via validation errors |

**Key behaviors:**
- No redirect on regeneration — user stays on the IaC step to see results
- Previous IaC output is cleared when regeneration starts (shows loading state)
- "Regenerate IaC" button is disabled until feedback is non-empty
- Cancel resets the feedback textarea and hides it

---

## 7. KB Integration

### 7.1 Template Discovery (Enhanced)

Extend `template_discovery.py` to also discover snippets:

```python
def discover_snippets(resource_types: list[str]) -> dict[str, list[SnippetInfo]]:
    """Discover composable CFT snippets for specific resource types.

    Scans s3://{bucket}/snippets/cloudformation/{resource_type}/*.yaml
    Returns: { "vpc": [SnippetInfo(...)], "product": [SnippetInfo(...)] }
    """
```

### 7.2 Snippet Matching Logic

```python
def resolve_template_path(params: ResolvedIaCParameters) -> TemplatePath:
    """Determine which template resolution path to use.

    Returns:
      - TemplatePath.PARAMETERIZE if code_template_files is available
      - TemplatePath.COMPOSE if snippets cover all required resource types
      - TemplatePath.GENERATE if neither (last resort)
    """
    if params.code_template_files:
        return TemplatePath.PARAMETERIZE

    required_types = _infer_resource_types(params)
    snippets = discover_snippets(required_types)
    coverage = sum(1 for t in required_types if snippets.get(t))

    if coverage == len(required_types):
        return TemplatePath.COMPOSE

    return TemplatePath.GENERATE
```

### 7.3 KB Search for Generation Context

For Path 3 (KB-grounded generation), the agent queries:

1. **Architecture docs**: Overall topology, traffic flows, VPC layout
2. **Components docs**: Specific AWS services, product features
3. **Configuration docs**: product CLI bootstrap, HA settings, routing
4. **Reference CFT**: `cft.json` as structural skeleton

All KB content is injected into the system prompt, NOT as tool calls. This ensures the LLM has all context in a single inference call rather than multiple tool-use rounds.

---

## 8. Notification Pipeline (Reuses Existing Design Agent Infrastructure)

**The IaC agent does NOT build its own WebSocket or notification infrastructure.**
It reuses the exact components already deployed for the design agent. The only change
is broadening the EventBridge Pipe filter to also capture `IAC_TASK#` records.

### 8.1 Existing Infrastructure (No Changes Needed)

The following components are already deployed and working for design task notifications:

| Component | Implementation | Purpose |
|-----------|---------------|---------|
| API Gateway WebSocket API | `infra/lib/websocket.ts` | `$connect` / `$disconnect` / `subscribe` routes |
| `ws-connect` Lambda | `backend/lambdas/ws/ws_connect.py` | Stores `WS#{connection_id}` + `CONNECTION` in DynamoDB (2h TTL) |
| `ws-disconnect` Lambda | `backend/lambdas/ws/ws_disconnect.py` | Queries `pk=WS#{connection_id}`, batch-deletes all items |
| `ws-subscribe` Lambda | `backend/lambdas/ws/ws_subscribe.py` | Stores subscription with `gsi2pk: SUB#{tenant_id}#{project_id}` |
| `ws-heartbeat` Lambda | `backend/lambdas/ws/ws_heartbeat.py` | Pings connections every 5min, cleans stale ones, publishes CW metrics |
| Local WS manager | `backend/src/services/ws_manager.py` | In-memory subscription map for local dev, thread-safe `notify()` |

### 8.2 Connection Tracking (DynamoDB — Existing Schema, No Changes)

Uses the existing `ai-deploy-table` single-table design:

```
# Connection record (created on $connect)
PK: WS#{connection_id}
SK: CONNECTION
  connection_id: str
  connected_at: ISO timestamp
  ttl: epoch + 2h (auto-cleanup stale connections)

# Subscription record (created on explicit subscribe action)
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

**GSI2 for subscription fan-out:**
```
GSI: GSI2
  PK: gsi2pk = SUB#{tenant_id}#{project_id}   ← find all connections for a project
  SK: gsi2sk = WS#{connection_id}
  Projection: ALL
```

### 8.3 Connection Lifecycle (Same as Design Agent)

1. **Connect**: Client opens WebSocket to `wss://{api-id}.execute-api.{region}.amazonaws.com/{stage}` → `ws-connect` Lambda stores connection record
2. **Subscribe**: Client sends `{"action": "subscribe", "project_id": "xxx", "tenant_id": "default"}` → `ws-subscribe` Lambda stores subscription record with GSI2 keys
3. **Push**: `ws-notification-bridge` Lambda queries GSI2 by `SUB#{tenant_id}#{project_id}` → pushes to all subscribed connections
4. **Disconnect**: `$disconnect` route → `ws-disconnect` Lambda batch-deletes connection + all subscriptions
5. **Stale cleanup**: 2-hour TTL + heartbeat Lambda every 5 minutes

### 8.4 Notification Pipeline (EventBridge Pipe — Filter Broadened)

The existing EventBridge Pipe is the notification trigger. The **only change** needed
is broadening its source filter to also match `IAC_TASK#` SK prefixes:

```
DynamoDB Stream (table: ai-deploy-table)
  │
  │ Stream view type: NEW_AND_OLD_IMAGES
  │
  ▼
EventBridge Pipe (ai-deploy-design-notification-pipe)
  │
  │ Source filter (UPDATED to include IaC tasks):
  │   eventName: MODIFY
  │   dynamodb.Keys.sk.S prefix: ["TASK#", "IAC_TASK#"]   ← BROADENED
  │   dynamodb.NewImage.status.S IN ["completed", "failed"]
  │   dynamodb.OldImage.status.S != dynamodb.NewImage.status.S
  │
  ▼
Lambda: ws-notification-bridge (ai-deploy-ws-notification-bridge)
  │
  │ 1. Extract task_id, tenant_id, project_id, status from stream NewImage
  │ 2. Skip if NewImage.status == OldImage.status (no actual change)
  │ 3. Determine domain from SK prefix:
  │      SK starts with "IAC_TASK#" → domain = "iac"
  │      Otherwise                  → domain = "design"
  │ 4. Map terminal statuses to distinct message types:
  │      completed → type = "{domain}_complete" + GetItem for result payload
  │      failed    → type = "{domain}_failed" + error from stream record
  │      other     → type = "{domain}_status"
  │ 5. Query GSI2: gsi2pk = SUB#{tenant_id}#{project_id}
  │ 6. For each subscribed connection_id:
  │    a. POST to API Gateway Management API (@connections/{connectionId})
  │    b. If GoneException: batch-delete connection + subscriptions
  │
  ▼
WebSocket push to frontend
```

**Changes to `ws-notification-bridge` Lambda** (`backend/lambdas/ws/ws_notification_bridge.py`):
- Determine domain from SK prefix (`"iac"` or `"design"`)
- Map terminal statuses to distinct message types: `{domain}_complete`, `{domain}_failed`
- For completed tasks: GetItem to include `result` payload
- For failed tasks: Extract `error_message` from stream record

**WebSocket message format (terminal state example):**
```json
{
  "type": "iac_complete",
  "task_id": "abc-123",
  "project_id": "project-456",
  "tenant_id": "default",
  "status": "completed",
  "result": { "files": {"template.yaml": "..."}, "validation_report": {...}, ... },
  "timestamp": "2026-02-25T15:30:00Z"
}
```

### 8.5 Local Dev Fallback

When running locally (no API Gateway WebSocket), the system uses the same mechanism
as the design agent:

- `local_worker.py` calls `ws_manager.notify(tenant_id, project_id, message)` after
  IaC task completion/failure (same pattern as design task notifications)
- `ws_manager.notify()` bridges from the worker thread to the async event loop via
  `asyncio.run_coroutine_threadsafe()`
- Frontend uses polling via `GET /api/iac/task/{task_id}` every 3 seconds as fallback
  when `NEXT_PUBLIC_WEBSOCKET_URL` is not configured
- Max polling attempts: 60 (3-minute timeout)

---

## 9. Error Handling & Resilience

### 9.1 Circuit Breaker

Reuse the existing `bedrock_breaker` for LLM calls. Pre-check before starting generation.

### 9.2 SQS Dead Letter Queue

After 3 failed processing attempts, the SQS message moves to a DLQ. A CloudWatch alarm triggers on DLQ messages.

### 9.3 Validation Timeout

Each validation layer has a timeout:
- Structural: 5s
- cfn-lint: 30s
- checkov: 60s
- cfn-guard: 15s

Total validation pipeline timeout: 120s (2 minutes). All layers are local — no network latency.

### 9.4 Fix Agent Guardrails

The fix agent:
- Has guardrails enabled (`include_guardrails=True` on `create_bedrock_model()`)
- Operates at the **structured JSON level** (Path 3: `ResourcePlan`, Paths 1 & 2: YAML → `ResourcePlan` fallback)
- Cannot add new resource types
- Cannot remove resources
- Cannot change the deployment architecture
- Uses `settings.iac_fix_max_tokens` (default: 32768) for output budget
- Uses `structured_output_model=ResourcePlan` to ensure fixes produce valid structured output
- Fix prompt includes error priority header: structural > cfn-lint > cfn-guard > checkov
- product-intentional patterns (SourceDestCheck=false on data-plane ENIs) are not "fixed"

### 9.5 Idempotency

SQS FIFO with deduplication ensures the same IaC task is not processed twice. The task_id is the deduplication key.

---

## 9.6 Observability Metrics (CloudWatch)

The IaC agent emits custom CloudWatch metrics to the `AI Deploy` namespace via the
existing `MetricsPublisher` (non-blocking background threads):

| Metric | Unit | Dimensions | When |
|--------|------|-----------|------|
| `IaCTemplatePath` | Count | Path, TenantId | After path resolution |
| `IaCValidationPassed` | Count (0/1) | Path, TenantId | After validation loop |
| `IaCFixAttempts` | Count | Path, TenantId | After validation loop |
| `IaCValidationLayerErrors` | Count | Layer, TenantId | Per validation layer |

These join the existing `BedrockInvocationLatencyMs`, `RateLimitExceeded`, and `RetryAttempt`
metrics already published by the design agent and other components.

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| KB has no template for a deployment pattern | Medium | Degrades to Path 3 (LLM generation) | Grow snippet library progressively; Path 3 is the bootstrap |
| checkov adds ~150MB dependency | Certain | Larger Docker image, slower cold starts | Consider Docker layer caching; or use targeted cfn-guard rules instead |
| cfn-guard subprocess not available in Lambda | Medium | Layer 3 skipped | Bundle cfn-guard binary in Lambda layer; or skip Layer 3 gracefully |
| WebSocket notification bridge changes | Low | IaC notifications not delivered | Reuses proven design agent WS infrastructure; only change is broadening EventBridge Pipe filter + dynamic `type` field |
| LLM generates invalid ResourcePlan JSON (Path 3) | Low | Pydantic validation rejects it | Pydantic enforces min 1 resource, no duplicate IDs, `AWS::` prefix. Strands `structured_output_model` guides the LLM to produce valid JSON. |
| Parameter mapper mismatches (Path 1) | Low | Wrong parameter values | Heuristic matching with fuzzy name normalisation; unmatched params retain KB defaults. Easily extended with new matching rules. |
| LLM produces empty SnippetAssemblyPlan (Path 2) | Low | Snippets merged with no wiring | Fallback to empty plan that still merges snippets (parameters, resources, outputs) — just without cross-references |
| Fix agent introduces new errors | Medium | Validation loop diverges | Cap at 3 attempts; revert-on-worse logic; fix at structured JSON level avoids YAML corruption cascade |

---