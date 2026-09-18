"""Trace capture for agent execution debugging."""

from dataclasses import dataclass, field
from typing import Any
import time

from strands.hooks import HookRegistry
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent

from medical_nudging.tracing.observability_events import emit_tool_started
from medical_nudging.tracing.metrics import emit_tool_call_count


@dataclass
class ToolCallTrace:
    """Represents a single tool call in the execution trace."""

    tool_name: str
    tool_use_id: str
    input_params: dict
    output: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    success: bool = True
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Whether the tool lifecycle recorded a valid completion time."""
        return self.end_time > 0.0 and self.end_time >= self.start_time

    @property
    def duration_ms(self) -> int | None:
        """Duration of a completed tool call in milliseconds."""
        if not self.completed:
            return None
        return int((self.end_time - self.start_time) * 1000)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "input_params": self.input_params,
            "output": self.output,
            "duration_ms": self.duration_ms,
            "completed": self.completed,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class ExecutionTrace:
    """Complete execution trace for debugging."""

    system_prompt: str = ""
    user_prompt: str = ""
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    raw_response: str | None = None
    token_usage: dict[str, int] | None = None
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def total_duration_ms(self) -> int:
        """Total execution duration in milliseconds."""
        if self.end_time <= 0.0 or self.end_time < self.start_time:
            return 0
        return int((self.end_time - self.start_time) * 1000)

    @property
    def tool_duration_ms(self) -> int:
        """Total time spent in completed tool calls in milliseconds."""
        return sum(duration for tc in self.tool_calls if (duration := tc.duration_ms) is not None)

    @property
    def model_duration_ms(self) -> int:
        """Estimated model inference time (total minus tools)."""
        return max(0, self.total_duration_ms - self.tool_duration_ms)

    @property
    def incomplete_tool_calls(self) -> int:
        """Number of tool calls missing a lifecycle completion event."""
        return sum(1 for tc in self.tool_calls if not tc.completed)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "messages": self.messages,
            "raw_response": self.raw_response,
            "token_usage": self.token_usage,
            "timing": {
                "total_ms": self.total_duration_ms,
                "tool_ms": self.tool_duration_ms,
                "model_ms": self.model_duration_ms,
                "incomplete_tool_calls": self.incomplete_tool_calls,
            },
        }


class TraceCapturingCallback:
    """Callback handler that captures execution trace for debugging.

    This is designed to work with Strands Agent callback_handler parameter.
    """

    def __init__(self, request_id: str | None = None) -> None:
        """Initialize callback handler.

        Args:
            request_id: Optional request ID for observability event correlation
        """
        self.trace = ExecutionTrace()
        self._current_tool: ToolCallTrace | None = None
        self._accumulated_text = ""
        self._request_id = request_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register tool lifecycle hooks for authoritative timing."""
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Start timing when Strands begins executing the selected tool."""
        tool_use_id = event.tool_use.get("toolUseId", "")
        tool_call = next(
            (call for call in reversed(self.trace.tool_calls) if call.tool_use_id == tool_use_id),
            None,
        )
        if tool_call is None:
            return

        raw_input = event.tool_use.get("input", {})
        if isinstance(raw_input, dict):
            tool_call.input_params = raw_input
        tool_call.start_time = time.perf_counter()
        tool_call.end_time = 0.0

    def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Close the matching tool trace when Strands finishes the call."""
        if event.retry:
            return

        tool_use_id = event.tool_use.get("toolUseId", "")
        tool_call = next(
            (
                call
                for call in reversed(self.trace.tool_calls)
                if call.tool_use_id == tool_use_id and not call.completed
            ),
            None,
        )
        if tool_call is None:
            return

        tool_call.end_time = time.perf_counter()
        if event.exception is not None:
            tool_call.success = False
            tool_call.error = str(event.exception)
        elif event.cancel_message:
            tool_call.success = False
            tool_call.error = event.cancel_message

    def __call__(self, **kwargs: Any) -> None:
        """Handle callback events from the agent."""
        # Capture tool use events
        current_tool_use = kwargs.get("current_tool_use", {})
        if current_tool_use and isinstance(current_tool_use, dict):
            tool_name = current_tool_use.get("name")
            tool_use_id = current_tool_use.get("toolUseId", "")

            if tool_name:
                # Check if this is a new tool call
                if self._current_tool is None or self._current_tool.tool_use_id != tool_use_id:
                    # Save previous tool call if exists
                    if self._current_tool and not self._current_tool.completed:
                        self._current_tool.end_time = time.perf_counter()

                    # Ensure input_params is always a dict
                    raw_input = current_tool_use.get("input", {})
                    input_params = raw_input if isinstance(raw_input, dict) else {}

                    self._current_tool = ToolCallTrace(
                        tool_name=tool_name,
                        tool_use_id=tool_use_id,
                        input_params=input_params,
                        start_time=time.perf_counter(),
                    )
                    self.trace.tool_calls.append(self._current_tool)

                    # Emit tool started event for observability
                    if self._request_id:
                        emit_tool_started(
                            request_id=self._request_id,
                            tool_name=tool_name,
                            tool_use_id=tool_use_id,
                            input_params=input_params,
                        )

                    # Emit CloudWatch tool call count metric
                    emit_tool_call_count(tool_name=tool_name)

        # Capture streaming text
        data = kwargs.get("data", "")
        if data:
            self._accumulated_text += str(data)

        # No `tool_result` branch: the SDK never passes one to a callback_handler because
        # `ToolResultEvent.is_callback_event` is False. Per-tool outcomes come from
        # `ObservabilityHookProvider` (AfterInvocationEvent -> `result.metrics.tool_metrics`)
        # and from `toolResult` blocks in `trace.messages`.

        # Capture completion — the SDK signals this with a terminal `result` (AgentResult)
        # kwarg, not the `complete` flag this handler previously looked for.
        if kwargs.get("result") is not None:
            self.trace.raw_response = self._accumulated_text

        # Capture message (final event)
        message = kwargs.get("message")
        if message and isinstance(message, dict):
            self.trace.messages.append(message)
