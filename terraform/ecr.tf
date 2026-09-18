# ============================================================================
# Medical Nudging AgentCore - ECR Repository
# ============================================================================

resource "aws_ecr_repository" "agent_ecr" {
  name                 = "${var.stack_name}-${var.ecr_repository_name}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # CKV_AWS_136: Use KMS encryption for ECR repository
  encryption_configuration {
    encryption_type = var.enable_kms_encryption ? "KMS" : "AES256"
    kms_key         = var.enable_kms_encryption ? aws_kms_key.ecr[0].arn : null
  }

  force_delete = true

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-ecr-repository"
    Module = "ECR"
  })
}

# ECR Repository Policy - Allow pull from account
resource "aws_ecr_repository_policy" "agent_ecr" {
  repository = aws_ecr_repository.agent_ecr.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowPullFromAccount"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
      },
      {
        Sid    = "AllowAgentCorePull"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })
}

# ECR Lifecycle Policy - Protect tagged images, clean untagged
resource "aws_ecr_lifecycle_policy" "agent_ecr" {
  repository = aws_ecr_repository.agent_ecr.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove untagged images older than 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep last 30 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["20"] # Matches date-based tags like 20260203-2023
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
