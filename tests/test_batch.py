"""Tests for the shared batch loop: caching, ordering, and the console summary."""

from datetime import UTC

import pytest

from medical_nudging.batch import InferenceCache, run_batch, summary_tables
from medical_nudging.inference import generate_report
from medical_nudging.models import InferenceResult


def _result(sample_id: str, status: str = "success") -> InferenceResult:
    return InferenceResult(
        sample_id=sample_id,
        file_path=f"fhir://{sample_id}",
        format="fhir_api",
        source="local",
        status=status,
        latency_ms=10,
        nudge_count=1,
        categories_used=["screening"],
    )


def test_run_batch_keeps_input_order_under_concurrency():
    samples = [{"sample_id": f"p{i}"} for i in range(6)]
    seen: list[tuple[str, bool]] = []

    outcome = run_batch(
        samples,
        lambda sample: (_result(sample["sample_id"]), {"timing": {"total_ms": 1}}),
        max_concurrency=3,
        on_complete=lambda sample, result, hit: seen.append((sample["sample_id"], hit)),
    )

    assert [r.sample_id for r in outcome.results] == [s["sample_id"] for s in samples]
    assert [sid for sid, _ in outcome.traces] == [s["sample_id"] for s in samples]
    assert sorted(seen) == sorted((s["sample_id"], False) for s in samples)
    assert (outcome.cache_hits, outcome.cache_misses) == (0, 6)


def test_run_batch_serves_repeats_from_cache(tmp_path):
    cache = InferenceCache(tmp_path / "cache")
    calls: list[str] = []

    def runner(sample):
        calls.append(sample["sample_id"])
        return _result(sample["sample_id"]), None

    samples = [{"sample_id": "a"}, {"sample_id": "b"}]
    first = run_batch(samples, runner, cache=cache, cache_key=lambda s: s["sample_id"])
    second = run_batch(samples, runner, cache=cache, cache_key=lambda s: s["sample_id"])

    assert calls == ["a", "b"]
    assert (first.cache_hits, first.cache_misses) == (0, 2)
    assert (second.cache_hits, second.cache_misses) == (2, 0)
    assert second.traces == []
    assert [r.sample_id for r in second.results] == ["a", "b"]


def test_run_batch_requires_key_builder_with_cache(tmp_path):
    with pytest.raises(ValueError, match="cache_key"):
        run_batch(
            [{"sample_id": "a"}], lambda s: (_result("a"), None), cache=InferenceCache(tmp_path)
        )


def test_summary_tables_add_category_table_only_when_present():
    with_categories = generate_report([_result("a")], "local")
    without = generate_report([], "local")

    assert len(summary_tables(with_categories, "t")) == 2
    assert len(summary_tables(without, "t")) == 1


def test_open_inference_cache_respects_enabled_and_clear(tmp_path):
    from medical_nudging.batch import open_inference_cache

    assert open_inference_cache(tmp_path / "off", enabled=False) is None

    cache = open_inference_cache(tmp_path / "on", enabled=True)
    assert cache is not None
    cache.set("k", _result("s"), None)
    assert cache.get("k") is not None

    cleared = open_inference_cache(tmp_path / "on", enabled=True, clear=True)
    assert cleared is not None
    assert cleared.get("k") is None


def test_progress_bar_has_description_and_percentage_columns():
    import io

    from rich.console import Console
    from rich.progress import BarColumn, TaskProgressColumn

    from medical_nudging.batch import progress_bar

    progress = progress_bar(Console(file=io.StringIO()))
    column_types = {type(column) for column in progress.columns}
    assert BarColumn in column_types
    assert TaskProgressColumn in column_types


def test_inference_cache_uses_json_not_pickle(tmp_path):
    """Cache entries are JSON (CVE-2025-69872: pickle in a writable dir is code execution)."""
    from datetime import datetime

    import diskcache

    cache = InferenceCache(tmp_path / "cache")
    assert isinstance(cache._cache.disk, diskcache.JSONDisk)

    result = _result("a")
    result.invocation_start = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    cache.set("k", result, {"spans": [{"latency_ms": 12}]})

    got_result, got_trace = cache.get("k")
    assert got_result == result
    assert got_trace == {"spans": [{"latency_ms": 12}]}
    assert cache.get("missing") is None
