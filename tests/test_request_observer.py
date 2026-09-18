"""The per-request observer owns the emit recipe for both orchestrator paths."""

from unittest.mock import patch

from medical_nudging.models import Nudge
from medical_nudging.tracing.request_observer import RequestObserver, nudge_breakdown

MODULE = "medical_nudging.tracing.request_observer"


def _nudge(urgency: str, category: str) -> Nudge:
    return Nudge(
        title="t",
        description="d",
        urgency=urgency,
        category=category,
        action_type=["order"],
        rationale="r",
        grounding="clinical_reasoning",
    )


def test_nudge_breakdown_counts_models_and_dicts_alike():
    models = [_nudge("urgent", "gaps_in_care"), _nudge("warning", "gaps_in_care")]
    dicts = [n.model_dump() for n in models]

    expected = {"by_urgency": {"urgent": 1, "warning": 1}, "by_category": {"gaps_in_care": 2}}
    assert nudge_breakdown(models) == expected
    assert nudge_breakdown(dicts) == expected
    assert nudge_breakdown([]) is None


def test_succeeded_emits_the_full_success_recipe():
    observer = RequestObserver(request_id="req", specialty="cardiology", visit_type="inpatient")
    nudges = [_nudge("urgent", "risk_alerts")]

    with (
        patch(f"{MODULE}.emit_response_sent") as sent,
        patch(f"{MODULE}.emit_success_count") as success,
        patch(f"{MODULE}.emit_request_latency") as request_latency,
        patch(f"{MODULE}.emit_inference_latency") as inference_latency,
        patch(f"{MODULE}.emit_nudge_count") as nudge_count,
    ):
        observer.succeeded(
            status="success",
            nudges=nudges,
            processing_time_ms=1200,
            token_usage={"input_tokens": 10, "output_tokens": 5},
            guidelines_used=["ADA 2026"],
        )

    sent.assert_called_once_with(
        request_id="req",
        status="success",
        nudge_count=1,
        processing_time_ms=1200,
        guidelines_used=["ADA 2026"],
        token_usage={"input_tokens": 10, "output_tokens": 5},
        nudge_breakdown={"by_urgency": {"urgent": 1}, "by_category": {"risk_alerts": 1}},
    )
    success.assert_called_once_with(specialty="cardiology", visit_type="inpatient")
    request_latency.assert_called_once_with(
        duration_ms=1200, specialty="cardiology", visit_type="inpatient"
    )
    inference_latency.assert_called_once_with(duration_ms=1200, status="success")
    nudge_count.assert_called_once_with(count=1, specialty="cardiology", visit_type="inpatient")


def test_failed_emits_event_count_and_latency_together():
    observer = RequestObserver(request_id="req", specialty="general", visit_type="unknown")

    with (
        patch(f"{MODULE}.emit_error") as error,
        patch(f"{MODULE}.emit_error_count") as count,
        patch(f"{MODULE}.emit_inference_latency") as latency,
    ):
        observer.failed(
            error_type="StructuredOutputError",
            error_message="no payload",
            stage="output_format",
            processing_time_ms=800,
        )

    error.assert_called_once_with(
        request_id="req",
        error_type="StructuredOutputError",
        error_message="no payload",
        stage="output_format",
    )
    count.assert_called_once_with(error_type="StructuredOutputError")
    latency.assert_called_once_with(duration_ms=800, status="error")


def test_request_received_emits_event_and_count():
    observer = RequestObserver(request_id="req", specialty="general", visit_type="ambulatory")

    with (
        patch(f"{MODULE}.emit_request_received") as received,
        patch(f"{MODULE}.emit_request_count") as count,
    ):
        observer.request_received({"demographics": {}}, {"visit_type": "ambulatory"})

    received.assert_called_once_with(
        request_id="req",
        parsed_data={"demographics": {}},
        visit_context={"visit_type": "ambulatory"},
    )
    count.assert_called_once_with(specialty="general", visit_type="ambulatory")
