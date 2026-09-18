"""Tests for ws_url_generator Lambda handler."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add lambda directory to path so we can import handler
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "ws_url_generator"))


@pytest.fixture(autouse=True)
def env_setup():
    """Set up environment variables for each test."""
    with patch.dict(
        os.environ,
        {
            "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789:runtime/test-runtime",
            "API_KEY_SECRET_NAME": "medical-nudging/test/api-key",
            "URL_EXPIRY_SECONDS": "300",
            "AWS_REGION": "us-east-1",
        },
    ):
        yield


@pytest.fixture
def valid_event():
    """Create a valid API Gateway proxy event."""
    return {
        "requestContext": {"httpMethod": "POST"},
        "headers": {"x-api-key": "test-api-key-12345"},
        "body": "{}",
    }


class TestWSUrlGeneratorHandler:
    """Tests for the ws_url_generator Lambda handler."""

    def test_generates_presigned_url(self, valid_event):
        """Test successful presigned URL generation."""
        import handler

        with (
            patch.object(handler, "validate_api_key", return_value=True),
            patch("bedrock_agentcore.runtime.AgentCoreRuntimeClient") as mock_client_class,
        ):
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = (
                "wss://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/test/ws"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
            )
            mock_client_class.return_value = mock_client

            result = handler.handler(valid_event, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["ws_url"].startswith("wss://")
            assert "session_id" in body
            assert body["expires_in"] == 300
            assert body["endpoint"] == "DEFAULT"

    def test_missing_api_key_returns_401(self):
        """Test request without API key returns 401."""
        import handler

        event = {
            "requestContext": {"httpMethod": "POST"},
            "headers": {},
            "body": "{}",
        }

        with patch.object(handler, "get_api_key", return_value="expected-key"):
            result = handler.handler(event, None)
            assert result["statusCode"] == 401

    def test_invalid_api_key_returns_401(self, valid_event):
        """Test request with wrong API key returns 401."""
        import handler

        with patch.object(handler, "get_api_key", return_value="different-key"):
            result = handler.handler(valid_event, None)
            assert result["statusCode"] == 401

    def test_options_returns_200(self):
        """Test CORS preflight OPTIONS request."""
        import handler

        event = {
            "requestContext": {"httpMethod": "OPTIONS"},
            "headers": {},
        }
        result = handler.handler(event, None)
        assert result["statusCode"] == 200

    def test_get_method_returns_405(self, valid_event):
        """Test non-POST method returns 405."""
        import handler

        valid_event["requestContext"]["httpMethod"] = "GET"
        result = handler.handler(valid_event, None)
        assert result["statusCode"] == 405

    def test_missing_runtime_arn_returns_500(self, valid_event):
        """Test missing AGENT_RUNTIME_ARN returns 500."""
        import handler

        with (
            patch.object(handler, "validate_api_key", return_value=True),
            patch.dict(os.environ, {"AGENT_RUNTIME_ARN": ""}),
        ):
            result = handler.handler(valid_event, None)
            assert result["statusCode"] == 500

    def test_cors_headers_present(self, valid_event):
        """Test CORS headers are present in response."""
        import handler

        with (
            patch.object(handler, "validate_api_key", return_value=True),
            patch("bedrock_agentcore.runtime.AgentCoreRuntimeClient") as mock_client_class,
        ):
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = "wss://test.example.com/ws"
            mock_client_class.return_value = mock_client

            result = handler.handler(valid_event, None)
            assert result["headers"]["Access-Control-Allow-Origin"] == "*"
            assert "X-Api-Key" in result["headers"]["Access-Control-Allow-Headers"]
