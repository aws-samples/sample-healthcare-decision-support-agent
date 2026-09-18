#!/usr/bin/env bash
# Tag existing resources for Control Tower Backup
# Usage: ./scripts/tag_existing_resources.sh [--profile PROFILE]

set -euo pipefail

PROFILE_ARG=""
if [[ "${1:-}" == "--profile" && -n "${2:-}" ]]; then
    PROFILE_ARG="--profile $2"
fi

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
TAG_KEY="aws-control-tower:backup"
TAG_VALUE="true"

echo "Tagging existing resources for Control Tower Backup..."
echo "Region: $REGION"

# Tag AgentCore runtimes
echo ""
echo "=== AgentCore Runtimes ==="
RUNTIMES=$(aws bedrock-agentcore-control list-agent-runtimes $PROFILE_ARG --region "$REGION" \
    --query 'agentRuntimeSummaries[?contains(agentRuntimeName, `medical_nudging`)].agentRuntimeArn' \
    --output text 2>/dev/null || echo "")

if [[ -n "$RUNTIMES" ]]; then
    for ARN in $RUNTIMES; do
        echo "Tagging: $ARN"
        aws bedrock-agentcore-control tag-resource $PROFILE_ARG --region "$REGION" \
            --resource-arn "$ARN" \
            --tags "$TAG_KEY=$TAG_VALUE" 2>/dev/null || echo "  (already tagged or no permission)"
    done
else
    echo "No AgentCore runtimes found matching 'medical_nudging'"
fi

echo ""
echo "Done! Run 'terraform apply' to tag Terraform-managed resources."
