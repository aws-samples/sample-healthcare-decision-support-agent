"""Tests for the deterministic evidence-presentation rule (evidence-view-v1)."""

import pytest

from medical_nudging.evidence_contract.evidence_view import (
    DEFAULT_PATIENT_SPAN_TOKEN_BUDGET,
    EVIDENCE_VIEW_VERSION,
    _estimate_tokens,
    build_evidence_view,
    evidence_view_content_hash,
    select_patient_spans,
)


def _span(span_id: str, resource_type: str, text: str) -> dict:
    return {
        "span_id": span_id,
        "query": f"{resource_type}?patient=x",
        "resource_type": resource_type,
        "content": {"resourceType": resource_type, "note": text},
        "retrieved_at": "2026-08-28T00:00:00Z",
    }


def _payload(spans: list[dict]) -> dict:
    return {
        "benchmark_original": {
            "nudge_text": "Consider heart failure medication review for reduced ejection fraction.",
            "proposed_action": "Review beta-blocker dosing per NICE NG106.",
            "citation_provenance": {
                "source": "NICE NG106",
                "section": "1.2 Treating heart failure with reduced ejection fraction",
                "page_number": None,
                "chunk_ids": ["c1"],
            },
        },
        "patient_evidence": spans,
        "guideline_evidence": [
            {
                "span_id": "g-1",
                "chunk_id": "c1",
                "source": "NICE NG106",
                "content": "Offer a beta-blocker licensed for heart failure.",
                "section": "1.2",
                "page_number": 14,
            }
        ],
        "query_coverage": {
            "queries_executed": ["Condition?patient=x"],
            "coverage_rule_version": "coverage-v2",
            "truncated_resource_types": [],
        },
    }


def test_relevant_spans_outrank_unrelated_ones() -> None:
    spans = [
        _span("s-1", "Observation", "unrelated dermatology finding"),
        _span("s-2", "Condition", "heart failure with reduced ejection fraction"),
        _span("s-3", "MedicationRequest", "beta-blocker bisoprolol for heart failure"),
    ]
    tight_budget = _estimate_tokens(spans[1]) + _estimate_tokens(spans[2])
    selected = select_patient_spans(_payload(spans), tight_budget)
    assert [span["span_id"] for span in selected] == ["s-2", "s-3"]


def test_selected_spans_keep_original_payload_order() -> None:
    spans = [
        _span("s-9", "MedicationRequest", "beta-blocker heart failure"),
        _span("s-1", "Condition", "heart failure reduced ejection fraction"),
    ]
    selected = select_patient_spans(_payload(spans), DEFAULT_PATIENT_SPAN_TOKEN_BUDGET)
    assert [span["span_id"] for span in selected] == ["s-9", "s-1"]


def test_oversized_span_is_skipped_not_a_stopping_point() -> None:
    huge = _span("s-1", "Condition", "heart failure " * 5000)
    small = _span("s-2", "Condition", "heart failure reduced ejection fraction")
    budget = _estimate_tokens(small) + 10
    selected = select_patient_spans(_payload([huge, small]), budget)
    assert [span["span_id"] for span in selected] == ["s-2"]


def test_ties_break_by_span_id() -> None:
    spans = [
        _span("s-b", "Observation", "identical text"),
        _span("s-a", "Observation", "identical text"),
    ]
    budget = _estimate_tokens(spans[0])
    selected = select_patient_spans(_payload(spans), budget)
    assert [span["span_id"] for span in selected] == ["s-a"]


def test_patient_resource_spans_are_always_included() -> None:
    demographics = _span("s-demo", "Patient", "born 1950 female")
    relevant = [
        _span(f"s-{i}", "Condition", f"heart failure reduced ejection fraction {i}")
        for i in range(20)
    ]
    budget = _estimate_tokens(demographics) + _estimate_tokens(relevant[0]) * 3
    selected = select_patient_spans(_payload([*relevant, demographics]), budget)
    assert "s-demo" in {span["span_id"] for span in selected}


def test_per_type_floor_keeps_low_scoring_resource_types() -> None:
    encounter = _span("s-enc", "Encounter", "inpatient stay ward transfer")
    relevant = [
        _span(f"s-{i}", "Condition", f"heart failure reduced ejection fraction {i}")
        for i in range(20)
    ]
    budget = _estimate_tokens(encounter) + _estimate_tokens(relevant[0]) * 6
    selected = select_patient_spans(_payload([*relevant, encounter]), budget)
    assert "s-enc" in {span["span_id"] for span in selected}


def test_view_is_deterministic() -> None:
    spans = [_span(f"s-{i}", "Observation", f"heart failure note {i}") for i in range(30)]
    payload = _payload(spans)
    first = build_evidence_view(payload, 200)
    second = build_evidence_view(payload, 200)
    assert first == second


def test_guideline_evidence_and_coverage_pass_through_verbatim() -> None:
    payload = _payload([_span("s-1", "Condition", "heart failure")])
    view = build_evidence_view(payload)
    assert view["guideline_evidence"] == payload["guideline_evidence"]
    assert view["query_coverage"] == payload["query_coverage"]
    assert view["benchmark_original"] == payload["benchmark_original"]


def test_budget_bounds_selected_patient_spans() -> None:
    spans = [_span(f"s-{i}", "Observation", f"heart failure note {i}") for i in range(50)]
    budget = 300
    view = build_evidence_view(_payload(spans), budget)
    selected_cost = sum(_estimate_tokens(span) for span in view["patient_evidence"])
    assert selected_cost <= budget
    assert 0 < len(view["patient_evidence"]) < len(spans)


def test_manifest_records_rule_identity() -> None:
    view = build_evidence_view(_payload([_span("s-1", "Condition", "heart failure")]))
    manifest = view["view_manifest"]
    assert manifest["view_version"] == EVIDENCE_VIEW_VERSION
    assert manifest["view_content_hash"] == evidence_view_content_hash()
    assert manifest["patient_spans_total"] == 1
    assert manifest["patient_spans_selected"] == 1


def test_hidden_fields_are_rejected() -> None:
    payload = _payload([_span("s-1", "Condition", "heart failure")])
    payload["control"] = {"control_type": "patient_swap"}
    with pytest.raises(ValueError, match="hidden fields"):
        build_evidence_view(payload)
