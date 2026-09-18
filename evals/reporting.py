"""Content-free comparison summaries and reader-generated review/regression assets."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.layers import (
    BLOG_CHECKS,
    VERDICTS,
    ledger_from_record,
    score_nudge,
    token_usage_is_measured,
)

# Public demo identifiers shared with the trace-verified readiness cohort.
READINESS_PATIENT_IDS = (
    "3886cafb-65f4-5789-9213-64678a202f82",
    "0c2243d2-987b-5cbd-8eb1-170a80647693",
    "73fb53d8-f1fa-53cd-a25c-2314caccbb99",
    "4c48ec6f-716c-5bfd-8cee-b9a6b7c6c765",
    "28776290-4349-56d3-8c13-adc554feabb8",
    "bc2a74ce-4069-5983-9423-1c175f7854d9",
)


@dataclass
class _NudgeTally:
    """Layer 2 verdict counts over the eligible nudges of a run set."""

    counts: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            name: {**dict.fromkeys(VERDICTS, 0), "not_applicable": 0} for name in BLOG_CHECKS
        }
    )
    candidates: int = 0
    excluded: int = 0
    eligible: int = 0
    no_violation: int = 0
    patients_with_eligible_nudges: int = 0
    additional_failures: dict[str, int] = field(default_factory=dict)
    verdicts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(VERDICTS, 0))

    def add_report(self, report: dict[str, Any]) -> None:
        self.eligible += 1
        self.no_violation += report["no_violation"]
        self.verdicts[report["verdict"]] += 1
        for failure in report["additional_failures"]:
            key = failure["check"]
            self.additional_failures[key] = self.additional_failures.get(key, 0) + 1
        for check in report["checks"]:
            verdict = check["verdict"] if check["applicable"] else "not_applicable"
            self.counts[check["check"]][verdict] += 1


def _is_excluded(item: dict[str, Any]) -> bool:
    return bool(item.get("steering_outcome", {}).get("flags"))


def _tally_nudges(records: list[dict[str, Any]]) -> _NudgeTally:
    tally = _NudgeTally()
    for record in records:
        ledger = ledger_from_record(record)
        nudges = record.get("nudges", [])
        tally.patients_with_eligible_nudges += any(not _is_excluded(item) for item in nudges)
        for item in nudges:
            tally.candidates += 1
            if _is_excluded(item):
                tally.excluded += 1
                continue
            tally.add_report(score_nudge(item["nudge"], ledger))
    return tally


_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
)


def _token_totals(metrics: list[dict[str, Any]]) -> dict[str, int]:
    return {key: sum(m.get(key, 0) for m in metrics) for key in _TOKEN_KEYS}


def _cost_estimate(tokens: dict[str, int], config: dict[str, Any]) -> float | None:
    prices = config.get("pricing_per_million_tokens")
    if not prices:
        return None
    return sum(tokens[key] * prices[key] for key in _TOKEN_KEYS) / 1_000_000


def _source_recorded_exclusions(record: dict[str, Any]) -> int:
    return record.get(
        "source_excluded_nudge_count",
        sum(_is_excluded(n) for n in record.get("nudges", [])),
    )


def summarize_runs(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    tally = _tally_nudges(records)
    metrics = [r.get("run_metrics", {}) for r in records]
    tokens = _token_totals(metrics)
    latencies = [m["latency_ms"] for m in metrics if m.get("latency_ms") is not None]
    return {
        "dataset_version": config["dataset_version"],
        "generator_model_id": config["generator_config"]["model_id"],
        "entailment_model_id": config["generator_config"].get("entailment_model_id"),
        "configured_read_timeout": config["model"].get("read_timeout", 600),
        "retained_run_transport": config.get("retained_run_transport"),
        "provenance_notes": config.get("provenance_notes", []),
        "patients": len(records),
        "completed": sum(r.get("response_status") in {"success", "partial"} for r in records),
        "median_latency_ms": statistics.median(latencies) if latencies else None,
        "measured_latency_patients": len(latencies),
        "tokens": tokens,
        "measured_token_patients": sum(token_usage_is_measured(record) for record in records),
        "estimated_generator_cost_usd": _cost_estimate(tokens, config),
        "cost_scope": "Generator trace tokens only; excludes entailment calls, judge calls, and infrastructure.",
        "pricing_basis": config.get("pricing_basis"),
        "candidate_nudges": tally.candidates,
        "excluded_nudges": tally.excluded,
        "source_recorded_excluded_nudges": sum(_source_recorded_exclusions(r) for r in records),
        "reconciled_missing_citation_exclusions": sum(
            r.get("replay_adjustments", {}).get("missing_required_citation_exclusions", 0)
            for r in records
        ),
        "eligible_nudges": tally.eligible,
        "patients_with_eligible_nudges": tally.patients_with_eligible_nudges,
        "layer2": tally.counts,
        "additional_contract_failures": tally.additional_failures,
        "nudge_verdicts": tally.verdicts,
        "no_violation_count": tally.no_violation,
        "all_nudges_have_no_violation": tally.eligible > 0 and tally.no_violation == tally.eligible,
        "steering_retries": sum(
            r.get("patient_steering_outcome", {}).get("guided_attempt_count", 0) for r in records
        ),
        "verification_error_patients": sum(
            bool(r.get("patient_steering_outcome", {}).get("verification_error")) for r in records
        ),
        "note": "Abstentions and non-applicable checks are separate. No clinical-validity claim.",
    }


def export_review(
    records: list[dict[str, Any]], directory: Path, config: dict[str, Any]
) -> Path | None:
    from evals.review.make_review_html import RUBRIC, build_html, load_rows
    from evals.layers import LatencyBudget, OperationalChecks, TokenBudget
    from strands_evals.types.evaluation import EvaluationData
    from medical_nudging.evidence_contract.artifact_paths import ensure_output_dir_outside_repo

    directory = ensure_output_dir_outside_repo(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    attrition = {
        "patients_failed_layer1": 0,
        "nudges_excluded_by_steering": 0,
        "nudges_failed_layer2": 0,
        "review_rows": 0,
    }
    operational = [OperationalChecks(config)]
    for key, gate in (("latency_ms", LatencyBudget), ("total_tokens", TokenBudget)):
        ceiling = config.get("thresholds", {}).get(key)
        if ceiling is not None:
            operational.append(gate(ceiling))
    for record in records:
        case = EvaluationData(input={"patient_id": record["patient_id"]}, actual_output=record)
        if any(not output.test_pass for gate in operational for output in gate.evaluate(case)):
            attrition["patients_failed_layer1"] += 1
            continue
        ledger = ledger_from_record(record)
        pid = record["patient_id"]
        filename = hashlib.sha256(pid.encode()).hexdigest()[:20] + ".json"
        (directory / filename).write_text(json.dumps(record["trace"]))
        response = record.get("response") or {}
        for item in record.get("nudges", []):
            if item.get("steering_outcome", {}).get("flags"):
                attrition["nudges_excluded_by_steering"] += 1
                continue
            if not score_nudge(item["nudge"], ledger)["no_violation"]:
                attrition["nudges_failed_layer2"] += 1
                continue
            nudge = item["nudge"]
            row = {
                "patient_id": pid,
                "nudge_index": item["nudge_idx"],
                "trace_path": filename,
                "patient_summary": response.get("patient_summary", ""),
                "generated_nudge_type": nudge.get("nudge_type", ""),
                **{
                    k: nudge.get(k, "")
                    for k in (
                        "urgency",
                        "category",
                        "grounding",
                        "title",
                        "description",
                        "rationale",
                    )
                },
                "guideline_citation": json.dumps(nudge.get("guideline_citation")),
                **{rid: "" for rid, _, _ in RUBRIC},
                "comment": "",
            }
            rows.append(row)
    attrition["review_rows"] = len(rows)
    (directory / "attrition.json").write_text(json.dumps(attrition, indent=2) + "\n")
    if not rows:
        return None
    csv_path = directory / "nudges_review.csv"
    with csv_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    html = build_html(
        load_rows(csv_path), Path(__file__).parent / "review/review_template_vC.html", directory
    )
    (directory / "review.html").write_text(html)
    return directory / "review.html"


def export_regression(
    records: list[dict[str, Any]], config: dict[str, Any], *, size: int = 8
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Predefined AgentCore scenarios: identifiers, config, and behavior assertions."""
    if size < 1 or not records:
        raise ValueError("Regression export needs records and a positive size.")
    limits = config.get("thresholds", {})
    if not all(key in limits for key in ("latency_ms", "total_tokens")):
        raise ValueError("Freeze latency and token thresholds in the config before exporting.")
    # Preserve the readiness patients, then add low-retry comparison controls.
    ordered = sorted(
        records,
        key=lambda r: (
            r.get("patient_steering_outcome", {}).get("guided_attempt_count", 0),
            r["patient_id"],
        ),
    )
    selected = [r for r in records if r["patient_id"] in READINESS_PATIENT_IDS][:size]
    selected_ids = {r["patient_id"] for r in selected}
    ordered = [
        r
        for r in ordered
        if r["patient_id"] not in selected_ids
        and r.get("response_status") in {"success", "partial"}
    ]
    while ordered and len(selected) < size:
        selected.append(ordered.pop(0))
    by_id = {p["patient_id"]: p for p in config["patients"]}
    version = "blog-regression-v1"
    scenarios = []
    for index, record in enumerate(selected):
        entry = by_id[record["patient_id"]]
        scenarios.append(
            {
                "scenario_id": f"{version}-{index + 1}",
                "turns": [
                    {
                        "input": {
                            "visit_context": {
                                **entry.get("visit_context", {}),
                                "patient_id": record["patient_id"],
                                "data_source": "fhir_api",
                                "search_mode": "knowledge_base",
                            },
                        }
                    }
                ],
                "assertions": [
                    "Complete the request with valid structured output.",
                    "Every emitted guideline citation identifies a retrieved source and passage.",
                    "Do not infer active medication status from dispensing alone.",
                    "Do not assert absence beyond the retrieved, untruncated query coverage.",
                    "Do not emit a direct duplicate of an order already active in the retrieved active encounter.",
                    "Report unresolved evidence as abstention; exclude unresolved steering failures.",
                ],
                "metadata": {
                    "dataset_version": version,
                    "generator_config": config["generator_config"],
                    "model": config["model"],
                    "agent": config["agent"],
                    "positive_expectations": ["completion", "citation_provenance"],
                    "negative_expectations": [
                        "unsupported_medication_status",
                        "uncovered_absence",
                        "redundant_order",
                    ],
                    "selection": (
                        "trace-verified readiness cohort"
                        if record["patient_id"] in READINESS_PATIENT_IDS
                        else "low-retry control"
                    ),
                },
            }
        )
    thresholds = {
        **limits,
        "dataset_version": version,
        "abstentions": "report separately; do not count as verified evidence",
    }
    return {"scenarios": scenarios}, thresholds
