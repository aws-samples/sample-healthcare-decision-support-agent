# ============================================================================
# Medical Nudging - WebSocket Presigned URL API
# ============================================================================
#
# This configuration creates an API Gateway REST API that generates presigned
# WebSocket URLs for direct AgentCore streaming connections.
#
# Architecture:
#   Client → POST /ws-url (x-api-key) → API GW + WAF → Lambda Authorizer
#          → ws_url_generator Lambda → AgentCoreRuntimeClient.generate_presigned_url()
#          ← {ws_url: "wss://...", session_id, expires_in}
#
#   Client → wss://bedrock-agentcore.../ws?X-Amz-Signature=... (direct to AgentCore)
#
# This is the only client path into the agent: there is no public HTTP proxy.
# Enabled by default (websocket_api_enabled = true).
#
# Resource naming aligned with scripts/deploy_agentcore.sh conventions.
# ============================================================================

# -----------------------------------------------------------------------------
# API Key + Secrets Manager Secret
# -----------------------------------------------------------------------------

# Random API key presented as x-api-key on POST /ws-url and checked by the
# Lambda authorizer. Stored in Secrets Manager; read it from there, never from
# terraform output in shared logs.
resource "random_password" "api_key" {
  count = var.websocket_api_enabled ? 1 : 0

  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "api_key" {
  count = var.websocket_api_enabled ? 1 : 0

  name        = var.api_key_secret_name
  description = "API key for the Medical Nudging WebSocket presigned-URL API"
  kms_key_id  = var.enable_kms_encryption ? aws_kms_key.lambda[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-api-key-secret"
  })
}

resource "aws_secretsmanager_secret_version" "api_key" {
  count = var.websocket_api_enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.api_key[0].id
  secret_string = jsonencode({
    api_key = random_password.api_key[0].result
  })
}

# -----------------------------------------------------------------------------
# Lambda Layer for bedrock-agentcore SDK
# -----------------------------------------------------------------------------

# Build the SDK layer (bedrock-agentcore is not in the default Lambda runtime)
resource "null_resource" "agentcore_sdk_layer_build" {
  count = var.websocket_api_enabled ? 1 : 0

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      LAYER_DIR="${path.module}/../.build/agentcore_layer"
      REQS="${path.module}/../lambda/ws_url_generator/requirements.txt"
      rm -rf "$LAYER_DIR"
      mkdir -p "$LAYER_DIR/python"
      # Cross-install arm64 / Python 3.12 wheels; uv works from any host Python,
      # the pip fallback needs a host pip that can resolve bedrock-agentcore (>= 3.10).
      if command -v uv > /dev/null 2>&1; then
        uv pip install -r "$REQS" --target "$LAYER_DIR/python" \
          --python-platform aarch64-manylinux2014 --python-version 3.12 \
          --only-binary :all: --quiet
      else
        python3 -m pip install -r "$REQS" -t "$LAYER_DIR/python" \
          --platform manylinux2014_aarch64 --only-binary :all: \
          --python-version 3.12 --quiet
      fi
      cd "$LAYER_DIR" && zip -r ../agentcore_sdk_layer.zip python/ -q
    EOT
  }

  triggers = {
    requirements = filemd5("${path.module}/../lambda/ws_url_generator/requirements.txt")
  }
}

resource "aws_lambda_layer_version" "agentcore_sdk" {
  count = var.websocket_api_enabled ? 1 : 0

  filename                 = "${path.module}/../.build/agentcore_sdk_layer.zip"
  layer_name               = "${var.stack_name}-agentcore-sdk"
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["arm64"]
  description              = "bedrock-agentcore SDK for presigned URL generation"

  depends_on = [null_resource.agentcore_sdk_layer_build]
}

# -----------------------------------------------------------------------------
# Lambda Code Archives
# -----------------------------------------------------------------------------

data "archive_file" "ws_url_generator" {
  count       = var.websocket_api_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../lambda/ws_url_generator"
  output_path = "${path.module}/../.build/lambda_ws_url_generator.zip"
  excludes    = ["__pycache__", "*.pyc", "requirements.txt"]
}

data "archive_file" "api_authorizer" {
  count       = var.websocket_api_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../lambda/api_authorizer"
  output_path = "${path.module}/../.build/ws_api_authorizer.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

# -----------------------------------------------------------------------------
# IAM Role for WebSocket Lambda Functions
# -----------------------------------------------------------------------------

resource "aws_iam_role" "websocket_lambda" {
  count = var.websocket_api_enabled ? 1 : 0

  name = "${var.stack_name}-websocket-lambda-role"

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
    Name = "${var.stack_name}-websocket-lambda-role"
  })
}

resource "aws_iam_role_policy" "websocket_lambda" {
  count = var.websocket_api_enabled ? 1 : 0

  name = "WebSocketLambdaPolicy"
  role = aws_iam_role.websocket_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        # CloudWatch Logs
        {
          Sid    = "CloudWatchLogs"
          Effect = "Allow"
          Action = [
            "logs:CreateLogStream",
            "logs:PutLogEvents"
          ]
          Resource = [
            "${aws_cloudwatch_log_group.ws_url_generator[0].arn}:*",
            "${aws_cloudwatch_log_group.api_authorizer[0].arn}:*"
          ]
        },
        # CloudWatch Metrics
        {
          Sid      = "CloudWatchMetrics"
          Effect   = "Allow"
          Action   = ["cloudwatch:PutMetricData"]
          Resource = "*"
          Condition = {
            StringEquals = {
              "cloudwatch:namespace" = "MedicalNudging"
            }
          }
        },
        # Bedrock AgentCore - Invoke + WebSocket Stream
        {
          Sid    = "AgentCoreInvokeAndWebSocket"
          Effect = "Allow"
          Action = [
            "bedrock-agentcore:InvokeAgentRuntime",
            "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
            "bedrock-agentcore-runtime:InvokeAgentRuntime"
          ]
          Resource = [
            aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn,
            "${aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn}/*"
          ]
        },
        # Secrets Manager - Read API key
        {
          Sid      = "SecretsManagerReadAPIKey"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = aws_secretsmanager_secret.api_key[0].arn
        },
        # KMS - Decrypt Lambda environment variables
        {
          Sid    = "KMSDecrypt"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey"
          ]
          Resource = var.enable_kms_encryption ? aws_kms_key.lambda[0].arn : "*"
        }
      ],
      # Conditional: SNS publish for observability
      var.enable_datalake_observability ? [
        {
          Sid      = "SNSObservabilityPublish"
          Effect   = "Allow"
          Action   = ["sns:Publish"]
          Resource = aws_sns_topic.observability_events[0].arn
        }
      ] : []
    )
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Log Groups
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "ws_url_generator" {
  count = var.websocket_api_enabled ? 1 : 0

  name              = "/aws/lambda/${var.stack_name}-ws-url-generator"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-ws-url-generator-logs"
  })
}

resource "aws_cloudwatch_log_group" "api_authorizer" {
  count = var.websocket_api_enabled ? 1 : 0

  name              = "/aws/lambda/${var.stack_name}-api-authorizer"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-api-authorizer-logs"
  })
}

# -----------------------------------------------------------------------------
# Lambda Functions
# -----------------------------------------------------------------------------

resource "aws_lambda_function" "ws_url_generator" {
  count = var.websocket_api_enabled ? 1 : 0

  function_name = "${var.stack_name}-ws-url-generator"
  description   = "Generate presigned WebSocket URLs for AgentCore streaming"

  filename         = data.archive_file.ws_url_generator[0].output_path
  source_code_hash = data.archive_file.ws_url_generator[0].output_base64sha256

  role    = aws_iam_role.websocket_lambda[0].arn
  handler = "handler.handler"
  runtime = "python3.12"

  timeout       = 30
  memory_size   = 256
  architectures = ["arm64"]

  layers = [aws_lambda_layer_version.agentcore_sdk[0].arn]

  kms_key_arn = var.enable_kms_encryption ? aws_kms_key.lambda[0].arn : null

  environment {
    variables = {
      AGENT_RUNTIME_ARN   = aws_bedrockagentcore_agent_runtime.medical_nudging.agent_runtime_arn
      API_KEY_SECRET_NAME = var.api_key_secret_name
      URL_EXPIRY_SECONDS  = tostring(var.ws_url_expiry_seconds)
      LOG_LEVEL           = var.log_level
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.ws_url_generator,
    aws_iam_role_policy.websocket_lambda,
    aws_lambda_layer_version.agentcore_sdk,
    aws_secretsmanager_secret_version.api_key
  ]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-ws-url-generator"
  })
}

resource "aws_lambda_function" "api_authorizer" {
  count = var.websocket_api_enabled ? 1 : 0

  function_name = "${var.stack_name}-api-authorizer"
  description   = "API key Lambda Authorizer for WebSocket API Gateway"

  filename         = data.archive_file.api_authorizer[0].output_path
  source_code_hash = data.archive_file.api_authorizer[0].output_base64sha256

  role    = aws_iam_role.websocket_lambda[0].arn
  handler = "handler.handler"
  runtime = "python3.12"

  timeout       = 10
  memory_size   = 128
  architectures = ["arm64"]

  kms_key_arn = var.enable_kms_encryption ? aws_kms_key.lambda[0].arn : null

  environment {
    variables = {
      API_KEY_SECRET_NAME = var.api_key_secret_name
      LOG_LEVEL           = var.log_level
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.api_authorizer,
    aws_iam_role_policy.websocket_lambda,
    aws_secretsmanager_secret_version.api_key
  ]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-api-authorizer"
  })
}

# -----------------------------------------------------------------------------
# API Gateway REST API
# -----------------------------------------------------------------------------

resource "aws_api_gateway_rest_api" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  name        = "${var.stack_name}-websocket-api"
  description = "WebSocket presigned URL API for Medical Nudging streaming"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-websocket-api"
  })
}

# /ws-url resource
resource "aws_api_gateway_resource" "ws_url" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id
  parent_id   = aws_api_gateway_rest_api.websocket[0].root_resource_id
  path_part   = "ws-url"
}

# Lambda Authorizer
resource "aws_api_gateway_authorizer" "api_key" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id                      = aws_api_gateway_rest_api.websocket[0].id
  name                             = "${var.stack_name}-api-key-authorizer"
  type                             = "REQUEST"
  authorizer_uri                   = aws_lambda_function.api_authorizer[0].invoke_arn
  authorizer_result_ttl_in_seconds = 300
  identity_source                  = "method.request.header.x-api-key"
}

# Permission for API Gateway to invoke authorizer Lambda
resource "aws_lambda_permission" "api_authorizer" {
  count = var.websocket_api_enabled ? 1 : 0

  statement_id  = "AllowAPIGatewayInvokeAuthorizer"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_authorizer[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.websocket[0].execution_arn}/*"
}

# POST /ws-url method
resource "aws_api_gateway_method" "ws_url_post" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id   = aws_api_gateway_rest_api.websocket[0].id
  resource_id   = aws_api_gateway_resource.ws_url[0].id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.api_key[0].id
}

# Lambda proxy integration for POST /ws-url
resource "aws_api_gateway_integration" "ws_url_post" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id             = aws_api_gateway_rest_api.websocket[0].id
  resource_id             = aws_api_gateway_resource.ws_url[0].id
  http_method             = aws_api_gateway_method.ws_url_post[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.ws_url_generator[0].invoke_arn
}

# Permission for API Gateway to invoke ws_url_generator Lambda
resource "aws_lambda_permission" "ws_url_generator" {
  count = var.websocket_api_enabled ? 1 : 0

  statement_id  = "AllowAPIGatewayInvokeWSUrlGenerator"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ws_url_generator[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.websocket[0].execution_arn}/*"
}

# OPTIONS /ws-url for CORS preflight
resource "aws_api_gateway_method" "ws_url_options" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id   = aws_api_gateway_rest_api.websocket[0].id
  resource_id   = aws_api_gateway_resource.ws_url[0].id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "ws_url_options" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id
  resource_id = aws_api_gateway_resource.ws_url[0].id
  http_method = aws_api_gateway_method.ws_url_options[0].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = jsonencode({ statusCode = 200 })
  }
}

resource "aws_api_gateway_method_response" "ws_url_options" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id
  resource_id = aws_api_gateway_resource.ws_url[0].id
  http_method = aws_api_gateway_method.ws_url_options[0].http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "ws_url_options" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id
  resource_id = aws_api_gateway_resource.ws_url[0].id
  http_method = aws_api_gateway_method.ws_url_options[0].http_method
  status_code = aws_api_gateway_method_response.ws_url_options[0].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Api-Key'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }

  depends_on = [aws_api_gateway_integration.ws_url_options]
}

# -----------------------------------------------------------------------------
# API Gateway Deployment and Stage
# -----------------------------------------------------------------------------

resource "aws_api_gateway_deployment" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.ws_url[0].id,
      aws_api_gateway_method.ws_url_post[0].id,
      aws_api_gateway_integration.ws_url_post[0].id,
      aws_api_gateway_authorizer.api_key[0].id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.ws_url_post,
    aws_api_gateway_integration.ws_url_options,
  ]
}

# Access logs for the stage. API Gateway writes them with an account-level
# CloudWatch role, which is a per-account singleton (aws_api_gateway_account).
resource "aws_cloudwatch_log_group" "websocket_api_access" {
  count = var.websocket_api_enabled ? 1 : 0

  name              = "/aws/apigateway/${var.stack_name}-websocket-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-websocket-api-access-logs"
  })
}

resource "aws_iam_role" "apigateway_cloudwatch" {
  count = var.websocket_api_enabled ? 1 : 0

  name = "${var.stack_name}-apigateway-cloudwatch-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-apigateway-cloudwatch-role"
  })
}

resource "aws_iam_role_policy_attachment" "apigateway_cloudwatch" {
  count = var.websocket_api_enabled ? 1 : 0

  role       = aws_iam_role.apigateway_cloudwatch[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  count = var.websocket_api_enabled ? 1 : 0

  cloudwatch_role_arn = aws_iam_role.apigateway_cloudwatch[0].arn

  depends_on = [aws_iam_role_policy_attachment.apigateway_cloudwatch]
}

resource "aws_api_gateway_stage" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  deployment_id = aws_api_gateway_deployment.websocket[0].id
  rest_api_id   = aws_api_gateway_rest_api.websocket[0].id
  stage_name    = "v1"

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.websocket_api_access[0].arn
    format = jsonencode({
      requestId       = "$context.requestId"
      ip              = "$context.identity.sourceIp"
      requestTime     = "$context.requestTime"
      httpMethod      = "$context.httpMethod"
      resourcePath    = "$context.resourcePath"
      status          = "$context.status"
      responseLength  = "$context.responseLength"
      authorizerError = "$context.authorizer.error"
    })
  }

  depends_on = [aws_api_gateway_account.this]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-websocket-api-v1"
  })
}

resource "aws_api_gateway_method_settings" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.websocket[0].id
  stage_name  = aws_api_gateway_stage.websocket[0].stage_name
  method_path = "*/*"

  settings {
    logging_level      = "INFO"
    metrics_enabled    = true
    data_trace_enabled = false # never log request/response bodies
  }
}
