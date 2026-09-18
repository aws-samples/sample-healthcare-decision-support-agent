"""Contract behaviour of the AgentCore candidate-acceptance gate and dataset tooling.

The synthetic records here carry no patient data. ``make_record`` builds a run record in
the shape the runtime's ``return_evidence`` path returns, passing every Layer 1 gate, so
each test changes exactly one thing and shows which check catches it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evals import agentcore_dataset, agentcore_gate
from evals.agentcore_gate import (
    GateError,
    build_payload,
    configuration_drift,
    dataset_fingerprint,
    evaluate_gate,
    scenario_plan,
)

CONFIG_PATH = Path("config/evals/blog_opus5.json")
THRESHOLDS_PATH = Path("config/evals/regression_thresholds_v1.json")
DATASET_PATH = Path("config/evals/regression_dataset_v1.json")
KNOWN_BAD_PATH = Path("tests/fixtures/evals/known_bad_records.json")

SOURCE = "Example Sepsis Guideline 2021"
PASSAGE = "Review lactate after initial resuscitation."
PATIENT_ID = "synthetic-patient-0001"
SUMMARY = (
    "Adult inpatient admitted with suspected sepsis and an elevated initial lactate. "
    "Retrieved records show intravenous fluids started on admission, a repeat lactate "
    "pending, and no documented allergy to first-line antibiotics within the retrieved "
    "window. Renal function is stable across the two retrieved creatinine results."
)


def arm_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def thresholds() -> dict:
    return json.loads(THRESHOLDS_PATH.read_text())


def make_nudge(source: str = SOURCE) -> dict:
    return {
        "title": "Review repeat lactate",
        "description": PASSAGE,
        "urgency": "informational",
        "category": "clinical_monitoring",
        "nudge_type": "standard_of_care",
        "action_type": ["assessment"],
        "rationale": PASSAGE,
        "grounding": "guideline",
        "guideline_citation": {
            "source": source,
            "section": "Initial resuscitation",
            "page_number": 3,
        },
        "icd_codes": [],
        "codes_validated": False,
    }


def make_record(
    *,
    cited_source: str = SOURCE,
    status: str = "success",
    latency_ms: int = 120_000,
    total_tokens: int = 40_000,
    config: dict | None = None,
) -> dict:
    """A run record shaped like the runtime's evidence block plus its response."""
    config = copy.deepcopy(config or arm_config())
    nudge = make_nudge(cited_source)
    search_text = f"=== {SOURCE} ===\n{PASSAGE}"
    trace = {
        "system_prompt": "",
        "user_prompt": "",
        "tool_calls": [
            {
                "tool_name": "query_patient_fhir",
                "tool_use_id": "t-fhir-1",
                "input_params": {"resource_type": "Observation"},
                "output": "1 Observation",
                "duration_ms": 40,
                "completed": True,
                "success": True,
                "error": None,
            },
            {
                "tool_name": "search_guidelines",
                "tool_use_id": "t-search-1",
                "input_params": {"query": "lactate resuscitation"},
                "output": search_text,
                "duration_ms": 30,
                "completed": True,
                "success": True,
                "error": None,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t-fhir-1",
                            "status": "success",
                            "content": [{"text": "1 Observation"}],
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t-search-1",
                            "status": "success",
                            "content": [{"text": search_text}],
                        }
                    }
                ],
            },
        ],
        "raw_response": None,
        "token_usage": {
            "input_tokens": total_tokens - 2_000,
            "output_tokens": 2_000,
            "total_tokens": total_tokens,
        },
        "timing": {"total_ms": latency_ms, "tool_ms": 70, "model_ms": latency_ms - 70},
    }
    response = {
        "status": status,
        "warnings": [],
        "error": None if status != "error" else "Nudge generation failed",
        "key_findings": [
            "Elevated initial lactate on admission",
            "Intravenous fluids started on admission",
            "Repeat lactate pending in retrieved records",
        ],
        "patient_summary": SUMMARY,
        "nudges": [nudge],
        "metadata": {"model_version": config["model"]["model_id"]},
    }
    return {
        "patient_id": PATIENT_ID,
        "generation_settings": {
            **{key: config.get(key) for key in ("model", "agent", "generator_config", "specialty")},
            "transport": {"read_timeout": 600, "retries": {"max_attempts": 2}},
        },
        "response_status": status,
        "response": response,
        "trace": trace,
        "run_metrics": {
            **trace["token_usage"],
            "latency_ms": latency_ms,
            "tool_duration_ms": 70,
            "tool_call_counts": {"query_patient_fhir": 1, "search_guidelines": 1},
        },
        "tool_ledger": {
            "patient_evidence": [
                {
                    "span_id": "p1",
                    "query": "Observation?patient=synthetic&code=lactate",
                    "resource_type": "Observation",
                    "content": {
                        "resourceType": "Observation",
                        "id": "obs-1",
                        "code": {"text": "Lactate"},
                        "valueQuantity": {"value": 3.1, "unit": "mmol/L"},
                    },
                    "retrieved_at": None,
                }
            ],
            "guideline_evidence": [
                {
                    "span_id": "g1",
                    "chunk_id": "c1",
                    "source": SOURCE,
                    "content": PASSAGE,
                    "section": "Initial resuscitation",
                    "page_number": 3,
                }
            ],
            "patient_evidence_fidelity": "raw_fhir",
            "fidelity_reasons": [],
            "absent_resource_types": [],
        },
        "query_coverage": {
            "queries_executed": ["Observation?patient=synthetic&code=lactate"],
            "coverage_rule_version": "synthetic",
            "truncated_resource_types": [],
        },
        "patient_steering_outcome": {
            "attempts": [],
            "generator_config": config["generator_config"],
        },
        "nudges": [
            {
                "nudge_idx": 0,
                "nudge": nudge,
                "steering_outcome": {"flags": []},
                "deterministic_verifier_report_source": "steering",
            }
        ],
        "excluded_nudge_count": 0,
        "completed_at": "2026-09-14T00:00:00+00:00",
        "runtime": {
            "image_tag": "synthetic",
            "stack_name": "synthetic",
            "opensearch_index": config["generator_config"]["corpus_version"],
            "fhir_max_pages": config["generator_config"]["fhir_max_pages"],
            "fhir_api_enabled": True,
        },
        "invocation": {"session_id": "session-synthetic-0001"},
    }


def make_plan(config: dict, scenario_id: str = "synthetic-1") -> list[dict]:
    return [
        {
            "scenario_id": scenario_id,
            "patient_id": PATIENT_ID,
            "visit_context": {"patient_id": PATIENT_ID, "data_source": "fhir_api"},
            "assertions": [],
            "metadata": {
                "dataset_version": "synthetic-v1",
                **{key: config[key] for key in ("model", "agent", "generator_config")},
            },
        }
    ]


def test_summary_fixture_is_within_reviewer_limits():
    assert 40 <= len(SUMMARY.split()) <= 70


def test_clean_record_passes_gate():
    config = arm_config()
    result = evaluate_gate(
        plan=make_plan(config),
        records={"synthetic-1": make_record(config=config)},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
    )
    assert result["gate"] == {"passed": True, "failures": []}
    row = result["scenarios"][0]
    assert row["completion"] and row["contract_violations"] == 0
    assert row["failed_checks"] == [] and row["configuration_drift"] == []
    assert result["thresholds"]["completion_rate"]["value"] == 1.0


def test_fabricated_citation_fails_gate_on_evidence_contract():
    config = arm_config()
    record = make_record(cited_source="Fabricated Consensus Statement 2019", config=config)
    result = evaluate_gate(
        plan=make_plan(config),
        records={"synthetic-1": record},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
    )
    assert not result["gate"]["passed"]
    row = result["scenarios"][0]
    assert row["contract_violations"] >= 1
    labels = {check["label"] for check in row["failed_checks"]}
    assert any("citation_resolution:violates_contract" in label for label in labels)
    assert not result["thresholds"]["contract_violations"]["passed"]


def test_impossible_threshold_fails_gate():
    config = arm_config()
    limits = {**thresholds(), "latency_ms": 1}
    result = evaluate_gate(
        plan=make_plan(config),
        records={"synthetic-1": make_record(config=config)},
        agentcore_results={},
        config=config,
        thresholds=limits,
    )
    assert not result["gate"]["passed"]
    assert any("LatencyBudget:over_budget" in failure for failure in result["gate"]["failures"])


def test_failed_run_breaks_completion_threshold():
    config = arm_config()
    record = make_record(status="error", config=config)
    result = evaluate_gate(
        plan=make_plan(config),
        records={"synthetic-1": record},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
    )
    assert not result["gate"]["passed"]
    assert result["thresholds"]["completion_rate"]["value"] == 0.0
    assert not result["thresholds"]["completion_rate"]["passed"]


def test_missing_record_is_a_gate_failure():
    config = arm_config()
    result = evaluate_gate(
        plan=make_plan(config),
        records={},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
    )
    assert not result["gate"]["passed"]
    assert result["scenarios"][0]["status"] == "not_invoked"


def test_configuration_drift_is_detected_per_pinned_key():
    config = arm_config()
    record = make_record(config=config)
    record["generation_settings"]["model"]["model_id"] = "us.anthropic.claude-sonnet-5"
    record["runtime"]["fhir_max_pages"] = 5
    record["runtime"]["opensearch_index"] = "guidelines"
    drift = configuration_drift(record, make_plan(config)[0]["metadata"])
    assert any(line.startswith("model.model_id") for line in drift)
    assert any(line.startswith("generator_config.fhir_max_pages") for line in drift)
    assert any(line.startswith("runtime.opensearch_index") for line in drift)
    result = evaluate_gate(
        plan=make_plan(config),
        records={"synthetic-1": record},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
    )
    assert not result["gate"]["passed"]


def test_agentcore_scores_are_recorded_and_only_gate_when_asked():
    config = arm_config()

    class FakeEvaluatorResult:
        evaluator_id = "Builtin.GoalSuccessRate"
        results = [{"value": 0.5, "label": "PARTIAL", "explanation": "half"}]

    class FakeScenarioResult:
        scenario_id = "synthetic-1"
        session_id = "session-synthetic-0001"
        status = "COMPLETED"
        error = None
        evaluator_results = [FakeEvaluatorResult()]

    common = dict(
        plan=make_plan(config),
        records={"synthetic-1": make_record(config=config)},
        agentcore_results={"synthetic-1": FakeScenarioResult()},
        config=config,
    )
    informational = evaluate_gate(**common, thresholds=thresholds())
    assert informational["gate"]["passed"]
    assert informational["agentcore_evaluator_summary"]["Builtin.GoalSuccessRate"]["mean"] == 0.5
    gated = evaluate_gate(
        **common,
        thresholds={**thresholds(), "min_agentcore_scores": {"Builtin.GoalSuccessRate": 0.9}},
    )
    assert not gated["gate"]["passed"]


def test_scenario_plan_uses_checked_in_dataset_and_rejects_config_drift():
    dataset = json.loads(DATASET_PATH.read_text())
    config = arm_config()
    plan = scenario_plan(dataset["scenarios"], config)
    assert len(plan) == 8
    assert len({item["patient_id"] for item in plan}) == 8
    assert {item["metadata"]["dataset_version"] for item in plan} == {"blog-regression-v1"}
    other = copy.deepcopy(config)
    other["model"]["model_id"] = "us.anthropic.claude-sonnet-5"
    with pytest.raises(GateError, match="differs from the arm config"):
        scenario_plan(dataset["scenarios"], other)


def test_dataset_fingerprint_is_order_and_path_independent_of_loader():
    scenarios = json.loads(DATASET_PATH.read_text())["scenarios"]
    assert dataset_fingerprint(scenarios) == dataset_fingerprint(copy.deepcopy(scenarios))
    assert dataset_fingerprint(scenarios) != dataset_fingerprint(scenarios[:-1])


def test_payload_pins_frozen_generator_and_requests_evidence():
    config = arm_config()
    payload = build_payload({"patient_id": "p", "data_source": "fhir_api"}, config)
    assert payload["config"]["return_evidence"] is True
    assert payload["config"]["model_version"] == config["generator_config"]["model_id"]
    assert payload["config"]["generator_config"] == config["generator_config"]
    assert payload["config"]["max_nudges"] == config["agent"]["max_nudges"]
    assert payload["visit_context"]["patient_id"] == "p"


def test_known_bad_fixture_fails_replay_gate(tmp_path):
    exit_code = agentcore_gate.main(
        [
            "--config",
            str(CONFIG_PATH),
            "--thresholds",
            str(THRESHOLDS_PATH),
            "--replay-records",
            str(KNOWN_BAD_PATH),
            "--output-dir",
            str(tmp_path / "gate"),
        ]
    )
    assert exit_code == 1
    result = json.loads((tmp_path / "gate" / "result.json").read_text())
    assert result["metadata"]["mode"] == "replay"
    assert result["metadata"]["dataset"]["kind"] == "replay_records"
    assert not result["gate"]["passed"]
    assert any("citation_resolution:violates_contract" in f for f in result["gate"]["failures"])
    assert result["metadata"]["evaluators"]["deterministic_set"] == "layers-v1"
    assert result["metadata"]["evaluators"]["rule_hashes"]


def test_replay_gate_passes_clean_records_and_refuses_repo_output(tmp_path):
    config = arm_config()
    records = [
        {"scenario_id": "clean-1", "patient_id": PATIENT_ID, "record": make_record(config=config)}
    ]
    path = tmp_path / "records.json"
    path.write_text(json.dumps(records))
    common = [
        "--config",
        str(CONFIG_PATH),
        "--thresholds",
        str(THRESHOLDS_PATH),
        "--replay-records",
        str(path),
    ]
    assert agentcore_gate.main([*common, "--output-dir", str(tmp_path / "out")]) == 0
    assert agentcore_gate.main([*common, "--output-dir", "results/gate"]) == 2


def test_dataset_publish_helpers_read_checked_in_file():
    scenarios = agentcore_dataset.load_scenarios(DATASET_PATH)
    assert len(scenarios) == 8
    assert agentcore_dataset.dataset_version_label(scenarios) == "blog-regression-v1"
    examples = agentcore_dataset.inline_examples(scenarios)
    assert all({"scenario_id", "turns", "assertions", "metadata"} <= set(e) for e in examples)


@pytest.fixture
def client():
    from agent import app

    return TestClient(app)


def test_runtime_evidence_path_returns_evidence_block(client, monkeypatch):
    import medical_nudging.steered_generation as steered

    config = arm_config()
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)
        return make_record(config=config)

    monkeypatch.setattr(steered, "steered_generation_record", fake_record)
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", config["generator_config"]["corpus_version"])
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    monkeypatch.setenv("IMAGE_TAG", "test-tag")
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)
    response = client.post("/invocations", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"output", "evidence", "runtime"}
    assert body["output"]["status"] == "success"
    assert "response" not in body["evidence"]
    assert body["evidence"]["tool_ledger"]["guideline_evidence"][0]["source"] == SOURCE
    assert body["runtime"]["image_tag"] == "test-tag"
    assert body["runtime"]["opensearch_index"] == config["generator_config"]["corpus_version"]
    assert captured["request_config"]["model_version"] == config["generator_config"]["model_id"]
    assert captured["generator_config"] == config["generator_config"]
    assert captured["data_source"] == "fhir_api"


def _assemble_sse(text: str):
    from medical_nudging.evidence_stream import EvidenceStreamAssembler

    return EvidenceStreamAssembler().feed(text.split("\n"))


def test_runtime_streams_evidence_when_the_client_accepts_event_stream(client, monkeypatch):
    import time

    import medical_nudging.evidence_stream as stream
    import medical_nudging.steered_generation as steered

    config = arm_config()

    def fake_record(**kwargs):
        time.sleep(0.25)
        return make_record(config=config)

    monkeypatch.setattr(steered, "steered_generation_record", fake_record)
    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(stream, "CHUNK_BYTES", 2048)
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", config["generator_config"]["corpus_version"])
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    monkeypatch.setenv("IMAGE_TAG", "test-tag")
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)

    plain = client.post("/invocations", json=payload).json()
    response = client.post("/invocations", json=payload, headers={"accept": "text/event-stream"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: keepalive") >= 1
    assert response.text.count("event: chunk") >= 2
    body = _assemble_sse(response.text)
    assert set(body) == {"output", "evidence", "runtime"}
    assert body["evidence"] == plain["evidence"] | {
        "completed_at": body["evidence"]["completed_at"]
    }
    assert body["runtime"]["image_tag"] == "test-tag"


def test_runtime_streaming_refusal_is_still_a_409(client, monkeypatch):
    config = arm_config()
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", "guidelines")
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)
    response = client.post("/invocations", json=payload, headers={"accept": "text/event-stream"})
    assert response.status_code == 409
    assert "frozen on corpus" in response.json()["detail"]


def test_runtime_streams_a_generation_failure_as_an_error_event(client, monkeypatch):
    import medical_nudging.steered_generation as steered

    config = arm_config()

    def failing_record(**kwargs):
        raise RuntimeError("bedrock exploded")

    monkeypatch.setattr(steered, "steered_generation_record", failing_record)
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", config["generator_config"]["corpus_version"])
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)
    response = client.post("/invocations", json=payload, headers={"accept": "text/event-stream"})
    assert response.status_code == 200
    assert "event: error" in response.text
    from medical_nudging.evidence_stream import EvidenceStreamError

    with pytest.raises(EvidenceStreamError, match="HTTP 500.*bedrock exploded"):
        _assemble_sse(response.text)


def test_runtime_refuses_evidence_request_on_corpus_mismatch(client, monkeypatch):
    config = arm_config()
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", "guidelines")
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)
    response = client.post("/invocations", json=payload)
    assert response.status_code == 409
    assert "frozen on corpus" in response.json()["detail"]


def test_plain_invocation_ignores_evidence_fields_by_default():
    from agent import InvocationRequest

    request = InvocationRequest.model_validate(
        {"visit_context": {"data_source": "fhir_api", "patient_id": "p"}}
    )
    assert request.config.return_evidence is False
    assert request.config.generator_config is None


# ---------------------------------------------------------------------------
# Configuration bundles: the prompt under test is pinned and recorded (B-21)
# ---------------------------------------------------------------------------

BUNDLE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:configuration-bundle/prompt-a1b2c3d4e5"
)
BUNDLE_VERSION = "11111111-1111-1111-1111-111111111111"


def _bundle_prompt_record(version: str | None, prompt_hash: str = "abc") -> dict:
    record = make_record()
    if version is None:
        record["prompt"] = {
            "prompt_source": "repository",
            "base_prompt_sha256": prompt_hash,
            "config_bundle": None,
        }
    else:
        record["prompt"] = {
            "prompt_source": "config_bundle",
            "base_prompt_sha256": prompt_hash,
            "config_bundle": {"bundle_id": "prompt-a1b2c3d4e5", "bundle_version": version},
        }
    return record


def test_prompt_drift_requires_the_pinned_bundle_version_and_hash():
    pinned = {
        "bundle_id": "prompt-a1b2c3d4e5",
        "bundle_version": BUNDLE_VERSION,
        "system_prompt_sha256": "abc",
    }
    assert agentcore_gate.prompt_drift(_bundle_prompt_record(BUNDLE_VERSION), pinned) == []
    other = agentcore_gate.prompt_drift(
        _bundle_prompt_record("22222222-2222-2222-2222-222222222222"), pinned
    )
    assert any("bundle_version" in line for line in other)
    wrong_hash = agentcore_gate.prompt_drift(_bundle_prompt_record(BUNDLE_VERSION, "zzz"), pinned)
    assert any("base_prompt_sha256" in line for line in wrong_hash)
    repository = agentcore_gate.prompt_drift(_bundle_prompt_record(None), pinned)
    assert any("expected the pinned configuration bundle" in line for line in repository)
    legacy = agentcore_gate.prompt_drift(make_record(), pinned)
    assert legacy == ["prompt: runtime reported no prompt provenance; a bundle version was pinned"]


def test_prompt_drift_flags_an_unpinned_bundle_and_accepts_repository_prompt():
    assert agentcore_gate.prompt_drift(_bundle_prompt_record(None), None) == []
    assert agentcore_gate.prompt_drift(make_record(), None) == []
    drift = agentcore_gate.prompt_drift(_bundle_prompt_record(BUNDLE_VERSION), None)
    assert drift and "but none was pinned" in drift[0]


def test_gate_fails_on_prompt_drift_and_records_it_per_scenario():
    config = arm_config()
    dataset = json.loads(DATASET_PATH.read_text())
    plan = agentcore_gate.scenario_plan(dataset["scenarios"], config)[:1]
    scenario_id = plan[0]["scenario_id"]
    pinned = {"bundle_id": "prompt-a1b2c3d4e5", "bundle_version": BUNDLE_VERSION}
    verdict = agentcore_gate.evaluate_gate(
        plan=plan,
        records={scenario_id: _bundle_prompt_record(None)},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
        expected_bundle=pinned,
    )
    assert verdict["gate"]["passed"] is False
    assert any("prompt" in line for line in verdict["scenarios"][0]["configuration_drift"])
    clean = agentcore_gate.evaluate_gate(
        plan=plan,
        records={scenario_id: _bundle_prompt_record(BUNDLE_VERSION)},
        agentcore_results={},
        config=config,
        thresholds=thresholds(),
        expected_bundle=pinned,
    )
    assert clean["gate"]["passed"] is True


def test_runtime_invoker_sends_the_bundle_as_baggage(monkeypatch):
    import boto3

    calls: list[dict] = []

    class FakeClient:
        def invoke_agent_runtime(self, **request):
            calls.append(request)
            body = {
                "output": {"status": "success", "nudges": []},
                "evidence": {"patient_id": "p"},
                "runtime": {},
            }

            class Body:
                def read(self):
                    return json.dumps(body).encode()

            return {"response": Body()}

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def client(self, *args, **kwargs):
            return FakeClient()

    monkeypatch.setattr(boto3, "Session", FakeSession)
    baggage = f"aws.agentcore.configbundle_arn={BUNDLE_ARN},aws.agentcore.configbundle_version={BUNDLE_VERSION}"
    invoker = agentcore_gate.RuntimeInvoker(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r-1",
        region="us-east-1",
        config=arm_config(),
        baggage=baggage,
    )

    class Input:
        session_id = "s-1"
        payload = {"visit_context": {"patient_id": PATIENT_ID, "data_source": "fhir_api"}}

    invoker(Input())
    assert calls[0]["baggage"] == baggage
    assert invoker.records["s-1"]["invocation"]["baggage"] == baggage


def test_runtime_invoker_reads_a_streamed_evidence_response(monkeypatch):
    import boto3

    from medical_nudging.evidence_stream import keepalive_event, payload_events

    calls: list[dict] = []
    body = {
        "output": {"status": "success", "nudges": []},
        "evidence": {"patient_id": "p", "big": "z" * 5000},
        "runtime": {"image_tag": "t"},
    }
    text = (
        keepalive_event(15) + keepalive_event(30) + "".join(payload_events(body, chunk_bytes=1000))
    )

    class Body:
        def iter_lines(self):
            for line in text.split("\n"):
                yield line.encode()

        def read(self):  # pragma: no cover - the streaming path must not call this
            raise AssertionError("read() called on a streamed response")

    class FakeClient:
        def invoke_agent_runtime(self, **request):
            calls.append(request)
            return {"contentType": "text/event-stream; charset=utf-8", "response": Body()}

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def client(self, *args, **kwargs):
            return FakeClient()

    monkeypatch.setattr(boto3, "Session", FakeSession)
    invoker = agentcore_gate.RuntimeInvoker(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r-1",
        region="us-east-1",
        config=arm_config(),
    )

    class Input:
        session_id = "s-2"
        payload = {"visit_context": {"patient_id": PATIENT_ID, "data_source": "fhir_api"}}

    out = invoker(Input())
    assert calls[0]["accept"] == "text/event-stream"
    assert out.agent_output == body["output"]
    assert invoker.records["s-2"]["big"] == "z" * 5000
    assert invoker.records["s-2"]["runtime"] == {"image_tag": "t"}


def test_runtime_invoker_surfaces_a_streamed_error_as_gate_error(monkeypatch):
    import boto3

    from medical_nudging.evidence_stream import error_event, keepalive_event

    text = keepalive_event(15) + error_event(500, "Internal error: boom")

    class Body:
        def iter_lines(self):
            for line in text.split("\n"):
                yield line.encode()

    class FakeClient:
        def invoke_agent_runtime(self, **request):
            return {"contentType": "text/event-stream", "response": Body()}

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def client(self, *args, **kwargs):
            return FakeClient()

    monkeypatch.setattr(boto3, "Session", FakeSession)
    invoker = agentcore_gate.RuntimeInvoker(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r-1",
        region="us-east-1",
        config=arm_config(),
    )

    class Input:
        session_id = "s-3"
        payload = {"visit_context": {"patient_id": PATIENT_ID, "data_source": "fhir_api"}}

    with pytest.raises(agentcore_gate.GateError, match="HTTP 500.*boom"):
        invoker(Input())


def test_gate_requires_bundle_id_and_version_together(tmp_path):
    code = agentcore_gate.main(
        [
            "--config",
            "config/evals/blog_opus5.json",
            "--thresholds",
            "config/evals/regression_thresholds_v1.json",
            "--output-dir",
            str(tmp_path / "out"),
            "--runtime-arn",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r-1",
            "--bundle-id",
            "prompt-a1b2c3d4e5",
        ]
    )
    assert code == 2


def test_runtime_applies_the_bundle_named_in_baggage(client, monkeypatch):
    import medical_nudging.steered_generation as steered
    from medical_nudging import config_bundle as cb

    import agent as agent_module

    config = arm_config()
    runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r-1"
    version = {
        "bundleArn": BUNDLE_ARN,
        "bundleId": "prompt-a1b2c3d4e5",
        "versionId": BUNDLE_VERSION,
        "components": {runtime_arn: {"configuration": {"system_prompt": "Bundle prompt."}}},
        "lineageMetadata": {"branchName": "mainline", "commitMessage": "v1"},
    }

    class FakePlane:
        def get_configuration_bundle_version(self, bundleId, versionId):
            return version

    resolver = cb.ConfigBundleResolver(runtime_arn=runtime_arn, client=FakePlane())
    monkeypatch.setattr(agent_module, "_bundle_resolver", lambda: resolver)
    seen: dict = {}

    def fake_record(**kwargs):
        seen["prompt"] = cb.prompt_provenance()  # evaluated inside the request's bundle scope
        record = make_record(config=config)
        record["prompt"] = seen["prompt"]
        return record

    monkeypatch.setattr(steered, "steered_generation_record", fake_record)
    monkeypatch.setenv("OPENSEARCH_INDEX_NAME", config["generator_config"]["corpus_version"])
    monkeypatch.setenv("FHIR_MAX_PAGES", str(config["generator_config"]["fhir_max_pages"]))
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, config)
    baggage = f"aws.agentcore.configbundle_arn={BUNDLE_ARN},aws.agentcore.configbundle_version={BUNDLE_VERSION}"
    response = client.post("/invocations", json=payload, headers={"baggage": baggage})
    assert response.status_code == 200, response.text
    prompt = response.json()["runtime"]["prompt"]
    assert prompt["prompt_source"] == "config_bundle"
    assert prompt["config_bundle"]["bundle_version"] == BUNDLE_VERSION
    assert prompt["base_prompt_sha256"] == cb.prompt_sha256("Bundle prompt.")
    assert cb.active_bundle() is None  # scope released after the request

    # Without the header the repository prompt applies and is reported as such.
    response = client.post("/invocations", json=payload)
    assert response.json()["runtime"]["prompt"]["prompt_source"] == "repository"


def test_runtime_refuses_a_bundle_it_cannot_apply(client, monkeypatch):
    from medical_nudging import config_bundle as cb

    import agent as agent_module

    class FailingPlane:
        def get_configuration_bundle_version(self, bundleId, versionId):
            raise RuntimeError("AccessDeniedException")

    resolver = cb.ConfigBundleResolver(runtime_arn="arn:x/runtime/r-1", client=FailingPlane())
    monkeypatch.setattr(agent_module, "_bundle_resolver", lambda: resolver)
    payload = build_payload({"patient_id": PATIENT_ID, "data_source": "fhir_api"}, arm_config())
    baggage = f"aws.agentcore.configbundle_arn={BUNDLE_ARN},aws.agentcore.configbundle_version={BUNDLE_VERSION}"
    response = client.post("/invocations", json=payload, headers={"baggage": baggage})
    assert response.status_code == 409
    assert "could not fetch bundle" in response.json()["detail"]
