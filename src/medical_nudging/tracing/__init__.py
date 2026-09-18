"""Tracing module for capturing agent execution details."""

from medical_nudging.tracing.trace_capture import (
    ExecutionTrace,
    ToolCallTrace,
    TraceCapturingCallback,
)
from medical_nudging.tracing.sns_publisher import (
    SNSEventPublisher,
    get_publisher,
)
from medical_nudging.tracing.observability_events import (
    emit_error,
    emit_processing_started,
    emit_request_received,
    emit_response_sent,
    emit_tool_started,
)
from medical_nudging.tracing.metrics import (
    emit_error_count,
    emit_inference_latency,
    emit_nudge_count,
    emit_request_count,
    emit_request_latency,
    emit_success_count,
    emit_tool_call_count,
    increment_counter,
    put_metric,
)

__all__ = [
    # Trace capture
    "ExecutionTrace",
    "ToolCallTrace",
    "TraceCapturingCallback",
    # SNS publisher
    "SNSEventPublisher",
    "get_publisher",
    # Observability events
    "emit_error",
    "emit_processing_started",
    "emit_request_received",
    "emit_response_sent",
    "emit_tool_started",
    # CloudWatch metrics
    "emit_error_count",
    "emit_inference_latency",
    "emit_nudge_count",
    "emit_request_count",
    "emit_request_latency",
    "emit_success_count",
    "emit_tool_call_count",
    "increment_counter",
    "put_metric",
]
