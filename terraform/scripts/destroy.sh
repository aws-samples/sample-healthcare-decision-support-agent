#!/bin/bash

# ============================================================================
# Destroy Script for Medical Nudging AgentCore (Terraform)
# ============================================================================
# This script safely destroys all resources created by this Terraform configuration
# Usage: ./scripts/destroy.sh  (run from terraform/)

set -e  # Exit on error

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source common functions
source "$SCRIPT_DIR/lib/common.sh"

# ============================================================================
# Pre-flight Checks
# ============================================================================

print_header "Medical Nudging AgentCore - Resource Cleanup"

print_warning "Starting resource cleanup process..."
echo ""

# Check Terraform installation
if ! command_exists terraform; then
    print_error "Terraform is not installed"
    exit 1
fi

# Check AWS CLI installation
if ! command_exists aws; then
    print_error "AWS CLI is not installed"
    exit 1
fi

# Load AWS profile from tfvars
load_aws_profile "$SCRIPT_DIR/terraform.tfvars"

# Legacy variable for backward compatibility
AWS_PROFILE_FROM_TFVARS=""
if [ -f "$SCRIPT_DIR/terraform.tfvars" ]; then
    AWS_PROFILE_FROM_TFVARS=$(grep 'aws_profile' "$SCRIPT_DIR/terraform.tfvars" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' || echo "")
fi

# Set AWS profile if specified
if [ -n "$AWS_PROFILE_FROM_TFVARS" ] && [ "$AWS_PROFILE_FROM_TFVARS" != "null" ]; then
    export AWS_PROFILE="$AWS_PROFILE_FROM_TFVARS"
    print_info "Using AWS profile: $AWS_PROFILE"
fi

# Check AWS credentials
if ! aws sts get-caller-identity > /dev/null 2>&1; then
    print_error "AWS credentials are not configured or invalid"
    exit 1
fi

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "")

print_info "AWS Account: $AWS_ACCOUNT"
if [ -n "$AWS_REGION" ]; then
    print_info "AWS Region: $AWS_REGION"
fi

echo ""

# ============================================================================
# Check for Terraform State
# ============================================================================

cd "$SCRIPT_DIR"

if [ ! -f "terraform.tfstate" ] && [ ! -d ".terraform" ]; then
    print_warning "No Terraform state found"
    print_info "Either no resources have been deployed, or state is stored remotely"

    read -p "Do you want to attempt to initialize Terraform? (yes/no): " -r
    echo ""

    if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        print_info "Initializing Terraform..."
        terraform init
    else
        print_info "Cleanup cancelled"
        exit 0
    fi
fi

# ============================================================================
# Show Resources to be Destroyed
# ============================================================================

print_header "Planning Resource Destruction"

print_info "Creating destruction plan..."
echo ""

if ! terraform plan -destroy -out=destroy.tfplan; then
    print_error "Failed to create destruction plan"
    exit 1
fi

echo ""

# ============================================================================
# First Confirmation
# ============================================================================

print_warning "========================================"
print_warning "RESOURCE DESTRUCTION CONFIRMATION"
print_warning "========================================"
echo ""
print_warning "This will permanently delete the following resources:"
print_warning "  - AgentCore Runtime (and all configurations)"
print_warning "  - Aurora Serverless v2 cluster (and ALL DATA)"
print_warning "  - Secrets Manager secret (credentials)"
print_warning "  - VPC, subnets, security groups"
print_warning "  - S3 Buckets (source code + guidelines)"
print_warning "  - ECR Repository (including all images)"
print_warning "  - CodeBuild Project"
print_warning "  - IAM Roles and Policies"
print_warning "  - CloudWatch Log Groups"
echo ""
print_error "THIS ACTION CANNOT BE UNDONE!"
print_error "ALL DATA IN AURORA DATABASE WILL BE LOST!"
echo ""

read -p "Are you absolutely sure you want to destroy all resources? (yes/no): " -r
echo ""

if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    print_info "Destruction cancelled by user"
    rm -f destroy.tfplan
    exit 0
fi

# ============================================================================
# Second Confirmation (Double Safety)
# ============================================================================

print_warning "========================================"
print_warning "SECOND CONFIRMATION REQUIRED"
print_warning "========================================"
echo ""
print_warning "You are about to destroy:"
print_warning "  - All Medical Nudging infrastructure"
print_warning "  - Database with clinical guidelines"
print_warning "  - Agent runtime and container images"
echo ""

read -p "Type 'DESTROY' to confirm: " -r
echo ""

if [ "$REPLY" != "DESTROY" ]; then
    print_info "Destruction cancelled - confirmation text did not match"
    rm -f destroy.tfplan
    exit 0
fi

# ============================================================================
# Execute Destruction
# ============================================================================

print_header "Destroying Resources"

print_warning "Starting resource destruction..."
print_warning "This may take 10-15 minutes (Aurora deletion is slow)..."
echo ""

if terraform apply destroy.tfplan; then
    print_success "All resources destroyed successfully"
else
    print_error "Destruction failed"
    print_warning "Some resources may still exist. Please check AWS Console"
    rm -f destroy.tfplan
    exit 1
fi

rm -f destroy.tfplan

echo ""

# ============================================================================
# Cleanup Local Files
# ============================================================================

print_info "Cleaning up local Terraform files..."
echo ""

# Ask about state file cleanup
read -p "Do you want to remove local Terraform state files? (yes/no): " -r
echo ""

if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    rm -f terraform.tfstate
    rm -f terraform.tfstate.backup
    rm -f tfplan
    rm -f destroy.tfplan
    print_success "Local state files removed"
fi

# Ask about .terraform directory
read -p "Do you want to remove .terraform directory? (yes/no): " -r
echo ""

if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    rm -rf .terraform
    rm -f .terraform.lock.hcl
    print_success ".terraform directory removed"
fi

# Ask about agent-code directory
if [ -d "$SCRIPT_DIR/agent-code" ]; then
    read -p "Do you want to remove agent-code directory? (yes/no): " -r
    echo ""

    if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        rm -rf "$SCRIPT_DIR/agent-code"
        print_success "agent-code directory removed"
    fi
fi

echo ""

# ============================================================================
# Verification
# ============================================================================

print_header "Verifying Cleanup"

# Get region from tfvars or default
REGION_FROM_TFVARS=$(grep 'aws_region' "$SCRIPT_DIR/terraform.tfvars" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' || echo "us-east-1")

# Check for remaining resources
STACK_NAME=$(grep 'stack_name' "$SCRIPT_DIR/terraform.tfvars" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' || echo "medical-nudging")

print_info "Checking for remaining resources matching '$STACK_NAME'..."
echo ""

# Check ECR repositories
ECR_REPOS=$(aws ecr describe-repositories --region "$REGION_FROM_TFVARS" 2>/dev/null | grep -c "$STACK_NAME" || echo "0")
if [ "$ECR_REPOS" -eq 0 ]; then
    print_success "ECR repositories cleaned up"
else
    print_warning "Found $ECR_REPOS ECR repositories matching '$STACK_NAME'"
fi

# Check S3 buckets
S3_BUCKETS=$(aws s3api list-buckets 2>/dev/null | grep -c "$STACK_NAME" || echo "0")
if [ "$S3_BUCKETS" -eq 0 ]; then
    print_success "S3 buckets cleaned up"
else
    print_warning "Found $S3_BUCKETS S3 buckets matching '$STACK_NAME'"
fi

# Check RDS clusters
RDS_CLUSTERS=$(aws rds describe-db-clusters --region "$REGION_FROM_TFVARS" 2>/dev/null | grep -c "$STACK_NAME" || echo "0")
if [ "$RDS_CLUSTERS" -eq 0 ]; then
    print_success "RDS clusters cleaned up"
else
    print_warning "Found $RDS_CLUSTERS RDS clusters matching '$STACK_NAME'"
fi

echo ""

# ============================================================================
# Completion Summary
# ============================================================================

print_header "Cleanup Complete"

print_success "All Medical Nudging resources have been destroyed"
echo ""

print_info "What to verify in AWS Console:"
print_info "1. Bedrock AgentCore - No runtimes remaining"
print_info "   https://console.aws.amazon.com/bedrock/home?region=$REGION_FROM_TFVARS#/agentcore"
echo ""
print_info "2. RDS - No Aurora clusters remaining"
print_info "   https://console.aws.amazon.com/rds/home?region=$REGION_FROM_TFVARS#databases"
echo ""
print_info "3. S3 - No buckets remaining"
print_info "   https://console.aws.amazon.com/s3/buckets"
echo ""
print_info "4. ECR - No repositories remaining"
print_info "   https://console.aws.amazon.com/ecr/repositories?region=$REGION_FROM_TFVARS"
echo ""
print_info "5. VPC - No VPCs remaining"
print_info "   https://console.aws.amazon.com/vpc/home?region=$REGION_FROM_TFVARS#vpcs"
echo ""
print_info "6. Secrets Manager - No secrets remaining"
print_info "   https://console.aws.amazon.com/secretsmanager/listsecrets?region=$REGION_FROM_TFVARS"
echo ""

print_success "Cleanup completed successfully!"
print_info "You can safely re-deploy by running: ./scripts/deploy.sh"
