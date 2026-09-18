"""Observability event emission helpers.

Helper functions for emitting standardized observability events at pipeline stages.
All functions are PHI-safe: patient data is hashed, outputs truncated, chief_complaint excluded.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from medical_nudging.tracing.sns_publisher import get_publisher

logger = logging.getLogger(__name__)

# Maximum length for output fields to prevent large payloads
MAX_OUTPUT_LENGTH = 2000


def _hash_phi(value: str | None) -> str | None:
    """Hash PHI data for safe logging.

    Args:
        value: Value to hash

    Returns:
        SHA-256 hash of value or None
    """
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _truncate(value: str | None, max_length: int = MAX_OUTPUT_LENGTH) -> str | None:
    """Truncate string to maximum length.

    Args:
        value: String to truncate
        max_length: Maximum allowed length

    Returns:
        Truncated string or None
    """
    if not value:
        return None
    if len(value) <= max_length:
        return value
    return value[:max_length] + f"... [truncated, total {len(value)} chars]"


def _safe_patient_info(parsed_data: dict[str, Any] | None) -> dict[str, Any]:
    """Extract safe patient info (no PHI) for logging.

    Args:
        parsed_data: Parsed patient data dictionary

    Returns:
        Dictionary with hashed/safe patient identifiers
    """
    if not parsed_data:
        return {}

    demographics = parsed_data.get("demographics", {})
    name = demographics.get("name", {})
    full_name = f"{name.get('given', '')} {name.get('family', '')}".strip()

    return {
        "name_hash": _hash_phi(full_name) if full_name else None,
        "mrn_hash": _hash_phi(demographics.get("mrn")),
        "age": demographics.get("age"),
        "gender": demographics.get("gender"),
        "data_format": parsed_data.get("format_type", "unknown"),
        "section_count": (
            len(parsed_data.get("sections", [])) if "sections" in parsed_data else None
        ),
    }


def emit_request_received(
    request_id: str,
    parsed_data: dict[str, Any] | None = None,
    visit_context: dict[str, Any] | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Emit event when request is received.

    Args:
        request_id: Unique request identifier
        parsed_data: Parsed patient data (PHI will be hashed)
        visit_context: Visit context (chief_complaint excluded)
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    # Build safe visit context (exclude chief_complaint for PHI safety)
    safe_visit_context = {}
    if visit_context:
        safe_visit_context = {
            "visit_type": visit_context.get("visit_type"),
            "specialty": visit_context.get("specialty"),
            # chief_complaint is excluded for PHI safety
        }

    payload = {
        "patient_info": _safe_patient_info(parsed_data),
        "visit_context": safe_visit_context,
        "received_at_ms": int(time.perf_counter() * 1000),
    }

    return publisher.publish(
        event_type="request_received",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="orchestrator",
    )


def emit_processing_started(
    request_id: str,
    model_id: str | None = None,
    thinking_enabled: bool = False,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Emit event when inference processing starts.

    Args:
        request_id: Unique request identifier
        model_id: Bedrock model ID being used
        thinking_enabled: Whether extended thinking is enabled
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    payload = {
        "model_id": model_id,
        "thinking_enabled": thinking_enabled,
        "started_at_ms": int(time.perf_counter() * 1000),
    }

    return publisher.publish(
        event_type="processing_started",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="orchestrator",
    )


def emit_tool_started(
    request_id: str,
    tool_name: str,
    tool_use_id: str,
    input_params: dict[str, Any] | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Emit event when a tool call starts.

    Args:
        request_id: Unique request identifier
        tool_name: Name of the tool being called
        tool_use_id: Unique tool use identifier
        input_params: Tool input parameters (will be truncated)
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    # Truncate input params for safety
    safe_params = {}
    if input_params:
        for key, value in input_params.items():
            if isinstance(value, str):
                safe_params[key] = _truncate(value, 500)
            else:
                safe_params[key] = value

    payload = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "input_params": safe_params,
        "started_at_ms": int(time.perf_counter() * 1000),
    }

    return publisher.publish(
        event_type="tool_started",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="orchestrator",
    )


def emit_response_sent(
    request_id: str,
    status: str,
    nudge_count: int,
    processing_time_ms: int,
    guidelines_used: list[str] | None = None,
    token_usage: dict[str, int] | None = None,
    nudge_breakdown: dict[str, dict[str, int]] | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Emit event when response is sent.

    Args:
        request_id: Unique request identifier
        status: Response status (success, partial, error)
        nudge_count: Number of nudges generated
        processing_time_ms: Total processing time in milliseconds
        guidelines_used: List of guidelines referenced
        token_usage: Token usage metrics (input_tokens, output_tokens)
        nudge_breakdown: Nudge counts by urgency and category
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    payload = {
        "status": status,
        "nudge_count": nudge_count,
        "processing_time_ms": processing_time_ms,
        "guidelines_used": guidelines_used or [],
        "token_usage": token_usage,
        "nudge_breakdown": nudge_breakdown,
        "sent_at_ms": int(time.perf_counter() * 1000),
    }

    return publisher.publish(
        event_type="response_sent",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="orchestrator",
    )


def emit_error(
    request_id: str,
    error_type: str,
    error_message: str,
    stage: str,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Emit event when an error occurs.

    Args:
        request_id: Unique request identifier
        error_type: Type/class of error
        error_message: Error message (will be truncated)
        stage: Pipeline stage where error occurred
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    payload = {
        "error_type": error_type,
        "error_message": _truncate(error_message, 1000),
        "stage": stage,
        "occurred_at_ms": int(time.perf_counter() * 1000),
    }

    return publisher.publish(
        event_type="error",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="orchestrator",
    )


def emit_strands_metrics(
    request_id: str,
    strands_metrics: dict[str, Any],
    session_id: str | None = None,
    trace_id: str | None = None,
    trace_s3_key: str | None = None,
) -> str | None:
    """Emit comprehensive Strands SDK metrics event.

    Emitted by ObservabilityHookProvider after agent invocation completes.
    Contains aggregated token usage, performance metrics, tool summaries,
    and a reference to the full trace in S3.

    Args:
        request_id: Unique request identifier
        strands_metrics: Metrics dict with token_usage, performance, tool_summary, cycles
        session_id: AgentCore session ID
        trace_id: OpenTelemetry trace ID
        trace_s3_key: S3 key for full trace file (if written)

    Returns:
        Event ID if published, None otherwise
    """
    publisher = get_publisher()

    payload = {
        "strands_metrics": strands_metrics,
        "emitted_at_ms": int(time.perf_counter() * 1000),
    }
    if trace_s3_key:
        payload["trace_s3_key"] = trace_s3_key

    return publisher.publish(
        event_type="strands_metrics",
        payload=payload,
        correlation={"request_id": request_id, "session_id": session_id, "trace_id": trace_id},
        source_component="strands_hook",
    )
