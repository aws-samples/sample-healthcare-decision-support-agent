# ============================================================================
# Medical Nudging AgentCore - S3 Buckets
# ============================================================================

# ============================================================================
# S3 Bucket for Agent Source Code (for CodeBuild)
# ============================================================================

resource "aws_s3_bucket" "agent_source" {
  bucket_prefix = "${var.stack_name}-agent-source-"
  force_destroy = true

  tags = merge(local.common_tags, {
    Name    = "${var.stack_name}-agent-source"
    Purpose = "Store agent source code for CodeBuild"
    Module  = "S3"
  })
}

# Block public access - source code bucket
resource "aws_s3_bucket_public_access_block" "agent_source" {
  bucket = aws_s3_bucket.agent_source.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning for source code tracking
resource "aws_s3_bucket_versioning" "agent_source" {
  bucket = aws_s3_bucket.agent_source.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Lifecycle configuration for source code - CKV2_AWS_61 and CKV_AWS_300: Abort incomplete multipart uploads
resource "aws_s3_bucket_lifecycle_configuration" "agent_source" {
  bucket = aws_s3_bucket.agent_source.id

  rule {
    id     = "cleanup-incomplete-uploads"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Server-side encryption - CKV_AWS_145: Use KMS encryption instead of AES256
resource "aws_s3_bucket_server_side_encryption_configuration" "agent_source" {
  bucket = aws_s3_bucket.agent_source.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
      sse_algorithm     = var.enable_kms_encryption ? "aws:kms" : "AES256"
    }
    bucket_key_enabled = true
  }
}

# ============================================================================
# Archive and Upload Agent Source Code
# ============================================================================

# Create agent source archive using cross-platform Python script
# This replaces the bash-based null_resource.prepare_agent_code for Windows compatibility
data "external" "agent_source_archive" {
  program = ["python", "${path.module}/scripts/create_agent_archive.py"]

  query = {
    project_root  = abspath("${path.module}/..")
    terraform_dir = abspath(path.module)
    output_path   = abspath("${path.module}/.terraform/agent-code.zip")
  }
}

# Trigger rebuild when source archive content changes.
# The archive already includes src/, prompts/, guidelines/, pyproject.toml, Dockerfile, etc.
locals {
  agent_source_triggers = {
    archive_md5 = data.external.agent_source_archive.result.md5
  }
}

# Upload to S3 (re-uploads when MD5 changes or source files change)
resource "aws_s3_object" "agent_source" {
  bucket = aws_s3_bucket.agent_source.id
  key    = "agent-code-${data.external.agent_source_archive.result.md5}.zip"
  source = data.external.agent_source_archive.result.path
  etag   = data.external.agent_source_archive.result.md5

  # Force re-upload when tracked source files change
  # This ensures CodeBuild gets the latest code even if MD5 detection fails
  lifecycle {
    replace_triggered_by = [
      terraform_data.agent_source_trigger.id
    ]
  }

  tags = merge(local.common_tags, {
    Name      = "agent-source-code"
    MD5       = data.external.agent_source_archive.result.md5
    Timestamp = timestamp()
  })
}

# Trigger resource that changes when source files change
resource "terraform_data" "agent_source_trigger" {
  input = local.agent_source_triggers
}

# ============================================================================
# S3 Bucket for Clinical Guidelines (PDFs)
# ============================================================================

resource "aws_s3_bucket" "guidelines" {
  bucket = "${coalesce(var.guidelines_bucket_name, "${var.stack_name}-guidelines")}-${local.account_id}"
  # Demo stacks must be destroyable; versioned buckets otherwise block `terraform destroy`.
  force_destroy = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name    = "${var.stack_name}-guidelines"
    Purpose = "Store clinical guideline PDFs"
    Module  = "S3"
  })
}

# Block public access - guidelines bucket
resource "aws_s3_bucket_public_access_block" "guidelines" {
  bucket = aws_s3_bucket.guidelines.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning for guidelines
resource "aws_s3_bucket_versioning" "guidelines" {
  bucket = aws_s3_bucket.guidelines.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption - guidelines - CKV_AWS_145: Use KMS encryption instead of AES256
resource "aws_s3_bucket_server_side_encryption_configuration" "guidelines" {
  bucket = aws_s3_bucket.guidelines.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
      sse_algorithm     = var.enable_kms_encryption ? "aws:kms" : "AES256"
    }
    bucket_key_enabled = true
  }
}

# Lifecycle rule for guidelines - intelligent tiering for cost optimization
resource "aws_s3_bucket_lifecycle_configuration" "guidelines" {
  bucket = aws_s3_bucket.guidelines.id

  rule {
    id     = "intelligent-tiering"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "INTELLIGENT_TIERING"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    # CKV_AWS_300: Abort incomplete multipart uploads after 7 days
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ============================================================================
# S3 Event Notifications for Audit Trail - CKV2_AWS_62
# ============================================================================

# SNS topic for agent source bucket audit events
resource "aws_sns_topic" "agent_source_audit" {
  name              = "${var.stack_name}-agent-source-audit"
  kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.sns[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-agent-source-audit"
  })
}

# Policy allowing S3 to publish to SNS
resource "aws_sns_topic_policy" "agent_source_audit" {
  arn = aws_sns_topic.agent_source_audit.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3Publish"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.agent_source_audit.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.agent_source.arn
          }
        }
      }
    ]
  })
}

# S3 bucket notification for agent source bucket
resource "aws_s3_bucket_notification" "agent_source_audit" {
  bucket = aws_s3_bucket.agent_source.id

  topic {
    topic_arn = aws_sns_topic.agent_source_audit.arn
    events    = ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
  }

  depends_on = [aws_sns_topic_policy.agent_source_audit]
}
