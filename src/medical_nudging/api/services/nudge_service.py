"""Nudge service wrapping the orchestrator with trace capture."""

import logging

from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator
from medical_nudging.models import NudgeResponse
from medical_nudging.tracing.trace_capture import ExecutionTrace
from medical_nudging.api.schemas.nudge import (
    ExecutionTraceResponse,
    ToolCallTraceResponse,
    TimingResponse,
)

logger = logging.getLogger(__name__)


class NudgeService:
    """Service for generating nudges with optional trace capture.

    Delegates to ``MedicalNudgingOrchestrator`` for all inference logic,
    including data source resolution, tool building, and prompt construction.
    """

    def __init__(self, specialty: str = "general"):
        """Initialize the nudge service.

        Args:
            specialty: Default specialty context
        """
        self.specialty = specialty
        self._orchestrator = MedicalNudgingOrchestrator(specialty=specialty)

    def generate_nudges_with_trace(
        self,
        patient_data: str | None = None,
        visit_context: dict | None = None,
        config: dict | None = None,
    ) -> tuple[NudgeResponse, ExecutionTrace]:
        """Generate nudges with full execution trace for debugging.

        Args:
            patient_data: Raw patient document (CCDA XML, FHIR JSON, or pre-parsed
                JSON).  ``None`` when ``data_source='fhir_api'``.
            visit_context: Visit type, specialty, chief complaint, data_source
            config: Optional configuration

        Returns:
            Tuple of (NudgeResponse, ExecutionTrace)
        """
        return self._orchestrator.generate_nudges_with_trace(
            patient_data=patient_data,
            visit_context=visit_context,
            config=config,
        )


def trace_to_response(trace: ExecutionTrace) -> ExecutionTraceResponse:
    """Convert ExecutionTrace to API response schema."""
    return ExecutionTraceResponse(
        system_prompt=trace.system_prompt,
        user_prompt=trace.user_prompt,
        tool_calls=[
            ToolCallTraceResponse(
                tool_name=tc.tool_name,
                tool_use_id=tc.tool_use_id,
                input_params=tc.input_params if isinstance(tc.input_params, dict) else {},
                output=tc.output,
                duration_ms=tc.duration_ms,
                completed=tc.completed,
                success=tc.success,
                error=tc.error,
            )
            for tc in trace.tool_calls
        ],
        messages=trace.messages,
        raw_response=trace.raw_response,
        timing=TimingResponse(
            total_ms=trace.total_duration_ms,
            tool_ms=trace.tool_duration_ms,
            model_ms=trace.model_duration_ms,
            incomplete_tool_calls=trace.incomplete_tool_calls,
        ),
    )
