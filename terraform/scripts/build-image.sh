#!/bin/bash

# ============================================================================
# Build and Verify Docker Image for Medical Nudging AgentCore Runtime
# ============================================================================
# This script is called by Terraform during deployment to:
# 1. Trigger CodeBuild to build the Docker image
# 2. Wait for the build to complete
# 3. Verify the image was successfully pushed to ECR
#
# Parameters:
#   $1 - CodeBuild project name
#   $2 - AWS region
#   $3 - ECR repository name
#   $4 - Image tag
#   $5 - ECR repository URL

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source common functions
source "$SCRIPT_DIR/lib/common.sh"

# Parameters
PROJECT_NAME="$1"
REGION="$2"
REPO_NAME="$3"
IMAGE_TAG="$4"
REPO_URL="$5"

# ============================================================================
# Validate Parameters
# ============================================================================

if [ -z "$PROJECT_NAME" ] || [ -z "$REGION" ] || [ -z "$REPO_NAME" ] || [ -z "$IMAGE_TAG" ] || [ -z "$REPO_URL" ]; then
    print_error "Missing required parameters"
    echo "Usage: $0 <project_name> <region> <repo_name> <image_tag> <repo_url>"
    exit 1
fi

# ============================================================================
# Start Build Process
# ============================================================================

print_header "Building Docker Image for Medical Nudging Agent"

print_info "CodeBuild Project: $PROJECT_NAME"
print_info "Region: $REGION"
print_info "Target Image: $REPO_URL:$IMAGE_TAG"
echo ""

# Start CodeBuild
print_info "Starting CodeBuild project..."

BUILD_ID=$(aws codebuild start-build \
  --project-name "$PROJECT_NAME" \
  --region "$REGION" \
  --query 'build.id' \
  --output text 2>&1)

if [ $? -ne 0 ]; then
  print_error "Failed to start CodeBuild"
  echo "$BUILD_ID"
  exit 1
fi

print_success "Build started: $BUILD_ID"
print_info "Waiting for build to complete (typically 5-10 minutes)..."
echo ""

# ============================================================================
# Monitor Build Progress
# ============================================================================

ATTEMPT=0
MAX_ATTEMPTS=90  # 15 minutes (90 * 10s)
LAST_PHASE=""

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  ATTEMPT=$((ATTEMPT + 1))

  # Get build status and current phase
  BUILD_INFO=$(aws codebuild batch-get-builds \
    --ids "$BUILD_ID" \
    --region "$REGION" \
    --query 'builds[0].{status:buildStatus,phase:currentPhase}' \
    --output json 2>/dev/null)

  STATUS=$(echo "$BUILD_INFO" | grep -o '"status": *"[^"]*"' | cut -d'"' -f4)
  PHASE=$(echo "$BUILD_INFO" | grep -o '"phase": *"[^"]*"' | cut -d'"' -f4)

  # Print phase changes
  if [ "$PHASE" != "$LAST_PHASE" ] && [ -n "$PHASE" ]; then
    print_info "Build phase: $PHASE"
    LAST_PHASE="$PHASE"
  fi

  if [ "$STATUS" != "IN_PROGRESS" ]; then
    print_info "Build process completed with status: $STATUS"
    break
  fi

  # Progress indicator every minute
  if [ $((ATTEMPT % 6)) -eq 0 ]; then
    MINUTES=$((ATTEMPT / 6))
    print_info "Build in progress... (${MINUTES} minutes elapsed)"
  fi

  sleep 10
done

if [ $ATTEMPT -eq $MAX_ATTEMPTS ]; then
  print_error "Build timeout after 15 minutes"
  print_warning "Check build status at:"
  print_info "https://console.aws.amazon.com/codesuite/codebuild/projects/$PROJECT_NAME/history?region=$REGION"
  exit 1
fi

# Check if build succeeded
if [ "$STATUS" != "SUCCEEDED" ]; then
  print_error "Build failed with status: $STATUS"
  print_warning "Check build logs at:"
  print_info "https://console.aws.amazon.com/codesuite/codebuild/projects/$PROJECT_NAME/history?region=$REGION"

  # Try to get build logs
  print_info "Fetching recent build logs..."
  aws logs tail "/aws/codebuild/$PROJECT_NAME" --since 5m --region "$REGION" 2>/dev/null | tail -50 || true

  exit 1
fi

echo ""

# ============================================================================
# Verify Image in ECR
# ============================================================================

print_header "Verifying Docker Image in ECR"

print_info "Checking for image: $REPO_NAME:$IMAGE_TAG"
print_info "Waiting for ECR propagation..."
echo ""

sleep 5  # Brief wait for ECR to register the push

VERIFY_ATTEMPT=0
MAX_VERIFY_ATTEMPTS=24  # 2 minutes (24 * 5s)

while [ $VERIFY_ATTEMPT -lt $MAX_VERIFY_ATTEMPTS ]; do
  VERIFY_ATTEMPT=$((VERIFY_ATTEMPT + 1))

  if aws ecr describe-images \
    --repository-name "$REPO_NAME" \
    --image-ids imageTag="$IMAGE_TAG" \
    --region "$REGION" >/dev/null 2>&1; then

    print_success "Docker image successfully verified in ECR!"
    echo ""
    print_info "Image URI: $REPO_URL:$IMAGE_TAG"

    # Get image details
    IMAGE_DETAILS=$(aws ecr describe-images \
      --repository-name "$REPO_NAME" \
      --image-ids imageTag="$IMAGE_TAG" \
      --region "$REGION" \
      --query 'imageDetails[0]' \
      --output json 2>/dev/null || echo "{}")

    IMAGE_SIZE=$(echo "$IMAGE_DETAILS" | grep -o '"imageSizeInBytes": *[0-9]*' | cut -d':' -f2 | tr -d ' ')
    IMAGE_DIGEST=$(echo "$IMAGE_DETAILS" | grep -o '"imageDigest": *"[^"]*"' | cut -d'"' -f4)

    if [ -n "$IMAGE_SIZE" ]; then
      IMAGE_SIZE_MB=$((IMAGE_SIZE / 1024 / 1024))
      print_info "Image Size: ${IMAGE_SIZE_MB} MB"
    fi

    if [ -n "$IMAGE_DIGEST" ]; then
      print_info "Image Digest: ${IMAGE_DIGEST:0:20}..."
    fi

    echo ""
    print_success "Build and verification completed successfully!"

    # Print summary
    print_header "Build Summary"
    print_info "Project: $PROJECT_NAME"
    print_info "Build ID: $BUILD_ID"
    print_info "Image: $REPO_URL:$IMAGE_TAG"
    print_info "Architecture: ARM64"
    print_info "Status: SUCCESS"

    exit 0
  fi

  if [ $((VERIFY_ATTEMPT % 4)) -eq 0 ]; then
    print_info "Still waiting for image to appear in ECR... (attempt $VERIFY_ATTEMPT/$MAX_VERIFY_ATTEMPTS)"
  fi

  sleep 5
done

# ============================================================================
# Error: Image Not Found
# ============================================================================

print_error "Docker image not found in ECR after build completion"
echo ""
print_warning "This indicates the build succeeded but push may have failed."
print_info "Troubleshooting steps:"
print_info ""
print_info "  1. Check CodeBuild logs:"
print_info "     https://console.aws.amazon.com/codesuite/codebuild/projects/$PROJECT_NAME/history?region=$REGION"
print_info ""
print_info "  2. Verify ECR repository:"
print_info "     aws ecr describe-images --repository-name $REPO_NAME --region $REGION"
print_info ""
print_info "  3. Check IAM permissions for CodeBuild role"
print_info ""
print_info "  4. Try manual push:"
print_info "     aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO_URL"
print_info "     docker push $REPO_URL:$IMAGE_TAG"

exit 1
