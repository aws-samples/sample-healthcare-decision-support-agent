# ============================================================================
# CloudWatch Observability Resources
# ============================================================================

# -----------------------------------------------------------------------------
# Transaction Search (X-Ray → CloudWatch Logs)
# -----------------------------------------------------------------------------
# Enables rich span data in CloudWatch for debugging agent executions.
# Reference: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-Cloudformation.html

# Resource policy allowing X-Ray to write spans to CloudWatch Logs
resource "aws_cloudwatch_log_resource_policy" "xray_transaction_search" {
  policy_name = "TransactionSearchAccess"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TransactionSearchXRayAccess"
        Effect = "Allow"
        Principal = {
          Service = "xray.amazonaws.com"
        }
        Action = "logs:PutLogEvents"
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/application-signals/data:*"
        ]
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:xray:${local.region}:${local.account_id}:*"
          }
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
      }
    ]
  })
}

# Enable Transaction Search - sends X-Ray traces to CloudWatch Logs
# Note: This resource requires the aws provider >= 5.0 with X-Ray support
# If not available, run manually: aws xray update-trace-segment-destination --destination CloudWatchLogs
#
# Cross-platform: Uses AWS CLI directly without bash interpreter for Windows/Mac/Linux compatibility
resource "null_resource" "enable_transaction_search" {
  depends_on = [aws_cloudwatch_log_resource_policy.xray_transaction_search]

  provisioner "local-exec" {
    # AWS CLI works identically on Windows, Mac, and Linux
    # Use || true to handle idempotency - succeeds if already set to CloudWatchLogs
    command = "aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${local.region} ${local.aws_profile_flag} || true"
  }

  triggers = {
    # Only re-run if policy changes
    policy_hash = sha256(jsonencode(aws_cloudwatch_log_resource_policy.xray_transaction_search.policy_document))
  }
}

# -----------------------------------------------------------------------------
# CloudWatch Log Group for Agent Runtime
# -----------------------------------------------------------------------------
# AgentCore creates the runtime's log group itself, named after the runtime id
# (`/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT`), as soon as the runtime
# exists, so Terraform cannot own it as an aws_cloudwatch_log_group. The settings
# are applied to the service-created group instead: retention and KMS through the
# AWS CLI (cross-platform, like the X-Ray step above), the PHI data-protection
# policy, metric filters, and the data-lake subscription through resources that
# accept an existing group name. An earlier version created a differently named
# group that stayed empty, so none of these applied to the runtime's real logs.

locals {
  agent_runtime_log_group = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_id}-DEFAULT"
  agent_runtime_log_group_kms_command = (
    var.enable_kms_encryption
    ? "aws logs associate-kms-key --log-group-name ${local.agent_runtime_log_group} --kms-key-id ${aws_kms_key.cloudwatch[0].arn} --region ${local.region} ${local.aws_profile_flag}"
    : "echo kms encryption disabled"
  )
}

resource "null_resource" "agent_runtime_log_group" {
  triggers = {
    log_group = local.agent_runtime_log_group
    retention = tostring(var.log_retention_days)
    kms       = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : "none"
  }

  # `create-log-group` is a no-op ("|| true") when the service already created it.
  provisioner "local-exec" {
    command = "aws logs create-log-group --log-group-name ${local.agent_runtime_log_group} --region ${local.region} ${local.aws_profile_flag} || true"
  }

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --log-group-name ${local.agent_runtime_log_group} --retention-in-days ${var.log_retention_days} --region ${local.region} ${local.aws_profile_flag}"
  }

  provisioner "local-exec" {
    command = local.agent_runtime_log_group_kms_command
  }
}

# -----------------------------------------------------------------------------
# Log Metric Filters
# -----------------------------------------------------------------------------

# Filter for agent errors
resource "aws_cloudwatch_log_metric_filter" "agent_errors" {
  name           = "${var.stack_name}-agent-errors"
  pattern        = "ERROR"
  log_group_name = local.agent_runtime_log_group
  depends_on     = [null_resource.agent_runtime_log_group]

  metric_transformation {
    name      = "AgentErrors"
    namespace = "MedicalNudging"
    value     = "1"
  }
}

# Filter for tool call failures
resource "aws_cloudwatch_log_metric_filter" "tool_failures" {
  name           = "${var.stack_name}-tool-failures"
  pattern        = "\"tool\" ERROR"
  log_group_name = local.agent_runtime_log_group
  depends_on     = [null_resource.agent_runtime_log_group]

  metric_transformation {
    name      = "ToolFailures"
    namespace = "MedicalNudging"
    value     = "1"
  }
}

# -----------------------------------------------------------------------------
# SNS Topic for CloudWatch Alarms
# -----------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  count = var.enable_cloudwatch_alarms ? 1 : 0

  name = "${var.stack_name}-alerts"

  # CKV_AWS_26: Add KMS encryption to SNS topic
  kms_master_key_id = var.enable_kms_encryption ? aws_kms_key.sns[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-alerts"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Alarms
# -----------------------------------------------------------------------------

# High latency alarm
resource "aws_cloudwatch_metric_alarm" "high_latency" {
  count = var.enable_cloudwatch_alarms ? 1 : 0

  alarm_name          = "${var.stack_name}-high-latency"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "InferenceLatency"
  namespace           = "MedicalNudging"
  period              = 300
  statistic           = "Average"
  threshold           = var.latency_alarm_threshold_ms
  alarm_description   = "Agent inference latency exceeds ${var.latency_alarm_threshold_ms}ms threshold"

  dimensions = {
    Mode = "agentcore"
  }

  alarm_actions = var.enable_cloudwatch_alarms ? [aws_sns_topic.alerts[0].arn] : []
  ok_actions    = var.enable_cloudwatch_alarms ? [aws_sns_topic.alerts[0].arn] : []

  tags = local.common_tags
}

# High error rate alarm
resource "aws_cloudwatch_metric_alarm" "high_error_rate" {
  count = var.enable_cloudwatch_alarms ? 1 : 0

  alarm_name          = "${var.stack_name}-high-error-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "AgentErrors"
  namespace           = "MedicalNudging"
  period              = 300
  statistic           = "Sum"
  threshold           = var.error_alarm_threshold
  alarm_description   = "Agent error count exceeds ${var.error_alarm_threshold} in 5 minutes"

  alarm_actions = var.enable_cloudwatch_alarms ? [aws_sns_topic.alerts[0].arn] : []
  ok_actions    = var.enable_cloudwatch_alarms ? [aws_sns_topic.alerts[0].arn] : []

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# CloudWatch Dashboard
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "agent_monitoring" {
  count = var.enable_cloudwatch_dashboard ? 1 : 0

  dashboard_name = "${var.stack_name}-monitoring"

  dashboard_body = jsonencode({
    widgets = [
      # Row 1: Latency metrics
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Inference Latency"
          region = local.region
          metrics = [
            ["MedicalNudging", "InferenceLatency", "Mode", "local", "Status", "success", { stat = "Average", label = "Local Avg" }],
            [".", ".", ".", "agentcore", ".", ".", { stat = "Average", label = "AgentCore Avg" }],
            [".", ".", ".", "local", ".", ".", { stat = "p95", label = "Local p95" }],
            [".", ".", ".", "agentcore", ".", ".", { stat = "p95", label = "AgentCore p95" }],
          ]
          period = 300
          yAxis = {
            left = { min = 0, label = "Milliseconds" }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Duration Breakdown"
          region = local.region
          metrics = [
            ["MedicalNudging", "ToolDuration", "Mode", "local", "Status", "success", { stat = "Average", label = "Tool Duration" }],
            [".", "ModelDuration", ".", ".", ".", ".", { stat = "Average", label = "Model Duration" }],
          ]
          period  = 300
          stacked = true
          yAxis = {
            left = { min = 0, label = "Milliseconds" }
          }
        }
      },
      # Row 2: Counts and success rate
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "Inference Count"
          region = local.region
          metrics = [
            ["MedicalNudging", "InferenceCount", "Mode", "local", "Status", "success", { stat = "Sum", label = "Local Success" }],
            [".", ".", ".", "local", ".", "error", { stat = "Sum", label = "Local Error" }],
            [".", ".", ".", "agentcore", ".", "success", { stat = "Sum", label = "AgentCore Success" }],
            [".", ".", ".", "agentcore", ".", "error", { stat = "Sum", label = "AgentCore Error" }],
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "Nudge Count"
          region = local.region
          metrics = [
            ["MedicalNudging", "NudgeCount", "Mode", "local", "Status", "success", { stat = "Average", label = "Local Avg" }],
            [".", ".", ".", "agentcore", ".", ".", { stat = "Average", label = "AgentCore Avg" }],
          ]
          period = 300
          yAxis = {
            left = { min = 0, label = "Nudges" }
          }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "Error Metrics"
          region = local.region
          metrics = [
            ["MedicalNudging", "AgentErrors", { stat = "Sum", label = "Agent Errors" }],
            [".", "ToolFailures", { stat = "Sum", label = "Tool Failures" }],
          ]
          period = 300
        }
      },
      # Row 3: Tool-specific metrics
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Tool Call Duration"
          region = local.region
          metrics = [
            ["MedicalNudging", "ToolCallDuration", "ToolName", "search_guidelines", { stat = "Average", label = "search_guidelines" }],
            [".", ".", ".", "get_patient_data", { stat = "Average", label = "get_patient_data" }],
            [".", ".", ".", "invoke_subagent", { stat = "Average", label = "invoke_subagent" }],
          ]
          period = 300
          yAxis = {
            left = { min = 0, label = "Milliseconds" }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Tool Call Success/Failure"
          region = local.region
          metrics = [
            ["MedicalNudging", "ToolCallSuccess", "ToolName", "search_guidelines", { stat = "Sum", label = "search_guidelines Success" }],
            [".", "ToolCallFailure", ".", ".", { stat = "Sum", label = "search_guidelines Failure" }],
            [".", "ToolCallSuccess", ".", "get_patient_data", { stat = "Sum", label = "get_patient_data Success" }],
            [".", "ToolCallFailure", ".", ".", { stat = "Sum", label = "get_patient_data Failure" }],
          ]
          period = 300
        }
      },
    ]
  })
}

# -----------------------------------------------------------------------------
# Log Data Protection Policy (DSR finding CW1)
# -----------------------------------------------------------------------------
# Detects and masks PHI (Protected Health Information) in CloudWatch Logs to
# prevent sensitive data exposure. Uses AWS managed data identifiers to detect:
#
# Health/PHI Data:
# - DEA registration numbers, Medicare/Medicaid numbers
# - National Provider Identifiers (NPI), National Drug Codes (NDC)
# - Healthcare procedure codes (HCPCS)
# - Health insurance claim numbers (HICN)
#
# Personal Identifiers:
# - Driver's License, SSN, Credit Card numbers
# - Email addresses, IP addresses, Phone numbers
# - Names and Physical addresses
#
# Actions performed:
# 1. Audit: Logs detection events to CloudWatch Logs metrics
# 2. De-identify: Masks detected PHI in the log stream
#
# Reference: https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html

resource "aws_cloudwatch_log_data_protection_policy" "agent_runtime_phi_protection" {
  count = var.enable_log_data_protection ? 1 : 0

  log_group_name = local.agent_runtime_log_group
  depends_on     = [null_resource.agent_runtime_log_group]

  policy_document = jsonencode({
    Name    = "PHI-Data-Protection-Policy"
    Version = "2021-06-01"

    Statement = [
      # Statement 1: Audit - log when sensitive data is detected
      {
        Sid = "AuditPHI"
        DataIdentifier = [
          # US Health/PHI Identifiers
          "arn:aws:dataprotection::aws:data-identifier/DrugEnforcementAgencyNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/HealthcareProcedureCode-US",
          "arn:aws:dataprotection::aws:data-identifier/HealthInsuranceClaimNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/MedicareBeneficiaryNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/NationalDrugCode-US",
          "arn:aws:dataprotection::aws:data-identifier/NationalProviderId-US",

          # US Government IDs
          "arn:aws:dataprotection::aws:data-identifier/DriversLicense-US",
          "arn:aws:dataprotection::aws:data-identifier/Ssn-US",

          # Financial Information
          "arn:aws:dataprotection::aws:data-identifier/CreditCardNumber",

          # Contact Information
          "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
          "arn:aws:dataprotection::aws:data-identifier/IpAddress",
          "arn:aws:dataprotection::aws:data-identifier/PhoneNumber-US",

          # Personal Identifiable Information
          "arn:aws:dataprotection::aws:data-identifier/Name",
          "arn:aws:dataprotection::aws:data-identifier/Address",
        ]

        Operation = {
          Audit = {
            FindingsDestination = {}
          }
        }
      },
      # Statement 2: Deidentify - mask the sensitive data
      {
        Sid = "DeidentifyPHI"
        DataIdentifier = [
          # US Health/PHI Identifiers
          "arn:aws:dataprotection::aws:data-identifier/DrugEnforcementAgencyNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/HealthcareProcedureCode-US",
          "arn:aws:dataprotection::aws:data-identifier/HealthInsuranceClaimNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/MedicareBeneficiaryNumber-US",
          "arn:aws:dataprotection::aws:data-identifier/NationalDrugCode-US",
          "arn:aws:dataprotection::aws:data-identifier/NationalProviderId-US",

          # US Government IDs
          "arn:aws:dataprotection::aws:data-identifier/DriversLicense-US",
          "arn:aws:dataprotection::aws:data-identifier/Ssn-US",

          # Financial Information
          "arn:aws:dataprotection::aws:data-identifier/CreditCardNumber",

          # Contact Information
          "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
          "arn:aws:dataprotection::aws:data-identifier/IpAddress",
          "arn:aws:dataprotection::aws:data-identifier/PhoneNumber-US",

          # Personal Identifiable Information
          "arn:aws:dataprotection::aws:data-identifier/Name",
          "arn:aws:dataprotection::aws:data-identifier/Address",
        ]

        Operation = {
          Deidentify = {
            MaskConfig = {}
          }
        }
      }
    ]
  })
}
