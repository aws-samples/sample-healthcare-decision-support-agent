#!/bin/bash
# Sync local guidelines to S3, triggering Lambda ingestion
# Usage: ./scripts/sync_guidelines_to_s3.sh [bucket-name]

set -euo pipefail

# Get bucket name from argument or terraform output
if [ $# -ge 1 ]; then
    BUCKET="$1"
else
    # Try to get from terraform
    if [ -d "terraform" ]; then
        BUCKET=$(cd terraform && terraform output -raw guidelines_bucket_name 2>/dev/null || echo "")
    fi
fi

if [ -z "${BUCKET:-}" ]; then
    echo "Usage: $0 <bucket-name>"
    echo "Or run from project root with terraform state available"
    exit 1
fi

# Source directory
SOURCE_DIR="${2:-data/Guidelines}"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Source directory not found: $SOURCE_DIR"
    echo "Expected structure: data/Guidelines/{ADA,AHA_ACC,CDC,MISC}/"
    exit 1
fi

echo "Syncing $SOURCE_DIR to s3://$BUCKET/"
echo "Excluding: ADA/parsed/*"

aws s3 sync "$SOURCE_DIR" "s3://$BUCKET/" \
    --exclude "ADA/parsed/*" \
    --exclude "*.DS_Store" \
    --delete

echo "Sync complete. S3 events will trigger Lambda ingestion."
echo ""
echo "To check ingestion status:"
echo "  aws logs tail /aws/lambda/medical-nudging-ingest-postgres --follow"
echo "  aws logs tail /aws/lambda/medical-nudging-ingest-opensearch --follow"
