#!/usr/bin/env bash
#
# Deploy AgentCore Runtime with the WebSocket presigned-URL API
#
# This script creates a NEW AgentCore runtime (keeping existing ones intact)
# to avoid Terraform state conflicts and KMS issues.
#
# Usage:
#   ./scripts/deploy_agentcore.sh [OPTIONS]
#
# Options:
#   --name NAME           Runtime name suffix (default: YYYYMMDD-HHMM timestamp)
#   --profile PROFILE     AWS profile (default: default credential chain)
#   --region REGION       AWS region (default: us-east-1)
#   --assume-role ARN     Assume this IAM role before running (e.g., Terraform role)
#   --no-observability    Disable observability (enabled by default)
#   --skip-docker         Skip Docker build and push (use existing image)
#   --image-tag TAG       Override image tag (default: uses --name value)
#   -h, --help            Show this help message
#
# Examples:
#   ./scripts/deploy_agentcore.sh                          # timestamp name, observability on
#   ./scripts/deploy_agentcore.sh --name demo              # named "demo", observability on
#   ./scripts/deploy_agentcore.sh --name test --skip-docker --image-tag 20260202
#   ./scripts/deploy_agentcore.sh --assume-role arn:aws:iam::123456789012:role/MedicalNudgingSandboxTerraformRole
#
# This script:
#   1. Builds and pushes Docker image to existing ECR
#   2. Creates new IAM role for AgentCore execution
#   3. Creates new AgentCore runtime
#   4. Generates an API key and stores it in Secrets Manager
#   5. Creates the WebSocket presigned-URL API (API Gateway + Lambda authorizer
#      + ws_url_generator Lambda + WAF) — the only client path into the agent
#   6. Updates config/settings.yaml with the new runtime ARN and API URL

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

# Default values
# Empty means the standard credential chain (env vars, SSO, instance role); a named
# profile is only used when AWS_PROFILE is set or --profile is given.
AWS_PROFILE="${AWS_PROFILE:-}"
PROFILE_OPT=""
if [[ -n "$AWS_PROFILE" ]]; then
  PROFILE_OPT="--profile $AWS_PROFILE"
fi
AWS_REGION="${AWS_REGION:-us-east-1}"
ASSUME_ROLE_ARN=""
SKIP_DOCKER=false
ENABLE_OBSERVABILITY=true
RUNTIME_NAME=""
IMAGE_TAG=""

# Existing resources to reuse
ECR_REPO="medical-nudging-medical-nudging-agent"
GUIDELINES_BUCKET="medical-nudging-guidelines"
OPENSEARCH_COLLECTION_NAME="medical-nudging-guidelines"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --name)
      RUNTIME_NAME="$2"
      shift 2
      ;;
    --profile)
      AWS_PROFILE="$2"
      PROFILE_OPT="--profile $2"
      shift 2
      ;;
    --region)
      AWS_REGION="$2"
      shift 2
      ;;
    --assume-role)
      ASSUME_ROLE_ARN="$2"
      shift 2
      ;;
    --observability)
      ENABLE_OBSERVABILITY=true
      shift
      ;;
    --no-observability)
      ENABLE_OBSERVABILITY=false
      shift
      ;;
    --skip-docker)
      SKIP_DOCKER=true
      shift
      ;;
    --image-tag)
      IMAGE_TAG="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --name NAME           Runtime name suffix (default: YYYYMMDD-HHMM timestamp)"
      echo "  --profile PROFILE     AWS profile (default: default credential chain)"
      echo "  --region REGION       AWS region (default: us-east-1)"
      echo "  --assume-role ARN     Assume this IAM role before running"
      echo "  --no-observability    Disable observability (enabled by default)"
      echo "  --skip-docker         Skip Docker build and push (use existing image)"
      echo "  --image-tag TAG       Override image tag (default: uses --name value)"
      echo "  -h, --help            Show this help message"
      echo ""
      echo "Examples:"
      echo "  $0                                    # timestamp name, observability on"
      echo "  $0 --name demo                        # named 'demo', observability on"
      echo "  $0 --name test --skip-docker --image-tag 20260202"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Default name to timestamp if not specified
if [[ -z "$RUNTIME_NAME" ]]; then
  RUNTIME_NAME="$(date +%Y%m%d-%H%M)"
fi

# Default image tag to runtime name if not specified
if [[ -z "$IMAGE_TAG" ]]; then
  IMAGE_TAG="$RUNTIME_NAME"
fi

# Resource naming based on --name
# AgentCore runtime names must match [a-zA-Z][a-zA-Z0-9_]{0,47} — replace hyphens with underscores
FULL_RUNTIME_NAME="medical_nudging_${RUNTIME_NAME//-/_}"
AGENT_EXECUTION_ROLE_NAME="medical-nudging-${RUNTIME_NAME}-agent-execution-role"
API_KEY_SECRET_NAME="medical-nudging/${RUNTIME_NAME}/api-key"

# WebSocket API resource names
WS_URL_GENERATOR_NAME="medical-nudging-${RUNTIME_NAME}-ws-url-generator"
WS_AUTHORIZER_NAME="medical-nudging-${RUNTIME_NAME}-api-authorizer"
WS_LAMBDA_ROLE_NAME="medical-nudging-${RUNTIME_NAME}-websocket-lambda-role"
WS_API_NAME="medical-nudging-${RUNTIME_NAME}-websocket-api"

# =============================================================================
# Assume Role (if specified)
# =============================================================================

if [[ -n "$ASSUME_ROLE_ARN" ]]; then
  echo "Assuming role: $ASSUME_ROLE_ARN"
  CREDS=$(aws $PROFILE_OPT --region "$AWS_REGION" sts assume-role \
    --role-arn "$ASSUME_ROLE_ARN" \
    --role-session-name "deploy-$(date +%H%M%S)" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text)
  
  export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | awk '{print $1}')
  export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | awk '{print $2}')
  export AWS_SESSION_TOKEN=$(echo "$CREDS" | awk '{print $3}')
  unset AWS_PROFILE
  
  # Use region-only opts when using assumed role credentials
  AWS_OPTS="--region $AWS_REGION"
else
  AWS_OPTS="$PROFILE_OPT --region $AWS_REGION"
fi

# Get script and project directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_ROOT/.build"
CONFIG_FILE="$PROJECT_ROOT/config/settings.yaml"

# Create build directory
mkdir -p "$BUILD_DIR"

# =============================================================================
# Helper Functions
# =============================================================================

create_or_update_api_key_secret() {
  local secret_name="$1"
  local api_key="$2"

  echo "Creating/updating Secrets Manager secret: $secret_name"

  # Check if secret exists
  if aws $AWS_OPTS secretsmanager describe-secret --secret-id "$secret_name" &>/dev/null; then
    # Update existing secret
    aws $AWS_OPTS secretsmanager put-secret-value \
      --secret-id "$secret_name" \
      --secret-string "{\"api_key\": \"$api_key\"}" \
      --output text > /dev/null
    echo "Updated existing secret: $secret_name"
  else
    # Create new secret
    aws $AWS_OPTS secretsmanager create-secret \
      --name "$secret_name" \
      --description "API key for the Medical Nudging WebSocket presigned-URL API - $RUNTIME_NAME" \
      --secret-string "{\"api_key\": \"$api_key\"}" \
      --tags "Key=Project,Value=MedicalNudging" "Key=RuntimeName,Value=$RUNTIME_NAME" \
      --output text > /dev/null
    echo "Created new secret: $secret_name"
  fi
}

echo "=============================================="
echo "AgentCore Deployment"
echo "=============================================="
echo "Profile:       ${AWS_PROFILE:-<default credential chain>}"
echo "Region:        $AWS_REGION"
echo "Runtime:       $FULL_RUNTIME_NAME"
echo "Image Tag:     $IMAGE_TAG"
echo "Observability: $ENABLE_OBSERVABILITY"
if [[ -n "$ASSUME_ROLE_ARN" ]]; then
echo "Assumed Role:  $ASSUME_ROLE_ARN"
fi
echo ""

# Get account ID
ACCOUNT_ID=$(aws $AWS_OPTS sts get-caller-identity --query Account --output text)
echo "Account:       $ACCOUNT_ID"
echo ""

ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# =============================================================================
# Step 0: Discover SNS Topic (if observability enabled)
# =============================================================================

SNS_TOPIC_ARN=""
if [[ "$ENABLE_OBSERVABILITY" == "true" ]]; then
  echo ">>> Step 0: Discovering observability SNS topic..."

  SNS_TOPIC_ARN=$(aws $AWS_OPTS sns list-topics \
    --query "Topics[?contains(TopicArn, 'observability-events')].TopicArn | [0]" \
    --output text 2>/dev/null || echo "")

  if [[ -z "$SNS_TOPIC_ARN" || "$SNS_TOPIC_ARN" == "None" ]]; then
    echo "ERROR: SNS topic 'observability-events' not found"
    echo "Ensure enable_datalake_observability=true was applied via Terraform"
    exit 1
  fi
  echo "Found SNS topic: $SNS_TOPIC_ARN"
  echo ""
fi

# =============================================================================
# Step 1: Build and Push Docker Image
# =============================================================================

if [[ "$SKIP_DOCKER" == "true" ]]; then
  echo ">>> Skipping Docker build (--skip-docker specified)"
else
  echo ">>> Step 1: Building and pushing Docker image..."

  # Login to ECR
  echo "Logging in to ECR..."
  aws $AWS_OPTS ecr get-login-password | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  # Create buildx builder if not exists
  docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch

  # Build and push ARM64 image
  echo "Building ARM64 image with tag: $IMAGE_TAG"
  cd "$PROJECT_ROOT"
  docker buildx build --platform linux/arm64 \
    -t "${ECR_URI}:${IMAGE_TAG}" \
    -f Dockerfile --push .

  echo "Docker image pushed: ${ECR_URI}:${IMAGE_TAG}"
fi

echo ""

# =============================================================================
# Step 2: Create AgentCore Execution IAM Role
# =============================================================================

echo ">>> Step 2: Creating AgentCore execution IAM role..."

# Check if role exists
AGENT_ROLE_ARN=$(aws $AWS_OPTS iam get-role --role-name "$AGENT_EXECUTION_ROLE_NAME" \
  --query "Role.Arn" --output text 2>/dev/null || echo "")

if [[ -z "$AGENT_ROLE_ARN" ]]; then
  echo "Creating IAM role: $AGENT_EXECUTION_ROLE_NAME"

  # Create trust policy
  TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeRolePolicy",
    "Effect": "Allow",
    "Principal": {
      "Service": "bedrock-agentcore.amazonaws.com"
    },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {
        "aws:SourceAccount": "${ACCOUNT_ID}"
      },
      "ArnLike": {
        "aws:SourceArn": "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:*"
      }
    }
  }]
}
EOF
)

  aws $AWS_OPTS iam create-role \
    --role-name "$AGENT_EXECUTION_ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --tags "Key=Project,Value=MedicalNudging" "Key=RuntimeName,Value=$RUNTIME_NAME" \
    --output text > /dev/null

  AGENT_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${AGENT_EXECUTION_ROLE_NAME}"
  echo "Created role: $AGENT_ROLE_ARN"

  # Wait for role to propagate (IAM is eventually consistent)
  echo "Waiting for role to propagate..."
  sleep 60
else
  echo "Using existing role: $AGENT_ROLE_ARN"
fi

# Get OpenSearch collection ARN from existing resources
echo "Looking up OpenSearch collection..."
OPENSEARCH_COLLECTION_ARN=$(aws $AWS_OPTS opensearchserverless list-collections \
  --query "collectionSummaries[?name=='${OPENSEARCH_COLLECTION_NAME}'].arn | [0]" \
  --output text 2>/dev/null || echo "")

if [[ -z "$OPENSEARCH_COLLECTION_ARN" || "$OPENSEARCH_COLLECTION_ARN" == "None" ]]; then
  # Fallback to wildcard
  OPENSEARCH_COLLECTION_ARN="arn:aws:aoss:${AWS_REGION}:${ACCOUNT_ID}:collection/*"
  echo "OpenSearch collection not found, using wildcard: $OPENSEARCH_COLLECTION_ARN"
else
  echo "Found OpenSearch collection: $OPENSEARCH_COLLECTION_ARN"
fi

# Get ECR repository ARN
ECR_REPO_ARN="arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${ECR_REPO}"

# Build policy statements
SNS_STATEMENT=""
if [[ "$ENABLE_OBSERVABILITY" == "true" && -n "$SNS_TOPIC_ARN" ]]; then
  # Get SNS KMS key ID if topic is encrypted
  SNS_KMS_KEY_ID=$(aws $AWS_OPTS sns get-topic-attributes \
    --topic-arn "$SNS_TOPIC_ARN" \
    --query "Attributes.KmsMasterKeyId" --output text 2>/dev/null || echo "")
  
  KMS_STATEMENT=""
  if [[ -n "$SNS_KMS_KEY_ID" && "$SNS_KMS_KEY_ID" != "None" ]]; then
    # Convert key ID to full ARN if needed
    if [[ "$SNS_KMS_KEY_ID" != arn:* ]]; then
      SNS_KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${SNS_KMS_KEY_ID}"
    else
      SNS_KMS_KEY_ARN="$SNS_KMS_KEY_ID"
    fi
    echo "SNS topic uses KMS key: $SNS_KMS_KEY_ARN"
    KMS_STATEMENT=',
    {
      "Sid": "KMSForSNS",
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
      "Resource": "'"$SNS_KMS_KEY_ARN"'"
    }'
  fi
  
  # S3 write permission for traces
  S3_BUCKET="medical-nudging-observability-${ACCOUNT_ID}"
  S3_KMS_KEY_ARN=$(aws $AWS_OPTS s3api get-bucket-encryption \
    --bucket "$S3_BUCKET" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.KMSMasterKeyID' \
    --output text 2>/dev/null || echo "")
  
  S3_KMS_STATEMENT=""
  if [[ -n "$S3_KMS_KEY_ARN" && "$S3_KMS_KEY_ARN" != "None" ]]; then
    echo "S3 bucket uses KMS key: $S3_KMS_KEY_ARN"
    S3_KMS_STATEMENT=',
    {
      "Sid": "KMSForS3",
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
      "Resource": "'"$S3_KMS_KEY_ARN"'"
    }'
  fi
  
  S3_STATEMENT=',
    {
      "Sid": "S3ObservabilityWrite",
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::'"${S3_BUCKET}"'/traces/*"
    }'"$S3_KMS_STATEMENT"
  
  SNS_STATEMENT=',
    {
      "Sid": "SNSObservabilityPublish",
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "'"$SNS_TOPIC_ARN"'"
    }'"$KMS_STATEMENT""$S3_STATEMENT"
fi

# Create/update inline policy
echo "Updating IAM policy..."
POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRImageAccess",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "${ECR_REPO_ARN}"
    },
    {
      "Sid": "ECRTokenAccess",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogStreams",
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"
    },
    {
      "Sid": "XRayTracing",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchMetrics",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": ["bedrock-agentcore", "MedicalNudging"]
        }
      }
    },
    {
      "Sid": "BedrockModelInvocation",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-*",
        "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
        "arn:aws:bedrock:*::foundation-model/meta.llama*",
        "arn:aws:bedrock:*::foundation-model/us.meta.llama*",
        "arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:inference-profile/anthropic.claude-*",
        "arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:inference-profile/us.anthropic.claude-*"
      ]
    },
    {
      "Sid": "GetAgentAccessToken",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*"
      ]
    },
    {
      "Sid": "S3GuidelinesAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${GUIDELINES_BUCKET}",
        "arn:aws:s3:::${GUIDELINES_BUCKET}/*"
      ]
    },
    {
      "Sid": "OpenSearchAccess",
      "Effect": "Allow",
      "Action": ["aoss:APIAccessAll"],
      "Resource": "${OPENSEARCH_COLLECTION_ARN}"
    },
    {
      "Sid": "CodeInterpreter",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:EndCodeInterpreterSession"
      ],
      "Resource": "arn:aws:bedrock-agentcore:${AWS_REGION}:aws:code-interpreter/*"
    }${SNS_STATEMENT}
  ]
}
EOF
)

aws $AWS_OPTS iam put-role-policy \
  --role-name "$AGENT_EXECUTION_ROLE_NAME" \
  --policy-name AgentCoreExecutionPolicy \
  --policy-document "$POLICY_DOC"

echo "IAM policy updated"

# Grant the execution role data access to the OpenSearch collection.
# IAM aoss:APIAccessAll is necessary but not sufficient: OpenSearch Serverless
# also requires the role to be listed as a principal in the collection's data
# access policy, otherwise guideline search gets 403 at runtime.
if [[ "$OPENSEARCH_COLLECTION_ARN" != *"/*" ]]; then
  echo "Granting data access to collection ${OPENSEARCH_COLLECTION_NAME}..."
  DATA_POLICY_NAME=""
  for _policy in $(aws $AWS_OPTS opensearchserverless list-access-policies --type data \
      --query 'accessPolicySummaries[].name' --output text 2>/dev/null); do
    if aws $AWS_OPTS opensearchserverless get-access-policy --type data --name "$_policy" \
        --query 'accessPolicyDetail.policy' --output json 2>/dev/null \
        | grep -q "collection/${OPENSEARCH_COLLECTION_NAME}\""; then
      DATA_POLICY_NAME="$_policy"
      break
    fi
  done

  if [[ -z "$DATA_POLICY_NAME" ]]; then
    echo "WARNING: no data access policy covers collection/${OPENSEARCH_COLLECTION_NAME}; guideline search will be denied"
  else
    DATA_POLICY_UPDATE=$(aws $AWS_OPTS opensearchserverless get-access-policy --type data \
      --name "$DATA_POLICY_NAME" --output json | python3 -c '
import json, sys
detail = json.load(sys.stdin)["accessPolicyDetail"]
policy, role = detail["policy"], sys.argv[1]
changed = False
for statement in policy:
    if role not in statement["Principal"]:
        statement["Principal"].append(role)
        changed = True
print(detail["policyVersion"] if changed else "")
print(json.dumps(policy, separators=(",", ":")))
' "$AGENT_ROLE_ARN")
    DATA_POLICY_VERSION=$(echo "$DATA_POLICY_UPDATE" | sed -n 1p)
    DATA_POLICY_JSON=$(echo "$DATA_POLICY_UPDATE" | sed -n 2p)
    if [[ -n "$DATA_POLICY_VERSION" ]]; then
      aws $AWS_OPTS opensearchserverless update-access-policy --type data \
        --name "$DATA_POLICY_NAME" --policy-version "$DATA_POLICY_VERSION" \
        --policy "$DATA_POLICY_JSON" --output text > /dev/null
      echo "Added ${AGENT_EXECUTION_ROLE_NAME} to data access policy ${DATA_POLICY_NAME}"
    else
      echo "Role already listed in data access policy ${DATA_POLICY_NAME}"
    fi
  fi
fi
echo ""

# =============================================================================
# Step 3: Create AgentCore Runtime
# =============================================================================

echo ">>> Step 3: Creating AgentCore runtime..."

# Check if runtime already exists
EXISTING_RUNTIME=$(aws $AWS_OPTS bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[?agentRuntimeName=='${FULL_RUNTIME_NAME}'].agentRuntimeArn | [0]" \
  --output text 2>/dev/null || echo "")

if [[ -n "$EXISTING_RUNTIME" && "$EXISTING_RUNTIME" != "None" ]]; then
  echo "Runtime already exists: $EXISTING_RUNTIME"
  AGENT_ARN="$EXISTING_RUNTIME"

  # Update the existing runtime with new image
  echo "Updating runtime with new image..."
  aws $AWS_OPTS bedrock-agentcore-control update-agent-runtime \
    --agent-runtime-id "${AGENT_ARN##*/}" \
    --agent-runtime-artifact "containerConfiguration={containerUri=${ECR_URI}:${IMAGE_TAG}}" \
    --output text > /dev/null 2>&1 || true
else
  echo "Creating new AgentCore runtime: $FULL_RUNTIME_NAME"

  # Get OpenSearch endpoint from config
  OPENSEARCH_ENDPOINT=""
  if [[ -f "$CONFIG_FILE" ]]; then
    OPENSEARCH_ENDPOINT=$(grep "opensearch_endpoint:" "$CONFIG_FILE" | awk '{print $2}' || echo "")
  fi

  # Get OpenSearch index name from config (default: guidelines)
  OPENSEARCH_INDEX_NAME="guidelines"
  if [[ -f "$CONFIG_FILE" ]]; then
    _idx=$(grep "opensearch_index:" "$CONFIG_FILE" | awk '{print $2}' || echo "")
    if [[ -n "$_idx" ]]; then
      OPENSEARCH_INDEX_NAME="$_idx"
    fi
  fi

  # Build environment variables
  ENV_VARS='{
    "AWS_REGION": "'"${AWS_REGION}"'",
    "AWS_DEFAULT_REGION": "'"${AWS_REGION}"'",
    "AUTH_ENABLED": "false",
    "LOG_LEVEL": "INFO",
    "MAX_NUDGES": "5",
    "GUIDELINES_PATH": "/app/guidelines",
    "SEARCH_BACKEND": "opensearch",
    "OPENSEARCH_ENDPOINT": "'"${OPENSEARCH_ENDPOINT}"'",
    "OPENSEARCH_INDEX_NAME": "'"${OPENSEARCH_INDEX_NAME}"'",
    "GUIDELINES_BUCKET": "'"${GUIDELINES_BUCKET}"'",
    "IMAGE_TAG": "'"${IMAGE_TAG}"'",
    "ECR_REPOSITORY": "'"${ECR_REPO}"'"'

  # Add observability env vars if enabled
  if [[ "$ENABLE_OBSERVABILITY" == "true" && -n "$SNS_TOPIC_ARN" ]]; then
    ENV_VARS="${ENV_VARS}"',
    "OBSERVABILITY_ENABLED": "true",
    "OBSERVABILITY_SNS_TOPIC_ARN": "'"${SNS_TOPIC_ARN}"'",
    "OBSERVABILITY_BUCKET_NAME": "medical-nudging-observability-'"${ACCOUNT_ID}"'",
    "STACK_NAME": "medical-nudging",
    "ENVIRONMENT": "dev"'
  fi

  ENV_VARS="${ENV_VARS}"'
  }'

  # Create runtime using AWS CLI (with retry for IAM propagation)
  AGENT_ARN=""
  MAX_RETRIES=6
  for attempt in $(seq 1 $MAX_RETRIES); do
    CREATE_OUTPUT=$(aws $AWS_OPTS bedrock-agentcore-control create-agent-runtime \
      --agent-runtime-name "$FULL_RUNTIME_NAME" \
      --description "Medical Nudging Agent - ${RUNTIME_NAME}" \
      --role-arn "$AGENT_ROLE_ARN" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${ECR_URI}:${IMAGE_TAG}}" \
      --network-configuration "networkMode=PUBLIC" \
      --environment-variables "$ENV_VARS" \
      --query "agentRuntimeArn" \
      --output text 2>&1) && { AGENT_ARN="$CREATE_OUTPUT"; break; }

    if echo "$CREATE_OUTPUT" | grep -qi "Access denied while validating ECR\|role.*cannot be assumed\|not authorized"; then
      echo "  IAM role not yet propagated, retrying in 15s... (attempt $attempt/$MAX_RETRIES)"
      sleep 15
    else
      echo "ERROR: Failed to create AgentCore runtime:"
      echo "  $CREATE_OUTPUT"
      exit 1
    fi
  done

  if [[ -z "$AGENT_ARN" ]]; then
    echo "ERROR: Failed to create AgentCore runtime after $MAX_RETRIES attempts (IAM role did not propagate in time)"
    exit 1
  fi

  echo "Created runtime: $AGENT_ARN"

  # Wait for runtime to be ready
  echo "Waiting for runtime to be ready (this may take a few minutes)..."
  for i in {1..60}; do
    STATUS=$(aws $AWS_OPTS bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "${AGENT_ARN##*/}" \
      --query "status" --output text 2>/dev/null || echo "PENDING")

    if [[ "$STATUS" == "READY" ]]; then
      echo "Runtime is READY"
      break
    elif [[ "$STATUS" == "FAILED" || "$STATUS" == "CREATE_FAILED" ]]; then
      echo "ERROR: Runtime creation FAILED"
      aws $AWS_OPTS bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "${AGENT_ARN##*/}" \
        --query "failureReason" --output text
      exit 1
    fi

    echo "  Status: $STATUS (waiting...)"
    sleep 10
  done
fi

echo ""

# =============================================================================
# Step 3b: Enable Transaction Search (One-time account setup)
# =============================================================================

echo ">>> Step 3b: Enabling Transaction Search..."

# Check if Transaction Search is already enabled
TRACE_DEST=$(aws $AWS_OPTS xray get-trace-segment-destination \
  --query "Destination" --output text 2>/dev/null || echo "")

if [[ "$TRACE_DEST" == "CloudWatchLogs" ]]; then
  echo "Transaction Search already enabled"
else
  # Create resource policy allowing X-Ray to write to aws/spans log group
  echo "Creating CloudWatch Logs resource policy for X-Ray..."
  aws $AWS_OPTS logs put-resource-policy \
    --policy-name "TransactionSearchAccess" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"TransactionSearchXRayAccess\",
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"xray.amazonaws.com\"},
        \"Action\": \"logs:PutLogEvents\",
        \"Resource\": [
          \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:aws/spans:*\",
          \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/application-signals/data:*\"
        ],
        \"Condition\": {
          \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:xray:${REGION}:${ACCOUNT_ID}:*\"},
          \"StringEquals\": {\"aws:SourceAccount\": \"${ACCOUNT_ID}\"}
        }
      }]
    }" > /dev/null 2>&1 || echo "WARNING: Failed to create resource policy (may already exist)"

  # Enable Transaction Search (route X-Ray traces to CloudWatch Logs)
  echo "Enabling X-Ray Transaction Search..."
  aws $AWS_OPTS xray update-trace-segment-destination --destination CloudWatchLogs > /dev/null 2>&1 && {
    echo "Transaction Search enabled (traces → CloudWatch Logs)"
  } || {
    echo "WARNING: Failed to enable Transaction Search"
  }
fi

echo ""

# =============================================================================
# Step 3c: Enable Service-Level Tracing (CloudWatch Delivery)
# =============================================================================

echo ">>> Step 3c: Enabling service-level tracing..."

# Unique names for delivery resources (based on runtime name)
TRACE_SOURCE_NAME="medical-nudging-${RUNTIME_NAME}-traces-src"
TRACE_DEST_NAME="medical-nudging-${RUNTIME_NAME}-traces-dest"

# Check if delivery source already exists
EXISTING_SOURCE=$(aws $AWS_OPTS logs describe-delivery-sources \
  --query "deliverySources[?name=='${TRACE_SOURCE_NAME}'].name | [0]" \
  --output text 2>/dev/null || echo "")

if [[ -z "$EXISTING_SOURCE" || "$EXISTING_SOURCE" == "None" ]]; then
  echo "Creating trace delivery source..."
  aws $AWS_OPTS logs put-delivery-source \
    --name "$TRACE_SOURCE_NAME" \
    --log-type "TRACES" \
    --resource-arn "$AGENT_ARN" \
    --output text > /dev/null 2>&1 || {
      echo "WARNING: Failed to create delivery source (may require additional permissions)"
    }
else
  echo "Trace delivery source already exists: $TRACE_SOURCE_NAME"
fi

# Check if delivery destination already exists
EXISTING_DEST=$(aws $AWS_OPTS logs describe-delivery-destinations \
  --query "deliveryDestinations[?name=='${TRACE_DEST_NAME}'].arn | [0]" \
  --output text 2>/dev/null || echo "")

if [[ -z "$EXISTING_DEST" || "$EXISTING_DEST" == "None" ]]; then
  echo "Creating trace delivery destination..."
  DEST_ARN=$(aws $AWS_OPTS logs put-delivery-destination \
    --name "$TRACE_DEST_NAME" \
    --delivery-destination-type "XRAY" \
    --query "deliveryDestination.arn" \
    --output text 2>/dev/null || echo "")

  if [[ -n "$DEST_ARN" && "$DEST_ARN" != "None" ]]; then
    echo "Created destination: $DEST_ARN"
  else
    echo "WARNING: Failed to create delivery destination"
  fi
else
  DEST_ARN="$EXISTING_DEST"
  echo "Trace delivery destination already exists: $TRACE_DEST_NAME"
fi

# Create delivery (connect source to destination) if both exist
if [[ -n "$DEST_ARN" && "$DEST_ARN" != "None" ]]; then
  # Check if delivery already exists
  EXISTING_DELIVERY=$(aws $AWS_OPTS logs describe-deliveries \
    --query "deliveries[?deliverySourceName=='${TRACE_SOURCE_NAME}'].id | [0]" \
    --output text 2>/dev/null || echo "")

  if [[ -z "$EXISTING_DELIVERY" || "$EXISTING_DELIVERY" == "None" ]]; then
    echo "Creating trace delivery..."
    aws $AWS_OPTS logs create-delivery \
      --delivery-source-name "$TRACE_SOURCE_NAME" \
      --delivery-destination-arn "$DEST_ARN" \
      --output text > /dev/null 2>&1 && {
        echo "Service-level tracing enabled (spans → aws/spans)"
      } || {
        echo "WARNING: Failed to create delivery (may already exist or require permissions)"
      }
  else
    echo "Trace delivery already exists"
  fi
fi

echo ""

# =============================================================================
# Step 3d: Generate API Key and store in Secrets Manager
# =============================================================================

echo ">>> Step 3d: Generating API key..."

API_KEY=$(openssl rand -hex 32)
echo "Generated API key: ${API_KEY:0:16}..."

create_or_update_api_key_secret "$API_KEY_SECRET_NAME" "$API_KEY"

echo ""

# =============================================================================
# Step 3e: Create WebSocket Presigned URL API
# =============================================================================

echo ">>> Step 3e: Creating WebSocket presigned URL API..."

# Create WebSocket Lambda IAM Role
WS_ROLE_ARN=$(aws $AWS_OPTS iam get-role --role-name "$WS_LAMBDA_ROLE_NAME" \
  --query "Role.Arn" --output text 2>/dev/null || echo "")

if [[ -z "$WS_ROLE_ARN" ]]; then
  echo "Creating WebSocket Lambda IAM role: $WS_LAMBDA_ROLE_NAME"

  aws $AWS_OPTS iam create-role \
    --role-name "$WS_LAMBDA_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' \
    --tags "Key=Project,Value=MedicalNudging" "Key=RuntimeName,Value=$RUNTIME_NAME" \
    --output text > /dev/null

  WS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${WS_LAMBDA_ROLE_NAME}"
  echo "Created role: $WS_ROLE_ARN"
  echo "Waiting for role to propagate..."
  sleep 60
else
  echo "Using existing role: $WS_ROLE_ARN"
fi

# WebSocket Lambda policy (includes InvokeAgentRuntimeWithWebSocketStream)
echo "Updating WebSocket Lambda IAM policy..."
WS_LAMBDA_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": [
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${WS_URL_GENERATOR_NAME}:*",
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${WS_AUTHORIZER_NAME}:*"
      ]
    },
    {
      "Sid": "AgentCoreWebSocket",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
        "bedrock-agentcore-runtime:InvokeAgentRuntime"
      ],
      "Resource": [
        "${AGENT_ARN}",
        "${AGENT_ARN}/*"
      ]
    },
    {
      "Sid": "SecretsManagerReadAPIKey",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:${API_KEY_SECRET_NAME}*"
    },
    {
      "Sid": "CloudWatchMetrics",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "MedicalNudging"
        }
      }
    }
  ]
}
EOF
)

aws $AWS_OPTS iam put-role-policy \
  --role-name "$WS_LAMBDA_ROLE_NAME" \
  --policy-name WebSocketLambdaPolicy \
  --policy-document "$WS_LAMBDA_POLICY"

# Create API Authorizer Lambda
echo "Creating API Authorizer Lambda..."
WS_AUTH_HANDLER_DIR="$PROJECT_ROOT/lambda/api_authorizer"
zip -j "$BUILD_DIR/ws_api_authorizer_${RUNTIME_NAME}.zip" "$WS_AUTH_HANDLER_DIR/handler.py" > /dev/null

AUTHORIZER_EXISTS=$(aws $AWS_OPTS lambda get-function --function-name "$WS_AUTHORIZER_NAME" 2>/dev/null || echo "")
if [[ -z "$AUTHORIZER_EXISTS" ]]; then
  WS_AUTH_CREATED=false
  for attempt in $(seq 1 6); do
    CREATE_OUTPUT=$(aws $AWS_OPTS lambda create-function \
      --function-name "$WS_AUTHORIZER_NAME" \
      --runtime python3.12 \
      --role "$WS_ROLE_ARN" \
      --handler handler.handler \
      --zip-file "fileb://${BUILD_DIR}/ws_api_authorizer_${RUNTIME_NAME}.zip" \
      --timeout 10 \
      --memory-size 128 \
      --architectures arm64 \
      --environment "Variables={API_KEY_SECRET_NAME=${API_KEY_SECRET_NAME},LOG_LEVEL=INFO}" \
      --tags "Project=MedicalNudging,RuntimeName=$RUNTIME_NAME" \
      --output text 2>&1) && { WS_AUTH_CREATED=true; break; }

    if echo "$CREATE_OUTPUT" | grep -qi "role.*cannot be assumed\|not authorized"; then
      echo "  IAM role not yet propagated, retrying in 15s... (attempt $attempt/6)"
      sleep 15
    else
      echo "ERROR: Failed to create Authorizer Lambda: $CREATE_OUTPUT"
      break
    fi
  done
  [[ "$WS_AUTH_CREATED" == "true" ]] && aws $AWS_OPTS lambda wait function-active --function-name "$WS_AUTHORIZER_NAME"
else
  echo "Authorizer Lambda already exists, updating..."
  aws $AWS_OPTS lambda update-function-code \
    --function-name "$WS_AUTHORIZER_NAME" \
    --zip-file "fileb://${BUILD_DIR}/ws_api_authorizer_${RUNTIME_NAME}.zip" \
    --output text > /dev/null
  aws $AWS_OPTS lambda wait function-updated --function-name "$WS_AUTHORIZER_NAME"
fi

AUTHORIZER_LAMBDA_ARN=$(aws $AWS_OPTS lambda get-function --function-name "$WS_AUTHORIZER_NAME" \
  --query "Configuration.FunctionArn" --output text)

# Create WS URL Generator Lambda (needs bedrock-agentcore layer)
echo "Creating WS URL Generator Lambda..."

# Build Lambda layer for bedrock-agentcore SDK
LAYER_DIR="$BUILD_DIR/agentcore_layer"
if [[ ! -f "$BUILD_DIR/agentcore_sdk_layer.zip" ]]; then
  echo "Building bedrock-agentcore SDK layer..."
  rm -rf "$LAYER_DIR"
  mkdir -p "$LAYER_DIR/python"
  # Cross-install arm64 / Python 3.12 wheels for the Lambda runtime. uv (the repo's
  # package manager) does this regardless of the host Python; the pip fallback needs
  # a host pip new enough to resolve bedrock-agentcore (Python >= 3.10).
  if command -v uv > /dev/null 2>&1; then
    uv pip install -r "$PROJECT_ROOT/lambda/ws_url_generator/requirements.txt" \
      --target "$LAYER_DIR/python" \
      --python-platform aarch64-manylinux2014 \
      --python-version 3.12 \
      --only-binary :all: --quiet
  else
    python3 -m pip install -r "$PROJECT_ROOT/lambda/ws_url_generator/requirements.txt" \
      -t "$LAYER_DIR/python" \
      --platform manylinux2014_aarch64 \
      --only-binary :all: \
      --python-version 3.12 \
      --quiet
  fi
  cd "$LAYER_DIR" && zip -r "$BUILD_DIR/agentcore_sdk_layer.zip" python/ -q
  cd "$PROJECT_ROOT"
fi

# Publish layer
LAYER_ARN=$(aws $AWS_OPTS lambda publish-layer-version \
  --layer-name "medical-nudging-${RUNTIME_NAME}-agentcore-sdk" \
  --zip-file "fileb://${BUILD_DIR}/agentcore_sdk_layer.zip" \
  --compatible-runtimes python3.12 \
  --compatible-architectures arm64 \
  --description "bedrock-agentcore SDK for presigned URL generation" \
  --query "LayerVersionArn" --output text)
echo "Published SDK layer: $LAYER_ARN"

# Create/update ws_url_generator function
WS_HANDLER_DIR="$PROJECT_ROOT/lambda/ws_url_generator"
zip -j "$BUILD_DIR/lambda_ws_url_generator_${RUNTIME_NAME}.zip" "$WS_HANDLER_DIR/handler.py" > /dev/null

WS_GEN_ENV="AGENT_RUNTIME_ARN=${AGENT_ARN},API_KEY_SECRET_NAME=${API_KEY_SECRET_NAME},URL_EXPIRY_SECONDS=300,LOG_LEVEL=INFO"

GENERATOR_EXISTS=$(aws $AWS_OPTS lambda get-function --function-name "$WS_URL_GENERATOR_NAME" 2>/dev/null || echo "")
if [[ -z "$GENERATOR_EXISTS" ]]; then
  WS_GEN_CREATED=false
  for attempt in $(seq 1 6); do
    CREATE_OUTPUT=$(aws $AWS_OPTS lambda create-function \
      --function-name "$WS_URL_GENERATOR_NAME" \
      --runtime python3.12 \
      --role "$WS_ROLE_ARN" \
      --handler handler.handler \
      --zip-file "fileb://${BUILD_DIR}/lambda_ws_url_generator_${RUNTIME_NAME}.zip" \
      --timeout 30 \
      --memory-size 256 \
      --architectures arm64 \
      --layers "$LAYER_ARN" \
      --environment "Variables={${WS_GEN_ENV}}" \
      --tags "Project=MedicalNudging,RuntimeName=$RUNTIME_NAME" \
      --output text 2>&1) && { WS_GEN_CREATED=true; break; }

    if echo "$CREATE_OUTPUT" | grep -qi "role.*cannot be assumed\|not authorized"; then
      echo "  IAM role not yet propagated, retrying in 15s... (attempt $attempt/6)"
      sleep 15
    else
      echo "ERROR: Failed to create WS URL Generator Lambda: $CREATE_OUTPUT"
      break
    fi
  done
  [[ "$WS_GEN_CREATED" == "true" ]] && aws $AWS_OPTS lambda wait function-active --function-name "$WS_URL_GENERATOR_NAME"
else
  echo "WS URL Generator Lambda already exists, updating..."
  aws $AWS_OPTS lambda update-function-code \
    --function-name "$WS_URL_GENERATOR_NAME" \
    --zip-file "fileb://${BUILD_DIR}/lambda_ws_url_generator_${RUNTIME_NAME}.zip" \
    --output text > /dev/null
  aws $AWS_OPTS lambda wait function-updated --function-name "$WS_URL_GENERATOR_NAME"
  aws $AWS_OPTS lambda update-function-configuration \
    --function-name "$WS_URL_GENERATOR_NAME" \
    --layers "$LAYER_ARN" \
    --environment "Variables={${WS_GEN_ENV}}" \
    --output text > /dev/null
  aws $AWS_OPTS lambda wait function-updated --function-name "$WS_URL_GENERATOR_NAME"
fi

GENERATOR_LAMBDA_ARN=$(aws $AWS_OPTS lambda get-function --function-name "$WS_URL_GENERATOR_NAME" \
  --query "Configuration.FunctionArn" --output text)

# Create API Gateway REST API
echo "Creating API Gateway REST API..."
EXISTING_API=$(aws $AWS_OPTS apigateway get-rest-apis \
  --query "items[?name=='${WS_API_NAME}'].id | [0]" --output text 2>/dev/null || echo "")

if [[ -z "$EXISTING_API" || "$EXISTING_API" == "None" ]]; then
  API_ID=$(aws $AWS_OPTS apigateway create-rest-api \
    --name "$WS_API_NAME" \
    --description "WebSocket presigned URL API for Medical Nudging" \
    --endpoint-configuration types=REGIONAL \
    --query "id" --output text)
  echo "Created REST API: $API_ID"
else
  API_ID="$EXISTING_API"
  echo "Using existing REST API: $API_ID"
fi

# Get root resource ID
ROOT_RESOURCE_ID=$(aws $AWS_OPTS apigateway get-resources --rest-api-id "$API_ID" \
  --query "items[?path=='/'].id" --output text)

# Create /ws-url resource
WS_URL_RESOURCE_ID=$(aws $AWS_OPTS apigateway get-resources --rest-api-id "$API_ID" \
  --query "items[?pathPart=='ws-url'].id" --output text 2>/dev/null || echo "")

if [[ -z "$WS_URL_RESOURCE_ID" || "$WS_URL_RESOURCE_ID" == "None" ]]; then
  WS_URL_RESOURCE_ID=$(aws $AWS_OPTS apigateway create-resource \
    --rest-api-id "$API_ID" \
    --parent-id "$ROOT_RESOURCE_ID" \
    --path-part "ws-url" \
    --query "id" --output text)
  echo "Created /ws-url resource: $WS_URL_RESOURCE_ID"
fi

# Create Lambda Authorizer
AUTHORIZER_ID=$(aws $AWS_OPTS apigateway get-authorizers --rest-api-id "$API_ID" \
  --query "items[?name=='api-key-authorizer'].id | [0]" --output text 2>/dev/null || echo "")

if [[ -z "$AUTHORIZER_ID" || "$AUTHORIZER_ID" == "None" ]]; then
  AUTHORIZER_URI="arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${AUTHORIZER_LAMBDA_ARN}/invocations"
  AUTHORIZER_ID=$(aws $AWS_OPTS apigateway create-authorizer \
    --rest-api-id "$API_ID" \
    --name "api-key-authorizer" \
    --type REQUEST \
    --authorizer-uri "$AUTHORIZER_URI" \
    --authorizer-result-ttl-in-seconds 300 \
    --identity-source "method.request.header.x-api-key" \
    --query "id" --output text)

  # Grant API Gateway permission to invoke authorizer Lambda
  aws $AWS_OPTS lambda add-permission \
    --function-name "$WS_AUTHORIZER_NAME" \
    --statement-id AllowAPIGatewayInvokeAuthorizer \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    --output text > /dev/null 2>&1 || true

  echo "Created Lambda Authorizer: $AUTHORIZER_ID"
fi

# Create POST method on /ws-url with CUSTOM authorization
aws $AWS_OPTS apigateway put-method \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method POST \
  --authorization-type CUSTOM \
  --authorizer-id "$AUTHORIZER_ID" \
  --output text > /dev/null 2>&1 || true

# Create Lambda proxy integration
GENERATOR_URI="arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${GENERATOR_LAMBDA_ARN}/invocations"
aws $AWS_OPTS apigateway put-integration \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method POST \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "$GENERATOR_URI" \
  --output text > /dev/null

# Grant API Gateway permission to invoke generator Lambda
aws $AWS_OPTS lambda add-permission \
  --function-name "$WS_URL_GENERATOR_NAME" \
  --statement-id AllowAPIGatewayInvokeWSUrlGenerator \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" \
  --output text > /dev/null 2>&1 || true

# Create OPTIONS method for CORS
aws $AWS_OPTS apigateway put-method \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method OPTIONS \
  --authorization-type NONE \
  --output text > /dev/null 2>&1 || true

aws $AWS_OPTS apigateway put-integration \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method OPTIONS \
  --type MOCK \
  --request-templates '{"application/json":"{\"statusCode\":200}"}' \
  --output text > /dev/null

aws $AWS_OPTS apigateway put-method-response \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method OPTIONS \
  --status-code 200 \
  --response-parameters '{"method.response.header.Access-Control-Allow-Headers":true,"method.response.header.Access-Control-Allow-Methods":true,"method.response.header.Access-Control-Allow-Origin":true}' \
  --output text > /dev/null 2>&1 || true

aws $AWS_OPTS apigateway put-integration-response \
  --rest-api-id "$API_ID" \
  --resource-id "$WS_URL_RESOURCE_ID" \
  --http-method OPTIONS \
  --status-code 200 \
  --response-parameters '{"method.response.header.Access-Control-Allow-Headers":"'"'"'Content-Type,X-Api-Key'"'"'","method.response.header.Access-Control-Allow-Methods":"'"'"'POST,OPTIONS'"'"'","method.response.header.Access-Control-Allow-Origin":"'"'"'*'"'"'"}' \
  --output text > /dev/null

# Deploy to v1 stage
echo "Deploying API to v1 stage..."
aws $AWS_OPTS apigateway create-deployment \
  --rest-api-id "$API_ID" \
  --stage-name "v1" \
  --description "WebSocket presigned URL API - ${RUNTIME_NAME}" \
  --output text > /dev/null

WEBSOCKET_API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/v1/ws-url"
echo "WebSocket API URL: $WEBSOCKET_API_URL"

# --- Step 3f: WAF Web ACL for API Gateway ---
echo ""
echo ">>> Step 3f: Creating WAF Web ACL for WebSocket API..."

WAF_NAME="medical-nudging-${RUNTIME_NAME}-websocket-waf"
WAF_ARN=$(aws $AWS_OPTS wafv2 list-web-acls --scope REGIONAL \
  --query "WebACLs[?Name=='${WAF_NAME}'].ARN | [0]" --output text 2>/dev/null || echo "")

if [[ -z "$WAF_ARN" || "$WAF_ARN" == "None" ]]; then
  echo "Creating WAF Web ACL: $WAF_NAME"
  WAF_ARN=$(aws $AWS_OPTS wafv2 create-web-acl \
    --name "$WAF_NAME" \
    --scope REGIONAL \
    --default-action '{"Allow":{}}' \
    --description "WAF for Medical Nudging WebSocket API - ${RUNTIME_NAME}" \
    --rules '[
      {
        "Name": "AWSManagedRulesCommonRuleSet",
        "Priority": 1,
        "OverrideAction": {"None":{}},
        "Statement": {
          "ManagedRuleGroupStatement": {
            "VendorName": "AWS",
            "Name": "AWSManagedRulesCommonRuleSet",
            "RuleActionOverrides": [
              {"Name": "SizeRestrictions_BODY", "ActionToUse": {"Count":{}}}
            ]
          }
        },
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "medical-nudging-websocket-common-rules"
        }
      },
      {
        "Name": "AWSManagedRulesKnownBadInputsRuleSet",
        "Priority": 2,
        "OverrideAction": {"None":{}},
        "Statement": {
          "ManagedRuleGroupStatement": {
            "VendorName": "AWS",
            "Name": "AWSManagedRulesKnownBadInputsRuleSet"
          }
        },
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "medical-nudging-websocket-bad-inputs"
        }
      },
      {
        "Name": "RateLimit",
        "Priority": 3,
        "Action": {"Block":{}},
        "Statement": {
          "RateBasedStatement": {
            "Limit": 100,
            "AggregateKeyType": "IP"
          }
        },
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "medical-nudging-websocket-rate-limit"
        }
      },
      {
        "Name": "BodySizeConstraint",
        "Priority": 4,
        "Action": {"Block":{}},
        "Statement": {
          "SizeConstraintStatement": {
            "ComparisonOperator": "GT",
            "Size": 8192,
            "FieldToMatch": {"Body":{"OversizeHandling":"MATCH"}},
            "TextTransformations": [{"Priority":0,"Type":"NONE"}]
          }
        },
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "medical-nudging-websocket-body-size"
        }
      }
    ]' \
    --visibility-config '{
      "SampledRequestsEnabled": true,
      "CloudWatchMetricsEnabled": true,
      "MetricName": "medical-nudging-websocket-waf"
    }' \
    --tags '[{"Key":"Project","Value":"MedicalNudging"},{"Key":"RuntimeName","Value":"'"$RUNTIME_NAME"'"}]' \
    --query "Summary.ARN" --output text)
  echo "Created WAF: $WAF_ARN"
else
  echo "Using existing WAF: $WAF_ARN"
fi

# Associate WAF with API Gateway stage
STAGE_ARN="arn:aws:apigateway:${AWS_REGION}::/restapis/${API_ID}/stages/v1"
echo "Associating WAF with API Gateway stage..."
aws $AWS_OPTS wafv2 associate-web-acl \
  --web-acl-arn "$WAF_ARN" \
  --resource-arn "$STAGE_ARN" \
  --output text > /dev/null 2>&1 || echo "  WAF already associated or association skipped"
echo "WAF protection enabled for WebSocket API"

echo ""

# =============================================================================
# Step 4: Update Config File
# =============================================================================

echo ">>> Step 4: Updating config/settings.yaml..."

if [[ -f "$CONFIG_FILE" ]]; then
  # Update agent_arn
  if grep -q "^agent_arn:" "$CONFIG_FILE"; then
    sed -i "s|^agent_arn:.*|agent_arn: $AGENT_ARN|" "$CONFIG_FILE"
  else
    echo "" >> "$CONFIG_FILE"
    echo "# AgentCore Configuration - ${RUNTIME_NAME}" >> "$CONFIG_FILE"
    echo "agent_arn: $AGENT_ARN" >> "$CONFIG_FILE"
  fi

  # Update websocket_api_url
  if grep -q "^websocket_api_url:" "$CONFIG_FILE"; then
    sed -i "s|^websocket_api_url:.*|websocket_api_url: $WEBSOCKET_API_URL|" "$CONFIG_FILE"
  else
    echo "" >> "$CONFIG_FILE"
    echo "# WebSocket Presigned URL API - ${RUNTIME_NAME}" >> "$CONFIG_FILE"
    echo "websocket_api_url: $WEBSOCKET_API_URL" >> "$CONFIG_FILE"
  fi

  # Update api_key_secret_name (the key itself stays in Secrets Manager)
  if grep -q "^api_key_secret_name:" "$CONFIG_FILE"; then
    sed -i "s|^api_key_secret_name:.*|api_key_secret_name: $API_KEY_SECRET_NAME|" "$CONFIG_FILE"
  else
    echo "api_key_secret_name: $API_KEY_SECRET_NAME" >> "$CONFIG_FILE"
  fi

  echo "Config updated."
else
  echo "WARNING: Config file not found at $CONFIG_FILE"
fi

echo ""

# =============================================================================
# Summary
# =============================================================================

echo "=============================================="
echo "Deployment Complete!"
echo "=============================================="
echo ""
echo "AgentCore Runtime:"
echo "  Name:     $FULL_RUNTIME_NAME"
echo "  ARN:      $AGENT_ARN"
if [[ "$ENABLE_OBSERVABILITY" == "true" ]]; then
echo "  Observability: Enabled (SNS: $SNS_TOPIC_ARN)"
fi
echo ""
echo "WebSocket API:"
echo "  URL:      $WEBSOCKET_API_URL"
echo "  Secret:   $API_KEY_SECRET_NAME (Secrets Manager)"
echo "  API Key:  ${API_KEY:0:8}... (full value in the secret)"
echo ""
echo "Get a presigned WebSocket URL:"
echo "  API_KEY=\$(aws $AWS_OPTS secretsmanager get-secret-value --secret-id \"$API_KEY_SECRET_NAME\" \\"
echo "    --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"api_key\"])')"
echo "  curl -s -X POST \"$WEBSOCKET_API_URL\" -H \"x-api-key: \$API_KEY\" | jq ."
echo ""
echo "End-to-end streaming test:"
echo "  uv run scripts/run_ws_streaming.py --api-url \"$WEBSOCKET_API_URL\" --api-key \"\$API_KEY\" \\"
echo "    --patient-file tests/fixtures/sample_ccda.xml"
echo ""
echo "Config file updated: $CONFIG_FILE"
echo ""
echo "Done!"
