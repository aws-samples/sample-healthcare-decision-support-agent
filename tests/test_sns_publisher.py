"""Tests for SNS event publisher and observability events."""

import json
import os
from unittest.mock import patch, MagicMock
from medical_nudging.tracing.sns_publisher import (
    SNSEventPublisher,
    get_publisher,
    EVENT_SCHEMA_VERSION,
    _is_observability_enabled,
    _get_topic_arn,
)
from medical_nudging.tracing.observability_events import (
    emit_request_received,
    emit_processing_started,
    emit_tool_started,
    emit_response_sent,
    emit_error,
    _safe_patient_info,
    _hash_phi,
    _truncate,
)


class TestSNSEventPublisher:
    """Tests for SNSEventPublisher class."""

    def test_init_with_defaults(self):
        """Test initialization with default values."""
        with patch.dict(os.environ, {}, clear=True):
            publisher = SNSEventPublisher()
            assert publisher.topic_arn is None
            assert publisher.stack_name == "medical-nudging"
            assert publisher.environment == "dev"
            assert publisher.region == "us-east-1"
            assert publisher.is_enabled() is False

    def test_init_with_env_vars(self):
        """Test initialization from environment variables."""
        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
            "STACK_NAME": "test-stack",
            "ENVIRONMENT": "prod",
            "AWS_REGION": "us-west-2",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            assert publisher.topic_arn == env["OBSERVABILITY_SNS_TOPIC_ARN"]
            assert publisher.stack_name == "test-stack"
            assert publisher.environment == "prod"
            assert publisher.region == "us-west-2"
            assert publisher.is_enabled() is True

    def test_init_with_explicit_params(self):
        """Test initialization with explicit parameters override env vars."""
        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:env-topic",
            "OBSERVABILITY_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher(
                topic_arn="arn:aws:sns:us-east-1:123456789012:explicit-topic",
                stack_name="explicit-stack",
                environment="staging",
                region="eu-west-1",
            )
            assert publisher.topic_arn == "arn:aws:sns:us-east-1:123456789012:explicit-topic"
            assert publisher.stack_name == "explicit-stack"
            assert publisher.environment == "staging"
            assert publisher.region == "eu-west-1"

    def test_is_enabled_false_when_no_topic_arn(self):
        """Test that publisher is disabled when topic ARN is missing."""
        env = {"OBSERVABILITY_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            assert publisher.is_enabled() is False

    def test_is_enabled_false_when_not_enabled(self):
        """Test that publisher is disabled when OBSERVABILITY_ENABLED is not true."""
        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            assert publisher.is_enabled() is False

    def test_publish_returns_none_when_disabled(self):
        """Test that publish returns None when observability is disabled."""
        with patch.dict(os.environ, {}, clear=True):
            publisher = SNSEventPublisher()
            result = publisher.publish(
                event_type="test_event",
                payload={"key": "value"},
                correlation={"request_id": "req-123"},
            )
            assert result is None

    @patch("medical_nudging.tracing.sns_publisher._get_sns_client")
    def test_publish_success(self, mock_get_client):
        """Test successful event publishing."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            result = publisher.publish(
                event_type="request_received",
                payload={"test": "data"},
                correlation={"request_id": "req-123", "session_id": "sess-456"},
                source_component="orchestrator",
            )

            assert result is not None
            mock_client.publish.assert_called_once()
            call_kwargs = mock_client.publish.call_args[1]
            assert call_kwargs["TopicArn"] == env["OBSERVABILITY_SNS_TOPIC_ARN"]

            message = json.loads(call_kwargs["Message"])
            assert message["version"] == EVENT_SCHEMA_VERSION
            assert message["event_type"] == "request_received"
            assert message["payload"]["test"] == "data"
            assert message["correlation"]["request_id"] == "req-123"
            assert message["correlation"]["session_id"] == "sess-456"

    @patch("medical_nudging.tracing.sns_publisher._get_sns_client")
    def test_publish_fire_and_forget_on_error(self, mock_get_client):
        """Test that publish failures are logged but not raised."""
        mock_client = MagicMock()
        mock_client.publish.side_effect = Exception("SNS error")
        mock_get_client.return_value = mock_client

        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            result = publisher.publish(
                event_type="test_event",
                payload={"key": "value"},
            )
            # Should not raise, returns None
            assert result is None

    @patch("medical_nudging.tracing.sns_publisher._get_sns_client")
    def test_publish_returns_none_when_client_unavailable(self, mock_get_client):
        """Test that publish returns None when SNS client is unavailable."""
        mock_get_client.return_value = None

        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            result = publisher.publish(
                event_type="test_event",
                payload={"key": "value"},
            )
            assert result is None

    @patch("medical_nudging.tracing.sns_publisher._get_sns_client")
    def test_build_event_structure(self, mock_get_client):
        """Test that event structure matches expected schema."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
            "STACK_NAME": "test-stack",
            "ENVIRONMENT": "prod",
            "AWS_REGION": "us-west-2",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            publisher.publish(
                event_type="tool_completed",
                payload={"tool_name": "search_guidelines", "duration_ms": 150},
                correlation={
                    "request_id": "req-123",
                    "session_id": "sess-456",
                    "trace_id": "trace-789",
                },
                source_component="orchestrator",
            )

            call_kwargs = mock_client.publish.call_args[1]
            message = json.loads(call_kwargs["Message"])

            # Verify schema structure
            assert "version" in message
            assert "event_id" in message
            assert "event_type" in message
            assert "timestamp" in message
            assert "correlation" in message
            assert "source" in message
            assert "payload" in message
            assert "metadata" in message

            # Verify correlation
            assert message["correlation"]["request_id"] == "req-123"
            assert message["correlation"]["session_id"] == "sess-456"
            assert message["correlation"]["trace_id"] == "trace-789"

            # Verify source
            assert message["source"]["component"] == "orchestrator"
            assert message["source"]["environment"] == "prod"

            # Verify metadata
            assert message["metadata"]["stack_name"] == "test-stack"
            assert message["metadata"]["region"] == "us-west-2"

    @patch("medical_nudging.tracing.sns_publisher._get_sns_client")
    def test_message_attributes(self, mock_get_client):
        """Test that message attributes are set correctly."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        env = {
            "OBSERVABILITY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "OBSERVABILITY_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            publisher = SNSEventPublisher()
            publisher.publish(
                event_type="response_sent",
                payload={},
                correlation={"request_id": "req-abc-123"},
            )

            call_kwargs = mock_client.publish.call_args[1]
            attrs = call_kwargs["MessageAttributes"]

            assert attrs["event_type"]["StringValue"] == "response_sent"
            assert attrs["request_id"]["StringValue"] == "req-abc-123"


class TestGetPublisher:
    """Tests for get_publisher singleton function."""

    def test_get_publisher_returns_instance(self):
        """Test that get_publisher returns an SNSEventPublisher instance."""
        # Reset global publisher
        import medical_nudging.tracing.sns_publisher as sns_module

        sns_module._global_publisher = None

        with patch.dict(os.environ, {}, clear=True):
            publisher = get_publisher()
            assert isinstance(publisher, SNSEventPublisher)

    def test_get_publisher_singleton(self):
        """Test that get_publisher returns the same instance."""
        import medical_nudging.tracing.sns_publisher as sns_module

        sns_module._global_publisher = None

        with patch.dict(os.environ, {}, clear=True):
            publisher1 = get_publisher()
            publisher2 = get_publisher()
            assert publisher1 is publisher2


class TestHelperFunctions:
    """Tests for helper functions in sns_publisher module."""

    def test_is_observability_enabled_true(self):
        """Test _is_observability_enabled returns True when enabled."""
        with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "true"}, clear=True):
            assert _is_observability_enabled() is True

    def test_is_observability_enabled_false(self):
        """Test _is_observability_enabled returns False when not enabled."""
        with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "false"}, clear=True):
            assert _is_observability_enabled() is False

    def test_is_observability_enabled_missing(self):
        """Test _is_observability_enabled returns False when env var missing."""
        with patch.dict(os.environ, {}, clear=True):
            assert _is_observability_enabled() is False

    def test_is_observability_enabled_case_insensitive(self):
        """Test _is_observability_enabled is case insensitive."""
        with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "TRUE"}, clear=True):
            assert _is_observability_enabled() is True
        with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "True"}, clear=True):
            assert _is_observability_enabled() is True

    def test_get_topic_arn_returns_value(self):
        """Test _get_topic_arn returns env var value."""
        arn = "arn:aws:sns:us-east-1:123456789012:test-topic"
        with patch.dict(os.environ, {"OBSERVABILITY_SNS_TOPIC_ARN": arn}, clear=True):
            assert _get_topic_arn() == arn

    def test_get_topic_arn_returns_none(self):
        """Test _get_topic_arn returns None when not set."""
        with patch.dict(os.environ, {}, clear=True):
            assert _get_topic_arn() is None


class TestObservabilityEvents:
    """Tests for observability event emission helper functions."""

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_request_received(self, mock_get_publisher):
        """Test emit_request_received emits correct event."""
        mock_publisher = MagicMock()
        mock_publisher.publish.return_value = "event-123"
        mock_get_publisher.return_value = mock_publisher

        parsed_data = {
            "demographics": {
                "name": {"given": "John", "family": "Doe"},
                "mrn": "12345",
                "age": 45,
                "gender": "male",
            },
            "format_type": "ccda",
            "sections": [{"name": "problems"}, {"name": "medications"}],
        }
        visit_context = {
            "visit_type": "ambulatory",
            "specialty": "endocrinology",
            "chief_complaint": "Diabetes follow-up",  # Should be excluded
        }

        result = emit_request_received(
            request_id="req-123",
            parsed_data=parsed_data,
            visit_context=visit_context,
            session_id="sess-456",
            trace_id="trace-789",
        )

        assert result == "event-123"
        mock_publisher.publish.assert_called_once()
        call_kwargs = mock_publisher.publish.call_args[1]

        assert call_kwargs["event_type"] == "request_received"
        assert call_kwargs["source_component"] == "orchestrator"

        payload = call_kwargs["payload"]
        # Verify chief_complaint is excluded
        assert "chief_complaint" not in payload.get("visit_context", {})
        assert payload["visit_context"]["visit_type"] == "ambulatory"
        assert payload["visit_context"]["specialty"] == "endocrinology"

        # Verify patient info is safe
        patient_info = payload["patient_info"]
        assert patient_info["age"] == 45
        assert patient_info["gender"] == "male"
        assert "name_hash" in patient_info
        assert "mrn_hash" in patient_info
        # Actual name/mrn should not be present
        assert "John" not in str(patient_info)
        assert "12345" not in str(patient_info)

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_processing_started(self, mock_get_publisher):
        """Test emit_processing_started emits correct event."""
        mock_publisher = MagicMock()
        mock_publisher.publish.return_value = "event-456"
        mock_get_publisher.return_value = mock_publisher

        result = emit_processing_started(
            request_id="req-123",
            model_id="us.anthropic.claude-sonnet-4-5-20250514-v1:0",
            thinking_enabled=True,
            session_id="sess-456",
        )

        assert result == "event-456"
        call_kwargs = mock_publisher.publish.call_args[1]

        assert call_kwargs["event_type"] == "processing_started"
        payload = call_kwargs["payload"]
        assert payload["model_id"] == "us.anthropic.claude-sonnet-4-5-20250514-v1:0"
        assert payload["thinking_enabled"] is True

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_tool_started(self, mock_get_publisher):
        """Test emit_tool_started emits correct event."""
        mock_publisher = MagicMock()
        mock_publisher.publish.return_value = "event-789"
        mock_get_publisher.return_value = mock_publisher

        result = emit_tool_started(
            request_id="req-123",
            tool_name="search_guidelines",
            tool_use_id="tool-use-abc",
            input_params={"query": "diabetes screening", "max_results": 10},
            session_id="sess-456",
        )

        assert result == "event-789"
        call_kwargs = mock_publisher.publish.call_args[1]

        assert call_kwargs["event_type"] == "tool_started"
        payload = call_kwargs["payload"]
        assert payload["tool_name"] == "search_guidelines"
        assert payload["tool_use_id"] == "tool-use-abc"
        assert payload["input_params"]["query"] == "diabetes screening"

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_tool_started_truncates_long_params(self, mock_get_publisher):
        """Test emit_tool_started truncates long string parameters."""
        mock_publisher = MagicMock()
        mock_get_publisher.return_value = mock_publisher

        long_query = "x" * 1000

        emit_tool_started(
            request_id="req-123",
            tool_name="search_guidelines",
            tool_use_id="tool-use-abc",
            input_params={"query": long_query},
        )

        call_kwargs = mock_publisher.publish.call_args[1]
        payload = call_kwargs["payload"]
        # Should be truncated to 500 chars + truncation message
        assert len(payload["input_params"]["query"]) < len(long_query)
        assert "truncated" in payload["input_params"]["query"]

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_response_sent(self, mock_get_publisher):
        """Test emit_response_sent emits correct event."""
        mock_publisher = MagicMock()
        mock_publisher.publish.return_value = "event-response"
        mock_get_publisher.return_value = mock_publisher

        result = emit_response_sent(
            request_id="req-123",
            status="success",
            nudge_count=3,
            processing_time_ms=5000,
            guidelines_used=["ADA_2024", "USPSTF_2023"],
            session_id="sess-456",
        )

        assert result == "event-response"
        call_kwargs = mock_publisher.publish.call_args[1]

        assert call_kwargs["event_type"] == "response_sent"
        payload = call_kwargs["payload"]
        assert payload["status"] == "success"
        assert payload["nudge_count"] == 3
        assert payload["processing_time_ms"] == 5000
        assert payload["guidelines_used"] == ["ADA_2024", "USPSTF_2023"]

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_error(self, mock_get_publisher):
        """Test emit_error emits correct event."""
        mock_publisher = MagicMock()
        mock_publisher.publish.return_value = "event-error"
        mock_get_publisher.return_value = mock_publisher

        result = emit_error(
            request_id="req-123",
            error_type="ValueError",
            error_message="Invalid CCDA format: missing patient section",
            stage="parsing",
            session_id="sess-456",
        )

        assert result == "event-error"
        call_kwargs = mock_publisher.publish.call_args[1]

        assert call_kwargs["event_type"] == "error"
        payload = call_kwargs["payload"]
        assert payload["error_type"] == "ValueError"
        assert payload["error_message"] == "Invalid CCDA format: missing patient section"
        assert payload["stage"] == "parsing"

    @patch("medical_nudging.tracing.observability_events.get_publisher")
    def test_emit_error_truncates_long_message(self, mock_get_publisher):
        """Test emit_error truncates long error messages."""
        mock_publisher = MagicMock()
        mock_get_publisher.return_value = mock_publisher

        long_error = "E" * 2000

        emit_error(
            request_id="req-123",
            error_type="Exception",
            error_message=long_error,
            stage="inference",
        )

        call_kwargs = mock_publisher.publish.call_args[1]
        payload = call_kwargs["payload"]
        # Should be truncated to 1000 chars + truncation message
        assert len(payload["error_message"]) < len(long_error)
        assert "truncated" in payload["error_message"]


class TestPHISafety:
    """Tests for PHI safety in observability events."""

    def test_safe_patient_info_hashes_phi(self):
        """Test that _safe_patient_info hashes sensitive data."""
        parsed_data = {
            "demographics": {
                "name": {"given": "Jane", "family": "Smith"},
                "mrn": "MRN-98765",
                "age": 35,
                "gender": "female",
            },
            "format_type": "fhir",
            "sections": [{"name": "conditions"}],
        }

        safe_info = _safe_patient_info(parsed_data)

        # Name and MRN should be hashed (16 char hex strings)
        assert safe_info["name_hash"] is not None
        assert len(safe_info["name_hash"]) == 16
        assert safe_info["mrn_hash"] is not None
        assert len(safe_info["mrn_hash"]) == 16

        # Actual values should not appear
        assert "Jane" not in str(safe_info)
        assert "Smith" not in str(safe_info)
        assert "98765" not in str(safe_info)

        # Non-PHI data should be preserved
        assert safe_info["age"] == 35
        assert safe_info["gender"] == "female"
        assert safe_info["data_format"] == "fhir"

    def test_safe_patient_info_handles_missing_data(self):
        """Test that _safe_patient_info handles missing demographic data."""
        parsed_data = {"format_type": "ccda"}
        safe_info = _safe_patient_info(parsed_data)
        assert safe_info["name_hash"] is None
        assert safe_info["mrn_hash"] is None

    def test_safe_patient_info_handles_none(self):
        """Test that _safe_patient_info handles None input."""
        safe_info = _safe_patient_info(None)
        assert safe_info == {}

    def test_truncate_long_strings(self):
        """Test that _truncate properly truncates long strings."""
        short_string = "short"
        assert _truncate(short_string) == "short"

        long_string = "x" * 3000
        truncated = _truncate(long_string, max_length=100)
        assert truncated is not None
        assert len(truncated) < 150  # Truncated + message
        assert "truncated" in truncated
        assert "3000 chars" in truncated

    def test_truncate_handles_none(self):
        """Test that _truncate handles None input."""
        assert _truncate(None) is None

    def test_hash_phi_consistent(self):
        """Test that _hash_phi produces consistent hashes."""
        value = "test-value"
        hash1 = _hash_phi(value)
        hash2 = _hash_phi(value)
        assert hash1 == hash2
        assert hash1 is not None
        assert len(hash1) == 16

    def test_hash_phi_different_for_different_values(self):
        """Test that _hash_phi produces different hashes for different values."""
        hash1 = _hash_phi("value1")
        hash2 = _hash_phi("value2")
        assert hash1 != hash2

    def test_hash_phi_handles_none(self):
        """Test that _hash_phi handles None input."""
        assert _hash_phi(None) is None
        assert _hash_phi("") is None
