# ============================================================================
# Medical Nudging AgentCore - Observability Data Lake
# ============================================================================
# Optional SNS + SQS + S3 data lake for observability events.
# Enabled via enable_datalake_observability variable.

# -----------------------------------------------------------------------------
# S3 Bucket for Observability Data Lake
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = "${var.stack_name}-observability-${local.account_id}"
  # Demo stacks must be destroyable; versioned buckets otherwise block `terraform destroy`.
  force_destroy = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability"
  })
}

resource "aws_s3_bucket_versioning" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.observability[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.observability[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.enable_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
    }
    bucket_key_enabled = var.enable_kms_encryption
  }
}

resource "aws_s3_bucket_public_access_block" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.observability[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  bucket = aws_s3_bucket.observability[0].id

  rule {
    id     = "transition-to-glacier"
    status = "Enabled"

    filter {
      prefix = "events/"
    }

    transition {
      days          = var.datalake_event_retention_days
      storage_class = "GLACIER"
    }

    expiration {
      days = var.datalake_event_retention_days + 365
    }
  }
}

# -----------------------------------------------------------------------------
# SNS Topic for Observability Events
# -----------------------------------------------------------------------------

resource "aws_sns_topic" "observability_events" {
  count = var.enable_datalake_observability ? 1 : 0

  name              = "${var.stack_name}-observability-events"
  kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.sns[0].id : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-events"
  })
}

resource "aws_sns_topic_policy" "observability_events" {
  count = var.enable_datalake_observability ? 1 : 0

  arn = aws_sns_topic.observability_events[0].arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowPublishFromAccount"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.observability_events[0].arn
      },
      {
        Sid    = "AllowSQSSubscription"
        Effect = "Allow"
        Principal = {
          Service = "sqs.amazonaws.com"
        }
        Action   = "sns:Subscribe"
        Resource = aws_sns_topic.observability_events[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sqs_queue.observability_events[0].arn
          }
        }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# SQS Queue for Buffering Events
# -----------------------------------------------------------------------------

resource "aws_sqs_queue" "observability_events_dlq" {
  count = var.enable_datalake_observability ? 1 : 0

  name                      = "${var.stack_name}-observability-events-dlq"
  message_retention_seconds = 1209600 # 14 days
  kms_master_key_id         = var.enable_kms_encryption ? aws_kms_key.sqs[0].id : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-events-dlq"
  })
}

resource "aws_sqs_queue" "observability_events" {
  count = var.enable_datalake_observability ? 1 : 0

  name                       = "${var.stack_name}-observability-events"
  visibility_timeout_seconds = 300    # 5 minutes for Lambda processing
  message_retention_seconds  = 345600 # 4 days
  kms_master_key_id          = var.enable_kms_encryption ? aws_kms_key.sqs[0].id : null

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.observability_events_dlq[0].arn
    maxReceiveCount     = 3
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-events"
  })
}

resource "aws_sqs_queue_policy" "observability_events" {
  count = var.enable_datalake_observability ? 1 : 0

  queue_url = aws_sqs_queue.observability_events[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSNSMessages"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.observability_events[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.observability_events[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "observability_events_sqs" {
  count = var.enable_datalake_observability ? 1 : 0

  topic_arn = aws_sns_topic.observability_events[0].arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.observability_events[0].arn

  raw_message_delivery = true
}

# -----------------------------------------------------------------------------
# Lambda Function for Datalake Collector
# -----------------------------------------------------------------------------

data "archive_file" "datalake_collector" {
  count = var.enable_datalake_observability ? 1 : 0

  type        = "zip"
  source_dir  = "${path.module}/../lambda/datalake_collector"
  output_path = "${path.module}/.build/datalake_collector.zip"
}

resource "aws_iam_role" "datalake_collector" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "${var.stack_name}-datalake-collector-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
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
    Name = "${var.stack_name}-datalake-collector-role"
  })
}

resource "aws_iam_role_policy" "datalake_collector" {
  count = var.enable_datalake_observability ? 1 : 0

  name = "DatalakeCollectorPolicy"
  role = aws_iam_role.datalake_collector[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.stack_name}-datalake-collector*"
      },
      {
        Sid    = "SQSAccess"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.observability_events[0].arn
      },
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectAcl"
        ]
        Resource = "${aws_s3_bucket.observability[0].arn}/*"
      }
      ], var.enable_kms_encryption ? [
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = [
          aws_kms_key.sqs[0].arn,
          aws_kms_key.s3[0].arn
        ]
      }
    ] : [])
  })
}

resource "aws_lambda_function" "datalake_collector" {
  count = var.enable_datalake_observability ? 1 : 0

  function_name = "${var.stack_name}-datalake-collector"
  role          = aws_iam_role.datalake_collector[0].arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.datalake_collector[0].output_path
  source_code_hash = data.archive_file.datalake_collector[0].output_base64sha256

  environment {
    variables = {
      BUCKET_NAME = aws_s3_bucket.observability[0].id
      STACK_NAME  = var.stack_name
      ENVIRONMENT = var.environment
      LOG_LEVEL   = var.log_level
    }
  }

  kms_key_arn = var.enable_kms_encryption ? aws_kms_key.lambda[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-datalake-collector"
  })
}

resource "aws_lambda_event_source_mapping" "datalake_collector_sqs" {
  count = var.enable_datalake_observability ? 1 : 0

  event_source_arn                   = aws_sqs_queue.observability_events[0].arn
  function_name                      = aws_lambda_function.datalake_collector[0].arn
  batch_size                         = 100
  maximum_batching_window_in_seconds = 30
  enabled                            = true
}

resource "aws_cloudwatch_log_group" "datalake_collector" {
  count = var.enable_datalake_observability ? 1 : 0

  name              = "/aws/lambda/${var.stack_name}-datalake-collector"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-datalake-collector-logs"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Alarms for Observability Pipeline
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "observability_dlq_messages" {
  count = var.enable_datalake_observability && var.enable_cloudwatch_alarms ? 1 : 0

  alarm_name          = "${var.stack_name}-observability-dlq-messages"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Alert when messages appear in the observability DLQ"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.observability_events_dlq[0].name
  }

  alarm_actions = var.enable_cloudwatch_alarms ? [aws_sns_topic.alerts[0].arn] : []

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-dlq-alarm"
  })
}

# -----------------------------------------------------------------------------
# Application CloudWatch Alarms (Custom Metrics from Observability)
# Note: These alarms use the new ErrorCount/RequestLatency metrics with
# Environment dimension, complementing the existing alarms in cloudwatch.tf
# which use AgentErrors/InferenceLatency metrics with Mode dimension.
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "observability_high_error_rate" {
  count = var.enable_datalake_observability && var.enable_cloudwatch_alarms ? 1 : 0

  alarm_name          = "${var.stack_name}-observability-high-error-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ErrorCount"
  namespace           = "MedicalNudging"
  period              = 300
  statistic           = "Sum"
  threshold           = var.error_alarm_threshold
  alarm_description   = "Alert when error rate exceeds threshold (${var.error_alarm_threshold} errors in 5 minutes)"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts[0].arn]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-high-error-rate-alarm"
  })
}

resource "aws_cloudwatch_metric_alarm" "observability_high_latency" {
  count = var.enable_datalake_observability && var.enable_cloudwatch_alarms ? 1 : 0

  alarm_name          = "${var.stack_name}-observability-high-latency"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RequestLatency"
  namespace           = "MedicalNudging"
  period              = 300
  extended_statistic  = "p95"
  threshold           = var.latency_alarm_threshold_ms
  alarm_description   = "Alert when p95 latency exceeds ${var.latency_alarm_threshold_ms}ms"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Environment = var.environment
  }

  alarm_actions = [aws_sns_topic.alerts[0].arn]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-high-latency-alarm"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Dashboard for Medical Nudging Metrics
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "medical_nudging" {
  count = var.enable_datalake_observability ? 1 : 0

  dashboard_name = "${var.stack_name}-dashboard"

  dashboard_body = jsonencode({
    widgets = [
      # Row 1: Request Volume (by Specialty)
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Request Volume"
          view    = "timeSeries"
          stacked = true
          region  = local.region
          metrics = [
            ["MedicalNudging", "RequestCount", "Specialty", "general", "VisitType", "ambulatory", "Environment", var.environment, { label = "General" }],
            ["...", "endocrinology", ".", ".", ".", ".", { label = "Endocrinology" }],
            ["...", "cardiology", ".", ".", ".", ".", { label = "Cardiology" }]
          ]
          period = 300
          stat   = "Sum"
        }
      },
      # Row 1: Request Latency
      {
        type   = "metric"
        x      = 8
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Request Latency"
          view    = "timeSeries"
          stacked = false
          region  = local.region
          metrics = [
            ["MedicalNudging", "RequestLatency", "Specialty", "general", "VisitType", "ambulatory", "Environment", var.environment, { label = "p50", stat = "p50", color = "#1f77b4" }],
            ["...", { label = "p95", stat = "p95", color = "#ff7f0e" }],
            ["...", { label = "p99", stat = "p99", color = "#d62728" }]
          ]
          period = 300
          yAxis = {
            left = {
              label     = "Milliseconds"
              showUnits = false
            }
          }
        }
      },
      # Row 1: Nudges Generated
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Nudges Generated"
          view    = "timeSeries"
          stacked = true
          region  = local.region
          metrics = [
            ["MedicalNudging", "NudgeCount", "Specialty", "general", "VisitType", "ambulatory", "Environment", var.environment, { label = "General", stat = "Sum" }],
            ["...", "endocrinology", ".", ".", ".", ".", { label = "Endocrinology", stat = "Sum" }],
            ["...", "cardiology", ".", ".", ".", ".", { label = "Cardiology", stat = "Sum" }]
          ]
          period = 300
        }
      },
      # Row 2: Tool Call Volume
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Tool Call Volume"
          view    = "timeSeries"
          stacked = true
          region  = local.region
          metrics = [
            ["MedicalNudging", "ToolCallCount", "ToolName", "search_guidelines", "Environment", var.environment, { label = "search_guidelines" }],
            ["...", "get_patient_data", ".", ".", { label = "get_patient_data" }],
            ["...", "list_guidelines", ".", ".", { label = "list_guidelines" }],
            ["...", "invoke_subagent", ".", ".", { label = "invoke_subagent" }],
            ["...", "calculate", ".", ".", { label = "calculate" }]
          ]
          period = 300
          stat   = "Sum"
        }
      },
      # Row 2: Inference Latency (AgentCore mode)
      {
        type   = "metric"
        x      = 8
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Inference Latency (AgentCore)"
          view    = "timeSeries"
          stacked = false
          region  = local.region
          metrics = [
            ["MedicalNudging", "InferenceLatency", "Mode", "agentcore", "Status", "success", { label = "p50", stat = "p50", color = "#1f77b4" }],
            ["...", { label = "p95", stat = "p95", color = "#ff7f0e" }],
            ["...", { label = "Avg", stat = "Average", color = "#2ca02c" }]
          ]
          period = 300
          yAxis = {
            left = {
              label     = "Milliseconds"
              showUnits = false
            }
          }
        }
      },
      # Row 2: Observability Pipeline Health
      {
        type   = "metric"
        x      = 16
        y      = 6
        width  = 8
        height = 6
        properties = {
          title   = "Observability Pipeline Health"
          view    = "timeSeries"
          stacked = false
          region  = local.region
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${var.stack_name}-observability-events", { label = "Queue Depth", color = "#1f77b4" }],
            [".", ".", ".", "${var.stack_name}-observability-events-dlq", { label = "DLQ Messages", color = "#d62728" }]
          ]
          period = 60
          stat   = "Maximum"
        }
      },
      # Row 3: Success vs Error Count
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Success vs Errors"
          view    = "timeSeries"
          stacked = false
          region  = local.region
          metrics = [
            ["MedicalNudging", "SuccessCount", "Specialty", "general", "VisitType", "ambulatory", "Environment", var.environment, { label = "Success (General)", color = "#2ca02c" }],
            ["...", "endocrinology", ".", ".", ".", ".", { label = "Success (Endo)", color = "#1f77b4" }],
            ["MedicalNudging", "InferenceCount", "Mode", "agentcore", "Status", "error", { label = "Errors (AgentCore)", color = "#d62728" }]
          ]
          period = 300
          stat   = "Sum"
        }
      },
      # Row 3: Inference Count by Mode
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Inference Count by Mode"
          view    = "timeSeries"
          stacked = true
          region  = local.region
          metrics = [
            ["MedicalNudging", "InferenceCount", "Mode", "agentcore", "Status", "success", { label = "AgentCore Success", color = "#2ca02c" }],
            ["...", ".", ".", "error", { label = "AgentCore Error", color = "#d62728" }],
            ["...", "local", ".", "success", { label = "Local Success", color = "#1f77b4" }]
          ]
          period = 300
          stat   = "Sum"
        }
      }
    ]
  })
}
