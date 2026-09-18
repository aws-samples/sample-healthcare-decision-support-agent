"""Re-score an existing run, validate its judge, and export reader-owned assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from evals.controls import HolisticJudge, build_controls, control_summary, load_control_exclusions
from evals.layers import EvidenceContract, LatencyBudget, OperationalChecks, TokenBudget
from evals.replay import load_runs, validate_provenance
from evals.reporting import export_regression, export_review, summarize_runs
from evals.runner import Completion, run_experiment


def _parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile")
    parser.add_argument("--validate-judge", action="store_true")
    parser.add_argument("--judge-prompt-version", choices=("v1", "v2"), default="v1")
    parser.add_argument(
        "--exclude-controls",
        type=Path,
        help="Previous control result directory; exclude its task names from the new set.",
    )
    parser.add_argument("--export-review", action="store_true")
    parser.add_argument("--export-regression", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    if (args.exclude_controls or args.judge_prompt_version != "v1") and not args.validate_judge:
        parser.error("Judge prompt/exclusion options require --validate-judge.")
    if args.judge_prompt_version == "v2" and not args.exclude_controls:
        parser.error("Judge v2 requires --exclude-controls for a held-out re-test.")
    return parser, args


def _check_cohort(
    records: list[dict],
    config: dict,
    allow_partial: bool,
    parser: argparse.ArgumentParser,
) -> set[str]:
    """Return the saved patient ids, or exit when they do not match the configured cohort."""
    expected = {p["patient_id"] for p in config["patients"]}
    actual = {r["patient_id"] for r in records}
    if len(actual) != len(records):
        parser.error("Duplicate patient run records.")
    if actual - expected or (expected - actual and not allow_partial):
        parser.error("Saved patient set differs from the configured cohort.")
    return actual


def _replay_content_sha256(records: list[dict]) -> str:
    """Source content is part of the task identity, even when a path is reused."""
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda r: r["patient_id"]):
        digest.update(json.dumps(record, sort_keys=True).encode())
    return digest.hexdigest()


def _build_evaluators(config: dict) -> list:
    evaluators = [Completion(), OperationalChecks(config), EvidenceContract()]
    limits = config.get("thresholds", {})
    if limits.get("latency_ms"):
        evaluators.append(LatencyBudget(limits["latency_ms"]))
    if limits.get("total_tokens"):
        evaluators.append(TokenBudget(limits["total_tokens"]))
    return evaluators


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _score_replay(records: list[dict], config: dict, output_dir: Path) -> Path:
    by_id = {record["patient_id"]: record for record in records}
    _, path = run_experiment(
        config=config,
        output_dir=output_dir,
        task=lambda case: {"output": by_id[case.input["patient_id"]]},
        evaluators=_build_evaluators(config),
        max_concurrency=4,
    )
    return path


def _validate_judge(
    records: list[dict],
    config: dict,
    excluded_names: set[str],
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> dict:
    options = config["judge"]
    cases, attrition = build_controls(
        records, options["control_cap"], excluded_names=excluded_names
    )
    if excluded_names and len(cases) != options["control_cap"]:
        parser.error("Not enough unused eligible controls for the fixed held-out set.")
    judge = HolisticJudge(
        options["model_id"],
        config["generator_config"]["model_id"],
        reasoning_effort=options["reasoning_effort"],
        max_concurrency=options.get("max_concurrency", 1),
        cache_dir=args.output_dir / "judge-cache",
        prompt_version=args.judge_prompt_version,
    )
    report, control_path = run_experiment(
        config={**config, "dataset_version": config["dataset_version"] + "-controls"},
        output_dir=args.output_dir,
        task=lambda case: {"output": case.input},
        evaluators=[judge],
        cases=cases,
        max_concurrency=options.get("max_concurrency", 1),
    )
    result = {
        **control_summary(report, len(cases)),
        "attrition": attrition,
        "prompt_version": args.judge_prompt_version,
        "prompt_sha256": judge.prompt_sha256,
        "excluded_controls_sha256": options.get("excluded_controls_sha256"),
    }
    _write_json(control_path / "control_summary.json", result)
    return result


def main() -> int:
    parser, args = _parse_args()
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    config = json.loads(args.config.read_text())
    excluded_names: set[str] = set()
    if args.validate_judge:
        config["judge"] = {**config["judge"], "prompt_version": args.judge_prompt_version}
    records = load_runs(args.runs)
    validate_provenance(records, config)
    actual = _check_cohort(records, config, args.allow_partial, parser)
    config["replay_content_sha256"] = _replay_content_sha256(records)
    if args.exclude_controls:
        try:
            excluded_names, excluded_hash = load_control_exclusions(
                args.exclude_controls, records, config
            )
        except ValueError as error:
            parser.error(str(error))
        config["judge"]["excluded_controls_sha256"] = excluded_hash
    config["patients"] = [p for p in config["patients"] if p["patient_id"] in actual]
    path = _score_replay(records, config, args.output_dir)
    summary = summarize_runs(records, config)
    _write_json(path / "summary.json", summary)
    if args.export_regression:
        dataset, thresholds = export_regression(records, config)
        _write_json(path / "regression_dataset.json", dataset)
        _write_json(path / "regression_thresholds.json", thresholds)
    if args.export_review:
        review = export_review(records, path / "clinician_review", config)
        summary["clinician_review_exported"] = review is not None
        _write_json(path / "summary.json", summary)
    if args.validate_judge:
        summary["judge_validation"] = _validate_judge(records, config, excluded_names, args, parser)
        _write_json(path / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    print(f"Results: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
