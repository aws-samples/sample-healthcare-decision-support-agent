# ============================================================================
# Medical Nudging AgentCore - Input Variables
# ============================================================================

# -----------------------------------------------------------------------------
# Required Variables
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-\\d{1}$", var.aws_region))
    error_message = "Must be a valid AWS region (e.g., us-east-1, eu-west-1)"
  }
}

# -----------------------------------------------------------------------------
# Agent Configuration
# -----------------------------------------------------------------------------

variable "agent_name" {
  description = "Name for the agent runtime"
  type        = string
  default     = "MedicalNudging"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.agent_name))
    error_message = "Agent name must start with a letter, max 48 characters, alphanumeric and underscores only."
  }
}

variable "stack_name" {
  description = "Stack name for resource naming"
  type        = string
  default     = "medical-nudging"

  validation {
    condition     = can(regex("^[a-z0-9-]{1,32}$", var.stack_name))
    error_message = "Stack name must be lowercase alphanumeric with hyphens, max 32 characters."
  }
}

variable "description" {
  description = "Description of the agent runtime"
  type        = string
  default     = "Medical Nudging clinical decision support agent with OpenSearch Serverless backend"
}

variable "network_mode" {
  description = "Network mode for AgentCore resources"
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "PRIVATE"], var.network_mode)
    error_message = "Network mode must be either PUBLIC or PRIVATE."
  }
}

# -----------------------------------------------------------------------------
# Container Configuration
# -----------------------------------------------------------------------------

variable "ecr_repository_name" {
  description = "Name of the ECR repository"
  type        = string
  default     = "medical-nudging-agent"
}

variable "image_tag" {
  description = "Docker image tag"
  type        = string
  default     = "latest"
}

# -----------------------------------------------------------------------------
# Environment Configuration
# -----------------------------------------------------------------------------

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "aws_profile" {
  description = "AWS CLI profile to use"
  type        = string
  default     = null
}

# -----------------------------------------------------------------------------
# Application Environment Variables
# -----------------------------------------------------------------------------

variable "auth_enabled" {
  description = "Enable JWT authentication (set to false for development)"
  type        = bool
  default     = false
}

variable "log_level" {
  description = "Application log level"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "Log level must be DEBUG, INFO, WARNING, or ERROR."
  }
}

variable "max_nudges" {
  description = "Maximum number of nudges to generate per request"
  type        = number
  default     = 5

  validation {
    condition     = var.max_nudges > 0 && var.max_nudges <= 20
    error_message = "Max nudges must be between 1 and 20."
  }
}

variable "model_id" {
  description = "Bedrock model ID for Claude"
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "environment_variables" {
  description = "Additional environment variables for the agent runtime"
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------------------
# Guidelines Storage
# -----------------------------------------------------------------------------

variable "guidelines_bucket_name" {
  description = "Name for the guidelines S3 bucket (suffixed with the account ID). Defaults to <stack_name>-guidelines so a second stack_name never collides."
  type        = string
  default     = null
}

# -----------------------------------------------------------------------------
# OpenSearch Serverless Configuration
# -----------------------------------------------------------------------------

variable "opensearch_enabled" {
  description = "Enable OpenSearch Serverless for guidelines search"
  type        = bool
  default     = true
}

variable "opensearch_index_name" {
  description = "OpenSearch index name for guidelines"
  type        = string
  default     = "guidelines"
}

variable "opensearch_standby_replicas" {
  description = "Standby replicas for OpenSearch collection (ENABLED or DISABLED)"
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.opensearch_standby_replicas)
    error_message = "Must be ENABLED or DISABLED."
  }
}

variable "search_backend" {
  description = "Default search backend (opensearch, ripgrep, auto)"
  type        = string
  default     = "opensearch"

  validation {
    condition     = contains(["opensearch", "ripgrep", "auto"], var.search_backend)
    error_message = "Must be opensearch, ripgrep, or auto."
  }
}

# -----------------------------------------------------------------------------
# AWS HealthLake FHIR Datastore
# -----------------------------------------------------------------------------

variable "healthlake_enabled" {
  description = <<-EOT
    Enable the AWS HealthLake FHIR datastore, its import staging bucket, KMS key,
    and import role. COST WARNING: a datastore bills from create to delete with
    no pause — roughly $0.27/hour, $6.48/day, $197/month while idle. Leave this
    false unless you are actively running the FHIR API path, and destroy the
    datastore when finished. Local file mode (CCDA/FHIR JSON) stays free.
  EOT
  type        = bool
  default     = false
}

variable "healthlake_datastore_name" {
  description = "Datastore name suffix (prefixed with stack_name). Changing this REPLACES the datastore and destroys imported data."
  type        = string
  default     = "fhir"

  validation {
    condition     = can(regex("^[a-zA-Z0-9-]{1,32}$", var.healthlake_datastore_name))
    error_message = "Datastore name suffix must be alphanumeric with hyphens, max 32 characters."
  }
}

variable "healthlake_staging_retention_days" {
  description = "Days before staged import NDJSON under import/ is expired from the staging bucket"
  type        = number
  default     = 30

  validation {
    condition     = var.healthlake_staging_retention_days >= 1 && var.healthlake_staging_retention_days <= 365
    error_message = "Retention days must be between 1 and 365."
  }
}

variable "healthlake_budget_alarm_enabled" {
  description = "Create an AWS Budgets budget scoped to HealthLake spend. Strongly recommended — a forgotten datastore is the main financial risk of this sample."
  type        = bool
  default     = true
}

variable "healthlake_monthly_budget_usd" {
  description = "Monthly HealthLake budget in USD. The default is just above one month of an idle datastore, so any second datastore trips it."
  type        = number
  default     = 250

  validation {
    condition     = var.healthlake_monthly_budget_usd > 0
    error_message = "Budget must be positive."
  }
}

variable "healthlake_budget_alert_emails" {
  description = "Email addresses notified at 80% forecasted and 100% actual HealthLake spend. Empty means the budget is created without notifications (visible in the console only)."
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# CloudWatch Observability Configuration
# -----------------------------------------------------------------------------

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch logs (default one year; checkov CKV_AWS_338)"
  type        = number
  default     = 365

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "Must be a valid CloudWatch Logs retention period."
  }
}

variable "enable_cloudwatch_dashboard" {
  description = "Enable CloudWatch dashboard for agent monitoring"
  type        = bool
  default     = true
}

variable "enable_cloudwatch_alarms" {
  description = "Enable CloudWatch alarms for agent monitoring"
  type        = bool
  default     = false
}

variable "latency_alarm_threshold_ms" {
  description = "Threshold in milliseconds for high latency alarm"
  type        = number
  default     = 60000

  validation {
    condition     = var.latency_alarm_threshold_ms > 0
    error_message = "Latency threshold must be positive."
  }
}

variable "error_alarm_threshold" {
  description = "Threshold for error count alarm (errors per 5 minutes)"
  type        = number
  default     = 5

  validation {
    condition     = var.error_alarm_threshold > 0
    error_message = "Error threshold must be positive."
  }
}

# -----------------------------------------------------------------------------
# Security Configuration
# -----------------------------------------------------------------------------

variable "enable_kms_encryption" {
  description = "Enable KMS encryption for all resources (S3, ECR, SNS, CloudWatch, Lambda). Adds ~$5/month cost but improves security posture."
  type        = bool
  default     = true
}

variable "opensearch_allow_public" {
  description = "Allow public access to OpenSearch Serverless collection. Set to false for production and use VPC endpoints instead."
  type        = bool
  default     = true # Default true for backwards compatibility during pilot
}

variable "vpc_id" {
  description = "VPC ID for private OpenSearch access (required when opensearch_allow_public=false)"
  type        = string
  default     = null
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for VPC endpoints (required when opensearch_allow_public=false)"
  type        = list(string)
  default     = []
}

variable "enable_log_data_protection" {
  description = "Enable CloudWatch Logs data protection for PHI masking. Note: Creates data identifiers for detecting and masking PHI in logs."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# WebSocket API Key
# -----------------------------------------------------------------------------

variable "api_key_secret_name" {
  description = "Secrets Manager secret name for the WebSocket API key (x-api-key on POST /ws-url)"
  type        = string
  default     = "medical-nudging/api-key"

  validation {
    condition     = can(regex("^[a-zA-Z0-9/_+=.@-]+$", var.api_key_secret_name))
    error_message = "Secret name must contain only alphanumeric characters and /_+=.@-"
  }
}

# -----------------------------------------------------------------------------
# Observability Data Lake Configuration
# -----------------------------------------------------------------------------

variable "enable_datalake_observability" {
  description = "Enable SNS + SQS + S3 data lake for observability events. Creates SNS topic for event emission, SQS queue for buffering, S3 bucket for storage, and Lambda collector."
  type        = bool
  default     = true
}

variable "datalake_event_retention_days" {
  description = "Number of days to retain events in S3 before transitioning to Glacier"
  type        = number
  default     = 90

  validation {
    condition     = var.datalake_event_retention_days >= 30 && var.datalake_event_retention_days <= 365
    error_message = "Retention days must be between 30 and 365."
  }
}

variable "datalake_runtime_logs_retention_days" {
  description = "Number of days to retain runtime logs in S3 before transitioning to Glacier. Runtime logs have higher volume than events, so shorter retention may be preferred."
  type        = number
  default     = 30

  validation {
    condition     = var.datalake_runtime_logs_retention_days >= 7 && var.datalake_runtime_logs_retention_days <= 365
    error_message = "Retention days must be between 7 and 365."
  }
}

# -----------------------------------------------------------------------------
# FHIR API data source on the deployed runtime
# -----------------------------------------------------------------------------

variable "fhir_datastore_arn" {
  description = <<-EOT
    ARN of an existing HealthLake datastore the deployed runtime may read when
    this stack does not create its own (healthlake_enabled = false). Grants the
    runtime role healthlake:ReadResource/SearchWithGet/SearchWithPost/GetCapabilities
    on that datastore. Point the runtime at it with environment_variables
    (FHIR_API_ENABLED, HEALTHLAKE_DATASTORE_ENDPOINT, FHIR_MAX_PAGES).
  EOT
  type        = string
  default     = null
}

# -----------------------------------------------------------------------------
# Candidate-acceptance gate (CI/CD regression dataset)
# -----------------------------------------------------------------------------

variable "candidate_gate_enabled" {
  description = "Create the CodeBuild project that runs the frozen regression dataset against the deployed runtime (no cost until a build runs)."
  type        = bool
  default     = true
}

variable "run_candidate_gate_on_apply" {
  description = "Run the candidate-acceptance gate during terraform apply once the runtime exists. A failing gate fails the apply. Each run invokes the generator once per scenario."
  type        = bool
  default     = false
}

variable "regression_dataset_id" {
  description = "AgentCore Dataset Management id of the published regression dataset (evals.agentcore_dataset publish). Empty falls back to regression_dataset_file."
  type        = string
  default     = ""
}

variable "regression_dataset_version" {
  description = "Immutable published version number the gate pins (required with regression_dataset_id)."
  type        = string
  default     = ""

  validation {
    condition     = var.regression_dataset_version == "" || can(regex("^[0-9]+$", var.regression_dataset_version))
    error_message = "Pin a published dataset version number, never the Draft."
  }
}

variable "regression_dataset_file" {
  description = "Checked-in predefined AgentCore dataset used when no published dataset id is set."
  type        = string
  default     = "config/evals/regression_dataset_v1.json"
}

variable "regression_gate_config" {
  description = "Arm config the regression dataset was frozen against (model, agent, generator_config)."
  type        = string
  default     = "config/evals/blog_opus5.json"
}

variable "regression_gate_thresholds" {
  description = "Frozen thresholds JSON applied by the gate."
  type        = string
  default     = "config/evals/regression_thresholds_v1.json"
}

variable "gate_agentcore_evaluators" {
  description = "AgentCore evaluator ids the gate applies to the collected spans. Recorded as trend signals; only thresholds in the thresholds file gate. GoalSuccessRate over the dataset assertions is the worked example; add others as clinician feedback shows what is worth watching."
  type        = list(string)
  default     = ["Builtin.GoalSuccessRate"]
}

variable "gate_concurrency" {
  description = "Scenarios the gate invokes in parallel."
  type        = number
  default     = 2

  validation {
    condition     = var.gate_concurrency >= 1 && var.gate_concurrency <= 8
    error_message = "Gate concurrency must be between 1 and 8."
  }
}

# -----------------------------------------------------------------------------
# Online evaluation (AgentCore, sampled production traffic)
# -----------------------------------------------------------------------------

variable "online_evaluation_enabled" {
  description = <<-EOT
    Create an AgentCore online evaluation configuration that samples the
    runtime's completed sessions and applies quality-trend evaluators. OFF by
    default: every sampled session costs evaluator model invocations. Scores are
    monitoring signals, not evidence of clinical safety or correctness.
  EOT
  type        = bool
  default     = false
}

variable "online_evaluation_sampling_percentage" {
  description = "Percentage of sessions evaluated (0.01-100). Start near 1 for real traffic and tune for volume, cost, and coverage; use high values only for a bounded demonstration."
  type        = number
  default     = 1.0

  validation {
    condition     = var.online_evaluation_sampling_percentage >= 0.01 && var.online_evaluation_sampling_percentage <= 100
    error_message = "Sampling percentage must be between 0.01 and 100."
  }
}

variable "online_evaluation_evaluators" {
  description = "Evaluator ids (built-in or custom, max 10). GoalSuccessRate is the worked example; the clinician feedback cycle decides which further evaluators are worth running live. Add a custom evidence-support evaluator only after it passes the controlled-corruption check."
  type        = list(string)
  default     = ["Builtin.GoalSuccessRate"]

  validation {
    condition     = length(var.online_evaluation_evaluators) >= 1 && length(var.online_evaluation_evaluators) <= 10
    error_message = "Choose between 1 and 10 evaluators."
  }
}

variable "online_evaluation_session_timeout_minutes" {
  description = "Idle minutes after the last span before a session counts as complete (1-60). Match the agent's typical request duration."
  type        = number
  default     = 15

  validation {
    condition     = var.online_evaluation_session_timeout_minutes >= 1 && var.online_evaluation_session_timeout_minutes <= 60
    error_message = "Session timeout must be between 1 and 60 minutes."
  }
}

variable "online_evaluation_log_group_names" {
  description = <<-EOT
    CloudWatch log groups the online evaluation reads (max 5). The runtime's own
    log group is always added to this list: under split telemetry (the pinned ADOT
    distro) spans go to the shared aws/spans group and the model/tool payload event
    records go to the runtime log group's otel-rt-logs stream, and the service must
    read both to reconstruct a session. Keep the shared aws/spans group here; on the
    unified span destination (ADOT >= 0.18 with UNIFIED_TRACES_DESTINATION_ENABLED)
    the runtime log group alone suffices.
  EOT
  type        = list(string)
  default     = ["aws/spans"]

  validation {
    condition     = length(var.online_evaluation_log_group_names) >= 1 && length(var.online_evaluation_log_group_names) <= 5
    error_message = "Name between 1 and 5 log groups."
  }
}

# -----------------------------------------------------------------------------
# Configuration bundles (versioned system prompt, applied per request)
# -----------------------------------------------------------------------------

variable "config_bundle_enabled" {
  description = "Create an AgentCore configuration bundle holding the runtime's base system prompt (prompts/orchestrator.md) as version 1 during apply. Off by default: the repository prompt applies until a request names a bundle version."
  type        = bool
  default     = false
}

variable "config_bundle_name" {
  description = "Bundle name (letters, digits, underscores). Default: <stack_name>_prompt."
  type        = string
  default     = ""

  validation {
    condition     = var.config_bundle_name == "" || can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,99}$", var.config_bundle_name))
    error_message = "Bundle names are letters, digits, and underscores, starting with a letter."
  }
}

variable "config_bundle_id" {
  description = "Bundle the candidate gate pins (from scripts/config_bundle.py create). Empty: the gate runs the repository prompt."
  type        = string
  default     = ""
}

variable "config_bundle_version" {
  description = "Immutable bundle version id the candidate gate pins and records (required with config_bundle_id)."
  type        = string
  default     = ""

  validation {
    condition     = (var.config_bundle_id == "") == (var.config_bundle_version == "")
    error_message = "Set config_bundle_id and config_bundle_version together."
  }
}
