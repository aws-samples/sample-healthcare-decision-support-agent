# ============================================================================
# Medical Nudging - WebSocket API Variables
# ============================================================================
#
# Variables for the presigned URL WebSocket streaming API, the only client
# path into the agent. Enabled by default; set websocket_api_enabled=false only
# for a runtime you will call with boto3 invoke_agent_runtime directly.
# ============================================================================

variable "websocket_api_enabled" {
  description = "Enable the WebSocket streaming API with presigned URLs. Creates the API key secret, API Gateway REST API, Lambda Authorizer, ws_url_generator Lambda, and WAF."
  type        = bool
  default     = true
}

variable "ws_url_expiry_seconds" {
  description = "Expiry time for presigned WebSocket URLs in seconds (max 300 per AgentCore SDK)"
  type        = number
  default     = 300

  validation {
    condition     = var.ws_url_expiry_seconds >= 60 && var.ws_url_expiry_seconds <= 300
    error_message = "URL expiry must be between 60 and 300 seconds (AgentCore SDK maximum is 300)."
  }
}

variable "websocket_waf_rate_limit" {
  description = "Rate limit for WebSocket API requests per 5-minute window per IP"
  type        = number
  default     = 100

  validation {
    condition     = var.websocket_waf_rate_limit >= 10 && var.websocket_waf_rate_limit <= 10000
    error_message = "Rate limit must be between 10 and 10000."
  }
}
