"""Tests for the contract-enforcing steering handler.

The handler is code-based: every Proceed/Guide decision comes from the deterministic
verifier, so these tests drive ``steer_after_model`` directly with a populated ledger
context instead of calling a model.
"""

from __future__ import annotations

import inspect

import pytest
from strands.vended_plugins.steering import Guide, Proceed

from medical_nudging.evidence_contract import (
    ContractEnforcingSteeringHandler,
    DeterministicVerifier,
    GeneratorConfiguration,
    extract_drafted_nudges,
)
from medical_nudging.evidence_contract import steering as steering_module

PASSAGE_TEXT = (
    "=== SRC_2025 ===\n"
    "Section: Synopsis\n"
    "Page: 18\n"
    "\n"
    "High-intensity statin therapy is recommended after revascularization.\n"
)

PATIENT_TEXT = (
    '{"resourceType": "Observation", "code": {"text": "LDL"}, '
    '"valueQuantity": {"value": 3.4, "unit": "mmol/L"}, '
    '"effectiveDateTime": "2140-10-04T09:00:00"}'
)


def _tool_call(tool_name: str, text: str, args: dict | None = None) -> dict:
    return {
        "tool_name": tool_name,
        "tool_args": args or {},
        "status": "success",
        "result": [{"text": text}],
        "completion_timestamp": "2140-10-05T10:00:00",
    }


def _nudge(section: str, page: int, rationale: str) -> dict:
    return {
        "title": "Start high-intensity statin",
        "description": "Order a high-intensity statin before discharge.",
        "rationale": rationale,
        "grounding": "guideline",
        "guideline_citation": {
            "source": "SRC_2025",
            "section": section,
            "page_number": page,
        },
    }


def _message(nudges: list[dict], tool_name: str = "GeneratedNudgeOutput") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"toolUse": {"toolUseId": "t1", "name": tool_name, "input": {"nudges": nudges}}}
        ],
    }


def _handler(retry_cap: int = 3) -> ContractEnforcingSteeringHandler:
    handler = ContractEnforcingSteeringHandler(
        config=GeneratorConfiguration(
            config_version="test-generator-v1",
            retry_cap=retry_cap,
            corpus_version="test-corpus",
            model_id="test-model",
        ),
        verifier=DeterministicVerifier(),
    )
    handler.steering_context.data.set(
        "ledger",
        {
            "tool_calls": [
                _tool_call("query_patient_fhir", PATIENT_TEXT, {"resource_types": "Observation"}),
                _tool_call("search_guidelines", PASSAGE_TEXT, {"query": "statin after CABG"}),
            ]
        },
    )
    return handler


def test_handler_module_contains_no_llm():
    """No prompt to a model, no judge, no client inside the handler."""
    source = inspect.getsource(steering_module)
    for forbidden in ("boto3", "BedrockModel", "invoke_model", "converse", "structured_output"):
        assert forbidden not in source, f"{forbidden!r} appears in the steering module"


def test_extract_drafted_nudges_reads_structured_output_tool_input():
    message = _message([_nudge("Synopsis", 18, "No statin is recorded.")])
    assert len(extract_drafted_nudges(message, "GeneratedNudgeOutput")) == 1
    assert extract_drafted_nudges(message, "OtherTool") == []
    assert (
        extract_drafted_nudges({"role": "assistant", "content": []}, "GeneratedNudgeOutput") == []
    )


def test_tool_ledger_is_built_from_raw_tool_results_only():
    handler = _handler()
    ledger = handler.build_tool_ledger()
    assert [span.resource_type for span in ledger.patient_evidence] == ["Observation"]
    assert ledger.guideline_evidence[0].section == "Synopsis"
    assert ledger.query_coverage().coverage_rule_version == "coverage-v2"
    assert len(ledger.queries_executed) == 2


@pytest.mark.asyncio
async def test_no_drafted_nudge_proceeds_without_recording_a_verification():
    handler = _handler()
    action = await handler.steer_after_model(
        agent=None,
        message={"role": "assistant", "content": [{"text": "thinking"}]},
        stop_reason="end_turn",
    )
    assert isinstance(action, Proceed)
    assert handler.guided_attempts == 0
    assert handler.outcome_record()["verified_draft_count"] == 0


@pytest.mark.asyncio
async def test_fabricated_citation_guides_a_retry_with_the_specific_failure():
    handler = _handler()
    action = await handler.steer_after_model(
        agent=None,
        message=_message([_nudge("Discharge planning", 62, "LDL is 3.4 mmol/L on 2140-10-04.")]),
        stop_reason="tool_use",
    )
    assert isinstance(action, Guide)
    assert "passage_identity" in action.reason
    assert "Discharge planning" in action.reason
    assert handler.guided_attempts == 1
    assert handler.attempts[-1].verdict == "violates_contract"
    assert not handler.contract_failures_unresolved


@pytest.mark.asyncio
async def test_supported_draft_proceeds():
    handler = _handler()
    action = await handler.steer_after_model(
        agent=None,
        message=_message([_nudge("Synopsis", 18, "LDL is 3.4 mmol/L on 2140-10-04.")]),
        stop_reason="tool_use",
    )
    assert isinstance(action, Proceed)
    assert handler.guided_attempts == 0
    assert not handler.contract_failures_unresolved


@pytest.mark.asyncio
async def test_retry_cap_emits_a_flagged_excluded_source_nudge():
    handler = _handler(retry_cap=2)
    message = _message([_nudge("Discharge planning", 62, "LDL is 3.4 mmol/L on 2140-10-04.")])
    actions = [
        await handler.steer_after_model(agent=None, message=message, stop_reason="tool_use")
        for _ in range(4)
    ]
    assert [type(action).__name__ for action in actions] == [
        "Guide",
        "Guide",
        "Proceed",
        "Proceed",
    ]
    assert handler.guided_attempts == 2
    assert handler.contract_failures_unresolved
    outcome = handler.outcome_record()
    assert outcome["flags"] == ["contract_failures_unresolved"]
    assert outcome["final_verdict"] == "violates_contract"
    # The failures are recorded rather than dropped, so attrition stays reportable.
    assert outcome["attempts"][-1]["failures"]


@pytest.mark.asyncio
async def test_empty_tool_ledger_proceeds_rather_than_guiding():
    handler = _handler()
    handler.steering_context.data.set("ledger", {"tool_calls": []})
    action = await handler.steer_after_model(
        agent=None,
        message=_message([_nudge("Discharge planning", 62, "LDL is 9.9 mmol/L.")]),
        stop_reason="tool_use",
    )
    assert isinstance(action, Proceed)
    assert handler.attempts[-1].verdict == "abstain_insufficient_evidence"


def test_generator_configuration_records_verifier_rule_provenance():
    handler = _handler()
    config = handler.config.as_dict(handler.verifier)
    assert config["config_version"] == "test-generator-v1"
    assert config["retry_cap"] == 3
    assert config["verifier_rule_versions"]["medication_status_rule_version"] == "medstatus-v2"
    assert set(config["verifier_rule_content_hashes"]) == {
        "medication_status_rule_v2.json",
        "query_coverage_rule_v2.json",
    }


def test_reset_clears_recorded_attempts_between_patients():
    handler = _handler()
    handler.attempts.append(steering_module.SteeringAttempt(attempt=1, action="guide", verdict="x"))
    handler.reset()
    assert handler.attempts == []
    assert handler.guided_attempts == 0


def test_raw_channel_scope_prevents_cross_patient_contamination():
    from medical_nudging.tools.raw_resource_channel import capture_raw_resources, default_channel

    with capture_raw_resources():
        default_channel().publish(
            tool_name="query_patient_fhir",
            query="MedicationRequest?patient=patient-a",
            resources=[{"resourceType": "MedicationRequest", "id": "patient-a-statin"}],
        )
        assert any(
            "patient-a-statin" in str(span.content)
            for span in _handler().build_tool_ledger().patient_evidence
        )
    assert not any(
        "patient-a-statin" in str(span.content)
        for span in _handler().build_tool_ledger().patient_evidence
    )
    default_channel().publish(tool_name="test", query="x", resources=[{"id": "unscoped"}])
    assert default_channel().is_empty


@pytest.mark.asyncio
async def test_require_citation_guides_a_draft_without_guideline_citation():
    """With require_citation, a citation-less draft is guided even when the verifier
    establishes no contract failure — missing provenance is abstention for the
    contract, but a generation requirement for the source set."""
    import dataclasses

    nudge = _nudge("Synopsis", 18, "No statin is recorded.")
    nudge["guideline_citation"] = None

    handler = _handler()
    handler.config = dataclasses.replace(handler.config, require_citation=True)
    action = await handler.steer_after_model(
        agent=None,
        message=_message([nudge]),
        stop_reason="tool_use",
    )
    assert isinstance(action, Guide)
    assert any("generation_requirement" in f for f in handler.attempts[-1].failures)
    assert not handler.nudge_outcome_record(0)["flags"]
    for _ in range(handler.config.retry_cap):
        action = await handler.steer_after_model(
            agent=None, message=_message([nudge]), stop_reason="tool_use"
        )
    assert isinstance(action, Proceed)
    outcome = handler.nudge_outcome_record(0)
    assert outcome["flags"] == ["contract_failures_unresolved"]
    assert outcome["generation_requirement_failures"]
    assert outcome["deterministic_verdict"] == "abstain_insufficient_evidence"

    handler = _handler()
    action = await handler.steer_after_model(
        agent=None,
        message=_message([nudge]),
        stop_reason="tool_use",
    )
    assert isinstance(action, Proceed)
