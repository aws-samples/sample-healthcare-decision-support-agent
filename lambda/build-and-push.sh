#!/bin/bash
set -e

# Configuration
AWS_PROFILE="${AWS_PROFILE:-}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Building and pushing Lambda images...${NC}"

# Authenticate Docker with ECR
echo -e "${BLUE}Authenticating with ECR...${NC}"
AWS_PROFILE=$AWS_PROFILE aws ecr get-login-password --region $AWS_REGION | \
    docker login --username AWS --password-stdin $ECR_BASE

# Build and push ingest_opensearch
echo -e "${BLUE}Building ingest_opensearch Lambda image...${NC}"
docker build \
    --platform linux/amd64 \
    -t medical-nudging-ingest-opensearch \
    -f ingest_opensearch/Dockerfile \
    .

echo -e "${BLUE}Tagging and pushing ingest_opensearch...${NC}"
docker tag medical-nudging-ingest-opensearch \
    ${ECR_BASE}/medical-nudging-ingest-opensearch:latest

docker push ${ECR_BASE}/medical-nudging-ingest-opensearch:latest

echo -e "${GREEN}Successfully pushed ingest_opensearch image${NC}"

echo -e "${GREEN}All Lambda images built and pushed successfully!${NC}"
