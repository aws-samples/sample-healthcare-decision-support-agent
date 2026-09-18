"""Tests for the shared inference run-artifact interface."""

import json

from medical_nudging.inference import build_inference_cache_key
from medical_nudging.run_artifacts import load_run, save_run


def sample_report():
    return {
        "timestamp": "2026-08-18T12:00:00",
        "mode": "fhir_api",
        "total_samples": 2,
        "results": [
            {"sample_id": "pat-a", "status": "success", "nudges": []},
            {"sample_id": "pat-b", "status": "error", "nudges": []},
        ],
    }


def test_save_and_load_run_owns_report_trace_and_index_format(tmp_path):
    results_path = tmp_path / "experiment" / "inference.json"
    traces_dir = results_path.parent / "traces"

    saved = save_run(
        sample_report(),
        results_path,
        [
            ("pat-a", {"tool_calls": [], "timing": {"total_ms": 42, "tool_ms": 7}}),
            ("pat-b", None),
        ],
        traces_dir=traces_dir,
    )

    assert saved.trace_count == 1
    assert saved.traces_dir == traces_dir
    report = json.loads(results_path.read_text())
    assert report["traces_dir"] == str(traces_dir)

    index = json.loads((traces_dir / "index.json").read_text())
    assert index["traces"] == [
        {
            "sample_id": "pat-a",
            "file": "sample_001_pat-a.json",
            "timing": {"total_ms": 42, "tool_ms": 7},
        }
    ]

    loaded = load_run(results_path)
    assert loaded.traces_dir == traces_dir
    assert set(loaded.traces) == {"pat-a"}
    assert loaded.traces["pat-a"]["tool_calls"] == []


def test_load_run_supports_legacy_experiment_trace_directory(tmp_path):
    results_path = tmp_path / "inference.json"
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    results_path.write_text(json.dumps(sample_report()))
    (traces_dir / "sample_001_pat-a.json").write_text(
        json.dumps({"sample_id": "pat-a", "tool_calls": []})
    )

    loaded = load_run(results_path)

    assert loaded.traces_dir == traces_dir
    assert set(loaded.traces) == {"pat-a"}


def test_save_run_without_traces_does_not_publish_phantom_directory(tmp_path):
    results_path = tmp_path / "inference.json"

    saved = save_run(sample_report(), results_path, traces_dir=tmp_path / "traces")

    assert saved.traces_dir is None
    assert "traces_dir" not in json.loads(results_path.read_text())


def test_fhir_cache_key_is_stable_across_runner_call_sites():
    key = build_inference_cache_key(
        "us.anthropic.claude-sonnet-4-6",
        "patient-123",
        "full",
        {"visit_type": "inpatient", "specialty": "general"},
    )

    patient_hash, visit_hash = key.split(":")[-3], key.split(":")[-1]
    assert len(patient_hash) == 8
    assert len(visit_hash) == 8
    assert key == build_inference_cache_key(
        "us.anthropic.claude-sonnet-4-6",
        b"patient-123",
        "full",
        {"specialty": "general", "visit_type": "inpatient"},
    )
