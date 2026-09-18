# ============================================================================
# Medical Nudging AgentCore - IAM Roles and Policies
# ============================================================================

# ============================================================================
# Agent Execution Role - For AgentCore Runtime
# ============================================================================

resource "aws_iam_role" "agent_execution" {
  name = "${var.stack_name}-agent-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AssumeRolePolicy"
      Effect = "Allow"
      Principal = {
        Service = "bedrock-agentcore.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:*"
        }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-agent-execution-role"
    Module = "IAM"
  })
}

# IAM4: Removed BedrockAgentCoreFullAccess managed policy - all required permissions are in inline policy

# Inline policy for agent execution
resource "aws_iam_role_policy" "agent_execution" {
  name = "AgentCoreExecutionPolicy"
  role = aws_iam_role.agent_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # ECR Access
      {
        Sid    = "ECRImageAccess"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability"
        ]
        Resource = aws_ecr_repository.agent_ecr.arn
      },
      # IAM5: ecr:GetAuthorizationToken requires Resource = "*" (AWS API requirement)
      {
        Sid      = "ECRTokenAccess"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogStreams",
          "logs:CreateLogGroup",
          "logs:DescribeLogGroups",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      # X-Ray Tracing - IAM5: X-Ray actions require Resource = "*" (cross-resource service)
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      },
      # CloudWatch Metrics - IAM5: cloudwatch:PutMetricData requires Resource = "*" but scoped by namespace condition
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = ["bedrock-agentcore", "MedicalNudging"]
          }
        }
      },
      # Bedrock Model Invocation - CKV_AWS_355: Scoped to approved foundation models
      # Note: Cross-region inference routes to multiple regions, so use * for region
      # IAM5: Region wildcard required for cross-region inference routing
      {
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          # Foundation models - use * for region to support cross-region inference
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
          "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-*",
          "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
          "arn:aws:bedrock:*::foundation-model/meta.llama*",
          "arn:aws:bedrock:*::foundation-model/us.meta.llama*",
          # Inference profiles (cross-region inference)
          "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/anthropic.claude-*",
          "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/us.anthropic.claude-*",
          "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/amazon.nova-*",
          "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/us.meta.llama*"
        ]
      },
      # Configuration bundles: the runtime reads the prompt version a request names.
      {
        Sid    = "ReadConfigurationBundles"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetConfigurationBundle",
          "bedrock-agentcore:GetConfigurationBundleVersion"
        ]
        Resource = "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:configuration-bundle/*"
      },
      # Workload Access Tokens
      {
        Sid    = "GetAgentAccessToken"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default/workload-identity/*"
        ]
      },
      # S3 - Guidelines Bucket Access
      {
        Sid    = "S3GuidelinesAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.guidelines.arn,
          "${aws_s3_bucket.guidelines.arn}/*"
        ]
      },
      # OpenSearch Serverless - Guidelines Search
      {
        Sid    = "OpenSearchAccess"
        Effect = "Allow"
        Action = [
          "aoss:APIAccessAll"
        ]
        Resource = var.opensearch_enabled ? aws_opensearchserverless_collection.guidelines[0].arn : "*"
      },
      # SNS Publish - Observability Events (conditional)
      {
        Sid    = "SNSObservabilityPublish"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = var.enable_datalake_observability ? aws_sns_topic.observability_events[0].arn : "arn:aws:sns:${local.region}:${local.account_id}:*-observability-events"
      },
      # S3 Write - Observability Traces (conditional)
      {
        Sid    = "S3ObservabilityWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = var.enable_datalake_observability ? "${aws_s3_bucket.observability[0].arn}/traces/*" : "arn:aws:s3:::*-observability-*/traces/*"
      },
      # KMS - For S3 observability bucket encryption (conditional on KMS + observability)
      {
        Sid    = "KMSForS3"
        Effect = "Allow"
        Action = [
          "kms:GenerateDataKey",
          "kms:Decrypt"
        ]
        Resource = var.enable_kms_encryption && var.enable_datalake_observability ? aws_kms_key.s3[0].arn : "arn:aws:kms:${local.region}:${local.account_id}:key/*"
        Condition = var.enable_kms_encryption && var.enable_datalake_observability ? {} : {
          StringEquals = {
            "kms:ViaService" = "s3.${local.region}.amazonaws.com"
          }
        }
      },
      # KMS - For SNS encryption (conditional on KMS + observability)
      {
        Sid    = "KMSForSNS"
        Effect = "Allow"
        Action = [
          "kms:GenerateDataKey",
          "kms:Decrypt"
        ]
        Resource = var.enable_kms_encryption && var.enable_datalake_observability ? aws_kms_key.sns[0].arn : "arn:aws:kms:${local.region}:${local.account_id}:key/*"
        Condition = var.enable_kms_encryption && var.enable_datalake_observability ? {} : {
          StringEquals = {
            "kms:ViaService" = "sns.${local.region}.amazonaws.com"
          }
        }
      },
      # OTEL Trace Query - for trace retrieval from aws/spans log group
      {
        Sid    = "OTELTraceQuery"
        Effect = "Allow"
        Action = [
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:StopQuery"
        ]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:aws/spans:*"
        ]
      }
    ], local.agent_healthlake_statements)
  })
}

# HealthLake read access for the FHIR API data source. The runtime signs
# `query_patient_fhir` requests with this role, so it needs read/search on the
# datastore it is pointed at: the stack's own datastore when `healthlake_enabled`,
# or an existing one named in `fhir_datastore_arn` (used by a fresh evaluation
# stack that reuses an already-imported demo datastore).
locals {
  agent_fhir_datastore_arn = (
    var.fhir_datastore_arn != null && var.fhir_datastore_arn != ""
    ? var.fhir_datastore_arn
    : local.healthlake_datastore_arn
  )
  agent_healthlake_statements = local.agent_fhir_datastore_arn != "" ? [
    {
      Sid    = "HealthLakeFHIRRead"
      Effect = "Allow"
      Action = [
        "healthlake:ReadResource",
        "healthlake:SearchWithGet",
        "healthlake:SearchWithPost",
        "healthlake:GetCapabilities"
      ]
      Resource = local.agent_fhir_datastore_arn
    }
  ] : []
}

# ============================================================================
# CodeBuild Service Role - For Docker Image Building
# ============================================================================

resource "aws_iam_role" "image_build" {
  name = "${var.stack_name}-codebuild-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "codebuild.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      # IAM10: Add SourceAccount condition to prevent confused deputy
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.account_id
        }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-codebuild-role"
    Module = "IAM"
  })
}

# Inline policy for CodeBuild
resource "aws_iam_role_policy" "image_build" {
  name = "CodeBuildPolicy"
  role = aws_iam_role.image_build.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CloudWatch Logs - log groups and log streams
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/codebuild/*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/codebuild/*:*"
        ]
      },
      # ECR Access - CKV_AWS_290: Split GetAuthorizationToken from repository-specific actions
      {
        Sid    = "ECRRepositoryAccess"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = aws_ecr_repository.agent_ecr.arn
      },
      # IAM5: ecr:GetAuthorizationToken requires Resource = "*" (AWS API requirement)
      {
        Sid      = "ECRAuthorizationToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # S3 Source Access (for agent-code)
      {
        Sid    = "S3SourceAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion"
        ]
        Resource = "${aws_s3_bucket.agent_source.arn}/*"
      },
      # CB9: Add s3:GetBucketAcl for CodeBuild compliance
      {
        Sid    = "S3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:GetBucketAcl"
        ]
        Resource = aws_s3_bucket.agent_source.arn
      },
      # KMS permissions for S3 bucket encryption
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey"
        ]
        Resource = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : "*"
      },
      # ECR access for Lambda ingest repository (when opensearch enabled)
      {
        Sid    = "ECRLambdaRepositoryAccess"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = aws_ecr_repository.agent_ecr.arn
      }
    ]
  })
}
