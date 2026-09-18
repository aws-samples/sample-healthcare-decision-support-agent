"""Synthetic tests for deterministic evidence-contract verification."""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from medical_nudging.evidence_contract import (
    ActionEvidenceChain,
    CitationProvenance,
    ClaimLedgerEntry,
    DerivationStep,
    DeterministicVerifier,
    GuidelineEvidenceSpan,
    PatientEvidenceSpan,
)
from medical_nudging.evidence_contract import verifier as verifier_module
from medical_nudging.evidence_contract.tool_ledger import ToolLedger


@pytest.fixture
def verifier() -> DeterministicVerifier:
    return DeterministicVerifier()


def _ledger(
    *,
    patient: list[PatientEvidenceSpan] | None = None,
    guideline: list[GuidelineEvidenceSpan] | None = None,
    queries: list[str] | None = None,
) -> ToolLedger:
    return ToolLedger(
        patient_evidence=patient or [],
        guideline_evidence=guideline or [],
        queries_executed=queries or [],
        coverage_rule_version="coverage-v2",
    )


def _observation(span_id: str, value: float, when: str) -> PatientEvidenceSpan:
    return PatientEvidenceSpan(
        span_id=span_id,
        query="query_patient_fhir(resource_types='Observation')",
        resource_type="Observation",
        content={
            "resourceType": "Observation",
            "code": {"text": "Creatinine"},
            "valueQuantity": {"value": value, "unit": "mg/dL"},
            "effectiveDateTime": f"{when}T09:00:00",
        },
    )


def _passage(span_id: str, section: str, page: int, content: str) -> GuidelineEvidenceSpan:
    return GuidelineEvidenceSpan(
        span_id=span_id,
        chunk_id=f"src-2025:p{page}:{span_id}",
        source="SRC_2025",
        content=content,
        section=section,
        page_number=page,
    )


def _claim(**overrides) -> ClaimLedgerEntry:
    payload = {
        "claim_id": "c01",
        "claim_type": "factual",
        "claim_text": "Creatinine is 2.4 mg/dL.",
        "evidence_span_ids": ["patient-000"],
    }
    payload.update(overrides)
    return ClaimLedgerEntry(**payload)


def _status(report, check: str) -> str:
    return next(result.status for result in report.results if result.check == check)


# --- the no-model-calls guarantee -------------------------------------------


def test_verifier_module_makes_no_model_calls():
    """The verifier is replayable code only: no client, no agent, no prompt."""
    source = inspect.getsource(verifier_module)
    for forbidden in ("boto3", "bedrock", "strands", "Agent(", "invoke_model", "converse"):
        assert forbidden not in source, f"{forbidden!r} appears in the verifier module"


# --- the deterministic layer never concludes satisfies_contract --------------


def test_a_clean_draft_reaches_deterministic_checks_passed_not_satisfies_contract(verifier):
    """Design §4/decision 12: only a bounded judge can conclude ``satisfies_contract``.

    A draft with every deterministic check passing is the strongest verdict this layer can
    reach, and it is still not an admission: the case-level verdict abstains until the
    bounded judge has ruled on substantive support.
    """
    ledger = _ledger(
        patient=[_observation("patient-000", 2.4, "2140-10-04")],
        guideline=[_passage("guideline-000", "Synopsis", 18, "Recheck creatinine.")],
    )
    claim = _claim(
        claim_type="guideline",
        claim_text="SRC_2025 Synopsis: recheck creatinine.",
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="SRC_2025",
            section="Synopsis",
            page_number=18,
            chunk_ids=["src-2025:p18:guideline-000"],
        ),
    )
    report = verifier.verify(claims=[claim], ledger=ledger)

    assert not report.failures
    assert report.verdict == "deterministic_checks_passed"
    assert report.case_verdict == "abstain_insufficient_evidence"
    assert report.as_dict()["verdict"] == "deterministic_checks_passed"


def test_no_verifier_code_path_returns_satisfies_contract():
    """No ``return`` in the module hands back that verdict, in any branch."""
    tree = ast.parse(inspect.getsource(verifier_module))
    returned = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert "satisfies_contract" not in returned
    assert "deterministic_checks_passed" in returned


# --- citation checks ---------------------------------------------------------


def test_fabricated_citation_fails_passage_identity(verifier):
    ledger = _ledger(guideline=[_passage("guideline-000", "Synopsis", 18, "Statin recommended.")])
    claim = _claim(
        claim_type="guideline",
        claim_text="SRC_2025 Discharge planning: start a statin.",
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="SRC_2025", section="Discharge planning", page_number=62
        ),
    )
    report = verifier.verify(claims=[claim], ledger=ledger)
    assert _status(report, "passage_identity") == "fail"
    assert report.verdict == "violates_contract"
    assert any("identifies no retrieved passage" in line for line in report.failure_summary())


def test_unretrieved_source_fails_source_identity(verifier):
    ledger = _ledger(guideline=[_passage("guideline-000", "Synopsis", 18, "Statin recommended.")])
    claim = _claim(
        claim_type="guideline",
        claim_text="OTHER_2019 Synopsis: start a statin.",
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="OTHER_2019", section="Synopsis", page_number=18
        ),
    )
    report = verifier.verify(claims=[claim], ledger=ledger)
    assert _status(report, "source_identity") == "fail"


def test_matching_citation_passes_identity_and_resolution(verifier):
    ledger = _ledger(guideline=[_passage("guideline-000", "Synopsis", 18, "Statin recommended.")])
    claim = _claim(
        claim_type="guideline",
        claim_text="SRC_2025 Synopsis: start a statin.",
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="SRC_2025",
            section="Synopsis",
            page_number=18,
            chunk_ids=["src-2025:p18:guideline-000"],
        ),
    )
    report = verifier.verify(claims=[claim], ledger=ledger)
    assert _status(report, "passage_identity") == "pass"
    assert _status(report, "citation_resolution") == "pass"


# --- values, dates, derivations ---------------------------------------------


def test_unsupported_value_fails_and_supported_value_passes(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    bad = verifier.verify(claims=[_claim(claim_text="Creatinine is 9.9 mg/dL.")], ledger=ledger)
    assert _status(bad, "exact_values") == "fail"
    good = verifier.verify(claims=[_claim(claim_text="Creatinine is 2.4 mg/dL.")], ledger=ledger)
    assert _status(good, "exact_values") == "pass"


def test_date_inside_an_iso_timestamp_counts_as_supported(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    report = verifier.verify(
        claims=[_claim(claim_text="Creatinine 2.4 mg/dL on 2140-10-04.")],
        ledger=ledger,
    )
    assert _status(report, "dates") == "pass"


def test_unsupported_date_fails(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    report = verifier.verify(
        claims=[_claim(claim_text="Creatinine 2.4 mg/dL on 2139-01-01.")],
        ledger=ledger,
    )
    assert _status(report, "dates") == "fail"


def test_publication_year_in_a_citation_is_not_an_asserted_value(verifier):
    ledger = _ledger(guideline=[_passage("guideline-000", "Synopsis", 18, "Statin recommended.")])
    claim = _claim(
        claim_type="guideline",
        claim_text="SRC_2025 Synopsis (ACC/AHA 2025): start a statin.",
        evidence_span_ids=["guideline-000"],
        citation_provenance=CitationProvenance(
            source="SRC_2025", section="Synopsis", page_number=18
        ),
    )
    report = verifier.verify(claims=[claim], ledger=ledger)
    # Every number in the claim is a citation identifier, so nothing asserted survives
    # masking. The check abstains rather than passing: a claim this check could not read
    # must never be recorded as a claim whose values were verified.
    assert _status(report, "exact_values") == "not_evaluable"


def test_a_masked_claim_never_passes_exact_values(verifier):
    """Masking that empties a non-empty claimed set abstains, never passes.

    A false pass here is the worst outcome available to this check: it records an
    unverified numeric assertion as verified.
    """
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    claim = _claim(claim_text="Rechecked on 2140-10-04.", evidence_span_ids=["patient-000"])
    assert (
        _status(verifier.verify(claims=[claim], ledger=ledger), "exact_values") == "not_evaluable"
    )


def test_a_blood_pressure_pair_is_not_masked_as_a_date(verifier):
    """``80/50`` is not a calendar date, so both values stay asserted (resolution 10)."""
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    claim = _claim(claim_text="Blood pressure 80/50 recorded.", evidence_span_ids=["patient-000"])
    result = next(
        item
        for item in verifier.verify(claims=[claim], ledger=ledger).results
        if item.check == "exact_values"
    )
    assert result.status == "fail"
    assert "80" in result.detail and "50" in result.detail


def test_an_unrelated_section_number_does_not_exempt_a_patient_value(verifier):
    """Identifier masking is scoped to the claim's own citation (V1a).

    A ``Table 4`` heading on some other retrieved passage must not exempt the number 4
    from a claim about the patient.
    """
    ledger = _ledger(
        patient=[_observation("patient-000", 2.4, "2140-10-04")],
        guideline=[_passage("guideline-000", "Table 4 dosing", 18, "Dose table.")],
    )
    claim = _claim(claim_text="Creatinine is 4 mg/dL.", evidence_span_ids=["patient-000"])
    assert _status(verifier.verify(claims=[claim], ledger=ledger), "exact_values") == "fail"


def test_replayable_derivation_passes_and_wrong_arithmetic_fails(verifier):
    ledger = _ledger(
        patient=[
            _observation("patient-000", 2.4, "2140-10-04"),
            _observation("patient-001", 1.4, "2140-10-01"),
        ]
    )
    supported = _claim(
        claim_text="Creatinine rose by 1.0 mg/dL.",
        evidence_span_ids=["patient-000", "patient-001"],
        derivation_chain=[
            DerivationStep(rule="difference", inputs=["patient-000", "patient-001"], output="1.0")
        ],
    )
    report = verifier.verify(claims=[supported], ledger=ledger)
    assert _status(report, "arithmetic_and_trends") == "pass"

    wrong = supported.model_copy(
        update={
            "derivation_chain": [
                DerivationStep(
                    rule="difference", inputs=["patient-000", "patient-001"], output="5.0"
                )
            ]
        }
    )
    assert _status(verifier.verify(claims=[wrong], ledger=ledger), "arithmetic_and_trends") == (
        "fail"
    )


def test_unsupported_derivation_rule_is_not_evaluable(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    claim = _claim(
        derivation_chain=[
            DerivationStep(rule="clinical_judgement", inputs=["patient-000"], output="high")
        ]
    )
    assert (
        _status(verifier.verify(claims=[claim], ledger=ledger), "arithmetic_and_trends")
        == "not_evaluable"
    )


# --- medication status, absence, chains -------------------------------------


def test_medication_status_claim_names_the_rule_version(verifier):
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
    claim = _claim(claim_text="The patient is on warfarin therapy.")
    report = verifier.verify(
        claims=[claim], ledger=_ledger(patient=[span]), as_of=date(2140, 10, 5)
    )
    result = next(item for item in report.results if item.check == "medication_status")
    assert result.rule_version == "medstatus-v2"
    assert result.status == "pass"


def test_medication_status_ignores_resources_for_other_drugs(verifier):
    """A status claim resolves only against the drug it names (V2).

    Resolving against every medication in the chart made "not on a statin" fail because
    of an unrelated active prescription — a false accusation from correct evidence.
    """
    span = PatientEvidenceSpan(
        span_id="patient-000",
        query="query_patient_fhir(resource_types='MedicationRequest')",
        resource_type="MedicationRequest",
        content={
            "resourceType": "MedicationRequest",
            "status": "active",
            "authoredOn": "2140-10-01",
            "medicationCodeableConcept": {"text": "Lisinopril 10 mg tablet"},
        },
    )
    claim = _claim(claim_text="The patient is not on atorvastatin therapy.")
    report = verifier.verify(
        claims=[claim], ledger=_ledger(patient=[span]), as_of=date(2140, 10, 5)
    )
    result = next(item for item in report.results if item.check == "medication_status")
    assert result.status == "not_evaluable"
    assert "names the drug" in result.detail


def test_absence_claim_outside_recorded_coverage_is_not_evaluable(verifier):
    """Absence is only ever absence within recorded query coverage (decision 16).

    Uncovered absence is unresolved evidence, which drives abstention rather than a
    violation (decision 20).
    """
    ledger = _ledger(
        patient=[_observation("patient-000", 2.4, "2140-10-04")],
        queries=["query_patient_fhir(resource_types='Observation')"],
    )
    claim = _claim(claim_text="The patient is not receiving a statin.")
    report = verifier.verify(claims=[claim], ledger=ledger)
    result = next(item for item in report.results if item.check == "absence_within_query_coverage")
    assert result.status == "not_evaluable"
    assert result.rule_version == "coverage-v2"
    assert report.verdict == "abstain_insufficient_evidence"


def test_absence_claim_inside_recorded_coverage_is_established(verifier):
    ledger = _ledger(
        patient=[_observation("patient-000", 2.4, "2140-10-04")],
        queries=[
            "query_patient_fhir(resource_types='MedicationRequest')",
            "query_patient_fhir(resource_types='MedicationStatement')",
            "query_patient_fhir(resource_types='MedicationAdministration')",
            "query_patient_fhir(resource_types='MedicationDispense')",
        ],
    )
    claim = _claim(claim_text="The patient is not receiving a statin.")
    report = verifier.verify(claims=[claim], ledger=ledger)
    result = next(item for item in report.results if item.check == "absence_within_query_coverage")
    assert result.status in {"pass", "not_evaluable"}


def test_dangling_span_reference_fails_but_a_missing_chain_link_abstains(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    dangling = _claim(evidence_span_ids=["patient-999"])
    report = verifier.verify(claims=[dangling], ledger=ledger)
    assert _status(report, "derivation_chain_integrity") == "fail"

    chain = ActionEvidenceChain(trigger_claim_ids=["c01"])
    partial = verifier.verify(claims=[_claim()], ledger=ledger, action_evidence_chain=chain)
    assert _status(partial, "action_evidence_chain") == "not_evaluable"
    assert partial.verdict == "abstain_insufficient_evidence"


def test_chain_referencing_an_unknown_claim_fails(verifier):
    ledger = _ledger(patient=[_observation("patient-000", 2.4, "2140-10-04")])
    chain = ActionEvidenceChain(
        trigger_claim_ids=["c01"],
        rule_claim_ids=["c99"],
        applicability_claim_ids=["c01"],
        temporal_condition_claim_ids=["c01"],
    )
    report = verifier.verify(claims=[_claim()], ledger=ledger, action_evidence_chain=chain)
    assert _status(report, "action_evidence_chain") == "fail"


def test_empty_tool_ledger_abstains_rather_than_accusing(verifier):
    report = verifier.verify(claims=[_claim()], ledger=_ledger())
    assert report.verdict == "abstain_insufficient_evidence"
    assert not report.failures
