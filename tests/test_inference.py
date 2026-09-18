"""Tests for shared inference helpers."""

import threading

from medical_nudging.inference import build_inference_cache_key, run_bounded_concurrently


def test_run_bounded_concurrently_overlaps_work_and_preserves_input_order():
    """Two workers overlap without changing the result order."""
    barrier = threading.Barrier(2, timeout=1)

    def worker(value: int) -> int:
        barrier.wait()
        return value * 10

    results = run_bounded_concurrently([2, 1], worker, max_concurrency=2)

    assert results == [20, 10]


def test_run_bounded_concurrently_rejects_non_positive_limit():
    """The concurrency bound must be at least one."""
    try:
        run_bounded_concurrently([], lambda value: value, max_concurrency=0)
    except ValueError as error:
        assert str(error) == "max_concurrency must be at least 1"
    else:
        raise AssertionError("Expected ValueError")


def test_inference_cache_key_changes_with_runtime_corpus_and_output_config():
    common = {
        "model_id": "us.anthropic.claude-opus-5",
        "patient_identity": "patient-1",
        "context_condition": "full",
        "visit_context": {"visit_type": "inpatient"},
    }

    v1_key = build_inference_cache_key(
        **common,
        runtime_config={
            "max_nudges": 5,
            "opensearch_index": "guidelines-icu-baseline",
        },
    )
    v2_key = build_inference_cache_key(
        **common,
        runtime_config={
            "max_nudges": 3,
            "opensearch_index": "guidelines-icu-baseline-v2",
        },
    )

    assert v1_key != v2_key
