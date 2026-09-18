#!/bin/bash
set -e

echo "=== Build Script ==="
echo "AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID"
echo "AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION"
echo "IMAGE_REPO_NAME=$IMAGE_REPO_NAME"
echo "IMAGE_TAG=$IMAGE_TAG"

IMAGE_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:$IMAGE_TAG"
echo "Target image: $IMAGE_URI"

echo "Building Docker image..."
docker build --platform linux/arm64 -t "$IMAGE_URI" -f Dockerfile .

echo "Listing Docker images..."
docker images

echo "Pushing to ECR..."
docker push "$IMAGE_URI"

echo "Build and push completed successfully!"
