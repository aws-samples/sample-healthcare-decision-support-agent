"""Streaming event models for WebSocket communication.

These models define the event types sent from the agent container
to WebSocket clients during streaming nudge generation.

Event lifecycle:
    text*         → incremental text chunks
    tool_start    → a tool invocation begins
    tool_end      → a tool invocation completes
    error         → recoverable error (connection stays open)
    complete      → final result with parsed nudges (connection closes)
    fatal         → unrecoverable error (connection closes)
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class TextEvent(BaseModel):
    """Incremental text chunk from the agent."""

    event: Literal["text"] = "text"
    data: str = Field(description="Text content chunk")


class ToolStartEvent(BaseModel):
    """A tool invocation has started."""

    event: Literal["tool_start"] = "tool_start"
    data: dict[str, Any] = Field(description="Tool name and input: {tool, input}")


class ToolEndEvent(BaseModel):
    """A tool invocation has completed."""

    event: Literal["tool_end"] = "tool_end"
    data: dict[str, Any] = Field(description="Tool name and duration: {tool, duration_ms}")


class CompleteEvent(BaseModel):
    """Final result with parsed nudges. Connection closes after this event."""

    event: Literal["complete"] = "complete"
    data: dict[str, Any] = Field(
        description=(
            "Final result: {status, key_findings, patient_summary, nudges, "
            "usage, processing_time_ms, request_id}. Adds 'error' when status "
            "is 'error' (the model produced no schema-valid output)."
        )
    )


class ErrorEvent(BaseModel):
    """Recoverable error. Connection stays open."""

    event: Literal["error"] = "error"
    data: dict[str, Any] = Field(description="Error details: {code, message, recoverable: true}")


class FatalEvent(BaseModel):
    """Unrecoverable error. Connection closes after this event."""

    event: Literal["fatal"] = "fatal"
    data: dict[str, Any] = Field(description="Error details: {code, message}")


StreamEvent = Union[
    TextEvent,
    ToolStartEvent,
    ToolEndEvent,
    CompleteEvent,
    ErrorEvent,
    FatalEvent,
]
