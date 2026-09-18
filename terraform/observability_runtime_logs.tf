# ============================================================================
# Medical Nudging AgentCore - Runtime Logs Export to S3
# ============================================================================
# Exports AgentCore runtime logs (tool I/O, HTTP traffic) to S3 via Kinesis Firehose.
# Enabled via enable_datalake_observability variable.
#
# WARNING: This bucket contains high PHI risk data (tool inputs/outputs).
# Apply stricter access controls than otel-spans bucket.

# -----------------------------------------------------------------------------
# S3 Bucket for Runtime Logs (Higher PHI Risk)
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = "${var.stack_name}-runtime-logs-${local.account_id}"
  # Demo stacks must be destroyable; versioned buckets otherwise block `terraform destroy`.
  force_destroy = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name     = "${var.stack_name}-runtime-logs"
    DataType = "runtime-logs"
    PHIRisk  = "high"
  })
}

resource "aws_s3_bucket_versioning" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.runtime_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.runtime_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.enable_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
    }
    bucket_key_enabled = var.enable_kms_encryption
  }
}

resource "aws_s3_bucket_public_access_block" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.runtime_logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.runtime_logs[0].id

  rule {
    id     = "transition-to-glacier"
    status = "Enabled"

    filter {
      prefix = "data/"
    }

    transition {
      days          = var.datalake_runtime_logs_retention_days
      storage_class = "GLACIER"
    }

    expiration {
      days = var.datalake_runtime_logs_retention_days + 365
    }
  }

  rule {
    id     = "cleanup-errors"
    status = "Enabled"

    filter {
      prefix = "errors/"
    }

    expiration {
      days = 30
    }
  }
}

# -----------------------------------------------------------------------------
# Firehose IAM Role (to write to S3)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "firehose_runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-firehose-runtime-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "firehose.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.account_id
        }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-runtime-logs-role"
  })
}

resource "aws_iam_role_policy" "firehose_runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "FirehoseS3Access"
  role = aws_iam_role.firehose_runtime_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectAcl",
          "s3:GetBucketLocation",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.runtime_logs[0].arn,
          "${aws_s3_bucket.runtime_logs[0].arn}/*"
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/kinesisfirehose/${var.stack_name}-runtime-logs:*"
      }
      ], var.enable_kms_encryption ? [
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = aws_kms_key.s3[0].arn
      }
    ] : [])
  })
}

# -----------------------------------------------------------------------------
# Firehose Delivery Stream
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "firehose_runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  name              = "/aws/kinesisfirehose/${var.stack_name}-runtime-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-runtime-logs-logs"
  })
}

resource "aws_cloudwatch_log_stream" "firehose_runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose_runtime_logs[0].name
}

resource "aws_kinesis_firehose_delivery_stream" "runtime_logs" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "${var.stack_name}-runtime-logs"
  destination = "extended_s3"

  dynamic "server_side_encryption" {
    for_each = var.enable_kms_encryption ? [1] : []
    content {
      enabled  = true
      key_type = "CUSTOMER_MANAGED_CMK"
      key_arn  = aws_kms_key.s3[0].arn
    }
  }

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose_runtime_logs[0].arn
    bucket_arn          = aws_s3_bucket.runtime_logs[0].arn
    prefix              = "data/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    buffering_size      = 5  # MB
    buffering_interval  = 60 # seconds
    compression_format  = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose_runtime_logs[0].name
      log_stream_name = aws_cloudwatch_log_stream.firehose_runtime_logs[0].name
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-runtime-logs"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Logs → Firehose IAM Role
# -----------------------------------------------------------------------------

resource "aws_iam_role" "cwlogs_to_firehose_runtime" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-cwlogs-firehose-runtime-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "logs.${local.region}.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringLike = {
          "aws:SourceArn" = "arn:aws:logs:${local.region}:${local.account_id}:*"
        }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-cwlogs-firehose-runtime-role"
  })
}

resource "aws_iam_role_policy" "cwlogs_to_firehose_runtime" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "PutToFirehose"
  role = aws_iam_role.cwlogs_to_firehose_runtime[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "firehose:PutRecord",
        "firehose:PutRecordBatch"
      ]
      Resource = aws_kinesis_firehose_delivery_stream.runtime_logs[0].arn
    }]
  })
}

# -----------------------------------------------------------------------------
# Subscription Filter: AgentCore Runtime Logs → Firehose
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_subscription_filter" "runtime_logs_to_firehose" {
  count = var.enable_datalake_observability ? 1 : 0

  name            = "${var.stack_name}-runtime-logs-export"
  log_group_name  = local.agent_runtime_log_group
  filter_pattern  = "" # All logs
  destination_arn = aws_kinesis_firehose_delivery_stream.runtime_logs[0].arn
  role_arn        = aws_iam_role.cwlogs_to_firehose_runtime[0].arn

  depends_on = [
    aws_iam_role_policy.cwlogs_to_firehose_runtime,
    null_resource.agent_runtime_log_group
  ]
}
