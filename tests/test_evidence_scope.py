"""Tests for the v4 generator-scope changes.

The v3 exact-span audit excluded all 22 cited candidates; the fixes under test:

* default FHIR queries carry no status filters (the filters silently hid every
  Condition and MedicationRequest in the MIMIC data);
* medstatus-v2: a completed MedicationDispense no longer establishes active status;
* coverage-v2: a page-capped query does not count as coverage for an absence claim;
* the steering entailment check: guideline-attributed content must be stated by the
  cited spans, with failures feeding the Guide retry loop and errors degrading to the
  deterministic verdict alone.
"""

from __future__ import annotations

from datetime import date

from medical_nudging.evidence_contract import (
    ClaimLedgerEntry,
    DeterministicVerifier,
    GeneratorConfiguration,
    GuidelineEvidenceSpan,
    PatientEvidenceSpan,
)
from medical_nudging.evidence_contract.entailment import (
    ENTAILMENT_PROMPT_VERSION,
    SteeringEntailmentCheck,
    entailment_prompt_hash,
)
from medical_nudging.evidence_contract.tool_ledger import (
    ToolLedger,
    ledger_from_steering_context,
)
from medical_nudging.tools.fhir_query import DEFAULT_RESOURCE_QUERIES


# --- default queries carry no status filters ---------------------------------


def test_default_resource_queries_have_no_status_filters():
    """MIMIC Conditions carry no clinicalStatus and its MedicationRequests never match
    status=active, so a status filter silently empties the resource type (v3 audit
    failure modes 2 and 3)."""
    for resource_type, params in DEFAULT_RESOURCE_QUERIES.items():
        assert "clinical-status" not in params, resource_type
        assert "status" not in params, resource_type
    assert set(DEFAULT_RESOURCE_QUERIES) == {
        "Condition",
        "MedicationRequest",
        "AllergyIntolerance",
        "Observation",
    }


# --- medstatus-v2 -------------------------------------------------------------


def _dispense_span(status: str = "completed") -> PatientEvidenceSpan:
    return PatientEvidenceSpan(
        span_id="patient-000",
        query="query_patient_fhir(resource_types='MedicationDispense')",
        resource_type="MedicationDispense",
        content={
            "resourceType": "MedicationDispense",
            "status": status,
            "whenHandedOver": "2140-10-01",
            "medicationCodeableConcept": {"text": "Warfarin 5 mg tablet"},
        },
    )


def _medication_claim(text: str) -> ClaimLedgerEntry:
    return ClaimLedgerEntry(
        claim_id="c01",
        claim_type="factual",
        claim_text=text,
        evidence_span_ids=["patient-000"],
    )


def test_a_completed_dispense_no_longer_establishes_active_status():
    """medstatus-v2: dispense-completed resolves indeterminate — the claim abstains
    instead of passing, so steering guides it toward hedged phrasing."""
    verifier = DeterministicVerifier()
    ledger = ToolLedger(patient_evidence=[_dispense_span()], coverage_rule_version="coverage-v2")
    claim = _medication_claim("The patient is on warfarin therapy.")
    report = verifier.verify(claims=[claim], ledger=ledger, as_of=date(2140, 10, 5))
    result = next(item for item in report.results if item.check == "medication_status")
    assert result.rule_version == "medstatus-v2"
    assert result.status == "not_evaluable"
    assert "indeterminate" in result.detail


def test_an_active_request_still_establishes_active_status():
    verifier = DeterministicVerifier()
    span = PatientEvidenceSpan(
        span_id="patient-000",
        query="query_patient_fhir(resource_types='MedicationRequest')",
        resource_type="MedicationRequest",
        content={
            "resourceType": "MedicationRequest",
            "status": "active",
            "authoredOn": "2140-10-01",
            "medicationCodeableConcept": {"text": "Warfarin 5 mg tablet"},
        },
    )
    ledger = ToolLedger(patient_evidence=[span], coverage_rule_version="coverage-v2")
    claim = _medication_claim("The patient is on warfarin therapy.")
    report = verifier.verify(claims=[claim], ledger=ledger, as_of=date(2140, 10, 5))
    result = next(item for item in report.results if item.check == "medication_status")
    assert result.status == "pass"


# --- coverage-v2: page-capped queries are not coverage ------------------------


_MEDICATION_QUERIES = [
    "query_patient_fhir(resource_types=['MedicationRequest'])",
    "query_patient_fhir(resource_types=['MedicationStatement'])",
    "query_patient_fhir(resource_types=['MedicationDispense'])",
    "query_patient_fhir(resource_types=['MedicationAdministration'])",
]


def test_a_page_capped_query_does_not_establish_absence():
    verifier = DeterministicVerifier()
    ledger = ToolLedger(
        patient_evidence=[
            PatientEvidenceSpan(
                span_id="patient-000",
                query="query_patient_fhir(resource_types='Observation')",
                resource_type="Observation",
                content={"resourceType": "Observation", "code": {"text": "Creatinine"}},
            )
        ],
        queries_executed=list(_MEDICATION_QUERIES),
        coverage_rule_version="coverage-v2",
        truncated_resource_types=["MedicationDispense"],
    )
    claim = _medication_claim("The patient is not receiving a statin.")
    report = verifier.verify(claims=[claim], ledger=ledger)
    result = next(item for item in report.results if item.check == "absence_within_query_coverage")
    assert result.status == "not_evaluable"
    assert "page" in result.detail and "MedicationDispense" in result.detail
    assert report.verdict == "abstain_insufficient_evidence"


def test_the_ledger_parses_the_page_cap_marker_from_the_tool_output():
    steering_ledger = {
        "tool_calls": [
            {
                "tool_name": "query_patient_fhir",
                "tool_args": {"patient_id": "p1", "resource_types": ["Observation"]},
                "status": "success",
                "result": [
                    {
                        "text": (
                            "Page cap reached for Observation: more result pages exist "
                            "beyond the retrieved window. Do not treat these results as "
                            "complete; add date or code filters to narrow the query.\n\n"
                            "### Observation (1 results)\n- Creatinine: 2.4 mg/dL"
                        )
                    }
                ],
            }
        ]
    }
    ledger = ledger_from_steering_context(steering_ledger, coverage_rule_version="coverage-v2")
    assert ledger.truncated_resource_types == ["Observation"]
    assert ledger.query_coverage().truncated_resource_types == ["Observation"]


def test_the_ledger_parses_the_history_window_marker_from_the_tool_output():
    """A recent-history filter that dropped results also truncates the window: the
    tool can render 'No X resources found' for a type the server did return, which
    coverage-v2 must not read as established absence."""
    steering_ledger = {
        "tool_calls": [
            {
                "tool_name": "query_patient_fhir",
                "tool_args": {"patient_id": "p1", "resource_types": ["MedicationDispense"]},
                "status": "success",
                "result": [
                    {
                        "text": (
                            "No MedicationDispense resources found.\n\n"
                            "History window excluded 12 older MedicationDispense "
                            "resource(s); set include_older_history=true only if needed."
                        )
                    }
                ],
            }
        ]
    }
    ledger = ledger_from_steering_context(steering_ledger, coverage_rule_version="coverage-v2")
    assert ledger.truncated_resource_types == ["MedicationDispense"]


# --- the steering entailment check --------------------------------------------


def _guideline_span(content: str) -> GuidelineEvidenceSpan:
    return GuidelineEvidenceSpan(
        span_id="guideline-000",
        chunk_id="src-2025:p12:abc",
        source="SRC_2025",
        content=content,
        section="Anticoagulation",
        page_number=12,
    )


def _attributed_claim(text: str) -> ClaimLedgerEntry:
    from medical_nudging.evidence_contract import CitationProvenance

    return ClaimLedgerEntry(
        claim_id="c02",
        claim_type="recommended_action",
        claim_text=text,
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="SRC_2025", section="Anticoagulation", page_number=12
        ),
    )


def _converse_response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


def test_a_non_entailed_directive_is_a_contract_failure():
    check = SteeringEntailmentCheck("us.anthropic.claude-opus-5")
    ledger = ToolLedger(
        guideline_evidence=[_guideline_span("Consider anticoagulation review on admission.")]
    )
    claim = _attributed_claim("Per SRC_2025, hold warfarin and recheck INR every 6 hours.")
    verdict = (
        '[{"claim_id": "c02", "entailed": false, '
        '"unsupported_content": "hold warfarin; INR every 6 hours"}]'
    )

    class _FakeClient:
        def converse(self, **kwargs):
            return _converse_response(verdict)

    check._client = _FakeClient()
    outcome = check.check_nudge([claim], ledger)
    assert not outcome.errored
    assert outcome.checked_claim_ids == ["c02"]
    assert len(outcome.failures) == 1
    assert "[steering_entailment]" in outcome.failures[0]
    assert "hold warfarin" in outcome.failures[0]


def test_an_entailed_claim_produces_no_failure():
    check = SteeringEntailmentCheck("us.anthropic.claude-opus-5")
    ledger = ToolLedger(
        guideline_evidence=[_guideline_span("Hold warfarin and recheck INR every 6 hours.")]
    )
    claim = _attributed_claim("Per SRC_2025, hold warfarin and recheck INR every 6 hours.")

    class _FakeClient:
        def converse(self, **kwargs):
            return _converse_response(
                '[{"claim_id": "c02", "entailed": true, "unsupported_content": ""}]'
            )

    check._client = _FakeClient()
    outcome = check.check_nudge([claim], ledger)
    assert not outcome.errored
    assert outcome.failures == []


def test_an_entailment_error_degrades_without_failing_anything():
    """An outage or malformed response must never pass or fail a claim."""
    check = SteeringEntailmentCheck("us.anthropic.claude-opus-5")
    ledger = ToolLedger(guideline_evidence=[_guideline_span("Some passage.")])
    claim = _attributed_claim("Per SRC_2025, do something specific.")

    class _BrokenClient:
        def converse(self, **kwargs):
            return _converse_response("I cannot answer in the requested format.")

    check._client = _BrokenClient()
    outcome = check.check_nudge([claim], ledger)
    assert outcome.errored
    assert outcome.failures == []


def test_claims_without_cited_spans_are_left_to_the_deterministic_checks():
    check = SteeringEntailmentCheck("us.anthropic.claude-opus-5")
    ledger = ToolLedger()  # no guideline evidence at all
    claim = _attributed_claim("Per SRC_2025, do something specific.")
    outcome = check.check_nudge([claim], ledger)
    assert not outcome.errored
    assert outcome.checked_claim_ids == []
    assert outcome.failures == []


# --- configuration provenance --------------------------------------------------


def test_the_generator_configuration_records_the_entailment_provenance():
    verifier = DeterministicVerifier()
    config = GeneratorConfiguration(
        config_version="p08-steered-generator-v4",
        entailment_model_id="us.anthropic.claude-opus-5",
        custom_instructions="Evidence discipline for this run: ...",
    )
    record = config.as_dict(verifier)
    assert record["entailment_model_id"] == "us.anthropic.claude-opus-5"
    assert record["entailment_prompt_version"] == ENTAILMENT_PROMPT_VERSION
    assert record["entailment_prompt_hash"] == entailment_prompt_hash()
    assert record["custom_instructions_hash"] is not None
    assert record["verifier_rule_versions"]["medication_status_rule_version"] == "medstatus-v2"
    assert record["verifier_rule_versions"]["query_coverage_rule_version"] == "coverage-v2"


def test_a_configuration_without_entailment_stays_deterministic():
    verifier = DeterministicVerifier()
    config = GeneratorConfiguration(config_version="p08-steered-generator-v3")
    record = config.as_dict(verifier)
    assert record["entailment_model_id"] is None
    assert "entailment_prompt_version" not in record
    assert record["custom_instructions_hash"] is None
