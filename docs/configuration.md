# Configuration

All configuration lives in `config/settings.yaml`. Copy the example to get started:

```bash
cp config/settings.yaml.example config/settings.yaml
```

The file is gitignored, so it's safe for local credentials and endpoints.

## Configuration Sources

Configuration is loaded by `src/medical_nudging/config.py` with the following priority:

1. **Environment variables** (highest) — for Lambda/AgentCore deployments
2. **`config/settings.yaml`** — for local development
3. **Hardcoded defaults** (lowest)

The config is loaded once and cached (`@lru_cache`). Nested keys use dot notation internally (e.g., `agent.search_backend`).

## settings.yaml (Local Development)

```yaml
# AWS Configuration
# aws_profile: your-aws-profile  # Optional: set to use a specific profile
aws_region: us-east-1

# Model Configuration
model:
  model_id: us.anthropic.claude-sonnet-5

# Agent Configuration
agent:
  max_nudges: 5
  search_backend: auto  # Options: opensearch, ripgrep, auto (Terraform default: opensearch)

# AgentCore Configuration
agent_arn: ""  # e.g., arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/...

# WebSocket presigned-URL API (written by scripts/deploy_agentcore.sh)
websocket_api_url: ""
api_key_secret_name: ""

# OpenSearch Serverless Configuration
opensearch_endpoint: ""
opensearch_index: guidelines

# Guidelines Configuration
guidelines_path: guidelines/

# Inference defaults
inference:
  samples: 10
  seed: 42
  ccda_dir: tests/fixtures
  fhir_dir: tests/fixtures
  output_dir: results

# Tracing/Observability Configuration
tracing:
  enabled: true            # Trace capture is enabled by default (use --no-trace to disable)
  publish_metrics: false   # Publish to CloudWatch Metrics (--publish-metrics flag overrides)
  cloudwatch_log_group: "" # e.g., /aws/bedrock-agentcore/runtimes/<agent_runtime_id>-DEFAULT
```

## Environment Variables (Deployed Environments)

These are set by Terraform for AgentCore/Lambda deployments and override the corresponding `settings.yaml` keys:

| Environment Variable | settings.yaml Key | Description |
|---------------------|-------------------|-------------|
| `AWS_PROFILE` | `aws_profile` | AWS profile name |
| `AWS_REGION` | `aws_region` | AWS region |
| `SEARCH_BACKEND` | `agent.search_backend` | Backend: `opensearch`, `ripgrep`, `auto` |
| `OPENSEARCH_ENDPOINT` | `opensearch_endpoint` | OpenSearch Serverless endpoint URL |
| `OPENSEARCH_INDEX_NAME` | `opensearch_index` | OpenSearch index name |
| `GUIDELINES_PATH` | `guidelines_path` | Path to guidelines directory |
| `FHIR_API_ENABLED` | `fhir_api.enabled` | Enable the HealthLake data source |
| `HEALTHLAKE_DATASTORE_ENDPOINT` | `fhir_api.datastore_endpoint` | HealthLake `DatastoreEndpoint`, used verbatim |
| `FHIR_MAX_PAGES` | `fhir_api.max_pages` | Page cap per FHIR search; the candidate gate requires it to match the dataset's frozen value |

### Observability (Terraform-managed only)

These environment variables are set by Terraform on the AgentCore runtime and Lambda. They have no `settings.yaml` equivalent:

| Environment Variable | Description |
|---------------------|-------------|
| `OBSERVABILITY_ENABLED` | Enable SNS event emission |
| `OBSERVABILITY_SNS_TOPIC_ARN` | SNS topic for pipeline events |
| `OBSERVABILITY_BUCKET_NAME` | S3 bucket for trace storage |
| `STACK_NAME` | Stack identifier for metrics |
| `ENVIRONMENT` | Environment name (`dev`, `prod`) |

## Configuration Reference

### AWS

| Key | Default | Description |
|-----|---------|-------------|
| `aws_profile` | — | AWS profile name (optional) |
| `aws_region` | `us-east-1` | AWS region |
| `agent_arn` | — | Bedrock AgentCore runtime ARN |

### Model

| Key | Default | Description |
|-----|---------|-------------|
| `model.model_id` | `us.anthropic.claude-sonnet-5` | Bedrock model ID |
| `model.thinking_enabled` | `true` | Enable extended thinking |
| `model.thinking_budget_tokens` | `8192` | Max tokens for thinking |
| `model.cache_system_prompt` | `true` | Cache system prompt across calls |

### Agent

| Key | Default | Description |
|-----|---------|-------------|
| `agent.max_nudges` | `5` | Maximum nudges to generate |
| `agent.search_backend` | `auto` | Search backend: `opensearch`, `ripgrep`, or `auto` |

### Search & Guidelines

| Key | Default | Description |
|-----|---------|-------------|
| `opensearch_endpoint` | — | OpenSearch Serverless endpoint URL |
| `opensearch_index` | `guidelines` | OpenSearch index name |
| `guidelines_path` | `guidelines/` | Path to guidelines directory (relative to project root) |

### WebSocket API

| Key | Default | Description |
|-----|---------|-------------|
| `websocket_api_url` | — | `POST /ws-url` endpoint that returns a presigned AgentCore WebSocket URL |
| `api_key_secret_name` | — | Secrets Manager secret holding the `x-api-key` for that endpoint |

### Inference

| Key | Default | Description |
|-----|---------|-------------|
| `inference.samples` | `10` | Number of patient samples to process |
| `inference.seed` | `42` | Random seed for reproducibility |
| `inference.ccda_dir` | `tests/fixtures` | Directory containing CCDA XML files |
| `inference.fhir_dir` | `tests/fixtures` | Directory containing FHIR JSON files |
| `inference.output_dir` | `results` | Output directory for inference results |

### Tracing

| Key | Default | Description |
|-----|---------|-------------|
| `tracing.enabled` | `true` | Enable trace capture (override with `--no-trace`) |
| `tracing.publish_metrics` | `false` | Publish metrics to CloudWatch (override with `--publish-metrics`) |
| `tracing.cloudwatch_log_group` | — | CloudWatch log group for AgentCore traces |
