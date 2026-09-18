# ============================================================================
# Medical Nudging - WAF for WebSocket API Gateway
# ============================================================================
#
# WAF Web ACL protecting the WebSocket presigned URL API Gateway.
# Includes AWS Managed Rules, rate limiting, and request size constraints.
#
# Enable with: terraform apply -var="websocket_api_enabled=true"
# ============================================================================

resource "aws_wafv2_web_acl" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  name        = "${var.stack_name}-websocket-waf"
  description = "WAF for Medical Nudging WebSocket presigned URL API"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  # Rule 1: AWS Managed Common Rule Set
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # Exclude body size rule since we handle it separately
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.stack_name}-websocket-common-rules"
      sampled_requests_enabled   = true
    }
  }

  # Rule 2: AWS Managed Known Bad Inputs
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.stack_name}-websocket-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # Rule 3: Rate limiting per IP
  rule {
    name     = "RateLimit"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.websocket_waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.stack_name}-websocket-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Rule 4: Body size constraint (8 KB max for /ws-url requests)
  rule {
    name     = "BodySizeConstraint"
    priority = 4

    action {
      block {}
    }

    statement {
      size_constraint_statement {
        comparison_operator = "GT"
        size                = 8192 # 8 KB
        field_to_match {
          body {
            oversize_handling = "MATCH"
          }
        }
        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.stack_name}-websocket-body-size"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.stack_name}-websocket-waf"
    sampled_requests_enabled   = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-websocket-waf"
  })
}

# Associate WAF with API Gateway stage
resource "aws_wafv2_web_acl_association" "websocket" {
  count = var.websocket_api_enabled ? 1 : 0

  resource_arn = aws_api_gateway_stage.websocket[0].arn
  web_acl_arn  = aws_wafv2_web_acl.websocket[0].arn
}
