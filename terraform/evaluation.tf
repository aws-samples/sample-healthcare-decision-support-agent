# ============================================================================
# Medical Nudging AgentCore - Deployment-quality loop
# ============================================================================
#
# Two pieces connect the layered evaluation framework (docs/evaluation.md) to
# deployment and production monitoring:
#
# 1. Candidate-acceptance gate (CodeBuild). Runs `evals.agentcore_gate` against
#    the runtime this stack just deployed: the frozen regression dataset, the
#    frozen deterministic evaluator set, the frozen thresholds. A delivery
#    pipeline places it before promotion or traffic routing. `terraform apply`
#    can run it once the runtime exists (`run_candidate_gate_on_apply`), which
#    makes a failing gate fail the apply. This repository does not ship a
#    complete CodePipeline.
#
# 2. Online evaluation (AgentCore). Continuously samples the runtime's
#    completed sessions from CloudWatch and applies broad quality-trend
#    evaluators asynchronously; it adds no request latency. DISABLED BY DEFAULT
#    (`online_evaluation_enabled = false`) because every sampled session costs
#    evaluator model invocations. Scores are trend signals, not evidence of
#    clinical safety, correctness, or effectiveness.
# ============================================================================

locals {
  gate_enabled     = var.candidate_gate_enabled ? 1 : 0
  gate_run_enabled = var.candidate_gate_enabled && var.run_candidate_gate_on_apply ? 1 : 0
  online_enabled   = var.online_evaluation_enabled ? 1 : 0

  runtime_service_name = "${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_name}.DEFAULT"

  # Config names allow letters, digits, and underscores; max 48 characters.
  online_evaluation_config_name = substr(
    replace("${var.stack_name}_${var.agent_name}_quality", "-", "_"), 0, 48
  )

  # The runtime's own log group holds the split-telemetry event records (model and
  # tool payloads) in its otel-rt-logs stream; the shared aws/spans group holds the
  # spans. Under split telemetry (ADOT < 0.18 / UNIFIED_TRACES_DESTINATION_ENABLED
  # unset) the evaluation service must read both to reconstruct a session, so the
  # runtime log group is always included alongside the configured groups. Capped at
  # the service maximum of five.
  runtime_log_group = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_id}-DEFAULT"
  online_evaluation_log_groups_all = distinct(
    concat(var.online_evaluation_log_group_names, [local.runtime_log_group])
  )
  online_evaluation_log_groups = slice(
    local.online_evaluation_log_groups_all, 0, min(5, length(local.online_evaluation_log_groups_all))
  )
}

# ----------------------------------------------------------------------------
# Gate source archive: agent package + evals/ + config/evals + fixtures + buildspec.
# Hashed separately from the agent archive so evaluation edits never rebuild
# the agent image.
# ----------------------------------------------------------------------------

data "external" "gate_source_archive" {
  count = local.gate_enabled

  program = ["python", "${path.module}/scripts/create_agent_archive.py"]

  query = {
    project_root  = abspath("${path.module}/..")
    terraform_dir = abspath(path.module)
    output_path   = abspath("${path.module}/.terraform/gate-code.zip")
    profile       = "gate"
  }
}

resource "aws_s3_object" "gate_source" {
  count = local.gate_enabled

  bucket = aws_s3_bucket.agent_source.id
  key    = "gate-code-${data.external.gate_source_archive[0].result.md5}.zip"
  source = data.external.gate_source_archive[0].result.path
  etag   = data.external.gate_source_archive[0].result.md5

  tags = merge(local.common_tags, {
    Name = "candidate-gate-source"
    MD5  = data.external.gate_source_archive[0].result.md5
  })
}

# ----------------------------------------------------------------------------
# Gate CodeBuild project
# ----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "candidate_gate" {
  count = local.gate_enabled

  name              = "/aws/codebuild/${var.stack_name}-candidate-gate"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-candidate-gate-logs"
    Module = "Evaluation"
  })
}

resource "aws_codebuild_project" "candidate_gate" {
  count = local.gate_enabled

  name          = "${var.stack_name}-candidate-gate"
  description   = "Candidate-acceptance gate: frozen regression dataset against the deployed AgentCore runtime for ${var.stack_name}"
  service_role  = aws_iam_role.candidate_gate[0].arn
  build_timeout = 120

  artifacts {
    type      = "S3"
    location  = aws_s3_bucket.agent_source.id
    path      = "candidate-gate-results"
    name      = "result"
    packaging = "NONE"
    # result.json holds verdicts and check labels only; raw evidence never leaves the build.
    encryption_disabled = false
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = local.region
    }
    environment_variable {
      name  = "AGENT_RUNTIME_ARN"
      value = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn
    }
    environment_variable {
      name  = "GATE_CONFIG"
      value = var.regression_gate_config
    }
    environment_variable {
      name  = "GATE_THRESHOLDS"
      value = var.regression_gate_thresholds
    }
    environment_variable {
      name  = "GATE_DATASET_ID"
      value = var.regression_dataset_id
    }
    environment_variable {
      name  = "GATE_DATASET_VERSION"
      value = var.regression_dataset_version
    }
    environment_variable {
      name  = "GATE_DATASET_FILE"
      value = var.regression_dataset_file
    }
    environment_variable {
      name  = "GATE_EVALUATORS"
      value = join(" ", var.gate_agentcore_evaluators)
    }
    environment_variable {
      name  = "GATE_REPLAY_RECORDS"
      value = ""
    }
    environment_variable {
      name  = "GATE_CONCURRENCY"
      value = tostring(var.gate_concurrency)
    }
    environment_variable {
      name  = "GATE_EVALUATION_DELAY_SECONDS"
      value = "180"
    }
    environment_variable {
      name  = "GATE_BUNDLE_ID"
      value = var.config_bundle_id
    }
    environment_variable {
      name  = "GATE_BUNDLE_VERSION"
      value = var.config_bundle_version
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.agent_source.id}/${aws_s3_object.gate_source[0].key}"
    buildspec = "buildspec.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.candidate_gate[0].name
      stream_name = "gate"
    }
  }

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-candidate-gate"
    Module = "Evaluation"
  })
}

resource "aws_iam_role" "candidate_gate" {
  count = local.gate_enabled

  name = "${var.stack_name}-candidate-gate-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-candidate-gate-role"
    Module = "Evaluation"
  })
}

resource "aws_iam_role_policy" "candidate_gate" {
  count = local.gate_enabled

  name = "CandidateGatePolicy"
  role = aws_iam_role.candidate_gate[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BuildLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          aws_cloudwatch_log_group.candidate_gate[0].arn,
          "${aws_cloudwatch_log_group.candidate_gate[0].arn}:*"
        ]
      },
      {
        Sid      = "GateSourceAndResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${aws_s3_bucket.agent_source.arn}/*"
      },
      {
        Sid      = "GateSourceBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketAcl"]
        Resource = aws_s3_bucket.agent_source.arn
      },
      {
        Sid      = "SourceBucketKms"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        Resource = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : "*"
      },
      {
        Sid    = "InvokeCandidateRuntime"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime"
        ]
        Resource = [
          aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn}/*"
        ]
      },
      {
        # The gate resolves the pinned prompt version to record it next to the runtime version.
        Sid    = "ReadConfigurationBundles"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetConfigurationBundle",
          "bedrock-agentcore:GetConfigurationBundleVersion"
        ]
        Resource = "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:configuration-bundle/*"
      },
      {
        # Evaluate is an account-level API; datasets are read at a pinned version.
        Sid    = "AgentCoreEvaluations"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:Evaluate",
          "bedrock-agentcore:GetDataset",
          "bedrock-agentcore:ListDatasetVersions",
          "bedrock-agentcore:GetEvaluator",
          "bedrock-agentcore:ListEvaluators"
        ]
        Resource = "*"
      },
      {
        # The on-demand runner collects the candidate's spans from CloudWatch.
        Sid    = "CollectRuntimeSpans"
        Effect = "Allow"
        Action = ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
        ]
      },
      {
        Sid      = "DescribeLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      }
    ]
  })
}

# Optional: run the gate as part of `terraform apply`, after the runtime exists.
# A failing gate fails the apply. Reruns when the runtime version or gate source changes.
resource "null_resource" "candidate_gate" {
  count = local.gate_run_enabled

  triggers = {
    runtime_version = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_version
    gate_source_md5 = data.external.gate_source_archive[0].result.md5
    dataset         = "${var.regression_dataset_id}:${var.regression_dataset_version}:${var.regression_dataset_file}"
  }

  provisioner "local-exec" {
    command = "python ${path.module}/scripts/wait_for_codebuild.py --project-name ${aws_codebuild_project.candidate_gate[0].name} --region ${local.region} --timeout 7200${var.aws_profile != null && var.aws_profile != "" ? " --profile ${var.aws_profile}" : ""}"
  }

  depends_on = [
    aws_bedrockagentcore_agent_runtime.medical_nudging,
    aws_cloudwatch_log_delivery.runtime_traces,
    aws_iam_role_policy.candidate_gate,
    aws_s3_object.gate_source
  ]
}

# ----------------------------------------------------------------------------
# Online evaluation (opt-in)
# ----------------------------------------------------------------------------

resource "aws_iam_role" "online_evaluation" {
  count = local.online_enabled

  name = "${var.stack_name}-online-evaluation-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TrustPolicyStatement"
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount"   = local.account_id
          "aws:ResourceAccount" = local.account_id
        }
        ArnLike = {
          "aws:SourceArn" = [
            "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:evaluator/*",
            "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:online-evaluation-config/*"
          ]
        }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-online-evaluation-role"
    Module = "Evaluation"
  })
}

resource "aws_iam_role_policy" "online_evaluation" {
  count = local.online_enabled

  name = "OnlineEvaluationPolicy"
  role = aws_iam_role.online_evaluation[0].id

  # The service reads sampled sessions, writes results, indexes the span log
  # group, and invokes evaluator models. Statements follow the AgentCore
  # Evaluations prerequisites; the read statement needs Resource "*".
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # DescribeLogGroups and GetQueryResults take no resource ARN; StartQuery is
        # scoped to the span and runtime log groups the service samples from.
        Sid      = "CloudWatchLogDescribeStatement"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups", "logs:GetQueryResults"]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogQueryStatement"
        Effect = "Allow"
        Action = ["logs:StartQuery"]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/evaluations/*"
        ]
      },
      {
        Sid      = "CloudWatchLogWriteStatement"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/evaluations/*"
      },
      {
        Sid    = "CloudWatchIndexPolicyStatement"
        Effect = "Allow"
        Action = ["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
        ]
      },
      {
        Sid    = "BedrockInvokeStatement"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:${local.region}::foundation-model/*",
          "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/*"
        ]
      }
    ]
  })
}

resource "aws_bedrockagentcore_online_evaluation_config" "quality_trend" {
  count = local.online_enabled

  online_evaluation_config_name = local.online_evaluation_config_name
  description                   = "Sampled quality-trend evaluation of ${local.runtime_service_name}; trend signal, not clinical evidence"
  enable_on_create              = true
  evaluation_execution_role_arn = aws_iam_role.online_evaluation[0].arn

  data_source_config {
    cloudwatch_logs {
      log_group_names = local.online_evaluation_log_groups
      service_names   = [local.runtime_service_name]
    }
  }

  dynamic "evaluator" {
    for_each = var.online_evaluation_evaluators
    content {
      evaluator_id = evaluator.value
    }
  }

  rule {
    sampling_config {
      sampling_percentage = var.online_evaluation_sampling_percentage
    }
    session_config {
      session_timeout_minutes = var.online_evaluation_session_timeout_minutes
    }
  }

  tags = merge(local.common_tags, {
    Name   = local.online_evaluation_config_name
    Module = "Evaluation"
  })

  depends_on = [
    aws_iam_role_policy.online_evaluation,
    aws_cloudwatch_log_delivery.runtime_traces,
    null_resource.enable_transaction_search
  ]
}

# -----------------------------------------------------------------------------
# Configuration bundle: version 1 of the system prompt, opt-in
# -----------------------------------------------------------------------------
# The provider has no configuration-bundle resource, so creation goes through
# scripts/config_bundle.py. It runs once per runtime (re-created runtimes get a new
# bundle because the component key is the runtime ARN) and writes the ids to
# terraform/config_bundle.json (gitignored). Later prompt versions are published with
# `scripts/config_bundle.py update`, never by re-running apply.

locals {
  config_bundle_name = var.config_bundle_name != "" ? var.config_bundle_name : replace("${var.stack_name}_prompt", "-", "_")
}

resource "null_resource" "config_bundle" {
  count = var.config_bundle_enabled ? 1 : 0

  triggers = {
    runtime_arn = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn
    bundle_name = local.config_bundle_name
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    command     = <<-EOT
      uv run scripts/config_bundle.py --region ${local.region} ${var.aws_profile != "" ? "--profile ${var.aws_profile}" : ""} create \
        --runtime-arn ${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn} \
        --name ${local.config_bundle_name} \
        --message "Initial system prompt from prompts/orchestrator.md (terraform apply)" \
        --output terraform/config_bundle.json
    EOT
  }
}
