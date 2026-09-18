# ============================================================================
# Medical Nudging AgentCore - Main Agent Runtime Resource
# ============================================================================

# Data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

# Local values
locals {
  account_id       = data.aws_caller_identity.current.account_id
  region           = data.aws_region.current.id
  aws_profile_flag = var.aws_profile != null && var.aws_profile != "" ? "--profile ${var.aws_profile}" : ""

  # Use archive MD5 prefix as image tag when default "latest" is used.
  # This is deterministic — only changes when source files change — avoiding
  # the timestamp() churn that would trigger a new runtime on every plan.
  effective_image_tag = var.image_tag == "latest" ? substr(data.external.agent_source_archive.result.md5, 0, 12) : var.image_tag

  # Common tags
  common_tags = {
    Project                    = "MedicalNudging"
    Environment                = var.environment
    StackName                  = var.stack_name
    "aws-control-tower:backup" = "true"
  }
}

# ============================================================================
# AgentCore Runtime - Main Agent Runtime Resource
# ============================================================================
# One runtime per stack, named after the stack and agent. A new image tag is an
# in-place update that AgentCore records as a new runtime version, so the runtime
# ARN, its log group, the online-evaluation configuration, and the configuration
# bundle keyed on that ARN all survive a rebuild. (Naming the runtime after the
# image tag replaced all four on every source change.)
# Observability is always enabled when SNS topic is configured.

resource "aws_bedrockagentcore_agent_runtime" "medical_nudging" {
  agent_runtime_name = replace("${var.stack_name}_${var.agent_name}", "-", "_")
  description        = var.description
  role_arn           = aws_iam_role.agent_execution.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agent_ecr.repository_url}:${local.effective_image_tag}"
    }
  }

  network_configuration {
    network_mode = var.network_mode
  }

  # NOTE: Do NOT set OTEL env vars (AWS_XRAY_SDK_ENABLED, OTEL_TRACES_EXPORTER, etc.)
  # AgentCore handles OpenTelemetry/X-Ray tracing automatically. Manual overrides break it.
  environment_variables = merge(
    {
      # AWS Configuration
      AWS_REGION         = local.region
      AWS_DEFAULT_REGION = local.region

      # Application Configuration
      AUTH_ENABLED    = tostring(var.auth_enabled)
      LOG_LEVEL       = var.log_level
      MODEL_ID        = var.model_id
      MAX_NUDGES      = tostring(var.max_nudges)
      GUIDELINES_PATH = "/app/guidelines"

      # Search Backend Configuration
      SEARCH_BACKEND        = var.search_backend
      OPENSEARCH_ENDPOINT   = var.opensearch_enabled ? aws_opensearchserverless_collection.guidelines[0].collection_endpoint : ""
      OPENSEARCH_INDEX_NAME = var.opensearch_index_name

      # S3 Guidelines Bucket
      GUIDELINES_BUCKET = aws_s3_bucket.guidelines.id

      # FHIR API data source. When this stack creates the HealthLake datastore the
      # runtime is pointed at it directly, so one apply is enough; a datastore from
      # another stack (fhir_datastore_arn) is wired through environment_variables.
      FHIR_API_ENABLED              = tostring(var.healthlake_enabled)
      HEALTHLAKE_DATASTORE_ENDPOINT = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_endpoint : ""

      # Observability Data Lake (always enabled when configured)
      OBSERVABILITY_SNS_TOPIC_ARN = var.enable_datalake_observability ? aws_sns_topic.observability_events[0].arn : ""
      OBSERVABILITY_BUCKET_NAME   = var.enable_datalake_observability ? aws_s3_bucket.observability[0].id : ""
      OBSERVABILITY_ENABLED       = tostring(var.enable_datalake_observability)
      STACK_NAME                  = var.stack_name
      ENVIRONMENT                 = var.environment

      # Image tag for debugging/auditing
      IMAGE_TAG      = local.effective_image_tag
      ECR_REPOSITORY = aws_ecr_repository.agent_ecr.name
    },
    var.environment_variables
  )

  depends_on = [
    null_resource.trigger_build,
    aws_iam_role_policy.agent_execution
  ]
}

# ============================================================================
# Service-Level Tracing (CloudWatch Delivery)
# ============================================================================
# Enables AgentCore service-level spans to be delivered to aws/spans log group.
# This is separate from agent code OTEL instrumentation - it captures platform-level
# invocation spans with metadata like latency_ms, error_type, session.id.

resource "aws_cloudwatch_log_delivery_source" "runtime_traces" {
  name         = "${var.stack_name}-runtime-traces-src"
  log_type     = "TRACES"
  resource_arn = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery_destination" "runtime_traces" {
  name                      = "${var.stack_name}-runtime-traces-dest"
  delivery_destination_type = "XRAY"
}

resource "aws_cloudwatch_log_delivery" "runtime_traces" {
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.runtime_traces.arn
  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_traces.name
}
