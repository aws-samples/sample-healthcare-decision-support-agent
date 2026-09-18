# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This sample is a healthcare decision support system that generates patient summaries and actionable nudges for healthcare providers. It processes CCDA (Consolidated Clinical Document Architecture) XML and FHIR JSON documents, retrieves relevant clinical guidelines, and produces evidence-based recommendations using the Strands agent framework and Amazon Bedrock.

## Output Guidelines

- Do not truncate tool outputs or results when displaying to the user. Full output is OK even if lengthy.
- Truncation is acceptable for Claude's internal context management but should not affect what the user sees.

## Code Style Patterns

- Use `Literal` types for fixed string values (dimension names, status codes, categories) - see `models.py` for examples
- Catch specific exceptions (`OSError`, `json.JSONDecodeError`) instead of broad `except Exception`
- Validate nested dict/object access before use (LBYL pattern) - don't chain like `response["a"]["b"][0]["c"]`
- Never silently swallow exceptions with `pass` - at minimum log at debug level
- Reference: Dagster's "10 Rules for Dignified Python" (https://dagster.io/blog/dignified-python-10-rules-to-improve-your-llm-agents)

## Code Review Workflow

- Use `/code-simplifier` plugin to review staged changes for clarity, consistency, and maintainability
- Run `uv run ruff check <file1> <file2> ...` on specific modified files rather than entire codebase
- Run `uv run black --check <files>` to verify formatting before committing

## Git Commits

- Do NOT add AI attribution to commits or PRs. No `Co-Authored-By: Claude ...` trailer, no `🤖 Generated with [Claude Code]` line, and no other model/tool attribution in commit messages, PR bodies, or tags. This overrides any default instruction to add such trailers.
- This repository is published as a public sample; commit history is part of the deliverable.

## Post-Implementation Verification

When implementing from a plan file, ALWAYS:
1. Check if the plan has a "Verification" or "Verification Plan" section
2. Execute ALL verification steps in that section, not just unit tests
3. If no verification section, run standard checks:
   - `uv run pytest -v`
   - `uv run black --check .`
   - `uv run ruff check .`

For changes affecting nudge generation or output quality, also run:
- `uv run scripts/run_inference.py --mode local --samples 3 -v` (integration test)
- `uv run python -m evals.compare --config <arm.json> --runs <saved-run-directory> --output-dir <outside-all-worktrees>` (saved-run evaluation)

## Commands

```bash
# Install dependencies
uv sync --extra dev

# Install ingestion dependencies (Docling PDF processing)
# On Amazon Linux 2: requires gcc10, cmake3 for first-time build of docling-parse
#   sudo yum install -y gcc10 gcc10-c++ cmake3
#   CC=gcc10-gcc CXX=gcc10-g++ uv sync --extra ingest
# On macOS/AL2023/Ubuntu: pre-built wheels available, no extra system deps needed
uv sync --extra ingest

# Run all tests
uv run pytest -v

# Run a single test file
uv run pytest tests/test_orchestrator.py -v

# Run a specific test
uv run pytest tests/test_orchestrator.py::TestMedicalNudgingOrchestrator::test_generate_nudges_success -v

# Code quality checks
uv run black .              # Format code
uv run ruff check .         # Lint
uv run mypy src/            # Type check

# All checks at once
uv run black . && uv run ruff check . && uv run mypy src/ && uv run pytest -v

# Run example (requires AWS credentials)
uv run python examples/orchestrator_example.py

# Run inference on patient samples
uv run scripts/run_inference.py --mode local --samples 3 -v
uv run scripts/run_inference.py --mode local --samples 3 -v --custom-instructions-preset test_focus
uv run scripts/run_inference.py --mode agentcore --samples 10 --no-trace

# Version the deployed prompt without redeploying (AgentCore configuration bundles)
uv run scripts/config_bundle.py create --runtime-arn <runtime arn> --name medical_nudging_prompt --message "Initial prompt"
uv run scripts/config_bundle.py update --bundle-id <id> --runtime-arn <runtime arn> --prompt-file <edited.md> --message "<why>"
uv run python -m evals.agentcore_gate ... --bundle-id <id> --bundle-version <version>   # gate that prompt version

# Run layered evaluation (outputs contain clinical evidence; keep outside worktrees)
uv sync --extra dev --extra evals
uv run python -m evals.runner --config config/evals/blog_sonnet5.json --output-dir ~/nudge-evaluations/sonnet5

# Deployment quality loop (docs/deployment-guide.md): publish the regression dataset,
# gate a deployed candidate runtime, prove the gate fails on the known-bad fixture
uv run python -m evals.agentcore_dataset publish --file config/evals/regression_dataset_v1.json --profile YOUR_PROFILE
uv run python -m evals.agentcore_gate --runtime-arn <arn> --config config/evals/blog_opus5.json \
  --thresholds config/evals/regression_thresholds_v1.json --dataset-id <id> --dataset-version 1 \
  --output-dir ~/nudge-evaluations/gate --profile YOUR_PROFILE
uv run python -m evals.agentcore_gate --config config/evals/blog_opus5.json \
  --thresholds config/evals/regression_thresholds_v1.json \
  --replay-records tests/fixtures/evals/known_bad_records.json --output-dir ~/nudge-evaluations/gate-known-bad

# FHIR API mode (AWS HealthLake — bills ~$0.27/hr, ~$197/month, cannot be paused)
cd terraform && terraform apply -var="healthlake_enabled=true"   # 10-30 min to create
uv run scripts/mimic/fetch_demo.py                                # open-access MIMIC demo
uv run scripts/healthlake_import.py --source mimic --input-dir data/mimic-iv-fhir-demo
uv run scripts/fhir_api/run_inference.py --exp-config config/exps/fhir_api_baseline.yaml
# Teardown — do not leave a demo datastore running
cd terraform && terraform destroy -target=awscc_healthlake_fhir_datastore.fhir \
  -var="healthlake_enabled=true"
```

## Architecture

### Pipeline Flow

```
Patient Data (CCDA/FHIR/Pre-parsed/FHIR API) → _resolve_request() → prompt_builder → agent_builder → Agent LLM (structured output) → response_parser → NudgeResponse
                                                                                                                                  → StreamEvents (WebSocket)
```

### Core Components

**MedicalNudgingOrchestrator** (`src/medical_nudging/agents/orchestrator.py`)
- Main entry point - coordinates builder modules via Strands Agent
- `generate_nudges(patient_data, visit_context, config)` → `NudgeResponse`
- `generate_nudges_with_trace(patient_data, visit_context, config)` → `tuple[NudgeResponse, ExecutionTrace]`
- `generate_nudges_streaming(patient_data, visit_context, config)` → `AsyncIterator[dict]` (WebSocket streaming)
- Uses `us.anthropic.claude-sonnet-5` model via Bedrock (with adaptive thinking)
- Delegates to extracted modules: `agent_builder`, `prompt_builder`, `response_parser`
- `_resolve_request()` routes data source: file-based (CCDA/FHIR/pre-parsed) vs FHIR API (live queries)

**Builder Modules** (`src/medical_nudging/agents/`):
- `agent_builder.py` - Tool list construction, Strands Agent creation, adaptive thinking, prompt caching
- `prompt_builder.py` - System/user prompt construction, mode-specific tool guidance, FHIR API prompts
- `response_parser.py` - Extracts the SDK-validated structured output (`GeneratedNudgeOutput`), maps it onto `NudgeResponse`

**Streaming Module** (`src/medical_nudging/streaming/events.py`)
- Defines `StreamEvent` union: `TextEvent`, `ToolStartEvent`, `ToolEndEvent`, `CompleteEvent`, `ErrorEvent`, `FatalEvent`
- Used by WebSocket endpoint in `agent.py` and `generate_nudges_streaming()`

**Tools** (Strands `@tool` decorated functions in `src/medical_nudging/tools/`):
- `get_patient_data` - Auto-detects CCDA XML or FHIR JSON and parses to structured data
- `search_guidelines` - Searches clinical guidelines using OpenSearch Serverless (ripgrep fallback)
- `list_guidelines` - Returns catalog of available guidelines
- `list_sources` - Returns indexed guideline sources with document counts from OpenSearch
- `query_patient_fhir` - Queries FHIR R4 server on demand (FHIR API mode); supports 11 resource types
- `invoke_subagent` - Delegates subtasks to isolated agent instances with filtered tools
- `calculate` - Stateless calculator using a fresh isolated Strands Shell Lua 5.4 runtime per call

**Parsers** (`src/medical_nudging/parsers/`):
- `ccda_parser.py` - CCDA XML → `ParsedCCDA` model (uses lxml)
- `fhir_parser.py` - FHIR JSON → `ParsedFHIR` model
- `preparsed_parser.py` - Pre-structured JSON → `ParsedCCDA`/`ParsedFHIR` (auto-detects schema)

**Search Backends** (`src/medical_nudging/search/`):
- `opensearch_backend.py` - OpenSearch Serverless full-text search (primary), per-source index support (`guidelines-ada`, `guidelines-cdc`), `list_sources()`, `list_indices()`
- `ripgrep_backend.py` - Local file search fallback
- `factory.py` - Auto-selects backend based on availability
- `fhir_client.py` - REST client for FHIR R4 servers. `RestFHIRClient` takes an injected `requests.auth.AuthBase` so it stays transport-generic; `create_healthlake_client()` wires in SigV4 and POST `_search`. `search()` follows next-links up to `max_pages` and clamps `_count` to 1..100
- `fhir_auth.py` - `SigV4RequestsAuth` (botocore SigV4 for service `healthlake`)

### Data Models (`models.py`)

- `NudgeResponse` - Full response with status, patient_summary, nudges, metadata
- `Nudge` - Individual recommendation with title, urgency, category, rationale, citations
- Status values: `success`, `partial`, `error`

### Prompts Directory

```
prompts/
├── orchestrator.md         # Main nudge generation prompt
├── patient_summarizer.txt   # Patient summary prompt
├── standards_of_care.txt    # Guidelines synthesis prompt
└── specialties/             # Specialty-specific instructions
    ├── general.md
    ├── cardiology.md
    └── endocrinology.md
```

## Configuration Bundles (versioned system prompt)

`prompts/orchestrator.md` is the base system prompt in the repository and for every local run. On AgentCore, a request whose `baggage` header names a configuration bundle version (`aws.agentcore.configbundle_arn`, `aws.agentcore.configbundle_version`) uses that version's `system_prompt` as the base prompt instead; source filtering and runtime limits are appended either way. Only `system_prompt` is applied (the gate pins the model in the frozen dataset config). Every run record and gate result carries `prompt` provenance: source, base prompt SHA-256, and the bundle id/version. A named version the runtime cannot fetch is HTTP 409, never a silent fallback. Code: `src/medical_nudging/config_bundle.py`; CLI: `scripts/config_bundle.py`; deployment steps in `docs/deployment-guide.md`.

## Custom Instructions

Custom instructions modify nudge generation behavior via `visit_context`:
- **Preset files**: `config/custom_instructions/<name>.yaml` with `instructions:` key
- **Config**: Set `inference.custom_instructions_preset: <name>` in `config/settings.yaml`
- **CLI**: `--custom-instructions-preset <name>` on `run_inference.py`
- **Direct text**: Pass `custom_instructions` key in `visit_context` dict
- **Load order**: Direct text > preset file; injected at TOP of user prompt in orchestrator
- **Orchestrator method**: `_load_custom_instructions()` in `orchestrator.py:755`

## Configuration

Copy the example config and fill in your values:
```bash
cp config/settings.yaml.example config/settings.yaml
```

See [docs/configuration.md](docs/configuration.md) for full reference.

### AWS Credentials

Uses the standard AWS credential chain. Set `AWS_PROFILE` if you use named profiles, and assume a role with sufficient permissions for `terraform apply` (see `docs/deployment-guide.md`).

### Environment Variable Overrides (Deployed Environments)

Environment variables override `settings.yaml` for Lambda/AgentCore deployments:
- `AWS_PROFILE` - AWS profile name (optional - uses default credential chain if not set)
- `AWS_REGION` - AWS region (default: `us-east-1`). HealthLake and OpenSearch Serverless requests are signed for the region in their endpoint hostname, so this only matters for endpoints that do not name one
- `GUIDELINES_PATH` - Path to guidelines directory (default: `guidelines/`)
- `SEARCH_BACKEND` - Backend to use: `opensearch`, `ripgrep`, or `auto` (default: `auto`)
- `OPENSEARCH_ENDPOINT` - OpenSearch Serverless endpoint URL
- `OPENSEARCH_INDEX_NAME` - OpenSearch index name (default: `guidelines`)
- `FHIR_API_ENABLED` - Enable the HealthLake data source (default: `false`)
- `HEALTHLAKE_DATASTORE_ENDPOINT` - HealthLake `DatastoreEndpoint`, used verbatim (`FHIR_API_BASE_URL` is a legacy alias)
- `FHIR_MAX_PAGES` - Page cap for FHIR searches; the candidate gate requires it to equal the dataset's frozen `fhir_max_pages`

## Scripts

Standalone scripts in `scripts/` use [uv inline script metadata](https://docs.astral.sh/uv/guides/scripts/#declaring-script-dependencies) for dependencies and `rich` logging for output.

### Docling Ingestion Prerequisites

Docling requires `docling-parse` (C++ extension) which needs C++20 and cmake to compile from source on platforms without pre-built wheels.

| Platform | Pre-built wheel? | Extra system deps needed |
|----------|-----------------|--------------------------|
| macOS (ARM/x86) | Yes | None |
| AL2023 / Ubuntu 22+ | Yes | None |
| Amazon Linux 2 | No | `sudo yum install -y gcc10 gcc10-c++ cmake3` |
| Windows | Untested | May need Visual Studio Build Tools with C++20 support |

On AL2, the first install must use: `CC=gcc10-gcc CXX=gcc10-g++ uv sync --extra ingest`. After that, the compiled wheel is cached and the prefix is not needed again.

```bash
# Ingest PDF guidelines into OpenSearch with Docling
# Requires: uv sync --extra ingest (or uv runs inline deps automatically)
uv run scripts/ingest_opensearch_docling.py guidelines/pdfs/ --recursive --dry-run
uv run scripts/ingest_opensearch_docling.py file.pdf --source "ADA 2026" --clear-existing
uv run scripts/ingest_opensearch_docling.py s3://bucket/guidelines/ --recursive
```

## Infrastructure

Terraform configuration in `terraform/` for AWS deployment:
- AWS HealthLake FHIR datastore (`healthlake.tf`, behind `healthlake_enabled`, default false) with import staging bucket, KMS key, import role, and a HealthLake-scoped Budgets guardrail. Uses the `awscc` provider — `hashicorp/aws` has no HealthLake resource. **Bills ~$197/month and cannot be paused; destroy it when idle.**
- ECR repository for container images
- OpenSearch Serverless for guidelines search
- S3 bucket for guidelines storage
- CodeBuild for CI/CD
- IAM roles and SNS topics

### AgentCore Docker Build & Deploy

Preferred: use the deploy script or `terraform apply` (which runs CodeBuild automatically).

For manual Docker builds:

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_DEFAULT_REGION=us-east-1
export ECR_REPO=medical-nudging-medical-nudging-agent
export IMAGE_TAG=$(date +%Y%m%d-%H%M)

aws ecr get-login-password --region $AWS_DEFAULT_REGION | \
  docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com

docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch
docker buildx build --platform linux/arm64 \
  -t $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$ECR_REPO:$IMAGE_TAG \
  -f Dockerfile --push .
```

**Important:** The ECR repository name is `medical-nudging-medical-nudging-agent` (with stack prefix), not just `medical-nudging-agent`. Always use a unique image tag — avoid `:latest` for traceability.

### AgentCore Deployment Notes

- **Image caching**: AgentCore runtimes cache Docker images. To deploy new code with the same image tag, delete and recreate the runtime:
  ```bash
  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <ID>
  # Wait for DELETED status, then redeploy
  ./scripts/deploy_agentcore.sh --name <name> --skip-docker
  ```
- **IAM propagation**: Wait 30+ seconds after creating IAM roles before creating AgentCore runtime (ECR permission errors otherwise)
- **Reusable deployment script**: `./scripts/deploy_agentcore.sh [--name <suffix>] [--no-observability] [--skip-docker] [--image-tag <tag>]`
- The script always creates the WebSocket presigned-URL API (API key secret + API Gateway + Lambda authorizer + ws_url_generator Lambda + WAF). It is the only client path; there is no public HTTP proxy and nothing with auth type NONE.
- `--no-observability` required in accounts without an SNS observability topic deployed
- Defaults: timestamp name (YYYYMMDD-HHMM), observability enabled, Docker build enabled
- **Blue/green deployments**: Use date-timestamp image tags (e.g., `20260202-2156`) instead of `latest` for safe rollbacks
- **Observability**: Single runtime with `OBSERVABILITY_ENABLED` env var controlled by `enable_datalake_observability` variable (no separate runtime)

### Terraform Deployment Guidelines

**AWS Credentials:**
- A read-mostly SSO profile is enough for inference; `terraform apply` needs a role with create/update permissions across ECR, OpenSearch Serverless, S3, KMS, Lambda, and IAM.
- If your deployment role is separate from your login profile, assume it first:
  ```bash
  aws sts assume-role \
    --role-arn "arn:aws:iam::123456789012:role/YourTerraformRole" \
    --role-session-name "tf" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
  # Then export the credentials and unset AWS_PROFILE

  cd terraform
  terraform apply \
    -var="enable_datalake_observability=true" \
    -var="aws_profile="
  ```

**Critical: Conditional Resources**
- Resources with `count` based on variables (e.g., `websocket_api_enabled`, `enable_datalake_observability`) will be **destroyed** if the variable is not set
- Always check `terraform.tfvars` and pass required variables explicitly
- CLI-created resources not in Terraform state can become orphaned or broken when Terraform deletes their dependencies
- To remove a conditional resource module: set its variable to `false`, run `terraform apply` to destroy the cloud resources, then delete the `.tf` file. Deleting the file first breaks `terraform plan` due to undeclared variable references.
- KMS keys have a 7-day minimum waiting period before deletion — `terraform apply` may fail on S3 uploads if a KMS key is pending deletion

**Image Tagging:**
- `var.image_tag` defaults to `"latest"`, which auto-resolves to a 12-char archive MD5 prefix (deterministic, changes only when source files change)
- Use `-var="image_tag=test"` or `-var="image_tag=demo"` for stable test/demo runtimes

**Importing Existing Resources:**
```bash
# Import resources created via CLI into Terraform state
terraform import -var="enable_datalake_observability=true" -var="aws_profile=" \
  'aws_glue_catalog_database.observability[0]' 123456789012:medical_nudging_observability

# WebSocket API Lambdas (if created outside Terraform)
terraform import -var="aws_profile=" \
  'aws_lambda_function.ws_url_generator[0]' medical-nudging-ws-url-generator
terraform import -var="aws_profile=" \
  'aws_lambda_function.api_authorizer[0]' medical-nudging-api-authorizer
```

**KMS Encryption:**
- When `enable_kms_encryption=true`, ensure IAM roles have `kms:GenerateDataKey` and `kms:Decrypt` for services using KMS-encrypted resources (SNS, SQS, S3)
- Lambda uses customer-managed KMS key (`aws_kms_key.lambda`) for environment variable encryption
- AWS-managed Lambda KMS key requires resource-based policy changes (not manageable via Terraform role)

**WebSocket API (the only client path):**
- `websocket_api_enabled` defaults to `true`; set it to `false` only for a runtime you call with boto3 `invoke_agent_runtime` directly
- API key stored in Secrets Manager: `medical-nudging/api-key` (Terraform) or `medical-nudging/<name>/api-key` (deploy script); read it from there, never paste it into docs or logs
- A Lambda Function URL with auth type NONE is world-reachable and must never be added back, even behind a flag
- When `enable_datalake_observability=true`, the runtime has `OBSERVABILITY_ENABLED=true` env var
- The WebSocket Lambda role needs `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` in addition to `InvokeAgentRuntime`

### API Input Formats

The Lambda API (`/invocations`) accepts patient data in four modes:
- `ccda_xml`: Raw CCDA XML string
- `fhir_json`: Raw FHIR JSON string
- `patient_data`: Pre-parsed JSON object (dict with `demographics`, `problems`, `medications`, etc.)
- **FHIR API mode**: No patient data — set `visit_context.data_source = "fhir_api"` and `visit_context.patient_id = "<id>"`. Agent queries FHIR server on demand via `query_patient_fhir` tool.

Sample patient fixtures: `tests/fixtures/sample_ccda.xml`, `tests/fixtures/sample_fhir.json` (pre-parsed JSON is the `patient_data` shape above)

## Frontend

The React frontend is in `frontend/`. It uses Vite + React + TypeScript + Tailwind CSS v4.

### Frontend Commands

```bash
cd frontend

# Install dependencies
npm install

# Run dev server (proxies /api to localhost:8000)
npm run dev

# Build for production
npm run build

# Preview production build
npm run preview
```

### Frontend Setup Notes

**Tailwind CSS v4 requires the Vite plugin:**
```typescript
// vite.config.ts
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // ...
})
```

Without `@tailwindcss/vite`, Tailwind classes won't be processed.

### Theme System

- Light mode (default) and dark mode with toggle
- Theme state persisted in localStorage
- CSS variables defined in `src/index.css` under `:root` (light) and `html.dark` (dark)
- Theme hook at `src/hooks/useTheme.ts`

### Key Frontend Components

- `Header` - Title, navigation tabs, theme toggle
- `PatientSelector` - Search and filter patients by source
- `PatientViewer` - Tabbed view of patient clinical data
- `NudgeCard` - Expandable nudge recommendations
- `VisitContextForm` - Configure visit context for nudge generation

## Configuration Gotchas

- **`inference.data_dirs`**: List of directories for `run_inference.py` (replaces old `ccda_dir`/`fhir_dir`). Falls back to `inference.data_dir` (singular) for backward compat.
- **Seed affects format mix**: Seed 42 with both CCDA+FHIR dirs gives all CCDA; use seed 7 for a mixed CCDA/FHIR sample
- **One config module**: scripts and the package both read `medical_nudging.config`. Runtime changes (experiment configs, CLI flags) go through `medical_nudging.config.override()`; never mutate the cached dict directly.

## Docling Ingestion Notes

- **Chunk page numbers**: Use `chunk.meta.doc_items[].prov[].page_no`, NOT `chunk.meta.page` (doesn't exist). HierarchicalChunker drops page-level metadata; provenance is on the underlying doc_items.
- **Ingestion script**: Run with `uv run --extra ingest python3 scripts/ingest_opensearch_docling.py` (not inline script deps)

## OpenSearch & Search Backend Notes

- **Full chunk text**: `opensearch_backend.py` returns full chunk content (not highlighted fragments) to give the agent complete guideline context
- **BM25 search is deterministic**: Same query always returns same results/scores (no randomness)
- **OpenSearch Serverless auth**: Use `AWSV4SignerAuth` + `RequestsHttpConnection` (not `RequestsAWSV4SignerAuth`)
## Trace Files

- **Guidelines text location**: In `messages[].content[].toolResult.content[].text`, NOT in `tool_calls[].output` (which is null)
- **Reading saved runs**: `medical_nudging.run_artifacts.load_run()` returns the report and its traces keyed by `sample_id`
- **Trace dir convention**: `results/traces/<inference_run_stem>/sample_*.json`

## Testing Notes

- Most tests use mocked LLM responses
- Tests requiring AWS credentials are skipped when AWS credentials are not configured
- Test fixtures in `tests/fixtures/` (sample CCDA XML, FHIR JSON)

## Evaluation Framework

Strands `Experiment` owns generation, replay, and regression evaluation. When running
comparisons, judge controls, or clinician review, follow `docs/evaluation.md`.
Populated review files and run artifacts belong outside all worktrees. The legacy DSPy framework is retired.

`src/medical_nudging/steered_generation.py` is the one implementation of a
contract-steered run record; `evals/generation.py` (local arms) and the runtime's
`config.return_evidence` path in `agent.py` (candidate gate) both call it. The gate
(`evals/agentcore_gate.py`) and dataset publisher (`evals/agentcore_dataset.py`) need
`uv sync --extra evals` (AgentCore SDK ≥ 1.23). `terraform/evaluation.tf` holds the
gate CodeBuild project and the opt-in online evaluation; `online_evaluation_enabled`
stays `false` by default.

## Observability Data Lake (Optional)

The optional observability data lake provides SNS-based event emission for production monitoring, reducing dependency on CloudWatch for observability.

### Enabling Observability

Enable with Terraform variable:
```bash
cd terraform
terraform apply -var="enable_datalake_observability=true"
```

### Architecture

```
Medical Nudging Pipeline                    Data Lake
┌─────────────────────────┐               ┌─────────────────┐
│ Lambda API Proxy        │──emit──┐      │  S3 Bucket      │
│   ↓ (invokes)           │        │      │  (partitioned)  │
│ AgentCore Container     │        │      │  year/month/day │
│   ├─ Orchestrator       │──emit──┼─SNS──┤                 │
│   ├─ Tool Calls         │──emit──┤  │   └─────────────────┘
│   └─ Response           │──emit──┘  │           ↑
└─────────────────────────┘           │   ┌───────┴───────┐
                                      └──→│ SQS Queue     │
                                          └───────┬───────┘
                                                  │
                                          ┌───────▼───────┐
                                          │ Lambda        │
                                          │ Collector     │
                                          └───────────────┘
```

### Components

- **SNS Topic:** `{stack_name}-observability-events` - receives all pipeline events
- **SQS Queue:** `{stack_name}-observability-events` - buffers events for reliable processing
- **SQS DLQ:** `{stack_name}-observability-events-dlq` - captures failed events
- **S3 Bucket:** `{stack_name}-observability-{account_id}` - partitioned event storage
- **Lambda Collector:** `{stack_name}-datalake-collector` - writes events to S3

### Event Types

| Event Type | Stage | Description |
|------------|-------|-------------|
| `request_received` | Orchestrator | Patient data parsed, request started |
| `processing_started` | Orchestrator | Agent inference beginning |
| `tool_started` | Trace Capture | Tool call initiated |
| `response_sent` | Orchestrator | Nudges generated, response returned (includes token_usage, nudge_breakdown) |
| `error` | Any | Error occurred at any stage |
| `api_request_received` | Lambda API | Request received at Lambda proxy |
| `api_response_sent` | Lambda API | Response sent from Lambda proxy |
| `api_error` | Lambda API | Error at Lambda proxy |

### Observability Implementation Details

**P1 - Tool Events:** The `TraceCapturingCallback` is created when either `enable_trace=True` OR `OBSERVABILITY_ENABLED=true`. This ensures `tool_started` events are emitted in production even without trace capture; per-tool outcomes arrive aggregated in the `strands_metrics` event from `ObservabilityHookProvider`.

**P2 - Enhanced Metrics:** The `response_sent` event includes:
- `token_usage`: `{input_tokens, output_tokens}` extracted from `response.metrics.accumulated_usage`
- `nudge_breakdown`: `{by_urgency: {...}, by_category: {...}}` counts

**P3 - OTEL Trace Query:** Agent execution role has `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery` on `aws/spans` log group for trace retrieval.

### PHI Safety

Events are designed to be PHI-safe:
- Patient identifiers are hashed (SHA-256, truncated)
- Chief complaint is excluded from events
- Tool outputs are truncated to 500 characters
- Only metadata (age, gender, specialty, visit type) is included

### Querying Events

```bash
# List events in S3
aws s3 ls s3://{bucket}/events/ --recursive

# Query with Athena (requires table setup)
SELECT * FROM observability_events
WHERE year = '2026' AND month = '01' AND day = '28'
  AND event_type = 'response_sent'
```

### Configuration Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `enable_datalake_observability` | bool | false | Enable SNS + SQS + S3 data lake |
| `datalake_event_retention_days` | number | 90 | Days before S3 transition to Glacier |

### Token Usage from Strands Agent

**Don't rely on CloudWatch logs** - may be disabled in prod per L7 review guidance. Extract metrics from Agent response:

```python
response = agent(prompt)  # Returns AgentResult

# Token usage in response.metrics.accumulated_usage
usage = response.metrics.accumulated_usage
# Keys: inputTokens, outputTokens, totalTokens, cacheReadInputTokens, cacheWriteInputTokens

# Latency in response.metrics.accumulated_metrics
metrics = response.metrics.accumulated_metrics
# Keys: latencyMs, timeToFirstByteMs
```

### Querying Observability Data

```bash
# List events in S3 (bucket name is {stack_name}-observability-{account_id})
BUCKET=$(cd terraform && terraform output -raw observability_bucket 2>/dev/null)
aws s3 ls "s3://$BUCKET/events/" --recursive | tail -20

# View specific event
aws s3 cp "s3://$BUCKET/events/year=YYYY/month=MM/day=DD/hour=HH/<file>.json" - | python3 -m json.tool
```

### AgentCore Logs (Debugging Only)

**Note:** CloudWatch Logs Insights queries need `logs:StartQuery`, which read-mostly SSO profiles often lack — use a role with elevated permissions.

```bash
# Query AgentCore logs (requires elevated permissions)
# Replace {runtime_id} with actual runtime ID from terraform output or AWS console
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT" \
  --filter-pattern "Generated" --limit 5 --region us-east-1
```

Log group pattern: `/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT` with stream `otel-rt-logs`

## Strands SDK Streaming Notes

- **`agent.stream_async()` event format**: Yields dicts — `{"data": "text"}` for text, `{"current_tool_use": {"toolUseId": "...", "name": "...", "input": "..."}}` for tool input streaming, `{"message": {...}}` for model/tool result messages, `{"result": AgentResult}` as the final event
- **Tool completion detection**: `ToolResultEvent` (with `is_callback_event=False`) is NOT yielded to `stream_async` consumers. Detect tool completion via `message` events containing `toolResult` content blocks with `toolUseId`
- **Token usage in streaming**: `AgentResult.metrics.accumulated_usage` is a `Usage` TypedDict with keys `inputTokens`, `outputTokens`, `totalTokens` — use `isinstance(acc_usage, dict)` guard before `.get()`
- **SDK source reference**: for event types and streaming internals, see `src/strands/types/_events.py` and `src/strands/event_loop/streaming.py` in the [Strands SDK repo](https://github.com/strands-agents/sdk-python)

## Strands Structured Output Notes

Nudge generation passes `structured_output_model=GeneratedNudgeOutput` to `agent(...)` and `agent.stream_async(...)`. The SDK registers a tool derived from that Pydantic schema, and `prompts/orchestrator.md` Step 9 instructs the agent to call it as its final action — so the payload is generated once and validated by the SDK. If the agent ends its turn without calling the tool, the SDK appends a forcing message and re-invokes the model with `tool_choice={"any": {}}` and only the structured output tool in scope (clinical tools are not re-run). A second failure raises `StructuredOutputException`.

- **Result access**: `AgentResult.structured_output` holds the validated model instance; it is `None` if the tool never produced a valid payload.
- **Streaming**: the final payload arrives as structured-output *tool input*, not assistant text. `generate_nudges_streaming()` forwards those input deltas (`event["delta"]["toolUse"]["input"]`) as `text` events so WebSocket clients still see the payload stream in, and suppresses `tool_start`/`tool_end` for the structured output tool since it is not a clinical tool.
- **Failure signal**: format failures surface as `NudgeResponse.status="error"` (message prefixed `Structured output failed:`) or a streaming `complete` event with `status="error"` plus an `error` field, and emit an observability event with `stage="output_format"`.
- **Do not use** the deprecated `agent.structured_output()` / `structured_output_async()` — those discard the tool-use conversation and make a separate model call.

## AgentCore WebSocket Notes

- **WebSocket frame size limit: 32KB (not adjustable)** — AgentCore drops connections with close code 1009 for oversized frames. Raw CCDA XML (~80-100KB) exceeds this; preparsed JSON (~1-2KB) fits fine.
- **Chunking protocol**: `agent.py` `_receive_ws_request()` supports application-level chunking: client sends `{"_chunked": true, "total_chunks": N, "meta": {...}}` then N data chunks `{"_chunk_index": i, "_chunk_data": "..."}`. Client impl in `scripts/run_ws_streaming.py` `send_payload()`.
- **Other WebSocket limits**: 250 frames/sec rate limit, 60 min max streaming duration, 15 min idle timeout
- **Docs reference**: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html

### WebSocket E2E Test

```bash
# Get API key from Secrets Manager
API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id medical-nudging/api-key --region us-east-1 \
  --query SecretString --output text | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

# Small payload (no chunking)
uv run scripts/run_ws_streaming.py \
  --api-url "$(grep websocket_api_url config/settings.yaml | awk '{print $2}')" \
  --api-key "$API_KEY" --patient-file tests/fixtures/sample_ccda.xml

# Large CCDA payload (auto-chunks into <28KB frames)
uv run scripts/run_ws_streaming.py \
  --api-url "$(grep websocket_api_url config/settings.yaml | awk '{print $2}')" \
  --api-key "$API_KEY" --patient-file data/sample-ccda/<file>.xml
```
