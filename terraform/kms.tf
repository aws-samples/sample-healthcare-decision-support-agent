# ============================================================================
# Medical Nudging AgentCore - KMS Keys for Encryption at Rest
# ============================================================================

# -----------------------------------------------------------------------------
# KMS Key for CloudWatch Logs Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "cloudwatch" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for CloudWatch Logs encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow CloudWatch Logs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${local.region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:CreateGrant",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${local.region}:${local.account_id}:*"
          }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-cloudwatch-kms"
  })
}

resource "aws_kms_alias" "cloudwatch" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-cloudwatch"
  target_key_id = aws_kms_key.cloudwatch[0].key_id
}

# -----------------------------------------------------------------------------
# KMS Key for S3 Bucket Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "s3" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for S3 bucket encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow S3 to use the key"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow CloudWatch Events to use the key"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-s3-kms"
  })
}

resource "aws_kms_alias" "s3" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-s3"
  target_key_id = aws_kms_key.s3[0].key_id
}

# -----------------------------------------------------------------------------
# KMS Key for ECR Repository Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "ecr" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for ECR repository encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow ECR to use the key"
        Effect = "Allow"
        Principal = {
          Service = "ecr.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-ecr-kms"
  })
}

resource "aws_kms_alias" "ecr" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-ecr"
  target_key_id = aws_kms_key.ecr[0].key_id
}

# -----------------------------------------------------------------------------
# KMS Key for SNS Topic Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "sns" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for SNS topic encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow SNS to use the key"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow S3 to use the key for SNS"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow CloudWatch to use the key"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-sns-kms"
  })
}

resource "aws_kms_alias" "sns" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-sns"
  target_key_id = aws_kms_key.sns[0].key_id
}

# -----------------------------------------------------------------------------
# KMS Key for Lambda Environment Variables Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "lambda" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for Lambda environment variables encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow Lambda to use the key"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow WebSocket Lambda Role"
        Effect = "Allow"
        Principal = {
          AWS = var.websocket_api_enabled ? aws_iam_role.websocket_lambda[0].arn : "arn:aws:iam::${local.account_id}:root"
        }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-lambda-kms"
  })
}

resource "aws_kms_alias" "lambda" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-lambda"
  target_key_id = aws_kms_key.lambda[0].key_id
}

# -----------------------------------------------------------------------------
# KMS Key for SQS Dead Letter Queue Encryption
# -----------------------------------------------------------------------------

resource "aws_kms_key" "sqs" {
  count = var.enable_kms_encryption ? 1 : 0

  description             = "KMS key for SQS queue encryption - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow SQS to use the key"
        Effect = "Allow"
        Principal = {
          Service = "sqs.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow Lambda to use the key"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid    = "Allow SNS to use the key for SQS delivery"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-sqs-kms"
  })
}

resource "aws_kms_alias" "sqs" {
  count = var.enable_kms_encryption ? 1 : 0

  name          = "alias/${var.stack_name}-sqs"
  target_key_id = aws_kms_key.sqs[0].key_id
}
