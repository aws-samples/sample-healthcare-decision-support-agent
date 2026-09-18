---
name: agentcore-deploy
description: Deploy Medical Nudging agent to AWS AgentCore runtime. Use when deploying code changes, updating Docker images, creating new runtimes, or testing deployed endpoints.
tags: [agentcore, deploy, aws, docker, lambda]
---

# AgentCore Deploy

## Overview
Deploy and test Medical Nudging agent to AWS AgentCore runtimes with Docker image management and Lambda API proxy setup.

## Usage
Use this skill when:
- Deploying code changes to AgentCore runtime
- Building and pushing new Docker images
- Creating or updating AgentCore runtimes
- Testing deployed Lambda API endpoints
- Force-updating a runtime with a new image

## Core Concepts

### Default Behavior
- Observability is on by default; pass `--no-observability` only in accounts without the Terraform data lake (the script exits early if the SNS topic is missing)
- Always create a NEW runtime with a new name (don't update existing)
- Ask user before deleting any runtime

### Image Tagging Convention
Use timestamp tags for safe rollbacks: `YYYYMMDD-HHMM` (e.g., `20260205-2108`)

### IAM Propagation
Wait 30+ seconds after creating IAM roles before creating AgentCore runtime (ECR permission errors otherwise).

## Quick Reference

### Deploy New Runtime (Full)
```bash
./scripts/deploy_agentcore.sh \
  --name YYYYMMDD_HHMM \
  --profile default \
  --assume-role arn:aws:iam::123456789012:role/MedicalNudgingSandboxTerraformRole
```

### Deploy with Existing Image (Skip Docker Build)
```bash
./scripts/deploy_agentcore.sh \
  --name YYYYMMDD_HHMM \
  --image-tag EXISTING_TAG \
  --skip-docker \
  --profile default \
  --assume-role arn:aws:iam::123456789012:role/MedicalNudgingSandboxTerraformRole
```

### Script Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `--name` | Yes | Runtime name suffix (creates `medical_nudging_<name>`) |
| `--image-tag` | No | Docker image tag (defaults to `--name` value) |
| `--profile` | No | AWS profile (omit to use the default credential chain) |
| `--no-observability` | No | Skip SNS observability events (needed when the Terraform data lake is not deployed) |
| `--skip-docker` | No | Skip Docker build, use existing image |
| `--assume-role` | No | IAM role ARN to assume |

### Force Update Existing Runtime
If user explicitly asks to update an existing runtime (not recommended):
1. Ask user to confirm deletion of the old runtime
2. Delete runtime:
   ```bash
   aws bedrock-agentcore-control delete-agent-runtime \
     --agent-runtime-id <RUNTIME_ID> --region us-east-1
   ```
3. Wait for deletion (~60s)
4. Redeploy with same name

## Testing Deployed Endpoint

### Get Credentials from Config
```bash
WS_API_URL=$(grep '^websocket_api_url:' config/settings.yaml | awk '{print $2}')
SECRET_NAME=$(grep '^api_key_secret_name:' config/settings.yaml | awk '{print $2}')
API_KEY=$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region us-east-1 \
  --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)["api_key"])')
```

### Request a Presigned WebSocket URL
```bash
curl -s -X POST "$WS_API_URL" -H "x-api-key: $API_KEY" | python3 -m json.tool
```

### Test with Pre-parsed Patient Data
Stream a run over the presigned WebSocket URL:
```bash
uv run scripts/run_ws_streaming.py --api-url "$WS_API_URL" --api-key "$API_KEY" \
  --patient-file tests/fixtures/sample_ccda.xml
```

The WebSocket payload is the same JSON the runtime accepts on `/invocations`, for example:
```json
{
    "patient_data": {
      "demographics": {"name": {"given": "Test", "family": "Patient"}, "dob": "1965-03-15", "gender": "female"},
      "problems": [{"description": "Type 2 Diabetes", "status": "active"}],
      "medications": [],
      "allergies": [],
      "vitals": [],
      "labs": [],
      "immunizations": [],
      "clinical_notes": []
    },
    "visit_context": {"visit_type": "ambulatory", "specialty": "general"}
}
```

### Visit Context Options
| Field | Values |
|-------|--------|
| `visit_type` | `ambulatory`, `inpatient`, `emergency` |
| `specialty` | `general`, `cardiology`, `endocrinology`, `pulmonology` |

## Debugging

### Check Runtime Logs (Requires Terraform Role)
```bash
CREDS=$(aws sts assume-role \
  --role-arn "arn:aws:iam::123456789012:role/MedicalNudgingSandboxTerraformRole" \
  --role-session-name "debug" \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text)

export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | awk '{print $1}')
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | awk '{print $2}')
export AWS_SESSION_TOKEN=$(echo "$CREDS" | awk '{print $3}')

aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT" \
  --start-time $(($(date +%s) * 1000 - 300000)) \
  --limit 20 --region us-east-1
```

### Common Errors
| Error | Cause | Fix |
|-------|-------|-----|
| 422 from runtime | Pydantic validation error in request payload | Check request JSON schema matches expected format |
| ECR access denied | IAM role not propagated | Wait 30s, retry |
| RuntimeClientError | Runtime restarting or unavailable | Wait and retry request |

### Deleting Old Runtimes
Only delete runtimes when user explicitly requests. To delete:
```bash
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> --region us-east-1
# Wait ~60s for deletion to complete
```

## Common Mistakes

### Updating Instead of Creating New Runtime
**Problem:** Trying to update existing runtime with new image.
**Fix:** Always create a new runtime with a new timestamp name. Old runtimes can be manually deleted later.

### Using `latest` Tag
**Problem:** Can't rollback, unclear which version is deployed.
**Fix:** Use timestamp tags: `YYYYMMDD-HHMM`

### `SNS topic 'observability-events' not found`
**Problem:** Observability is on by default but the Terraform data lake is not deployed in this account.
**Fix:** Deploy it (`enable_datalake_observability=true`) or pass `--no-observability`.
