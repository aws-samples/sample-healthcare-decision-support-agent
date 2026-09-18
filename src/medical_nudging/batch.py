"""Batch inference: one loop that owns caching, bounded concurrency, and trace export.

``scripts/run_inference.py`` and ``scripts/fhir_api/run_inference.py`` differ in how
they discover samples and which per-sample runner they call. Everything after that
(cache lookup and fill, running the samples, collecting traces, the OTEL span log,
the console summary) is the same recipe and lives here so it exists once.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from os import linesep
from pathlib import Path

import diskcache  # type: ignore[import-untyped]
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from .inference import run_bounded_concurrently
from .models import InferenceReport, InferenceResult

CACHE_SIZE_LIMIT = 1_000_000_000  # 1 GB
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

SampleRunner = Callable[[dict], tuple[InferenceResult, dict | None]]
CacheKeyFn = Callable[[dict], str]


class InferenceCache:
    """Disk cache of ``(InferenceResult, trace)`` pairs keyed by run identity.

    Keys come from :func:`medical_nudging.inference.build_inference_cache_key`, so a
    cache directory can be shared by every runner that builds keys the same way.
    """

    _DATETIME_FIELDS = ("invocation_start", "invocation_end")

    def __init__(self, directory: Path, *, size_limit: int = CACHE_SIZE_LIMIT) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # JSONDisk instead of the default pickle serializer: a writable cache
        # directory must never be a code-execution path (CVE-2025-69872).
        self._cache = diskcache.Cache(
            str(self.directory), size_limit=size_limit, disk=diskcache.JSONDisk
        )

    def get(self, key: str) -> tuple[InferenceResult, dict | None] | None:
        cached = self._cache.get(key)
        if cached is None:
            return None
        result_dict, trace_dict = cached  # JSON round-trips the tuple as a list
        for name in self._DATETIME_FIELDS:
            if isinstance(result_dict.get(name), str):
                result_dict[name] = datetime.fromisoformat(result_dict[name])
        return InferenceResult(**result_dict), trace_dict

    def set(self, key: str, result: InferenceResult, trace: dict | None) -> None:
        result_dict = asdict(result)
        for name in self._DATETIME_FIELDS:
            if isinstance(result_dict.get(name), datetime):
                result_dict[name] = result_dict[name].isoformat()
        # Coerce anything non-JSON in the trace (Decimal, datetime) to strings.
        trace_json = json.loads(json.dumps(trace, default=str)) if trace is not None else None
        self._cache.set(key, [result_dict, trace_json], expire=CACHE_TTL_SECONDS)

    def clear(self) -> None:
        self._cache.clear()


def open_inference_cache(
    directory: Path, *, enabled: bool, clear: bool = False
) -> InferenceCache | None:
    """Open the cache at ``directory`` when enabled, emptied first if ``clear``; else ``None``."""
    if not enabled:
        return None
    cache = InferenceCache(directory)
    if clear:
        cache.clear()
    return cache


def progress_bar(console: Console) -> Progress:
    """The spinner-bar-percent progress display both inference scripts show."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    )


@dataclass
class BatchOutcome:
    """Everything a batch produced, in input order."""

    results: list[InferenceResult] = field(default_factory=list)
    traces: list[tuple[str, dict | None]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0


def run_batch(
    samples: Sequence[dict],
    runner: SampleRunner,
    *,
    cache: InferenceCache | None = None,
    cache_key: CacheKeyFn | None = None,
    max_concurrency: int = 1,
    on_complete: Callable[[dict, InferenceResult, bool], None] | None = None,
) -> BatchOutcome:
    """Run every sample once, serving repeats from the cache when one is given.

    Args:
        samples: Sample dicts; each needs a ``sample_id``.
        runner: Produces ``(InferenceResult, trace_dict or None)`` for one sample.
        cache: Where to look before running and store after running.
        cache_key: Builds the cache key for a sample. Required when ``cache`` is set.
        max_concurrency: Upper bound on samples in flight at once.
        on_complete: Called with ``(sample, result, cache_hit)`` as each finishes.

    Returns:
        Results and traces in the order of ``samples``, plus cache counts.
    """
    if cache is not None and cache_key is None:
        raise ValueError("cache_key is required when a cache is given")

    def process(sample: dict) -> tuple[InferenceResult, dict | None, bool]:
        key = cache_key(sample) if cache is not None and cache_key is not None else None
        if cache is not None and key is not None:
            cached = cache.get(key)
            if cached is not None:
                return cached[0], cached[1], True
        result, trace = runner(sample)
        if cache is not None and key is not None:
            cache.set(key, result, trace)
        return result, trace, False

    def report(sample: dict, produced: tuple[InferenceResult, dict | None, bool]) -> None:
        if on_complete is not None:
            on_complete(sample, produced[0], produced[2])

    produced = run_bounded_concurrently(
        samples, process, max_concurrency=max_concurrency, on_complete=report
    )

    outcome = BatchOutcome()
    for sample, (result, trace, cache_hit) in zip(samples, produced, strict=True):
        outcome.results.append(result)
        if trace is not None:
            outcome.traces.append((sample["sample_id"], trace))
        if cache_hit:
            outcome.cache_hits += 1
        else:
            outcome.cache_misses += 1
    return outcome


@contextmanager
def otel_span_log(path: Path) -> Iterator[Path]:
    """Write every Strands OTEL span emitted inside the block to ``path`` as JSONL."""
    from strands.telemetry import StrandsTelemetry

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wt") as handle:
        StrandsTelemetry().setup_console_exporter(
            out=handle,
            formatter=lambda span: span.to_json() + linesep,
        )
        yield path


def summary_tables(report: InferenceReport, title: str) -> list[Table]:
    """Console tables for a finished batch: headline counts, then categories."""
    table = Table(title=title)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Total Samples", str(report.total_samples))
    table.add_row("Success", f"[green]{report.success_count}[/green]")
    table.add_row("Partial", f"[yellow]{report.partial_count}[/yellow]")
    table.add_row("Errors", f"[red]{report.error_count}[/red]")
    table.add_row("Avg Latency", f"{report.avg_latency_ms:.0f}ms")
    table.add_row("P50 Latency", f"{report.p50_latency_ms:.0f}ms")
    table.add_row("P95 Latency", f"{report.p95_latency_ms:.0f}ms")
    tables = [table]

    if report.category_distribution:
        categories = Table(title="Category Distribution")
        categories.add_column("Category", style="cyan")
        categories.add_column("Count", justify="right")
        for category, count in sorted(report.category_distribution.items(), key=lambda x: -x[1]):
            categories.add_row(category, str(count))
        tables.append(categories)
    return tables
