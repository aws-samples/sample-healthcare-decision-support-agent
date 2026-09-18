#!/bin/bash

# ============================================================================
# Deploy Script for Medical Nudging AgentCore (Terraform)
# ============================================================================
# This script automates the deployment process for the Terraform configuration
# Usage: ./scripts/deploy.sh [--auto-approve]  (run from terraform/)
#
# Options:
#   --auto-approve    Skip confirmation prompts (use with caution)

set -e  # Exit on error

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
AUTO_APPROVE=false

# Source common functions
source "$SCRIPT_DIR/lib/common.sh"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --auto-approve)
      AUTO_APPROVE=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# ============================================================================
# Pre-flight Checks
# ============================================================================

print_header "Medical Nudging AgentCore Deployment"

print_info "Starting deployment process..."
echo ""

# Check Terraform installation
if ! command_exists terraform; then
    print_error "Terraform is not installed. Please install Terraform >= 1.6"
    print_info "Visit: https://www.terraform.io/downloads"
    exit 1
fi

# Check Terraform version
TERRAFORM_VERSION=$(terraform version -json | grep -o '"terraform_version":"[^"]*' | cut -d'"' -f4)
print_success "Terraform version: $TERRAFORM_VERSION"

# Check AWS CLI installation
if ! command_exists aws; then
    print_error "AWS CLI is not installed. Please install and configure AWS CLI"
    print_info "Visit: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi

print_success "AWS CLI is installed"

# Check for AWS profile in tfvars
AWS_PROFILE_FROM_TFVARS=""
if [ -f "$SCRIPT_DIR/terraform.tfvars" ]; then
    AWS_PROFILE_FROM_TFVARS=$(grep '^aws_profile' "$SCRIPT_DIR/terraform.tfvars" 2>/dev/null | head -1 | cut -d'"' -f2 || echo "")
fi

# Set AWS profile if specified
if [ -n "$AWS_PROFILE_FROM_TFVARS" ] && [ "$AWS_PROFILE_FROM_TFVARS" != "null" ]; then
    export AWS_PROFILE="$AWS_PROFILE_FROM_TFVARS"
    print_info "Using AWS profile: $AWS_PROFILE"
fi

# Check AWS credentials
if ! aws sts get-caller-identity > /dev/null 2>&1; then
    print_error "AWS credentials are not configured or invalid"
    print_info "Run: aws configure"
    if [ -n "$AWS_PROFILE" ]; then
        print_info "Or check your profile: aws configure --profile $AWS_PROFILE"
    fi
    exit 1
fi

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "")
AWS_USER=$(aws sts get-caller-identity --query Arn --output text)

print_success "AWS Account: $AWS_ACCOUNT"
print_success "AWS User: $AWS_USER"
if [ -n "$AWS_REGION" ]; then
    print_success "AWS Region: $AWS_REGION"
fi

echo ""

# ============================================================================
# Configuration Check
# ============================================================================

print_info "Checking configuration files..."

# Check if terraform.tfvars exists
if [ ! -f "$SCRIPT_DIR/terraform.tfvars" ]; then
    print_warning "terraform.tfvars not found"
    print_info "Creating terraform.tfvars from example..."

    if [ -f "$SCRIPT_DIR/terraform.tfvars.example" ]; then
        cp "$SCRIPT_DIR/terraform.tfvars.example" "$SCRIPT_DIR/terraform.tfvars"
        print_success "Created terraform.tfvars"
        print_warning "Please review and update terraform.tfvars with your settings"
        print_info "Then run this script again"
        exit 0
    else
        print_error "terraform.tfvars.example not found"
        exit 1
    fi
fi

print_success "Configuration file found: terraform.tfvars"

# Check if agent.py exists in project root
if [ ! -f "$PROJECT_ROOT/agent.py" ]; then
    print_warning "agent.py not found in project root"
    print_info "Make sure agent.py is created before running terraform apply"
fi

# Check if Dockerfile exists in project root
if [ ! -f "$PROJECT_ROOT/Dockerfile" ]; then
    print_warning "Dockerfile not found in project root"
    print_info "Make sure Dockerfile is created before running terraform apply"
fi

echo ""

# ============================================================================
# Terraform Initialization
# ============================================================================

print_header "Initializing Terraform"

cd "$SCRIPT_DIR"

if terraform init; then
    print_success "Terraform initialized successfully"
else
    print_error "Terraform initialization failed"
    exit 1
fi

echo ""

# ============================================================================
# Terraform Validation
# ============================================================================

print_info "Validating Terraform configuration..."

if terraform validate; then
    print_success "Terraform configuration is valid"
else
    print_error "Terraform validation failed"
    exit 1
fi

echo ""

# ============================================================================
# Terraform Format Check
# ============================================================================

print_info "Checking Terraform formatting..."

if terraform fmt -check -recursive > /dev/null 2>&1; then
    print_success "Terraform files are properly formatted"
else
    print_warning "Some files need formatting. Running terraform fmt..."
    terraform fmt -recursive
    print_success "Files formatted"
fi

echo ""

# ============================================================================
# Terraform Plan
# ============================================================================

print_header "Creating Terraform Plan"

print_warning "This may take a few moments..."
echo ""

if terraform plan -out=tfplan; then
    print_success "Terraform plan created successfully"
else
    print_error "Terraform plan failed"
    exit 1
fi

echo ""

# ============================================================================
# Deployment Confirmation
# ============================================================================

if [ "$AUTO_APPROVE" = false ]; then
    print_warning "========================================"
    print_warning "DEPLOYMENT CONFIRMATION"
    print_warning "========================================"
    echo ""
    print_info "This will deploy the following resources:"
    print_info "  - VPC with subnets for Aurora"
    print_info "  - Aurora Serverless v2 PostgreSQL cluster"
    print_info "  - Secrets Manager secret for credentials"
    print_info "  - S3 Buckets (source code + guidelines)"
    print_info "  - ECR Repository"
    print_info "  - CodeBuild Project (ARM64)"
    print_info "  - IAM Roles and Policies"
    print_info "  - AgentCore Runtime"
    echo ""
    print_info "The deployment includes:"
    print_info "  - Building Docker image via CodeBuild"
    print_info "  - Creating Aurora database"
    print_info "  - Setting up network infrastructure"
    echo ""
    print_warning "Estimated deployment time: 15-20 minutes"
    print_warning "(Aurora cluster creation takes ~10 minutes)"
    echo ""

    read -p "Do you want to proceed with deployment? (yes/no): " -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        print_info "Deployment cancelled by user"
        rm -f tfplan
        exit 0
    fi
fi

# ============================================================================
# Terraform Apply
# ============================================================================

print_header "Deploying Resources"

print_info "Starting deployment..."
echo ""

if terraform apply tfplan; then
    print_success "Deployment completed successfully!"
else
    print_error "Deployment failed"
    rm -f tfplan
    exit 1
fi

# Clean up plan file
rm -f tfplan

echo ""

# ============================================================================
# Deployment Summary
# ============================================================================

print_header "Deployment Complete!"

print_info "Retrieving deployment outputs..."
echo ""

# Get outputs
RUNTIME_ID=$(terraform output -raw agent_runtime_id 2>/dev/null || echo "N/A")
RUNTIME_ARN=$(terraform output -raw agent_runtime_arn 2>/dev/null || echo "N/A")
ECR_URL=$(terraform output -raw ecr_repository_url 2>/dev/null || echo "N/A")
GUIDELINES_BUCKET=$(terraform output -raw guidelines_bucket_name 2>/dev/null || echo "N/A")
OPENSEARCH_ENDPOINT=$(terraform output -raw opensearch_collection_endpoint 2>/dev/null || echo "N/A")

echo ""
print_success "Agent Runtime ID: $RUNTIME_ID"
print_success "Agent Runtime ARN: $RUNTIME_ARN"
print_success "ECR Repository URL: $ECR_URL"
print_success "Guidelines Bucket: $GUIDELINES_BUCKET"
print_success "OpenSearch Endpoint: $OPENSEARCH_ENDPOINT"

echo ""
print_header "Next Steps"

print_info "1. Upload guidelines to S3 (triggers OpenSearch ingestion):"
print_info "   ./scripts/upload-guidelines.sh"
echo ""
print_info "2. Test the agent:"
print_info "   aws bedrock-agentcore invoke-agent-runtime \\"
print_info "     --agent-runtime-arn $RUNTIME_ARN \\"
print_info "     --qualifier DEFAULT \\"
print_info "     --payload \$(echo '{\"prompt\": \"Hello\"}' | base64) \\"
print_info "     response.json"
echo ""
print_info "3. View all outputs:"
print_info "   terraform output"
echo ""

print_success "Deployment completed successfully!"
