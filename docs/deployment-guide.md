# Deployment Guide

This guide covers deploying the Medical Nudging system to AWS. Use Terraform to deploy all infrastructure from scratch. Use the shell script or targeted Terraform commands to update the AgentCore runtime after code changes.

## Deployment Methods

| Method | Use Case |
|--------|----------|
| Terraform (full) | First-time setup: deploys ECR, S3, OpenSearch, IAM, AgentCore, CloudWatch |
| Terraform (targeted) | Update AgentCore runtime after code changes |
| Shell Script | Quick AgentCore iteration during development |

Terraform triggers the CodeBuild image build and creates the runtime. A second
CodeBuild project, the [candidate-acceptance gate](#candidate-acceptance-gate-and-online-evaluation),
scores that runtime against the frozen regression dataset. The repository ships
these two build steps and the evaluation configuration, not a complete
CodePipeline: wire them into your own pipeline between "candidate deployed" and
"traffic routed".

---

## Prerequisites

### All Platforms

1. **AWS CLI v2**: [Installation Guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
   ```bash
   aws --version  # Requires 2.x
   aws sts get-caller-identity  # Verify access
   ```

2. **Docker Desktop** with buildx support for ARM64 images: [Windows](https://docs.docker.com/desktop/install/windows-install/) | [macOS](https://docs.docker.com/desktop/install/mac-install/) | [Linux](https://docs.docker.com/desktop/install/linux-install/)

3. **Terraform** v1.5.0 or later
   ```bash
   terraform --version
   ```

### Windows Setup

The shell scripts require Bash. Choose one option:

**Option A: WSL2 (Recommended)**
```powershell
wsl --install
# After restart, open Ubuntu terminal and proceed with Linux instructions
# Enable Docker Desktop WSL2 backend in Docker settings
```

**Option B: Git Bash**
```powershell
# Install Git for Windows from https://git-scm.com/download/win
# Open Git Bash to run shell scripts
```

**Option C: Terraform Only**

Use Terraform for all deployments. This works natively in PowerShell without Bash.

---

## Initial Setup: Deploy All Infrastructure

Use Terraform to deploy the complete infrastructure from scratch.

### Step 1: Configure Variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:
```hcl
aws_region  = "us-east-1"
stack_name  = "medical-nudging"
agent_name  = "MedicalNudging"
environment = "dev"

model_id   = "us.anthropic.claude-sonnet-5"
max_nudges = 5

opensearch_enabled    = true
search_backend        = "opensearch"
enable_kms_encryption = true

# Optional features
websocket_api_enabled         = true   # presigned-URL WebSocket API (the only client path)
enable_datalake_observability = false
```

### Step 2: Deploy

```bash
cd terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

Deployment takes 10-15 minutes. OpenSearch collection creation is the longest step at 5 minutes.

### Step 3: Upload Guidelines

```bash
BUCKET=$(terraform output -raw guidelines_bucket_name)
aws s3 sync guidelines/pdfs/ s3://$BUCKET/pdfs/
```

### Step 4: Verify

```bash
terraform output
```

---

## Candidate acceptance gate and online evaluation

[docs/evaluation.md](evaluation.md) defines four evaluation layers and where each
runs. This section is the deployment half of that table: a regression gate on every
candidate runtime, and sampled online evaluation of the runtime that serves traffic.
Neither produces evidence of clinical safety, correctness, or effectiveness. The gate
establishes that a candidate still completes fixed scenarios inside budget and keeps
every nudge honest about its retrieved evidence; online scores are trend signals.

### 1. Publish the regression dataset as an immutable version

`config/evals/regression_dataset_v1.json` holds eight reviewed scenarios in the
AgentCore predefined dataset schema: patient identifiers from the open MIMIC-IV FHIR
demo, the frozen generator configuration, and behavioural assertions. No patient
records. Publish it once to AgentCore Dataset Management and pin the version number:

```bash
uv sync --extra dev --extra evals
uv run python -m evals.agentcore_dataset publish \
  --file config/evals/regression_dataset_v1.json \
  --name medical_nudging_regression --region us-east-1 \
  --pointer ~/nudge-evaluations/regression_dataset_pointer.json
```

The command prints the dataset id and the published version. Every dataset has one
mutable Draft and numbered immutable versions; the gate only ever runs a published
version, so a later Draft edit cannot change what an earlier gate run scored. Put the
id and version in `terraform.tfvars`:

```hcl
regression_dataset_id      = "<dataset id>"
regression_dataset_version = "1"
```

Without these two values the gate falls back to the checked-in file and records its
SHA-256 instead.

### 2. Deploy the candidate with the settings the dataset was frozen on

The dataset's `generator_config` pins the corpus index and the FHIR page cap. The
runtime refuses an evidence request whose frozen settings it cannot honour
(HTTP 409), so set them on the runtime. With `healthlake_enabled = true` the stack
wires `FHIR_API_ENABLED` and `HEALTHLAKE_DATASTORE_ENDPOINT` itself, so only the index
name and page cap remain:

```hcl
# The frozen corpus and coverage window from config/evals/blog_opus5.json.
opensearch_index_name = "guidelines-icu-baseline-v3"
environment_variables = {
  FHIR_MAX_PAGES = "50"
}

# Reusing a datastore another stack created? Point the runtime at it explicitly:
# environment_variables = {
#   FHIR_API_ENABLED              = "true"
#   HEALTHLAKE_DATASTORE_ENDPOINT = "<terraform output healthlake_datastore_endpoint>"
#   FHIR_MAX_PAGES                = "50"
# }
# Reusing a datastore another stack created? Grant the runtime read access to it.
fhir_datastore_arn = "arn:aws:healthlake:us-east-1:123456789012:datastore/fhir/<id>"
```

The runtime role gets HealthLake read/search on that datastore (or on the stack's own
datastore when `healthlake_enabled = true`).

### 3. Run the gate against the candidate

`terraform apply` creates the `${stack_name}-candidate-gate` CodeBuild project
(`candidate_gate_enabled`, default true; no cost until a build runs). Run it after
the runtime exists:

```bash
# From a delivery pipeline or the console: start the gate build
aws codebuild start-build --project-name medical-nudging-candidate-gate

# Or during apply, once the runtime is created; a failing gate fails the apply
terraform apply -var="run_candidate_gate_on_apply=true"

# Or from a workstation (the runtime ARN is a Terraform output)
uv run python -m evals.agentcore_gate \
  --runtime-arn "$(terraform -chdir=terraform output -raw agent_runtime_arn)" \
  --config config/evals/blog_opus5.json \
  --thresholds config/evals/regression_thresholds_v1.json \
  --dataset-id <dataset id> --dataset-version 1 \
  --output-dir ~/nudge-evaluations/gate --profile YOUR_PROFILE
```

What one run does:

1. Loads the pinned dataset version and checks its frozen `model`, `agent`, and
   `generator_config` against the arm config.
2. Invokes the candidate once per scenario through the AgentCore
   `OnDemandEvaluationDatasetRunner`, with `config.return_evidence = true`. The
   runtime runs the contract-steered generator and returns its execution trace, tool
   ledger, query coverage, and per-nudge verifier reports next to the response.
3. Waits for CloudWatch span ingestion, collects the candidate's spans, and applies
   the configured AgentCore evaluators (`Builtin.GoalSuccessRate` over the dataset
   assertions by default). These are recorded as trend signals; they gate only if
   `min_agentcore_scores` is added to the thresholds file.
4. Applies the frozen deterministic evaluator set from `evals/layers.py` to the
   returned evidence: every Layer 1 gate and every Layer 2 evidence-contract check,
   plus the latency and token ceilings.
5. Applies `config/evals/regression_thresholds_v1.json`: 100% completion, zero
   contract violations, and per-patient latency and token ceilings frozen at 1.25
   times the saved baseline maxima. Any drift between the runtime's effective settings
   and the dataset's frozen settings is also a failure.
6. Writes `result.json` (verdict, per-scenario check labels, and the metadata below)
   and `records.json` (the raw evidence, which carries patient records and stays
   outside the repository and outside the CodeBuild artifact), then exits 0 on pass,
   1 on fail, 2 when the gate could not run.

`result.json` keeps evaluator versions, rule hashes, the implementation hash,
AgentCore SDK version, dataset id/version/content hash, runtime ARN/version/image,
thresholds, and sampling together in `metadata`, so a verdict can be disputed later.

Each live run invokes the generator once per scenario at the frozen configuration
(Opus 5, high effort). Expect roughly the per-patient cost of the saved baseline run
times eight, plus a few cents of evaluator calls.

AgentCore Runtime hard-caps a synchronous invocation at 15 minutes. The frozen
per-patient latency threshold (about 10.5 minutes) sits below that, so a scenario
that would hit the platform cap already fails the gate on latency. Concurrency is a
wall-clock-versus-latency trade: the heaviest readiness-cohort patients drive long
multi-retry steered generations, and running several at once makes each slower
because they contend for the same account's Bedrock Opus throughput. Run the gate at
`--concurrency 1` (Terraform `gate_concurrency = 1`) for the readiness cohort; raise
it only for lighter datasets.

### 4. Prove the gate fails

A gate that has never failed proves nothing. Two checks, both without patient data:

```bash
# Known-bad fixture: one synthetic nudge cites a source the agent never retrieved.
# Exit code 1; result.json names citation_resolution:violates_contract.
uv run python -m evals.agentcore_gate \
  --config config/evals/blog_opus5.json \
  --thresholds config/evals/regression_thresholds_v1.json \
  --replay-records tests/fixtures/evals/known_bad_records.json \
  --output-dir ~/nudge-evaluations/gate-known-bad

# Same fixture through the CodeBuild project (overrides one variable for this build)
python terraform/scripts/wait_for_codebuild.py \
  --project-name medical-nudging-candidate-gate --region us-east-1 \
  --env GATE_REPLAY_RECORDS=tests/fixtures/evals/known_bad_records.json
```

The second command exits nonzero when the build fails, which is the expected result.
An impossible threshold works the same way: copy the thresholds file, set
`latency_ms` to `1`, and pass it as `--thresholds`.

**Transport.** The gate asks for the evidence response as server-sent events
(`Accept: text/event-stream`). The runtime sends a `keepalive` event every 15 seconds
while the steered generation runs, then the record as `chunk` events (at most 1 MiB
each) and one `end` event with the byte count and SHA-256. A plain synchronous body
that stays silent for more than about five minutes never reaches the client, although
the runtime logs a 200; streaming keeps the connection alive without writing the record
anywhere but memory on either side. A runtime built before this change still answers
with one JSON body and the gate accepts it, up to the AgentCore 15-minute synchronous
cap.

### 5. Turn on online evaluation for the runtime that serves traffic

Online evaluation is an AgentCore configuration that samples completed sessions from
CloudWatch and scores them asynchronously with the same built-in evaluators. It adds
no request latency, and it is **off by default** because every sampled session costs
evaluator model invocations:

```hcl
online_evaluation_enabled             = true
online_evaluation_sampling_percentage = 1.0   # start near 1% for real traffic
online_evaluation_evaluators          = ["Builtin.GoalSuccessRate"]
```

The default roster is deliberately one evaluator. `Builtin.GoalSuccessRate` is the
worked example of how a built-in evaluator is wired, sampled, and read; it is not a
claim that it is the right monitor for clinical nudges. Which further evaluators are
worth running live (for example `Builtin.Faithfulness` against the retrieved passages,
`Builtin.InstructionFollowing` after a prompt change, or a validated custom
evidence-support evaluator) is an output of the clinician feedback cycle on the nudges,
not an up-front choice. Every score stays an operational trend signal.

Use a high sampling percentage only for a bounded demonstration, then lower it. Tune
for volume, cost, and coverage. Results land in the log group named by
`terraform output online_evaluation_results_log_group` and as metrics in the
`Bedrock-AgentCore/Evaluations` namespace, next to the sample's latency, error, cost,
and tool-health telemetry ([observability guide](observability-guide.md)).

The pinned ADOT distro uses split telemetry: spans go to the shared `aws/spans` log
group and the model and tool payloads go to the runtime's own log group in the
`otel-rt-logs` stream. The evaluation service reads both to reconstruct a session, so
Terraform always adds the runtime log group to `online_evaluation_log_group_names`. If
only `aws/spans` is configured, the built-in evaluators return a
`LogEventMissingException` because the payload event records are absent. On the unified
span destination (ADOT 0.18 or later with `UNIFIED_TRACES_DESTINATION_ENABLED=true`)
the runtime log group alone carries both. The
evaluators here are broad quality-trend signals. A custom evidence-support evaluator
belongs in this list only after it has passed the controlled-corruption check in
`docs/evaluation.md` and the trace context it needs is reliably present.

### 6. Close the loop

A low-scoring or failed online session is a review candidate, not a dataset entry.
After human review, a de-identified scenario (patient identifier from the open demo
data, run configuration, assertions) may be added to the dataset Draft, and the Draft
published as the next immutable version, which the gate then pins. Never ingest
production clinical traces automatically.

### 7. Version the prompt without redeploying

Reviewer and clinician feedback mostly lands as prompt changes. With the prompt baked
into the container, every wording change is an image build, a runtime update, and a
gate run before anyone can compare. AgentCore configuration bundles keep the base
system prompt as service-side configuration instead: every update is an immutable
version with a commit message and a parent, the runtime applies whichever version a
request names, and the same gate scores the new version before it is promoted. This
is optional and off by default. Local runs and the paper's experiments never touch it;
`prompts/orchestrator.md` stays the prompt of record in the repository.

**Create the bundle** once per runtime. Version 1 is the repository prompt, so the
first gate run under a bundle scores the same prompt as before:

```bash
# During apply (writes terraform/config_bundle.json, gitignored) ...
terraform apply -var="config_bundle_enabled=true"

# ... or from a workstation
uv run scripts/config_bundle.py create \
  --runtime-arn "$(terraform -chdir=terraform output -raw agent_runtime_arn)" \
  --name medical_nudging_prompt --message "Initial prompt from prompts/orchestrator.md"
```

The runtime's execution role can read bundle versions; the bundle's component key is
the runtime ARN. The runtime is named after the stack, so image rebuilds keep its ARN; if the
runtime is ever re-created, `create` on the existing bundle name publishes a new version that
carries the prompt for the new ARN, and the history stays in one bundle.

**Publish a change** as the next version. Edit a copy of the prompt, then:

```bash
uv run scripts/config_bundle.py update --bundle-id <bundle id> \
  --runtime-arn "$(terraform -chdir=terraform output -raw agent_runtime_arn)" \
  --prompt-file /path/to/edited-orchestrator.md \
  --message "Ask for the supporting passage before recommending a repeat test" \
  --created-by "clinician review 2026-09"

uv run scripts/config_bundle.py versions --bundle-id <bundle id>
uv run scripts/config_bundle.py diff --bundle-id <bundle id> --runtime-arn <arn> --from <v1> --to <v2>
```

The version list and the diff are the audit trail: who changed what, when, and why,
with each version's parent. A recommendation from an AgentCore batch evaluation can be
published the same way (`--prompt-file` with the recommended prompt).

**Gate the new version** before anything serves it. The gate sends the version in the
request's `baggage` header, the runtime applies it and reports it back, and the gate
fails on any mismatch, so the verdict is tied to exactly one (runtime version, bundle
version) pair:

```bash
uv run python -m evals.agentcore_gate \
  --runtime-arn "$(terraform -chdir=terraform output -raw agent_runtime_arn)" \
  --config config/evals/blog_opus5.json \
  --thresholds config/evals/regression_thresholds_v1.json \
  --dataset-id <dataset id> --dataset-version 1 \
  --bundle-id <bundle id> --bundle-version <new version> \
  --output-dir ~/nudge-evaluations/gate-prompt-v2 --profile YOUR_PROFILE
```

`result.json` records `metadata.runtime.config_bundle` (id, version, commit message,
parent, prompt SHA-256) next to `runtime_version`, and every run record carries the
same `prompt` block. In CodeBuild, set `config_bundle_id` and `config_bundle_version`
in `terraform.tfvars` and the gate project pins that version.

**Promote or roll back** by pinning. The runtime has no "current" bundle version; the
caller names one per request. Promotion is changing the version your gateway, Lambda
proxy, or pipeline sends in `baggage`; rollback is sending the previous one. A request
with no `baggage` gets the repository prompt, and a request naming a version the
runtime cannot fetch, or one with no section for that runtime, is refused with
HTTP 409 rather than silently served with a different prompt.

Only `system_prompt` is read from a bundle. The gate pins the model in the frozen
dataset configuration and fails on drift, so a model change is a dataset re-freeze,
not a hot-swap. The runtime records any other bundle keys as ignored.

### Boundaries

- The public demonstration uses the open-access MIMIC-IV FHIR demo. Real clinical
  deployments require customer-specific decisions about trace content, PHI handling,
  access, retention, consent, and review before any trace is sampled or evaluated.
- Evaluator scores, built-in or custom, are trend signals. Passing the gate means the
  candidate did not regress on fixed, reviewed scenarios. It does not mean the advice
  is clinically correct.
- The evidence returned by `config.return_evidence` contains raw patient records.
  Only the gate role can invoke the runtime this way, and the gate writes it only
  outside the working tree.
- A prompt version that passes the gate did not regress on fixed scenarios. Online
  scores and service recommendations remain operational signals; neither is evidence
  that a prompt change is clinically safer.

---

## Updating AgentCore Runtime After Code Changes

After modifying `src/`, `agent.py`, `Dockerfile`, `prompts/`, or `guidelines/`, rebuild and redeploy the AgentCore runtime.

### Option A: Terraform (Recommended)

```bash
cd terraform

# Trigger CodeBuild to rebuild Docker image
terraform apply -replace="null_resource.trigger_build"

# Or update AgentCore runtime directly
terraform apply -target=aws_bedrockagentcore_agent_runtime.medical_nudging
```

### Option B: Shell Script (Quick Iteration)

The shell script creates a new AgentCore runtime for rapid development cycles:

```bash
./scripts/deploy_agentcore.sh --name 20260205
./scripts/deploy_agentcore.sh --name 20260205 --no-observability
./scripts/deploy_agentcore.sh --name 20260205 --skip-docker --image-tag 20260204
```

| Option | Description |
|--------|-------------|
| `--name NAME` | Required. Runtime name suffix (creates `medical_nudging_NAME`) |
| `--profile PROFILE` | AWS profile (default: the standard AWS credential chain) |
| `--region REGION` | AWS region (default: `us-east-1`) |
| `--no-observability` | Skip SNS event emission. Required when the Terraform data lake (`enable_datalake_observability`) is not deployed in the account; observability is on by default |
| `--skip-docker` | Skip Docker build and use existing image |
| `--image-tag TAG` | Override image tag (default: uses `--name`) |
| `--assume-role ARN` | Assume IAM role before running |

The script reuses the ECR repository and the `medical-nudging-guidelines` OpenSearch collection created by `terraform apply` (and the observability SNS topic unless `--no-observability`), so run Terraform first. It builds the Docker image, creates IAM roles, adds the new execution role to the collection's data access policy, creates the AgentCore runtime, generates an API key in Secrets Manager, creates the WebSocket presigned-URL API (API Gateway + Lambda authorizer + `ws_url_generator` Lambda + WAF), and updates `config/settings.yaml`. The WebSocket API is the only client path; the runtime has no public HTTP endpoint.

Output:
```
AgentCore Runtime:
  Name:     medical_nudging_20260205
  ARN:      arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/...

WebSocket API:
  URL:      https://xxxxx.execute-api.us-east-1.amazonaws.com/v1/ws-url
  Secret:   medical-nudging/20260205/api-key (Secrets Manager)
```

---

## Testing the Deployment

Read the API key from Secrets Manager and request a presigned WebSocket URL:
```bash
API_KEY=$(aws secretsmanager get-secret-value --secret-id medical-nudging/20260205/api-key \
  --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)["api_key"])')

curl -s -X POST "https://xxxxx.execute-api.us-east-1.amazonaws.com/v1/ws-url" \
  -H "x-api-key: $API_KEY" | python3 -m json.tool
```

Stream a full run end to end (see [Streaming WebSocket API](streaming-websocket-api.md)):
```bash
uv run scripts/run_ws_streaming.py \
  --api-url "https://xxxxx.execute-api.us-east-1.amazonaws.com/v1/ws-url" \
  --api-key "$API_KEY" \
  --patient-file tests/fixtures/sample_ccda.xml
```

Run inference script (invokes the runtime directly with IAM credentials):
```bash
uv sync
uv run scripts/run_inference.py --mode agentcore --samples 2
```

---

## Cleanup

### Terraform-Managed Resources

```bash
cd terraform
aws s3 rm s3://$(terraform output -raw guidelines_bucket_name) --recursive
aws s3 rm s3://$(terraform output -raw source_bucket_name) --recursive
terraform destroy
```

### Script-Created Resources

Delete resources created by `deploy_agentcore.sh` manually:

```bash
NAME=20260205

# WebSocket API: WAF association, REST API, Lambdas, layer, role
API_ID=$(aws apigateway get-rest-apis --query "items[?name=='medical-nudging-${NAME}-websocket-api'].id | [0]" --output text)
WAF_ARN=$(aws wafv2 list-web-acls --scope REGIONAL --query "WebACLs[?Name=='medical-nudging-${NAME}-websocket-waf'].ARN | [0]" --output text)
aws wafv2 disassociate-web-acl --resource-arn "arn:aws:apigateway:us-east-1::/restapis/${API_ID}/stages/v1"
WAF_ID=$(aws wafv2 list-web-acls --scope REGIONAL --query "WebACLs[?Name=='medical-nudging-${NAME}-websocket-waf'].Id | [0]" --output text)
LOCK=$(aws wafv2 get-web-acl --scope REGIONAL --name "medical-nudging-${NAME}-websocket-waf" --id "$WAF_ID" --query LockToken --output text)
aws wafv2 delete-web-acl --scope REGIONAL --name "medical-nudging-${NAME}-websocket-waf" --id "$WAF_ID" --lock-token "$LOCK"
aws apigateway delete-rest-api --rest-api-id "$API_ID"
aws lambda delete-function --function-name "medical-nudging-${NAME}-ws-url-generator"
aws lambda delete-function --function-name "medical-nudging-${NAME}-api-authorizer"
for v in $(aws lambda list-layer-versions --layer-name "medical-nudging-${NAME}-agentcore-sdk" --query 'LayerVersions[].Version' --output text); do
  aws lambda delete-layer-version --layer-name "medical-nudging-${NAME}-agentcore-sdk" --version-number "$v"
done
aws iam delete-role-policy --role-name "medical-nudging-${NAME}-websocket-lambda-role" --policy-name WebSocketLambdaPolicy
aws iam delete-role --role-name "medical-nudging-${NAME}-websocket-lambda-role"

# Runtime and its execution role
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <runtime-id>
aws iam delete-role-policy --role-name "medical-nudging-${NAME}-agent-execution-role" --policy-name AgentCoreExecutionPolicy
aws iam delete-role --role-name "medical-nudging-${NAME}-agent-execution-role"

# API key
aws secretsmanager delete-secret --secret-id "medical-nudging/${NAME}/api-key" --force-delete-without-recovery
```

---

## Troubleshooting

**Docker build fails on Windows**: Enable WSL2 backend in Docker Desktop and run from WSL2 terminal.

**AgentCore runtime stuck in PENDING**:
```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id>
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-name> --follow
```

**`POST /ws-url` returns 401/403**: Verify the `x-api-key` header matches the value in Secrets Manager. Check the WebSocket Lambda role for `secretsmanager:GetSecretValue` and `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`.

---

## Related Documentation

- [Terraform README](../terraform/README.md): Full Terraform configuration reference
- [Streaming WebSocket API](streaming-websocket-api.md): presigned-URL flow, WAF, and streaming protocol
- [Observability Guide](observability-guide.md): Monitoring and analytics
