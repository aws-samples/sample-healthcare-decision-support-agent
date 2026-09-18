# Scripts

Utility scripts for development, deployment, and operations.

## Inference & Evaluation

| Script | Description |
|--------|-------------|
| `run_inference.py` | Run inference on local patient files, through the orchestrator or AgentCore |
| `fhir_api/run_inference.py` | Run inference with the agent querying HealthLake on demand |
| `run_model_compatibility.py` | Test model compatibility across Bedrock models |
| `token_analysis.py` | Analyze token usage from inference results |
| `fetch_traces.py` | Fetch OTEL traces from CloudWatch |

```bash
# Local inference
uv run scripts/run_inference.py --mode local --samples 10

# AgentCore inference
uv run scripts/run_inference.py --mode agentcore --samples 10
```

## Guidelines Ingestion

| Script | Description |
|--------|-------------|
| `build_guideline_catalog.py` | Build and verify the runtime catalog from an exact OpenSearch index |
| `ingest_opensearch_docling.py` | Ingest PDFs into OpenSearch using Docling |
| `verify_opensearch.py` | Verify OpenSearch index health and content |
| `summarize-guidelines.sh` | Generate AI summaries for guidelines |
| `sync_guidelines_to_s3.sh` | Sync local guidelines to S3 bucket |

Docling ingestion requires the `[ingest]` extra. See [README.md](../README.md#guidelines-ingestion) for platform-specific install instructions.

```bash
# Install ingestion dependencies (one-time)
uv sync --extra ingest
# On Amazon Linux 2: CC=gcc10-gcc CXX=gcc10-g++ uv sync --extra ingest

# Dry run — inspect extraction quality without indexing
uv run scripts/ingest_opensearch_docling.py /path/to/file.pdf --dry-run --verbose

# Ingest into OpenSearch
uv run scripts/ingest_opensearch_docling.py /path/to/guidelines/ --recursive \
  --endpoint https://xxx.us-east-1.aoss.amazonaws.com --bucket my-bucket

# Per-source indices (guidelines-ada, guidelines-cdc, etc.)
uv run scripts/ingest_opensearch_docling.py s3://bucket/guidelines/ --recursive --per-source-index

# Verify index
uv run python scripts/verify_opensearch.py
```

## Deployment

| Script | Description |
|--------|-------------|
| `deploy_agentcore.sh` | Deploy AgentCore runtime with Lambda proxy |
| `config_bundle.py` | Version the deployed runtime's system prompt with AgentCore configuration bundles (create, update, versions, show, diff) |
| `tag_existing_resources.sh` | Add Control Tower backup tags to resources |

```bash
# Deploy new runtime
./scripts/deploy_agentcore.sh --runtime-name my_runtime --build

# Tag existing resources
./scripts/tag_existing_resources.sh
```

## Configuration

| Script | Description |
|--------|-------------|
| `validate_config_defaults.py` | Validate settings.yaml and Terraform defaults align |

```bash
uv run scripts/validate_config_defaults.py
```

## Running Scripts

Python scripts fall into two categories:

**Scripts with inline metadata** (self-contained dependencies, e.g. ingestion scripts):
```bash
uv run scripts/<script>.py [args]
```

**Scripts that import from the project** (e.g. `verify_opensearch.py`):
```bash
uv run python scripts/<script>.py [args]
```

Shell scripts run directly:

```bash
./scripts/<script>.sh [args]
```
