"""Per-request observability: one place that knows what a request lifecycle emits.

The orchestrator has two execution paths (blocking and streaming). Both used to
restate the fan-out recipe by hand, and the copies drifted: the streaming path
skipped the request-latency metric, the guideline list, and the nudge breakdown,
and its fatal branch skipped the latency metric altogether. A ``RequestObserver``
is constructed once per request and owns the recipe, so the invariant "a failure
produces an SNS error event, an ErrorCount metric, and an InferenceLatency metric"
lives here instead of in caller discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from medical_nudging.tracing.metrics import (
    emit_error_count,
    emit_inference_latency,
    emit_nudge_count,
    emit_request_count,
    emit_request_latency,
    emit_success_count,
)
from medical_nudging.tracing.observability_events import (
    emit_error,
    emit_request_received,
    emit_response_sent,
)


ResponseStatus = Literal["success", "partial", "error"]
FailureStage = Literal[
    "parsing", "invalid_input", "output_format", "streaming", "token_limit", "inference"
]


def nudge_breakdown(nudges: Sequence[Any]) -> dict[str, dict[str, int]] | None:
    """Count nudges by urgency and category; ``None`` when there are no nudges.

    Accepts ``Nudge`` models or their ``model_dump()`` dicts.
    """
    if not nudges:
        return None
    by_urgency: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for nudge in nudges:
        if isinstance(nudge, Mapping):
            urgency = nudge.get("urgency") or "unknown"
            category = nudge.get("category") or "unknown"
        else:
            urgency = getattr(nudge, "urgency", None) or "unknown"
            category = getattr(nudge, "category", None) or "unknown"
        by_urgency[urgency] = by_urgency.get(urgency, 0) + 1
        by_category[category] = by_category.get(category, 0) + 1
    return {"by_urgency": by_urgency, "by_category": by_category}


@dataclass(frozen=True)
class RequestObserver:
    """Emits the observability events and CloudWatch metrics for one request."""

    request_id: str
    specialty: str
    visit_type: str

    def request_received(self, parsed_data: dict[str, Any], visit_context: dict) -> None:
        """The request parsed and is about to run."""
        emit_request_received(
            request_id=self.request_id, parsed_data=parsed_data, visit_context=visit_context
        )
        emit_request_count(specialty=self.specialty, visit_type=self.visit_type)

    def succeeded(
        self,
        *,
        status: ResponseStatus,
        nudges: Sequence[Any],
        processing_time_ms: int,
        token_usage: dict[str, int] | None,
        guidelines_used: list[str] | None = None,
    ) -> None:
        """The request produced a response; ``status`` is the response's own status."""
        count = len(nudges)
        emit_response_sent(
            request_id=self.request_id,
            status=status,
            nudge_count=count,
            processing_time_ms=processing_time_ms,
            guidelines_used=guidelines_used or [],
            token_usage=token_usage,
            nudge_breakdown=nudge_breakdown(nudges),
        )
        emit_success_count(specialty=self.specialty, visit_type=self.visit_type)
        emit_request_latency(
            duration_ms=processing_time_ms, specialty=self.specialty, visit_type=self.visit_type
        )
        emit_inference_latency(duration_ms=processing_time_ms, status="success")
        emit_nudge_count(count=count, specialty=self.specialty, visit_type=self.visit_type)

    def failed(
        self,
        *,
        error_type: str,
        error_message: str,
        stage: FailureStage,
        processing_time_ms: int,
    ) -> None:
        """The request ended in an error at ``stage``."""
        emit_error(
            request_id=self.request_id,
            error_type=error_type,
            error_message=error_message,
            stage=stage,
        )
        emit_error_count(error_type=error_type)
        emit_inference_latency(duration_ms=processing_time_ms, status="error")
