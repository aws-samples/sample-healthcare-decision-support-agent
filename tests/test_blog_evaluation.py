"""Contract behavior of the public evaluation runner and review interface."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands_evals import Case
from strands_evals.types.evaluation import EvaluationData

from evals.controls import control_summary, model_family
from evals.layers import LatencyBudget, TokenBudget, score_nudge
from evals.review.make_review_html import RUBRIC, build_html, load_rows
from evals.runner import Completion, run_experiment
from medical_nudging.evidence_contract import GuidelineEvidenceSpan, PatientEvidenceSpan, ToolLedger
from medical_nudging.evidence_contract.chart_state import check_redundant_order
from medical_nudging.evidence_contract.perturb import perturb_citation_fabrication
from medical_nudging.tools.raw_resource_channel import capture_raw_resources, default_channel


def cited_nudge():
    return {
        "title": "Review the recorded finding",
        "description": "Review the recorded finding.",
        "rationale": "Review the recorded finding.",
        "grounding": "guideline",
        "guideline_citation": {
            "source": "Example guideline",
            "section": "Review",
            "page_number": 2,
        },
    }


def cited_ledger():
    return ToolLedger(
        guideline_evidence=[
            GuidelineEvidenceSpan(
                span_id="g1",
                chunk_id="c1",
                source="Example guideline",
                section="Review",
                page_number=2,
                content="Review the recorded finding.",
            )
        ]
    )


def test_fabricated_source_is_detected_without_mutating_input():
    nudge, ledger = cited_nudge(), cited_ledger()
    original = copy.deepcopy(nudge)
    result = perturb_citation_fabrication(nudge, ledger)
    assert result.status == "constructed"
    assert score_nudge(result.nudge, ledger)["verdict"] == "violates_contract"
    assert nudge == original
    assert result.nudge["rationale"] == original["rationale"]


def test_missing_evidence_abstains():
    score = score_nudge(cited_nudge(), ToolLedger())
    assert score["verdict"] == "abstain_insufficient_evidence"
    assert score["no_violation"]


def test_factual_failure_remains_a_gate_failure_outside_highlighted_subset():
    from evals.layers import EvidenceContract

    ledger = ToolLedger(
        patient_evidence=[
            PatientEvidenceSpan(
                span_id="p1",
                query="Observation",
                resource_type="Observation",
                content={
                    "resourceType": "Observation",
                    "code": {"text": "Creatinine"},
                    "valueQuantity": {"value": 4, "unit": "mg/dL"},
                },
            )
        ]
    )
    nudge = {"title": "Review creatinine", "description": "Creatinine is 999 mg/dL."}
    score = score_nudge(nudge, ledger)
    assert score["full_verifier"]["verdict"] == "violates_contract"
    assert not score["no_violation"]
    record = {
        "tool_ledger": {"patient_evidence": [p.model_dump() for p in ledger.patient_evidence]},
        "nudges": [{"nudge_idx": 0, "nudge": nudge}],
    }
    results = EvidenceContract().evaluate(EvaluationData(input={}, actual_output=record))
    assert any(result.test_pass is False for result in results)


def test_replay_rejects_another_generator_arm():
    from evals.replay import validate_provenance

    config = json.loads(Path("config/evals/blog_sonnet5.json").read_text())
    record = {
        "patient_steering_outcome": {
            "generator_config": {
                **config["generator_config"],
                "model_id": "us.anthropic.claude-opus-5",
            }
        }
    }
    with pytest.raises(ValueError, match="model_id"):
        validate_provenance([record], config)


def test_saved_run_replay_reconciles_exclusions_and_rejects_old_timeout(tmp_path, monkeypatch):
    import hashlib
    from evals.compare import main as compare

    config = json.loads(Path("config/evals/blog_sonnet5.json").read_text())
    config["patients"] = config["patients"][:1]
    saved_config = copy.deepcopy(config)
    del saved_config["model"]["read_timeout"]
    generator = config["generator_config"]
    cited = cited_nudge()
    uncited = {**cited, "guideline_citation": None}
    record = {
        "patient_id": config["patients"][0]["patient_id"],
        "response_status": "success",
        "response": {"status": "success", "nudges": [cited, uncited]},
        "generation_settings": {
            key: saved_config[key] for key in ("model", "agent", "generator_config", "specialty")
        },
        "patient_steering_outcome": {
            "generator_config": {
                **generator,
                "custom_instructions_hash": hashlib.sha256(
                    generator["custom_instructions"].encode()
                ).hexdigest(),
            },
            "attempts": [
                {
                    "action": "proceed_unresolved",
                    "failures": ["[generation_requirement] Missing required citation."],
                }
            ],
        },
        "tool_ledger": {
            "guideline_evidence": [span.model_dump() for span in cited_ledger().guideline_evidence]
        },
        "nudges": [
            {"nudge_idx": i, "nudge": nudge, "steering_outcome": {"flags": []}}
            for i, nudge in enumerate((cited, uncited))
        ],
        "run_metrics": {"latency_ms": 100, "input_tokens": 10, "output_tokens": 10},
    }
    _, source = run_experiment(
        config=saved_config,
        output_dir=tmp_path / "generation",
        task=lambda case: {"output": record},
        evaluators=[Completion()],
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    output = tmp_path / "comparison"
    task_file = next((source / "tasks").glob("*.json"))
    original_task = task_file.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare",
            "--config",
            str(config_path),
            "--runs",
            str(source),
            "--output-dir",
            str(output),
        ],
    )
    with pytest.raises(ValueError, match="without an explicit retention decision"):
        compare()
    config["retained_run_transport"] = {
        "read_timeout": 120,
        "replacement_read_timeout": 600,
        "reason": "Explicit retention of a successful historical request.",
    }
    config_path.write_text(json.dumps(config))
    assert compare() == 0
    summary_path = next(output.rglob("summary.json"))
    summary = json.loads(summary_path.read_text())
    assert summary["candidate_nudges"] == 2
    assert summary["eligible_nudges"] == summary["excluded_nudges"] == 1
    assert summary["source_recorded_excluded_nudges"] == 0
    assert summary["reconciled_missing_citation_exclusions"] == 1
    assert summary["layer2"]["citation_resolution"]["violates_contract"] == 0
    assert task_file.read_bytes() == original_task
    replayed = json.loads(next((summary_path.parent / "tasks").glob("*.json")).read_text())
    assert replayed["actual_output"]["response"]["nudges"] == [cited]

    task = json.loads(task_file.read_text())
    task["actual_output"]["response_status"] = "error"
    task["actual_output"]["response"] = {
        "status": "error",
        "error": "Nudge generation failed: AWSHTTPSConnectionPool(host='example.invalid', "
        "port=443): Read timed out.",
    }
    task_file.write_text(json.dumps(task))
    with pytest.raises(ValueError, match="hit the discarded timeout"):
        compare()


def test_effective_prompt_override_invalidates_cache(tmp_path, monkeypatch):
    from evals.runner import run_identity

    monkeypatch.setenv("PROMPTS_PATH", str(tmp_path))
    config = {"generator_config": {"corpus_version": "test"}}
    prompt = tmp_path / "orchestrator.md"
    prompt.write_text("First prompt")
    first = run_identity(config)
    prompt.write_text("Changed prompt")
    assert run_identity(config) != first


@pytest.mark.parametrize(
    "status,encounter,expected",
    [
        ("active", True, "violates_contract"),
        ("completed", True, "abstain_insufficient_evidence"),
        ("active", False, "abstain_insufficient_evidence"),
    ],
)
def test_redundant_order_requires_active_request_and_encounter(status, encounter, expected):
    resources = [
        {
            "resourceType": "ServiceRequest",
            "status": status,
            "intent": "order",
            "code": {"text": "Example assay"},
            "encounter": {"reference": "Encounter/e"},
        }
    ]
    if encounter:
        resources.append({"resourceType": "Encounter", "id": "e", "status": "in-progress"})
    ledger = ToolLedger(
        patient_evidence=[
            PatientEvidenceSpan(
                span_id=f"p{i}", query="request", resource_type=r["resourceType"], content=r
            )
            for i, r in enumerate(resources)
        ]
    )
    assert check_redundant_order({"title": "Order Example assay"}, ledger)["verdict"] == expected
    assert (
        check_redundant_order({"title": "Repeat Example assay"}, ledger)["verdict"]
        != "violates_contract"
    )
    for title, description in [
        ("Order different assay", "Example assay is already ordered"),
        ("Order different assay before administering Example assay", ""),
        ("Discuss whether to order Example assay", ""),
    ]:
        assert (
            check_redundant_order({"title": title, "description": description}, ledger)["verdict"]
            != "violates_contract"
        )


@pytest.mark.parametrize(
    "gate,metric", [(LatencyBudget, "latency_ms"), (TokenBudget, "total_tokens")]
)
def test_budget_threshold_and_missing_measurement(gate, metric):
    evaluator = gate(100)
    for value, expected in [(100, True), (101, False), (None, False), (-1, False), (True, False)]:
        case = EvaluationData(input={}, actual_output={"run_metrics": {metric: value}})
        assert evaluator.evaluate(case)[0].test_pass is expected


def test_raw_capture_is_nested_and_thread_isolated():
    def capture(value):
        with capture_raw_resources() as channel:
            default_channel().publish(tool_name="test", query="x", resources=[{"id": value}])
            with capture_raw_resources():
                assert default_channel().is_empty
            return channel.resources()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(capture, ["one", "two"])) == [[{"id": "one"}], [{"id": "two"}]]


def test_experiment_replays_failed_task_but_invalidates_changed_input(tmp_path, monkeypatch):
    from evals.reporting import summarize_runs

    config = {
        "dataset_version": "test-v1",
        "patients": [],
        "model": {"model_id": "test-generator"},
        "generator_config": {"corpus_version": "corpus-v1", "model_id": "test-generator"},
    }
    calls = []
    record = {
        "response_status": "error",
        "run_metrics": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }

    def task(case):
        calls.append(case.input)
        return {"output": record}

    cases = [Case(name="case-one", session_id="stable", input={"patient_id": "one"})]
    kwargs = dict(
        config=config, output_dir=tmp_path, task=task, evaluators=[TokenBudget(100)], cases=cases
    )
    first, path = run_experiment(**kwargs)
    second, again = run_experiment(**kwargs)
    assert first.test_passes == second.test_passes == [False]
    assert summarize_runs([record], config)["measured_token_patients"] == 0
    assert path == again and len(calls) == 1
    cases[0].input = {"patient_id": "two"}
    _, changed = run_experiment(**kwargs)
    assert changed != path and len(calls) == 2
    monkeypatch.setenv("HEALTHLAKE_DATASTORE_ENDPOINT", "https://example.invalid/another-dataset")
    _, relocated = run_experiment(**kwargs)
    assert relocated != changed and len(calls) == 3


def test_empty_or_missed_controls_never_validate_judge():
    assert not control_summary(SimpleNamespace(test_passes=[]), 0)[
        "trusted_for_citation_provenance"
    ]
    assert not control_summary(SimpleNamespace(test_passes=[True, False]), 2)[
        "trusted_for_citation_provenance"
    ]
    assert control_summary(SimpleNamespace(test_passes=[True, True]), 2)[
        "trusted_for_citation_provenance"
    ]
    assert model_family("us.anthropic.claude-sonnet-5") == "anthropic"


def test_experiment_coordinates_judge_cache_and_reports_corruption(tmp_path, monkeypatch):
    import threading
    import time
    from evals.controls import HolisticJudge

    calls = []
    lock = threading.Lock()

    def converse(**kwargs):
        with lock:
            calls.append(kwargs)
        time.sleep(0.05)
        return {"output": {"message": {"content": [{"text": '{"verdict": "violates_contract"}'}]}}}

    monkeypatch.setattr(
        "evals.controls.boto3.client", lambda *a, **kw: SimpleNamespace(converse=converse)
    )
    cache = tmp_path / "judge-cache"
    judge = HolisticJudge(
        "us.openai.gpt-5.6-sol",
        "us.anthropic.claude-opus-5",
        cache_dir=cache,
        max_concurrency=4,
    )
    kwargs = {
        "config": {
            "dataset_version": "test-v1",
            "patients": [],
            "generator_config": {"corpus_version": "test"},
        },
        "output_dir": tmp_path / "results",
        "task": lambda case: {"output": case.input},
        "evaluators": [judge],
        "cases": [Case(name=f"case-{i}", session_id=f"session-{i}", input={}) for i in range(4)],
        "max_concurrency": 4,
    }
    report, _ = run_experiment(**kwargs)
    assert report.test_passes == [True] * 4
    assert len(calls) == 1
    next(cache.glob("*.json")).write_text("[]")
    corrupted, _ = run_experiment(**kwargs)
    assert corrupted.test_passes == [False] * 4
    assert len(calls) == 1


def test_compare_retests_a_versioned_judge_on_unused_controls(tmp_path, monkeypatch):
    """Saved runs → set A → held-out set B, with separate prompts and frozen A."""
    import hashlib
    from evals.compare import main as compare

    config = json.loads(Path("config/evals/blog_sonnet5.json").read_text())
    config["patients"] = [{"patient_id": name} for name in ("alpha", "beta")]
    config["judge"]["control_cap"] = 1
    records = {}
    for patient in config["patients"]:
        name = patient["patient_id"]
        nudge, ledger = cited_nudge(), cited_ledger()
        nudge["guideline_citation"]["source"] += f" {name}"
        ledger.guideline_evidence[0].source += f" {name}"
        records[name] = {
            "patient_id": name,
            "response_status": "success",
            "generation_settings": {
                key: config[key] for key in ("model", "agent", "generator_config", "specialty")
            },
            "patient_steering_outcome": {
                "generator_config": {
                    **config["generator_config"],
                    "custom_instructions_hash": hashlib.sha256(
                        config["generator_config"]["custom_instructions"].encode()
                    ).hexdigest(),
                }
            },
            "tool_ledger": {
                "guideline_evidence": [s.model_dump() for s in ledger.guideline_evidence],
            },
            "nudges": [{"nudge_idx": 0, "nudge": nudge}],
            "run_metrics": {"latency_ms": 10, "total_tokens": 2},
        }
    _, source = run_experiment(
        config=config,
        output_dir=tmp_path / "generation",
        task=lambda case: {"output": records[case.input["patient_id"]]},
        evaluators=[Completion()],
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    calls = []

    def converse(**kwargs):
        calls.append(kwargs)
        prompt = kwargs["system"][0]["text"]
        verdict = (
            "violates_contract"
            if "holistic-judge-v2" in prompt
            else "abstain_insufficient_evidence"
        )
        return {"output": {"message": {"content": [{"text": json.dumps({"verdict": verdict})}]}}}

    monkeypatch.setattr(
        "evals.controls.boto3.client", lambda *a, **kw: SimpleNamespace(converse=converse)
    )
    command = [
        "compare",
        "--config",
        str(config_path),
        "--runs",
        str(source),
        "--validate-judge",
    ]
    first = tmp_path / "set-a"
    monkeypatch.setattr("sys.argv", command + ["--output-dir", str(first)])
    assert compare() == 0
    a_summary = next(first.rglob("control_summary.json"))
    frozen_a = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    assert json.loads(a_summary.read_text())["detected"] == 0

    second = tmp_path / "set-b"
    v2_command = command + ["--output-dir", str(second), "--judge-prompt-version", "v2"]
    monkeypatch.setattr("sys.argv", v2_command)
    with pytest.raises(SystemExit):
        compare()  # The CLI must reject a v2 run without a held-out exclusion set.
    assert len(calls) == 1
    monkeypatch.setattr("sys.argv", v2_command + ["--exclude-controls", str(source)])
    with pytest.raises(SystemExit):
        compare()  # An ordinary patient replay is not a previous control set.
    import shutil

    unrelated = tmp_path / "unrelated-controls"
    shutil.copytree(a_summary.parent, unrelated)
    manifest = json.loads((unrelated / "manifest.json").read_text())
    manifest["config"]["replay_content_sha256"] = "different saved generations"
    (unrelated / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", v2_command + ["--exclude-controls", str(unrelated)])
    with pytest.raises(SystemExit):
        compare()
    assert len(calls) == 1
    monkeypatch.setattr("sys.argv", v2_command + ["--exclude-controls", str(a_summary.parent)])
    assert compare() == 0
    b_summary = next(second.rglob("control_summary.json"))
    result = json.loads(b_summary.read_text())
    assert result["detected"] == 1 and result["trusted_for_citation_provenance"]
    assert result["attrition"]["previously_used"] == 1
    assert result["excluded_controls_sha256"]
    a_names = {p.name for p in (a_summary.parent / "tasks").glob("*.json")}
    b_names = {p.name for p in (b_summary.parent / "tasks").glob("*.json")}
    assert a_names.isdisjoint(b_names)
    assert result["prompt_sha256"] != json.loads(a_summary.read_text())["prompt_sha256"]
    assert len(calls) == 2
    assert frozen_a == {
        p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()
    }


def test_review_export_applies_current_gates_to_saved_output(tmp_path):
    from evals.reporting import export_review

    nudge = {
        "title": "Review creatinine",
        "description": "Creatinine is 4 mg/dL.",
        "rationale": "Review the recorded result.",
        "grounding": "clinical_reasoning",
    }
    record = {
        "patient_id": "synthetic",
        "response_status": "success",
        "response": {
            "status": "success",
            "nudges": [nudge],
            "key_findings": ["Synthetic finding"] * 3,
            "patient_summary": "Synthetic context for this test. " * 8,
        },
        "trace": {
            "messages": [],
            "tool_calls": [{"tool_name": "query_patient_fhir", "success": True, "completed": True}],
        },
        "tool_ledger": {
            "patient_evidence": [
                {
                    "span_id": "p1",
                    "query": "Observation",
                    "resource_type": "Observation",
                    "content": {
                        "resourceType": "Observation",
                        "code": {"text": "Creatinine"},
                        "valueQuantity": {"value": 4, "unit": "mg/dL"},
                    },
                }
            ]
        },
        "nudges": [{"nudge_idx": 0, "nudge": nudge}],
    }
    config = {"agent": {"max_nudges": 5, "tool_limits": {}}}
    assert export_review([record], tmp_path / "accepted", config).is_file()
    nudge["description"] = "Creatinine is 999 mg/dL."
    assert export_review([record], tmp_path / "failed-evidence", config) is None
    attrition = json.loads((tmp_path / "failed-evidence/attrition.json").read_text())
    assert attrition["nudges_failed_layer2"] == 1
    record["response"]["status"] = "error"
    assert export_review([record], tmp_path / "failed-run", config) is None
    attrition = json.loads((tmp_path / "failed-run/attrition.json").read_text())
    assert attrition["patients_failed_layer1"] == 1


@pytest.mark.parametrize("response", ["[]", "null", '{"verdict": "unknown"}', "not json"])
def test_malformed_judge_output_is_a_control_miss(response):
    import threading
    from evals.controls import HolisticJudge

    judge = object.__new__(HolisticJudge)
    judge.model_id = "us.openai.gpt-5.6-sol"
    judge.reasoning_effort = "high"
    judge.prompt = "Synthetic prompt"
    judge.prompt_sha256 = "test"
    judge.cache_dir = None
    judge.semaphore = threading.BoundedSemaphore(1)
    judge.client = SimpleNamespace(
        converse=lambda **kwargs: {"output": {"message": {"content": [{"text": response}]}}}
    )
    assert judge.judge({})["verdict"] == "abstain_insufficient_evidence"


def test_review_parses_nullable_json_and_legacy_citations():
    from evals.review.make_review_html import parse_citation

    citation = {"source": "Example guideline", "section": None, "page": 2}
    assert parse_citation(json.dumps(citation)) == citation
    assert parse_citation(str(citation)) == citation
    assert parse_citation("null") is None


def test_review_preserves_evidence_and_separates_human_nudge_type(tmp_path):
    import csv

    (tmp_path / "trace.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "t",
                                    "name": "query_patient_fhir",
                                    "input": {"resource_types": ["Observation"]},
                                }
                            }
                        ]
                    },
                    {
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": "t",
                                    "content": [{"text": "TEST EVIDENCE"}],
                                }
                            }
                        ]
                    },
                ]
            }
        )
    )
    row = {
        "patient_id": "synthetic",
        "nudge_index": "0",
        "trace_path": "trace.json",
        "urgency": "info",
        "category": "review",
        "nudge_type": "lab_review",
        "grounding": "patient",
        "title": "</script><script>bad()</script>",
        "description": "Review",
        "rationale": "Review",
        "guideline_citation": "",
    }
    path = tmp_path / "review.csv"
    with path.open("w") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    rows = load_rows(path)
    assert rows[0]["generated_nudge_type"] == "lab_review" and rows[0]["nudge_type"] == ""
    html = build_html(rows, Path("evals/review/review_template_vC.html"), tmp_path)
    assert "TEST EVIDENCE" in html
    assert "<script>bad()" not in html
    assert "Internal AWS review artifact" not in html
    assert {r[0] for r in RUBRIC} >= {"priority_appropriate", "nudge_type"}
    assert '"options": ["clinical", "administrative"]' in html


def test_summarize_runs_golden_output():
    """Full summary dict, key order included, frozen before the C-23 refactor."""
    from evals.reporting import summarize_runs

    cited = cited_nudge()
    uncited = {**cited, "guideline_citation": None}
    spans = [span.model_dump() for span in cited_ledger().guideline_evidence]
    config = {
        "dataset_version": "golden-v1",
        "model": {"model_id": "gen", "read_timeout": 120},
        "generator_config": {"model_id": "gen", "entailment_model_id": "ent"},
        "pricing_per_million_tokens": {
            "input_tokens": 3.0,
            "output_tokens": 15.0,
            "cache_read_input_tokens": 0.3,
            "cache_write_input_tokens": 3.75,
        },
        "pricing_basis": "list",
        "provenance_notes": ["note"],
        "retained_run_transport": {"read_timeout": 120},
    }
    records = [
        {
            "patient_id": "p1",
            "response_status": "success",
            "tool_ledger": {"guideline_evidence": spans},
            "nudges": [
                {"nudge_idx": 0, "nudge": cited, "steering_outcome": {"flags": []}},
                {"nudge_idx": 1, "nudge": uncited, "steering_outcome": {"flags": ["unresolved"]}},
            ],
            "run_metrics": {
                "latency_ms": 100,
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
            },
            "patient_steering_outcome": {"guided_attempt_count": 2, "verification_error": True},
            "replay_adjustments": {"missing_required_citation_exclusions": 1},
        },
        {
            "patient_id": "p2",
            "response_status": "error",
            "nudges": [],
            "run_metrics": {"input_tokens": 0, "output_tokens": 0},
            "source_excluded_nudge_count": 3,
        },
    ]
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "evals" / "summarize_runs_golden.json").read_text()
    )
    actual = summarize_runs(records, config)
    assert list(actual) == list(expected)
    assert json.loads(json.dumps(actual)) == expected
