"""Tests for the Strands callback and hook trace capture contracts.

The kwargs asserted here mirror what the SDK actually delivers to a
``callback_handler``; ``tool_result`` and ``complete`` are deliberately absent
(see ``trace_capture.TraceCapturingCallback.__call__``).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from medical_nudging.tracing.trace_capture import (
    ExecutionTrace,
    ToolCallTrace,
    TraceCapturingCallback,
)


class TestTraceCapturingCallback:
    """Tests for TraceCapturingCallback.__call__ event handling."""

    @patch("medical_nudging.tracing.trace_capture.emit_tool_call_count")
    def test_captures_tool_use_start(self, mock_count):
        """A current_tool_use event records a tool call and emits the count metric."""
        cb = TraceCapturingCallback()

        cb(
            current_tool_use={
                "toolUseId": "tu-1",
                "name": "search_guidelines",
                "input": {"q": "a1c"},
            }
        )

        assert len(cb.trace.tool_calls) == 1
        tool_call = cb.trace.tool_calls[0]
        assert tool_call.tool_name == "search_guidelines"
        assert tool_call.tool_use_id == "tu-1"
        assert tool_call.input_params == {"q": "a1c"}
        mock_count.assert_called_once_with(tool_name="search_guidelines")

    @patch("medical_nudging.tracing.trace_capture.emit_tool_call_count")
    def test_non_dict_tool_input_coerced_to_dict(self, _mock_count):
        """Partial streaming input arrives as a string; it must not break capture."""
        cb = TraceCapturingCallback()

        cb(current_tool_use={"toolUseId": "tu-1", "name": "search_guidelines", "input": '{"q":'})

        assert cb.trace.tool_calls[0].input_params == {}

    @patch("medical_nudging.tracing.trace_capture.emit_tool_call_count")
    def test_second_tool_closes_out_previous(self, _mock_count):
        """Starting a new tool stamps end_time on the previous one."""
        cb = TraceCapturingCallback()

        cb(current_tool_use={"toolUseId": "tu-1", "name": "search_guidelines", "input": {}})
        cb(current_tool_use={"toolUseId": "tu-2", "name": "list_guidelines", "input": {}})

        assert len(cb.trace.tool_calls) == 2
        assert cb.trace.tool_calls[0].end_time > 0.0

    @patch("medical_nudging.tracing.trace_capture.emit_tool_call_count")
    @patch(
        "medical_nudging.tracing.trace_capture.time.perf_counter",
        side_effect=[9.5, 10.0, 10.25],
    )
    def test_after_tool_call_hook_closes_final_tool(self, _perf_counter, _mock_count):
        """Lifecycle hooks measure execution and close the final tool."""
        cb = TraceCapturingCallback()
        cb(current_tool_use={"toolUseId": "tu-1", "name": "search_guidelines", "input": {}})
        cb.on_before_tool_call(
            SimpleNamespace(
                tool_use={"toolUseId": "tu-1", "input": {"query": "sepsis"}},
            )
        )

        cb.on_after_tool_call(
            SimpleNamespace(
                tool_use={"toolUseId": "tu-1"},
                exception=None,
                cancel_message=None,
                retry=False,
            )
        )

        tool_call = cb.trace.tool_calls[0]
        assert tool_call.input_params == {"query": "sepsis"}
        assert tool_call.start_time == 10.0
        assert tool_call.end_time == 10.25
        assert tool_call.duration_ms == 250

    def test_incomplete_tool_is_explicit_and_excluded_from_timing_arithmetic(self):
        """An unfinished call cannot create negative tool time or inflated model time."""
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

        serialized = trace.to_dict()

        assert serialized["tool_calls"][0]["duration_ms"] is None
        assert serialized["tool_calls"][0]["completed"] is False
        assert serialized["timing"] == {
            "total_ms": 10_000,
            "tool_ms": 0,
            "model_ms": 10_000,
            "incomplete_tool_calls": 1,
        }

    def test_serializes_token_usage(self):
        trace = ExecutionTrace(
            token_usage={
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_input_tokens": 800,
            }
        )

        assert trace.to_dict()["token_usage"] == {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 800,
        }

    def test_accumulates_streaming_text_into_raw_response_on_result(self):
        """raw_response is populated from the terminal `result` kwarg, not `complete`."""
        cb = TraceCapturingCallback()

        cb(data="Patient ")
        cb(data="summary.")
        assert cb.trace.raw_response is None

        cb(result=MagicMock())

        assert cb.trace.raw_response == "Patient summary."

    def test_captures_messages(self):
        """message events are appended to the trace for downstream parsing."""
        cb = TraceCapturingCallback()
        message = {"role": "user", "content": [{"toolResult": {"toolUseId": "tu-1"}}]}

        cb(message=message)

        assert cb.trace.messages == [message]

    def test_ignores_unknown_and_empty_kwargs(self):
        """Unrelated SDK events are harmless no-ops."""
        cb = TraceCapturingCallback()

        cb(init_event_loop=True)
        cb(data="")
        cb(current_tool_use={})

        assert cb.trace.tool_calls == []
        assert cb.trace.messages == []
        assert cb.trace.raw_response is None

    @patch("medical_nudging.tracing.trace_capture.emit_tool_started")
    @patch("medical_nudging.tracing.trace_capture.emit_tool_call_count")
    def test_emits_tool_started_only_with_request_id(self, _mock_count, mock_started):
        """Observability events are correlated by request_id and skipped without one."""
        TraceCapturingCallback()(
            current_tool_use={"toolUseId": "tu-1", "name": "list_guidelines", "input": {}}
        )
        mock_started.assert_not_called()

        TraceCapturingCallback(request_id="req-1")(
            current_tool_use={"toolUseId": "tu-1", "name": "list_guidelines", "input": {}}
        )
        mock_started.assert_called_once_with(
            request_id="req-1",
            tool_name="list_guidelines",
            tool_use_id="tu-1",
            input_params={},
        )
