# ============================================================================
# Medical Nudging AgentCore - OTEL Spans Export to S3
# ============================================================================
# Exports X-Ray OTEL spans from aws/spans log group to S3 via Kinesis Firehose.
# Enabled via enable_datalake_observability variable.

# -----------------------------------------------------------------------------
# S3 Bucket for OTEL Spans
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = "${var.stack_name}-otel-spans-${local.account_id}"
  # Demo stacks must be destroyable; versioned buckets otherwise block `terraform destroy`.
  force_destroy = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name     = "${var.stack_name}-otel-spans"
    DataType = "otel-spans"
    PHIRisk  = "low"
  })
}

resource "aws_s3_bucket_versioning" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.otel_spans[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.otel_spans[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.enable_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
    }
    bucket_key_enabled = var.enable_kms_encryption
  }
}

resource "aws_s3_bucket_public_access_block" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.otel_spans[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.otel_spans[0].id

  rule {
    id     = "transition-to-glacier"
    status = "Enabled"

    filter {
      prefix = "data/"
    }

    transition {
      days          = var.datalake_event_retention_days
      storage_class = "GLACIER"
    }

    expiration {
      days = var.datalake_event_retention_days + 365
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

resource "aws_iam_role" "firehose_otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-firehose-otel-spans-role"

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
    Name = "${var.stack_name}-firehose-otel-spans-role"
  })
}

resource "aws_iam_role_policy" "firehose_otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "FirehoseS3Access"
  role = aws_iam_role.firehose_otel_spans[0].id

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
          aws_s3_bucket.otel_spans[0].arn,
          "${aws_s3_bucket.otel_spans[0].arn}/*"
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/kinesisfirehose/${var.stack_name}-otel-spans:*"
      },
      {
        Sid    = "LambdaInvoke"
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction",
          "lambda:GetFunctionConfiguration"
        ]
        Resource = "${aws_lambda_function.firehose_transform_spans[0].arn}:*"
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

resource "aws_cloudwatch_log_group" "firehose_otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name              = "/aws/kinesisfirehose/${var.stack_name}-otel-spans"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-otel-spans-logs"
  })
}

resource "aws_cloudwatch_log_stream" "firehose_otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose_otel_spans[0].name
}

# -----------------------------------------------------------------------------
# Firehose Transformation Lambda
# -----------------------------------------------------------------------------

data "archive_file" "firehose_transform_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  type        = "zip"
  source_file = "${path.module}/../lambda/firehose_transform_spans/handler.py"
  output_path = "${path.module}/.terraform/tmp/firehose_transform_spans.zip"
}

resource "aws_cloudwatch_log_group" "firehose_transform_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name              = "/aws/lambda/${var.stack_name}-firehose-transform-spans"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-transform-spans-logs"
  })
}

resource "aws_iam_role" "firehose_transform_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-firehose-transform-spans-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-transform-spans-role"
  })
}

resource "aws_iam_role_policy" "firehose_transform_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "LambdaBasicExecution"
  role = aws_iam_role.firehose_transform_spans[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      Resource = "${aws_cloudwatch_log_group.firehose_transform_spans[0].arn}:*"
    }]
  })
}

resource "aws_lambda_function" "firehose_transform_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  function_name = "${var.stack_name}-firehose-transform-spans"
  description   = "Transform CloudWatch Logs format to flat OTEL span JSON"

  filename         = data.archive_file.firehose_transform_spans[0].output_path
  source_code_hash = data.archive_file.firehose_transform_spans[0].output_base64sha256

  role        = aws_iam_role.firehose_transform_spans[0].arn
  handler     = "handler.handler"
  runtime     = "python3.12"
  timeout     = 60
  memory_size = 128

  depends_on = [aws_cloudwatch_log_group.firehose_transform_spans]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-firehose-transform-spans"
  })
}

resource "aws_kinesis_firehose_delivery_stream" "otel_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "${var.stack_name}-otel-spans"
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
    role_arn            = aws_iam_role.firehose_otel_spans[0].arn
    bucket_arn          = aws_s3_bucket.otel_spans[0].arn
    prefix              = "data/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    buffering_size      = 5  # MB
    buffering_interval  = 60 # seconds
    compression_format  = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose_otel_spans[0].name
      log_stream_name = aws_cloudwatch_log_stream.firehose_otel_spans[0].name
    }

    processing_configuration {
      enabled = true

      processors {
        type = "Lambda"

        parameters {
          parameter_name  = "LambdaArn"
          parameter_value = "${aws_lambda_function.firehose_transform_spans[0].arn}:$LATEST"
        }
      }
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-otel-spans"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Logs → Firehose IAM Role
# -----------------------------------------------------------------------------

resource "aws_iam_role" "cwlogs_to_firehose_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-cwlogs-firehose-spans-role"

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
    Name = "${var.stack_name}-cwlogs-firehose-spans-role"
  })
}

resource "aws_iam_role_policy" "cwlogs_to_firehose_spans" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "PutToFirehose"
  role = aws_iam_role.cwlogs_to_firehose_spans[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "firehose:PutRecord",
        "firehose:PutRecordBatch"
      ]
      Resource = aws_kinesis_firehose_delivery_stream.otel_spans[0].arn
    }]
  })
}

# -----------------------------------------------------------------------------
# Subscription Filter: aws/spans → Firehose
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_subscription_filter" "otel_spans_to_firehose" {
  count = var.enable_datalake_observability ? 1 : 0

  name            = "${var.stack_name}-otel-spans-export"
  log_group_name  = "aws/spans"
  filter_pattern  = "" # All logs
  destination_arn = aws_kinesis_firehose_delivery_stream.otel_spans[0].arn
  role_arn        = aws_iam_role.cwlogs_to_firehose_spans[0].arn

  depends_on = [
    aws_iam_role_policy.cwlogs_to_firehose_spans
  ]
}
