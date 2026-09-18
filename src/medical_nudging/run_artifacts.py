"""Persistence for inference reports and their execution traces."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class SavedRun:
    """Paths written for one inference run."""

    results_path: Path
    traces_dir: Path | None
    trace_count: int


@dataclass(frozen=True)
class LoadedRun:
    """A saved inference run loaded through the artifact interface."""

    results_path: Path
    report: dict[str, Any]
    traces_dir: Path | None
    traces: dict[str, dict[str, Any]]


def _report_dict(report: Any) -> dict[str, Any]:
    if is_dataclass(report) and not isinstance(report, type):
        payload = asdict(report)
    elif isinstance(report, Mapping):
        payload = dict(report)
    else:
        raise TypeError("report must be a dataclass or mapping")
    return payload


def save_run(
    report: Any,
    results_path: Path,
    traces: Iterable[tuple[str, dict[str, Any] | None]] = (),
    *,
    traces_dir: Path | None = None,
) -> SavedRun:
    """Persist an inference report and optional per-sample traces.

    The report always points at the trace directory when traces were written.
    Trace files are indexed by their embedded ``sample_id`` rather than filename
    convention so evaluation readers do not depend on ordinal naming.
    """
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    trace_items = list(traces)
    resolved_traces_dir = Path(traces_dir) if traces_dir is not None else None
    index_entries: list[dict[str, Any]] = []

    if resolved_traces_dir is not None:
        for index, (sample_id, trace) in enumerate(trace_items, 1):
            if not isinstance(trace, dict):
                continue
            resolved_traces_dir.mkdir(parents=True, exist_ok=True)
            filename = f"sample_{index:03d}_{sample_id}.json"
            (resolved_traces_dir / filename).write_text(
                json.dumps({"sample_id": sample_id, **trace}, indent=2, default=str) + "\n"
            )
            timing = trace.get("timing")
            timing = timing if isinstance(timing, dict) else {}
            index_entries.append(
                {
                    "sample_id": sample_id,
                    "file": filename,
                    "timing": {
                        "total_ms": timing.get("total_ms", 0),
                        "tool_ms": timing.get("tool_ms", 0),
                    },
                }
            )

    payload = _report_dict(report)
    if index_entries and resolved_traces_dir is not None:
        index_payload = {
            "timestamp": payload.get("timestamp", datetime.now().isoformat()),
            "mode": payload.get("mode"),
            "total_samples": len(index_entries),
            "traces": index_entries,
        }
        (resolved_traces_dir / "index.json").write_text(
            json.dumps(index_payload, indent=2, default=str) + "\n"
        )
        payload["traces_dir"] = str(resolved_traces_dir)
    else:
        resolved_traces_dir = None
        payload.pop("traces_dir", None)

    results_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return SavedRun(
        results_path=results_path,
        traces_dir=resolved_traces_dir,
        trace_count=len(index_entries),
    )


def read_report(results_path: Path) -> dict[str, Any]:
    """Load an inference report JSON object."""
    results_path = Path(results_path)
    payload = json.loads(results_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{results_path}: expected a JSON object, got {type(payload).__name__}")
    return payload


def resolve_trace_dir(report: Mapping[str, Any], results_path: Path) -> Path | None:
    """Resolve traces from embedded metadata or supported legacy conventions."""
    results_path = Path(results_path)
    embedded = report.get("traces_dir")
    if isinstance(embedded, str) and embedded:
        embedded_path = Path(embedded)
        candidates = [embedded_path]
        if not embedded_path.is_absolute():
            candidates.append(results_path.parent / embedded_path)
        for candidate in candidates:
            if candidate.is_dir():
                return candidate

    conventional = [
        results_path.parent / "traces" / results_path.stem,
        results_path.parent / "traces",
    ]
    return next((candidate for candidate in conventional if candidate.is_dir()), None)


def read_traces(trace_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Load per-sample traces keyed by embedded ``sample_id``."""
    if trace_dir is None:
        return {}
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        return {}

    traces: dict[str, dict[str, Any]] = {}
    for path in sorted(trace_dir.glob("sample_*.json")):
        try:
            trace = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(trace, dict):
            continue
        sample_id = trace.get("sample_id")
        if isinstance(sample_id, str):
            traces[sample_id] = trace
    return traces


def load_run(results_path: Path) -> LoadedRun:
    """Load a report and all traces reachable from it."""
    results_path = Path(results_path)
    report = read_report(results_path)
    traces_dir = resolve_trace_dir(report, results_path)
    return LoadedRun(
        results_path=results_path,
        report=report,
        traces_dir=traces_dir,
        traces=read_traces(traces_dir),
    )
