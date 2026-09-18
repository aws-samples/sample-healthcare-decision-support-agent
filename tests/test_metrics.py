"""Tests for CloudWatch metrics module."""

import os
from unittest.mock import MagicMock, patch


from medical_nudging.tracing.metrics import (
    METRIC_NAMESPACE,
    emit_error_count,
    emit_nudge_count,
    emit_request_count,
    emit_request_latency,
    emit_success_count,
    emit_tool_call_count,
    increment_counter,
    put_metric,
)


class TestPutMetric:
    """Tests for put_metric function."""

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_success(self, mock_get_client):
        """Test successful metric emission."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true", "ENVIRONMENT": "test"}):
            result = put_metric(
                name="TestMetric",
                value=42.0,
                unit="Count",
                dimensions={"Specialty": "cardiology"},
            )

            assert result is True
            mock_client.put_metric_data.assert_called_once()
            call_kwargs = mock_client.put_metric_data.call_args[1]

            assert call_kwargs["Namespace"] == METRIC_NAMESPACE
            metric_data = call_kwargs["MetricData"][0]
            assert metric_data["MetricName"] == "TestMetric"
            assert metric_data["Value"] == 42.0
            assert metric_data["Unit"] == "Count"

            # Check dimensions
            dims = {d["Name"]: d["Value"] for d in metric_data["Dimensions"]}
            assert dims["Specialty"] == "cardiology"
            assert dims["Environment"] == "test"

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_adds_default_environment(self, mock_get_client):
        """Test that Environment dimension is added by default."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true", "ENVIRONMENT": "prod"}):
            put_metric(name="TestMetric", value=1.0)

            call_kwargs = mock_client.put_metric_data.call_args[1]
            metric_data = call_kwargs["MetricData"][0]
            dims = {d["Name"]: d["Value"] for d in metric_data["Dimensions"]}
            assert dims["Environment"] == "prod"

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_custom_namespace(self, mock_get_client):
        """Test metric with custom namespace."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true"}):
            put_metric(name="TestMetric", value=1.0, namespace="CustomNamespace")

            call_kwargs = mock_client.put_metric_data.call_args[1]
            assert call_kwargs["Namespace"] == "CustomNamespace"

    def test_put_metric_disabled(self):
        """Test that metrics are not emitted when disabled."""
        with patch.dict(os.environ, {"METRICS_ENABLED": "false"}):
            result = put_metric(name="TestMetric", value=1.0)
            assert result is False

    def test_put_metric_disabled_by_default_missing_env(self):
        """Test that metrics work with default enabled setting."""
        # By default (env not set), metrics should be enabled (true)
        with patch.dict(os.environ, {}, clear=True):
            with patch("medical_nudging.tracing.metrics._get_cloudwatch_client") as mock_get_client:
                mock_client = MagicMock()
                mock_get_client.return_value = mock_client

                result = put_metric(name="TestMetric", value=1.0)
                assert result is True
                mock_client.put_metric_data.assert_called_once()

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_handles_client_error(self, mock_get_client):
        """Test that ClientError is handled gracefully."""
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_client.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalFailure", "Message": "Test error"}},
            "PutMetricData",
        )
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true"}):
            result = put_metric(name="TestMetric", value=1.0)
            assert result is False

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_handles_generic_exception(self, mock_get_client):
        """Test that generic exceptions are handled gracefully."""
        mock_client = MagicMock()
        mock_client.put_metric_data.side_effect = Exception("Unexpected error")
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true"}):
            result = put_metric(name="TestMetric", value=1.0)
            assert result is False

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_put_metric_skips_none_dimensions(self, mock_get_client):
        """Test that None dimension values are skipped."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true", "ENVIRONMENT": "test"}):
            put_metric(
                name="TestMetric",
                value=1.0,
                dimensions={"Specialty": None, "VisitType": "ambulatory"},
            )

            call_kwargs = mock_client.put_metric_data.call_args[1]
            metric_data = call_kwargs["MetricData"][0]
            dims = {d["Name"]: d["Value"] for d in metric_data["Dimensions"]}

            assert "Specialty" not in dims
            assert dims["VisitType"] == "ambulatory"


class TestIncrementCounter:
    """Tests for increment_counter function."""

    @patch("medical_nudging.tracing.metrics.put_metric")
    def test_increment_counter_default(self, mock_put_metric):
        """Test increment_counter with default count."""
        mock_put_metric.return_value = True

        result = increment_counter("RequestCount", {"Specialty": "general"})

        assert result is True
        mock_put_metric.assert_called_once_with(
            "RequestCount", 1.0, unit="Count", dimensions={"Specialty": "general"}
        )

    @patch("medical_nudging.tracing.metrics.put_metric")
    def test_increment_counter_custom_count(self, mock_put_metric):
        """Test increment_counter with custom count."""
        mock_put_metric.return_value = True

        increment_counter("ErrorCount", count=5)

        mock_put_metric.assert_called_once_with("ErrorCount", 5.0, unit="Count", dimensions=None)


class TestConvenienceFunctions:
    """Tests for convenience metric functions."""

    @patch("medical_nudging.tracing.metrics.increment_counter")
    def test_emit_request_count(self, mock_increment):
        """Test emit_request_count function."""
        mock_increment.return_value = True

        result = emit_request_count(specialty="cardiology", visit_type="ambulatory")

        assert result is True
        mock_increment.assert_called_once_with(
            "RequestCount", {"Specialty": "cardiology", "VisitType": "ambulatory"}
        )

    @patch("medical_nudging.tracing.metrics.increment_counter")
    def test_emit_request_count_minimal(self, mock_increment):
        """Test emit_request_count with no arguments."""
        mock_increment.return_value = True

        emit_request_count()

        mock_increment.assert_called_once_with("RequestCount", {})

    @patch("medical_nudging.tracing.metrics.increment_counter")
    def test_emit_success_count(self, mock_increment):
        """Test emit_success_count function."""
        mock_increment.return_value = True

        result = emit_success_count(specialty="endocrinology", visit_type="inpatient")

        assert result is True
        mock_increment.assert_called_once_with(
            "SuccessCount", {"Specialty": "endocrinology", "VisitType": "inpatient"}
        )

    @patch("medical_nudging.tracing.metrics.increment_counter")
    def test_emit_error_count(self, mock_increment):
        """Test emit_error_count function."""
        mock_increment.return_value = True

        result = emit_error_count(
            specialty="general", visit_type="ambulatory", error_type="CCDAParseError"
        )

        assert result is True
        mock_increment.assert_called_once_with(
            "ErrorCount",
            {"Specialty": "general", "VisitType": "ambulatory", "ErrorType": "CCDAParseError"},
        )

    @patch("medical_nudging.tracing.metrics.put_metric")
    def test_emit_request_latency(self, mock_put_metric):
        """Test emit_request_latency function."""
        mock_put_metric.return_value = True

        result = emit_request_latency(duration_ms=5000, specialty="cardiology")

        assert result is True
        mock_put_metric.assert_called_once_with(
            "RequestLatency",
            5000.0,
            unit="Milliseconds",
            dimensions={"Specialty": "cardiology"},
        )

    @patch("medical_nudging.tracing.metrics.increment_counter")
    def test_emit_tool_call_count(self, mock_increment):
        """Test emit_tool_call_count function."""
        mock_increment.return_value = True

        result = emit_tool_call_count(tool_name="search_guidelines", specialty="general")

        assert result is True
        mock_increment.assert_called_once_with(
            "ToolCallCount", {"ToolName": "search_guidelines", "Specialty": "general"}
        )

    @patch("medical_nudging.tracing.metrics.put_metric")
    def test_emit_nudge_count(self, mock_put_metric):
        """Test emit_nudge_count function."""
        mock_put_metric.return_value = True

        result = emit_nudge_count(count=3, specialty="endocrinology", visit_type="ambulatory")

        assert result is True
        mock_put_metric.assert_called_once_with(
            "NudgeCount",
            3.0,
            unit="Count",
            dimensions={"Specialty": "endocrinology", "VisitType": "ambulatory"},
        )


class TestMetricsIntegration:
    """Integration tests for metrics module."""

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_full_request_lifecycle_metrics(self, mock_get_client):
        """Test emitting metrics for a complete request lifecycle."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true", "ENVIRONMENT": "test"}):
            # Request received
            emit_request_count(specialty="cardiology", visit_type="ambulatory")

            # Tool calls
            emit_tool_call_count(tool_name="search_guidelines")

            # Request completed
            emit_success_count(specialty="cardiology", visit_type="ambulatory")
            emit_request_latency(duration_ms=5000, specialty="cardiology", visit_type="ambulatory")
            emit_nudge_count(count=3, specialty="cardiology", visit_type="ambulatory")

            # Verify all metrics were emitted
            assert mock_client.put_metric_data.call_count == 5

    @patch("medical_nudging.tracing.metrics._get_cloudwatch_client")
    def test_error_scenario_metrics(self, mock_get_client):
        """Test emitting metrics for an error scenario."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"METRICS_ENABLED": "true", "ENVIRONMENT": "test"}):
            # Request received
            emit_request_count(specialty="general")

            # Error occurred
            emit_error_count(error_type="CCDAParseError", specialty="general")

            # Verify metrics were emitted
            assert mock_client.put_metric_data.call_count == 2

            # Check error metric
            error_call = mock_client.put_metric_data.call_args_list[1]
            metric_data = error_call[1]["MetricData"][0]
            assert metric_data["MetricName"] == "ErrorCount"
