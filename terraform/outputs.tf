# ============================================================================
# Medical Nudging AgentCore - Outputs
# ============================================================================

# -----------------------------------------------------------------------------
# AgentCore Runtime Outputs
# -----------------------------------------------------------------------------

output "agent_runtime_id" {
  description = "ID of the created agent runtime"
  value       = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_id
}

output "agent_runtime_arn" {
  description = "ARN of the created agent runtime"
  value       = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn
}

output "agent_runtime_version" {
  description = "Version of the created agent runtime"
  value       = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_version
}

output "agent_runtime_name" {
  description = "Name of the created agent runtime"
  value       = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_name
}

# -----------------------------------------------------------------------------
# ECR Outputs
# -----------------------------------------------------------------------------

output "ecr_repository_url" {
  description = "URL of the ECR repository"
  value       = aws_ecr_repository.agent_ecr.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repository"
  value       = aws_ecr_repository.agent_ecr.arn
}

output "ecr_repository_name" {
  description = "Name of the ECR repository"
  value       = aws_ecr_repository.agent_ecr.name
}

# -----------------------------------------------------------------------------
# IAM Outputs
# -----------------------------------------------------------------------------

output "agent_execution_role_arn" {
  description = "ARN of the agent execution role"
  value       = aws_iam_role.agent_execution.arn
}

output "codebuild_role_arn" {
  description = "ARN of the CodeBuild service role"
  value       = aws_iam_role.image_build.arn
}

# -----------------------------------------------------------------------------
# CodeBuild Outputs
# -----------------------------------------------------------------------------

output "codebuild_project_name" {
  description = "Name of the CodeBuild project"
  value       = aws_codebuild_project.agent_image.name
}

output "codebuild_project_arn" {
  description = "ARN of the CodeBuild project"
  value       = aws_codebuild_project.agent_image.arn
}

# -----------------------------------------------------------------------------
# S3 Outputs
# -----------------------------------------------------------------------------

output "source_bucket_name" {
  description = "S3 bucket containing agent source code"
  value       = aws_s3_bucket.agent_source.id
}

output "source_bucket_arn" {
  description = "ARN of the S3 bucket containing agent source code"
  value       = aws_s3_bucket.agent_source.arn
}

output "source_object_key" {
  description = "S3 object key for the agent source code archive"
  value       = aws_s3_object.agent_source.key
}

output "source_code_md5" {
  description = "MD5 hash of the agent source code (triggers rebuild when changed)"
  value       = data.external.agent_source_archive.result.md5
}

output "guidelines_bucket_name" {
  description = "S3 bucket for clinical guidelines"
  value       = aws_s3_bucket.guidelines.id
}

output "guidelines_bucket_arn" {
  description = "ARN of the S3 bucket for clinical guidelines"
  value       = aws_s3_bucket.guidelines.arn
}

output "invoke_command" {
  description = "Command to test the agent"
  value       = <<-EOT
    # Test with sample data (from project root):
    uv run scripts/run_inference.py --mode agentcore --samples 2

    # Test with more samples:
    uv run scripts/run_inference.py --mode agentcore --samples 10
  EOT
}

# -----------------------------------------------------------------------------
# Quick Start Commands
# -----------------------------------------------------------------------------

output "quick_start" {
  description = "Quick start commands after deployment"
  value       = <<-EOT

    =============================================================
    Medical Nudging AgentCore Deployment Complete!
    =============================================================

    1. Upload guidelines to S3:
       ./scripts/upload-guidelines.sh

    2. Test agent health:
       curl https://<agent-endpoint>/ping

    3. View all outputs:
       terraform output

    =============================================================
  EOT
}

# -----------------------------------------------------------------------------
# OpenSearch Serverless Outputs
# -----------------------------------------------------------------------------

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint"
  value       = var.opensearch_enabled ? aws_opensearchserverless_collection.guidelines[0].collection_endpoint : null
}

output "opensearch_collection_arn" {
  description = "OpenSearch Serverless collection ARN"
  value       = var.opensearch_enabled ? aws_opensearchserverless_collection.guidelines[0].arn : null
}

output "opensearch_dashboard_endpoint" {
  description = "OpenSearch Serverless dashboard endpoint"
  value       = var.opensearch_enabled ? aws_opensearchserverless_collection.guidelines[0].dashboard_endpoint : null
}

# -----------------------------------------------------------------------------
# Observability Data Lake Outputs
# -----------------------------------------------------------------------------

output "observability_sns_topic_arn" {
  description = "SNS topic ARN for observability events (requires enable_datalake_observability=true)"
  value       = var.enable_datalake_observability ? aws_sns_topic.observability_events[0].arn : null
}

output "observability_sqs_queue_url" {
  description = "SQS queue URL for observability event buffering"
  value       = var.enable_datalake_observability ? aws_sqs_queue.observability_events[0].url : null
}

output "observability_s3_bucket_name" {
  description = "S3 bucket name for observability data lake"
  value       = var.enable_datalake_observability ? aws_s3_bucket.observability[0].id : null
}

output "observability_s3_bucket_arn" {
  description = "S3 bucket ARN for observability data lake"
  value       = var.enable_datalake_observability ? aws_s3_bucket.observability[0].arn : null
}

output "datalake_collector_lambda_name" {
  description = "Lambda function name for datalake collector"
  value       = var.enable_datalake_observability ? aws_lambda_function.datalake_collector[0].function_name : null
}

output "observability_dlq_url" {
  description = "Dead letter queue URL for failed observability events"
  value       = var.enable_datalake_observability ? aws_sqs_queue.observability_events_dlq[0].url : null
}

output "observability_dashboard_url" {
  description = "CloudWatch dashboard URL for Medical Nudging metrics"
  value       = var.enable_datalake_observability ? "https://${local.region}.console.aws.amazon.com/cloudwatch/home?region=${local.region}#dashboards/dashboard/${var.stack_name}-dashboard" : null
}

# -----------------------------------------------------------------------------
# OTEL Spans Export Outputs
# -----------------------------------------------------------------------------

output "otel_spans_s3_bucket_name" {
  description = "S3 bucket name for OTEL spans export (low PHI risk)"
  value       = var.enable_datalake_observability ? aws_s3_bucket.otel_spans[0].id : null
}

output "otel_spans_s3_bucket_arn" {
  description = "S3 bucket ARN for OTEL spans export"
  value       = var.enable_datalake_observability ? aws_s3_bucket.otel_spans[0].arn : null
}

output "otel_spans_firehose_name" {
  description = "Kinesis Firehose delivery stream name for OTEL spans"
  value       = var.enable_datalake_observability ? aws_kinesis_firehose_delivery_stream.otel_spans[0].name : null
}

# -----------------------------------------------------------------------------
# Runtime Logs Export Outputs
# -----------------------------------------------------------------------------

output "runtime_logs_s3_bucket_name" {
  description = "S3 bucket name for runtime logs export (high PHI risk - restrict access)"
  value       = var.enable_datalake_observability ? aws_s3_bucket.runtime_logs[0].id : null
}

output "runtime_logs_s3_bucket_arn" {
  description = "S3 bucket ARN for runtime logs export"
  value       = var.enable_datalake_observability ? aws_s3_bucket.runtime_logs[0].arn : null
}

output "runtime_logs_firehose_name" {
  description = "Kinesis Firehose delivery stream name for runtime logs"
  value       = var.enable_datalake_observability ? aws_kinesis_firehose_delivery_stream.runtime_logs[0].name : null
}

# -----------------------------------------------------------------------------
# Athena Analytics Outputs
# -----------------------------------------------------------------------------

output "athena_database_name" {
  description = "Glue database name for observability events"
  value       = var.enable_datalake_observability ? aws_glue_catalog_database.observability[0].name : null
}

output "athena_table_name" {
  description = "Glue table name for observability events"
  value       = var.enable_datalake_observability ? aws_glue_catalog_table.observability_events[0].name : null
}

output "athena_workgroup_name" {
  description = "Athena workgroup for observability queries"
  value       = var.enable_datalake_observability ? aws_athena_workgroup.observability[0].name : null
}

output "athena_console_url" {
  description = "Athena console URL for running queries"
  value       = var.enable_datalake_observability ? "https://${local.region}.console.aws.amazon.com/athena/home?region=${local.region}#/query-editor/workgroup/${var.stack_name}-observability" : null
}

# -----------------------------------------------------------------------------
# WebSocket Presigned URL API Outputs
# -----------------------------------------------------------------------------

output "websocket_api_url" {
  description = "WebSocket API URL - POST to get presigned URL (requires websocket_api_enabled=true)"
  value       = var.websocket_api_enabled ? "${aws_api_gateway_stage.websocket[0].invoke_url}/ws-url" : null
}

output "websocket_api_id" {
  description = "API Gateway REST API ID for WebSocket presigned URL API"
  value       = var.websocket_api_enabled ? aws_api_gateway_rest_api.websocket[0].id : null
}

output "api_key_secret_arn" {
  description = "Secrets Manager secret holding the x-api-key for POST /ws-url"
  value       = var.websocket_api_enabled ? aws_secretsmanager_secret.api_key[0].arn : null
}

output "api_key" {
  description = "API key for the WebSocket presigned-URL API (sensitive; prefer reading the secret)"
  value       = var.websocket_api_enabled ? random_password.api_key[0].result : null
  sensitive   = true
}


# -----------------------------------------------------------------------------
# HealthLake Outputs
# -----------------------------------------------------------------------------

output "healthlake_datastore_id" {
  description = "HealthLake FHIR datastore ID (requires healthlake_enabled=true)"
  value       = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_id : null
}

output "healthlake_datastore_endpoint" {
  description = "HealthLake FHIR base URL. Use verbatim as FHIR_API_BASE_URL / HEALTHLAKE_DATASTORE_ENDPOINT — never assemble it by hand."
  value       = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_endpoint : null
}

output "healthlake_datastore_arn" {
  description = "HealthLake FHIR datastore ARN"
  value       = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_arn : null
}

output "healthlake_datastore_status" {
  description = "HealthLake datastore status (CREATING, ACTIVE, DELETING, DELETED)"
  value       = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_status : null
}

output "healthlake_staging_bucket" {
  description = "S3 bucket used to stage import NDJSON (import/) and collect job output (output/)"
  value       = var.healthlake_enabled ? aws_s3_bucket.healthlake_staging[0].id : null
}

output "healthlake_import_role_arn" {
  description = "IAM role ARN HealthLake assumes for bulk import jobs"
  value       = var.healthlake_enabled ? aws_iam_role.healthlake_import[0].arn : null
}

output "healthlake_import_kms_key_arn" {
  description = "KMS key ARN required by StartFHIRImportJob's JobOutputDataConfig"
  value       = var.healthlake_enabled ? aws_kms_key.healthlake_import[0].arn : null
}

output "healthlake_teardown_command" {
  description = "Reminder: a HealthLake datastore bills ~$197/month and cannot be paused"
  value       = var.healthlake_enabled ? "terraform destroy -target=awscc_healthlake_fhir_datastore.fhir -var=\"healthlake_enabled=true\"" : null
}

# -----------------------------------------------------------------------------
# Deployment-quality loop: candidate gate and online evaluation
# -----------------------------------------------------------------------------

output "candidate_gate_project_name" {
  description = "CodeBuild project that runs the frozen regression dataset against the deployed runtime"
  value       = var.candidate_gate_enabled ? aws_codebuild_project.candidate_gate[0].name : null
}

output "candidate_gate_command" {
  description = "Run the candidate-acceptance gate from a workstation against this runtime"
  value       = <<-EOT
    uv run python -m evals.agentcore_gate \
      --runtime-arn ${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn} \
      --config ${var.regression_gate_config} --thresholds ${var.regression_gate_thresholds} \
      ${var.regression_dataset_id != "" ? "--dataset-id ${var.regression_dataset_id} --dataset-version ${var.regression_dataset_version}" : "--dataset-file ${var.regression_dataset_file}"} \
      --output-dir ~/nudge-evaluations/gate --region ${local.region}
  EOT
}

output "online_evaluation_config_id" {
  description = "AgentCore online evaluation configuration id (requires online_evaluation_enabled=true)"
  value       = var.online_evaluation_enabled ? aws_bedrockagentcore_online_evaluation_config.quality_trend[0].online_evaluation_config_id : null
}

output "online_evaluation_results_log_group" {
  description = "CloudWatch log group receiving online evaluation results; scores also publish to the Bedrock-AgentCore/Evaluations metrics namespace"
  value       = var.online_evaluation_enabled ? one(aws_bedrockagentcore_online_evaluation_config.quality_trend[0].output_config[*].cloudwatch_config[0].log_group_name) : null
}

output "online_evaluation_service_name" {
  description = "OpenTelemetry service name the online evaluation filters on (<runtime name>.DEFAULT)"
  value       = local.runtime_service_name
}

output "config_bundle_file" {
  description = "Where terraform apply wrote the bundle id, ARN, and version 1 (requires config_bundle_enabled=true)"
  value       = var.config_bundle_enabled ? "${path.module}/config_bundle.json" : null
}

output "config_bundle_update_command" {
  description = "Publish an edited prompt as the next immutable bundle version for this runtime"
  value       = <<-EOT
    uv run scripts/config_bundle.py update --bundle-id <bundle id> \
      --runtime-arn ${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn} \
      --prompt-file <edited orchestrator.md> --message "<what changed and why>" --region ${local.region}
  EOT
}
