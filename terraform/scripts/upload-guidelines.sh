#!/bin/bash

# ============================================================================
# Upload Guidelines to S3 - Medical Nudging AgentCore
# ============================================================================
# This script uploads clinical guideline PDFs from the guidelines/ directory
# to the S3 guidelines bucket created by Terraform.
#
# Usage: ./upload-guidelines.sh [--dry-run]
#
# Options:
#   --dry-run    Show what would be uploaded without actually uploading

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$TERRAFORM_DIR")"
GUIDELINES_DIR="$PROJECT_ROOT/guidelines"
DRY_RUN=false

# Source common functions
source "$SCRIPT_DIR/lib/common.sh"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [--dry-run]"
      exit 1
      ;;
  esac
done

# ============================================================================
# Pre-flight Checks
# ============================================================================

print_header "Upload Clinical Guidelines to S3"

if [ "$DRY_RUN" = true ]; then
    print_warning "DRY RUN MODE - No files will be uploaded"
    echo ""
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    print_error "AWS CLI is not installed"
    exit 1
fi

# Load AWS profile from tfvars
load_aws_profile "$TERRAFORM_DIR/terraform.tfvars"

# Check AWS credentials
if ! aws sts get-caller-identity > /dev/null 2>&1; then
    print_error "AWS credentials are not configured or invalid"
    exit 1
fi

# Check guidelines directory exists
if [ ! -d "$GUIDELINES_DIR" ]; then
    print_error "Guidelines directory not found: $GUIDELINES_DIR"
    exit 1
fi

# Get bucket name from Terraform output
cd "$TERRAFORM_DIR"

if [ ! -f "terraform.tfstate" ] && [ ! -d ".terraform" ]; then
    print_error "Terraform state not found. Run ./scripts/deploy.sh first."
    exit 1
fi

# Initialize terraform if needed
if [ ! -d ".terraform" ]; then
    print_info "Initializing Terraform..."
    terraform init > /dev/null
fi

BUCKET_NAME=$(terraform output -raw guidelines_bucket_name 2>/dev/null || echo "")

if [ -z "$BUCKET_NAME" ]; then
    print_error "Could not get guidelines bucket name from Terraform output"
    print_info "Make sure the infrastructure is deployed: ./scripts/deploy.sh"
    exit 1
fi

print_success "Target bucket: $BUCKET_NAME"
echo ""

# ============================================================================
# Find and Upload Guidelines
# ============================================================================

print_header "Scanning Guidelines Directory"

cd "$GUIDELINES_DIR"

# Find all PDF files
PDF_FILES=$(find . -name "*.pdf" -type f | sort)
PDF_COUNT=$(echo "$PDF_FILES" | grep -c "\.pdf$" || echo "0")

if [ "$PDF_COUNT" -eq 0 ]; then
    print_warning "No PDF files found in $GUIDELINES_DIR"
    exit 0
fi

print_info "Found $PDF_COUNT PDF file(s) to upload:"
echo ""

# List files with sizes
TOTAL_SIZE=0
while IFS= read -r file; do
    if [ -n "$file" ]; then
        SIZE=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null || echo "0")
        SIZE_MB=$(echo "scale=2; $SIZE / 1024 / 1024" | bc)
        TOTAL_SIZE=$((TOTAL_SIZE + SIZE))
        print_info "  $file (${SIZE_MB} MB)"
    fi
done <<< "$PDF_FILES"

TOTAL_SIZE_MB=$(echo "scale=2; $TOTAL_SIZE / 1024 / 1024" | bc)
echo ""
print_info "Total size: ${TOTAL_SIZE_MB} MB"
echo ""

# ============================================================================
# Upload Files
# ============================================================================

if [ "$DRY_RUN" = true ]; then
    print_header "Dry Run - Files Would Be Uploaded"
    while IFS= read -r file; do
        if [ -n "$file" ]; then
            # Remove leading ./
            S3_KEY="${file#./}"
            print_info "Would upload: $file -> s3://$BUCKET_NAME/$S3_KEY"
        fi
    done <<< "$PDF_FILES"
    echo ""
    print_success "Dry run complete. Use without --dry-run to actually upload."
    exit 0
fi

print_header "Uploading Guidelines to S3"

UPLOADED=0
FAILED=0

while IFS= read -r file; do
    if [ -n "$file" ]; then
        # Remove leading ./
        S3_KEY="${file#./}"

        print_info "Uploading: $file"

        if aws s3 cp "$file" "s3://$BUCKET_NAME/$S3_KEY" \
            --content-type "application/pdf" \
            --metadata "source=guidelines-upload,uploaded-at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            2>/dev/null; then
            print_success "  Uploaded: s3://$BUCKET_NAME/$S3_KEY"
            UPLOADED=$((UPLOADED + 1))
        else
            print_error "  Failed to upload: $file"
            FAILED=$((FAILED + 1))
        fi
    fi
done <<< "$PDF_FILES"

echo ""

# ============================================================================
# Summary
# ============================================================================

print_header "Upload Summary"

print_success "Successfully uploaded: $UPLOADED file(s)"
if [ "$FAILED" -gt 0 ]; then
    print_error "Failed to upload: $FAILED file(s)"
fi

echo ""
print_info "Guidelines are now available at:"
print_info "  s3://$BUCKET_NAME/"
echo ""
print_info "To verify:"
print_info "  aws s3 ls s3://$BUCKET_NAME/ --recursive"
echo ""

# List uploaded files by source
print_header "Uploaded Guidelines by Source"

aws s3 ls "s3://$BUCKET_NAME/" --recursive 2>/dev/null | while read -r line; do
    SIZE=$(echo "$line" | awk '{print $3}')
    FILE=$(echo "$line" | awk '{print $4}')
    SIZE_MB=$(echo "scale=2; $SIZE / 1024 / 1024" | bc)
    print_info "  $FILE (${SIZE_MB} MB)"
done

echo ""
print_success "Guidelines upload complete!"
print_info ""
print_info "Next step: Ingest guidelines to Aurora database:"
print_info "  cd $PROJECT_ROOT"
print_info "  python scripts/ingest_guidelines.py guidelines/ada/2026/*.pdf --source 'ADA 2026' --init-schema"
