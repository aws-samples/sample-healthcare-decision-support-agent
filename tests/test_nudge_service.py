"""Tests for API nudge service helpers."""

from medical_nudging.api.services.nudge_service import trace_to_response
from medical_nudging.tracing.trace_capture import ExecutionTrace, ToolCallTrace


def test_trace_to_response_preserves_incomplete_tool_state():
    """An unfinished tool remains explicit without breaking API validation."""
    trace = ExecutionTrace(
        start_time=10.0,
        end_time=20.0,
        tool_calls=[
            ToolCallTrace(
                tool_name="search_guidelines",
                tool_use_id="tu-1",
                input_params={},
                start_time=12.0,
            )
        ],
    )

    response = trace_to_response(trace)

    assert response.tool_calls[0].duration_ms is None
    assert response.tool_calls[0].completed is False
    assert response.timing.incomplete_tool_calls == 1
    assert response.timing.tool_ms == 0
    assert response.timing.model_ms == 10_000
