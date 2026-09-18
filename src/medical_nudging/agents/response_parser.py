"""Mapping of agent structured output onto the medical nudging response schema.

Nudge generation uses the Strands SDK's native structured output: the agent is
invoked with ``structured_output_model=GeneratedNudgeOutput``, so the SDK exposes
a tool derived from that Pydantic schema and validates the model's payload
against it before the result reaches this module.  The functions here pull the
validated object off the agent result and map it into the public
:class:`NudgeResponse` (non-streaming) or WebSocket dict (streaming) shapes.
"""

import logging
import time
from typing import Any

from medical_nudging.models import GeneratedNudgeOutput, Nudge, NudgeMetadata, NudgeResponse

logger = logging.getLogger(__name__)

# The SDK derives the structured output tool name from the model's JSON schema
# title, which Pydantic defaults to the class name (asserted in tests).
STRUCTURED_OUTPUT_TOOL_NAME = GeneratedNudgeOutput.__name__


class StructuredOutputMissingError(ValueError):
    """Raised when an agent result carries no validated structured output."""


def extract_structured_output(response: Any) -> GeneratedNudgeOutput:
    """Pull the SDK-validated structured output off a Strands agent result.

    Args:
        response: ``AgentResult`` from ``agent(...)`` or the final ``stream_async``
            result event

    Returns:
        The validated :class:`GeneratedNudgeOutput`

    Raises:
        StructuredOutputMissingError: If the result carries no structured output,
            or one of an unexpected type
    """
    generated = getattr(response, "structured_output", None)
    if generated is None:
        raise StructuredOutputMissingError(
            f"Agent result carries no structured output: the {STRUCTURED_OUTPUT_TOOL_NAME} "
            "tool was never invoked with a schema-valid payload"
        )
    if not isinstance(generated, GeneratedNudgeOutput):
        raise StructuredOutputMissingError(
            f"Agent returned unexpected structured output type: {type(generated).__name__}"
        )
    return generated


def _select_nudges(generated: GeneratedNudgeOutput, config: dict) -> list[Nudge]:
    """Apply the ``max_nudges`` cap and reset model-asserted validation flags."""
    max_nudges = config.get("max_nudges", 5)
    nudges = generated.nudges[:max_nudges]
    for nudge in nudges:
        # The model has no access to official code sets, so its ICD codes are
        # never validated regardless of what it puts in the field.
        nudge.codes_validated = False
    return nudges


def _guidelines_used(nudges: list[Nudge]) -> list[str]:
    """Collect the distinct guideline sources cited across nudges."""
    return sorted(
        {
            nudge.guideline_citation.source
            for nudge in nudges
            if nudge.guideline_citation and nudge.guideline_citation.source
        }
    )


def build_nudge_response(
    generated: GeneratedNudgeOutput, start_time: float, config: dict
) -> NudgeResponse:
    """Map validated agent output into a :class:`NudgeResponse`.

    Args:
        generated: Structured output from :func:`extract_structured_output`
        start_time: ``time.perf_counter()`` value from before inference
        config: Dict with optional ``max_nudges`` and ``model_version``

    Returns:
        NudgeResponse with ``status="success"``
    """
    nudges = _select_nudges(generated, config)
    processing_time = int((time.perf_counter() - start_time) * 1000)

    return NudgeResponse(
        status="success",
        key_findings=list(generated.key_findings),
        patient_summary=generated.patient_summary,
        nudges=nudges,
        metadata=NudgeMetadata(
            model_version=config.get("model_version", "unknown"),
            processing_time_ms=processing_time,
            guidelines_used=_guidelines_used(nudges),
        ),
    )


def build_streaming_result(generated: GeneratedNudgeOutput, config: dict) -> dict[str, Any]:
    """Map validated agent output into the streaming ``complete`` payload.

    Like :func:`build_nudge_response` but returns a plain dict for WebSocket
    transmission (timing and metadata are added by the caller).

    Args:
        generated: Structured output from :func:`extract_structured_output`
        config: Dict with optional ``max_nudges``

    Returns:
        Dict with ``status``, ``key_findings``, ``patient_summary``, ``nudges``
    """
    nudges = _select_nudges(generated, config)
    return {
        "status": "success",
        "key_findings": list(generated.key_findings),
        "patient_summary": generated.patient_summary,
        "nudges": [nudge.model_dump() for nudge in nudges],
    }


def create_streaming_error_result(error: str) -> dict[str, Any]:
    """Build the streaming ``complete`` payload for a structured output failure.

    Args:
        error: Human-readable failure description

    Returns:
        Dict with ``status="error"`` and no nudges
    """
    return {
        "status": "error",
        "key_findings": [],
        "patient_summary": None,
        "nudges": [],
        "error": error,
    }


def create_error_response(
    error_type: str, error: Exception, start_time: float, config: dict
) -> NudgeResponse:
    """Create a standardised error NudgeResponse.

    Args:
        error_type: Human-readable label (e.g. "CCDA parsing")
        error: The exception that occurred
        start_time: ``time.perf_counter()`` value from before processing
        config: Dict with optional ``model_version``

    Returns:
        NudgeResponse with ``status="error"``
    """
    processing_time = int((time.perf_counter() - start_time) * 1000)
    return NudgeResponse(
        status="error",
        error=f"{error_type} failed: {str(error)}",
        metadata=NudgeMetadata(
            model_version=config.get("model_version", "unknown"),
            processing_time_ms=processing_time,
            guidelines_used=[],
        ),
    )
