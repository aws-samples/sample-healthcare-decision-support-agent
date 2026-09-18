"""Lambda handler for generating pre-signed AgentCore WebSocket URLs.

Validates the API key, then generates a short-lived pre-signed WebSocket URL
that clients can use to connect directly to AgentCore for streaming nudge generation.

Environment Variables:
    AGENT_RUNTIME_ARN: AgentCore runtime ARN
    API_KEY_SECRET_NAME: Secrets Manager secret name for API key
    URL_EXPIRY_SECONDS: Pre-signed URL expiry in seconds (default: 300, max: 300)
    AWS_REGION: AWS region (set automatically by Lambda)
    LOG_LEVEL: Logging level (default: INFO)
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Cached clients
_secrets_client: Any = None

# API key caching
_cached_api_key: str | None = None
_api_key_cache_time: float = 0
API_KEY_CACHE_TTL_SECONDS = 300  # 5 minutes


def get_secrets_client() -> Any:
    """Get cached Secrets Manager client."""
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def get_api_key() -> str:
    """Get API key from Secrets Manager with caching."""
    global _cached_api_key, _api_key_cache_time
    current_time = time.time()
    if _cached_api_key and (current_time - _api_key_cache_time) < API_KEY_CACHE_TTL_SECONDS:
        return _cached_api_key

    secret_name = os.environ.get("API_KEY_SECRET_NAME")
    if not secret_name:
        raise RuntimeError("API_KEY_SECRET_NAME not configured")

    client = get_secrets_client()
    response = client.get_secret_value(SecretId=secret_name)
    secret_value = response.get("SecretString", "")

    # Handle both JSON-formatted and plain string secrets
    if secret_value.startswith("{"):
        try:
            secret_data = json.loads(secret_value)
            api_key = secret_data.get("api_key", secret_value)
        except json.JSONDecodeError:
            api_key = secret_value
    else:
        api_key = secret_value

    if not api_key:
        raise RuntimeError("API key is empty")

    _cached_api_key = api_key
    _api_key_cache_time = current_time
    return api_key


def validate_api_key(event: dict) -> bool:
    """Validate API key from request headers using timing-safe comparison."""
    headers = event.get("headers", {})
    # API Gateway normalizes headers to lowercase
    provided_key = headers.get("x-api-key") or headers.get("X-Api-Key", "")
    if not provided_key:
        return False

    try:
        expected_key = get_api_key()
    except RuntimeError:
        log.exception("Failed to retrieve API key from Secrets Manager")
        return False

    return hmac.compare_digest(provided_key.encode(), expected_key.encode())


def build_response(status_code: int, body: dict) -> dict:
    """Build API Gateway proxy integration response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,X-Api-Key",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def handler(event: dict, context: Any) -> dict:  # noqa: ARG001
    """Generate pre-signed WebSocket URL for AgentCore streaming.

    Returns:
        API Gateway proxy response with {ws_url, session_id, expires_in}
    """
    # Handle CORS preflight
    http_method = event.get("requestContext", {}).get("httpMethod") or event.get(
        "requestContext", {}
    ).get("http", {}).get("method", "")

    if http_method == "OPTIONS":
        return build_response(200, {"message": "OK"})

    if http_method != "POST":
        return build_response(405, {"error": "Method not allowed. Use POST."})

    # Defense-in-depth: validate API key even though Lambda Authorizer checks it
    if not validate_api_key(event):
        return build_response(401, {"error": "Unauthorized"})

    agent_runtime_arn = os.environ.get("AGENT_RUNTIME_ARN")
    if not agent_runtime_arn:
        log.error("AGENT_RUNTIME_ARN not configured")
        return build_response(500, {"error": "Server configuration error"})

    region = os.environ.get("AWS_REGION", "us-east-1")
    expiry = min(int(os.environ.get("URL_EXPIRY_SECONDS", "300")), 300)
    session_id = str(uuid.uuid4())

    try:
        from bedrock_agentcore.runtime import AgentCoreRuntimeClient

        client = AgentCoreRuntimeClient(region)
        presigned_url = client.generate_presigned_url(
            runtime_arn=agent_runtime_arn,
            session_id=session_id,
            endpoint_name="DEFAULT",
            expires=expiry,
        )

        log.info(f"Generated presigned URL for session {session_id} (expires in {expiry}s)")

        return build_response(
            200,
            {
                "ws_url": presigned_url,
                "session_id": session_id,
                "expires_in": expiry,
                "endpoint": "DEFAULT",
            },
        )

    except ImportError:
        log.error("bedrock-agentcore SDK not available in Lambda environment")
        return build_response(
            500, {"error": "SDK not available - check Lambda layer configuration"}
        )
    except ValueError as e:
        log.error("Validation error generating URL: %s", e)
        return build_response(400, {"error": str(e)})
    except Exception as e:
        log.exception("Failed to generate presigned URL")
        return build_response(500, {"error": f"Internal error: {type(e).__name__}"})
