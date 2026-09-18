"""Lambda authorizer for API Gateway - validates x-api-key header.

REQUEST-type Lambda Authorizer that validates API keys stored in
AWS Secrets Manager. Used by the WebSocket API Gateway to authenticate
clients before granting access to the ws-url generation endpoint.

Environment Variables:
    API_KEY_SECRET_NAME: Secrets Manager secret name for API key
    LOG_LEVEL: Logging level (default: INFO)
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
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


def generate_policy(principal_id: str, effect: str, resource: str) -> dict:
    """Generate IAM policy document for API Gateway authorization.

    Args:
        principal_id: Principal identifier
        effect: "Allow" or "Deny"
        resource: Method ARN from the authorizer event

    Returns:
        IAM policy document for API Gateway
    """
    # Wildcard the resource to cover all methods/stages on this API
    arn_parts = resource.split(":")
    api_gateway_arn = ":".join(arn_parts[:5])
    api_rest = arn_parts[5].split("/")
    wildcard_resource = f"{api_gateway_arn}:{api_rest[0]}/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": wildcard_resource,
                }
            ],
        },
    }


def handler(event: dict, context: Any) -> dict:  # noqa: ARG001
    """REQUEST-type Lambda Authorizer for API Gateway.

    Validates x-api-key header against Secrets Manager.

    Returns:
        IAM Allow/Deny policy for API Gateway
    """
    headers = event.get("headers", {})
    provided_key = headers.get("x-api-key") or headers.get("X-Api-Key", "")
    method_arn = event.get("methodArn", "")

    if not provided_key:
        log.info("Missing x-api-key header")
        return generate_policy("anonymous", "Deny", method_arn)

    try:
        expected_key = get_api_key()
        is_valid = hmac.compare_digest(provided_key.encode(), expected_key.encode())
    except RuntimeError:
        log.exception("Failed to retrieve API key from Secrets Manager")
        is_valid = False

    if is_valid:
        log.info("API key validated successfully")
        return generate_policy("api-key-user", "Allow", method_arn)

    log.info("Invalid API key provided")
    return generate_policy("anonymous", "Deny", method_arn)
