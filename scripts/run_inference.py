#!/usr/bin/env python
"""Run inference on local patient files, through the orchestrator or AgentCore.

Sample discovery lives here; caching, OTEL span export, trace saving, and reporting
are the shared batch recipe in ``medical_nudging.batch``.

Configuration:
    Copy config/settings.yaml.example to config/settings.yaml and fill in:
    - agent_arn: AgentCore runtime ARN
    - aws_profile: AWS profile name
    - aws_region: AWS region
    - tracing.cloudwatch_log_group: CloudWatch log group for AgentCore traces

Usage:
    # Run local orchestrator inference (traces enabled by default)
    uv run scripts/run_inference.py --mode local --samples 10

    # Run without trace capture
    uv run scripts/run_inference.py --mode local --samples 2 --no-trace

    # Run AgentCore inference (traces fetched from CloudWatch by default)
    uv run scripts/run_inference.py --mode agentcore --samples 10

    # AgentCore without trace fetching
    uv run scripts/run_inference.py --mode agentcore --samples 5 --no-trace

    # Override config with CLI args
    uv run scripts/run_inference.py --mode agentcore --agent-arn arn:aws:... --samples 10

    # Run with CloudWatch metrics publishing
    uv run scripts/run_inference.py --mode local --samples 10 --publish-metrics

    # Run with caching (skip samples already in cache)
    uv run scripts/run_inference.py --mode local --samples 10 --cache

    # Clear cache before running
    uv run scripts/run_inference.py --mode local --samples 10 --cache --clear-cache

    # Run with experiment config file
    uv run scripts/run_inference.py --exp-config config/exps/local_baseline.yaml
"""

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, TaskID

from medical_nudging.batch import (
    InferenceCache,
    open_inference_cache,
    otel_span_log,
    progress_bar,
    run_batch,
    summary_tables,
)
from medical_nudging.config import (
    apply_experiment_config,
    experiment_output_dir,
    get_value,
    load_config,
    load_experiment_config,
)
from medical_nudging.inference import (
    build_inference_cache_key,
    fetch_agentcore_traces,
    generate_report,
    run_agentcore,
    run_local,
)
from medical_nudging.models import InferenceResult
from medical_nudging.run_artifacts import save_run

CACHE_DIR = Path(".cache/inference")

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("inference")
console = Console()


def detect_format(content: str) -> str:
    """Detect format from file content.

    Detection logic:
    - Starts with '<' → CCDA (XML)
    - Starts with '{':
      - Has "resourceType": "Bundle" → FHIR
      - Has "demographics" → preparsed
      - Otherwise → generic
    """
    content = content.strip()
    if content.startswith("<"):
        return "ccda"
    if content.startswith("{"):
        try:
            data = json.loads(content)
            if data.get("resourceType") == "Bundle":
                return "fhir"
            if "demographics" in data:
                return "preparsed"
            return "generic"
        except json.JSONDecodeError:
            return "unknown"
    return "unknown"


def get_sample_files(n_samples: int, seed: int, data_dirs: list[Path]) -> list[dict]:
    """Get random sample of patient files from one or more directories with auto-detected format.

    Args:
        n_samples: Number of samples to return
        seed: Random seed for reproducibility
        data_dirs: Directories containing patient files (.xml or .json)

    Returns:
        List of sample dicts with sample_id, file_path, and format
    """
    random.seed(seed)
    files: list[Path] = []

    for data_dir in data_dirs:
        if not data_dir.exists():
            log.warning(f"Directory not found: {data_dir}")
            continue
        dir_files = list(data_dir.glob("*.xml")) + list(data_dir.glob("*.json"))
        if not dir_files:
            # The Synthea sample archives unpack into a subdirectory (ccda/, fhir/).
            dir_files = list(data_dir.rglob("*.xml")) + list(data_dir.rglob("*.json"))
        if not dir_files:
            log.warning(f"No .xml or .json files found in: {data_dir}")
        files.extend(dir_files)

    if not files:
        return []

    # Sample files and detect format
    selected_files = random.sample(files, min(n_samples, len(files)))
    samples = []
    for f in selected_files:
        content = f.read_text()
        fmt = detect_format(content)
        samples.append({"sample_id": f.stem, "file_path": str(f), "format": fmt})

    return samples


def get_sample_files_from_sources(data_sources: list[dict], seed: int) -> list[dict]:
    """Get patient files with per-source sampling and visit context.

    Each data_source dict specifies a directory, sample count, and optional
    visit_context that overrides the global default for those samples.

    Args:
        data_sources: List of dicts with keys: dir, samples, visit_context (optional)
        seed: Random seed for reproducibility

    Returns:
        List of sample dicts with sample_id, file_path, format, and visit_context
    """
    random.seed(seed)
    all_samples: list[dict] = []

    for source in data_sources:
        data_dir = Path(source["dir"])
        n_samples = source.get("samples", 10)
        source_visit_context = source.get("visit_context")

        if not data_dir.exists():
            log.warning(f"Directory not found: {data_dir}")
            continue

        files = list(data_dir.glob("*.xml")) + list(data_dir.glob("*.json"))
        if not files:
            log.warning(f"No .xml or .json files found in: {data_dir}")
            continue

        selected = random.sample(files, min(n_samples, len(files)))
        for f in selected:
            content = f.read_text()
            fmt = detect_format(content)
            sample: dict = {"sample_id": f.stem, "file_path": str(f), "format": fmt}
            if source_visit_context:
                sample["visit_context"] = source_visit_context
            all_samples.append(sample)

    return all_samples


def default_data_dirs() -> list[Path]:
    """Directories from ``inference.data_dirs``, else ``inference.data_dir``, else the
    ``inference.ccda_dir`` / ``inference.fhir_dir`` pair that ``settings.yaml.example`` ships."""
    configured = get_value("inference.data_dirs", default=None)
    if configured and isinstance(configured, list):
        return [Path(d) for d in configured]
    single = get_value("inference.data_dir", default=None)
    if single:
        return [Path(single)]
    legacy = [
        get_value("inference.ccda_dir", default=None),
        get_value("inference.fhir_dir", default=None),
    ]
    dirs = list(dict.fromkeys(Path(d) for d in legacy if d))
    return dirs or [Path("data/sample-ccda")]


def build_parser(config: dict) -> argparse.ArgumentParser:
    """CLI with defaults drawn from config/settings.yaml."""
    parser = argparse.ArgumentParser(
        description="Run inference on Medical Nudging Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["local", "agentcore"],
        default="local",
        help="Inference mode: local (orchestrator) or agentcore (boto3)",
    )
    parser.add_argument(
        "--agent-arn",
        default=get_value("agent_arn", "AGENT_ARN"),
        help="AgentCore ARN (default: from config)",
    )
    parser.add_argument(
        "--region",
        default=config.get("aws_region", "us-east-1"),
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=get_value("inference.samples", default=2),
        help="Total samples to process (split evenly CCDA/FHIR)",
    )
    parser.add_argument(
        "--data-dirs",
        type=Path,
        nargs="+",
        default=default_data_dirs(),
        help="Directories containing patient files (format auto-detected from content)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=get_value("inference.seed", default=42),
        help="Random seed for reproducibility",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed progress")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to save results (default: results/, or results/experiments/<name> "
        "with --exp-config)",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="Disable trace capture (traces are enabled by default)",
    )
    parser.add_argument(
        "--log-group",
        default=get_value("tracing.cloudwatch_log_group", default=""),
        help="CloudWatch log group for AgentCore traces (default: from config)",
    )
    parser.add_argument(
        "--publish-metrics",
        action="store_true",
        help="Publish metrics to CloudWatch",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Enable caching to skip samples already processed",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cache before running (use with --cache)",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        default=get_value("model.model_id", default="us.anthropic.claude-sonnet-5"),
        help="Model ID for inference (default: from config)",
    )
    parser.add_argument(
        "--context-condition",
        type=str,
        choices=["full", "summaries_only", "no_guidelines"],
        default="full",
        help="Context condition for guidelines access",
    )
    parser.add_argument(
        "--custom-instructions-preset",
        type=str,
        default=get_value("inference.custom_instructions_preset", default=None),
        help="Custom instructions preset name (loads from config/custom_instructions/<name>.yaml)",
    )
    parser.add_argument(
        "--exp-config",
        type=Path,
        help="Experiment config YAML file (overrides other args)",
    )
    return parser


def apply_experiment_overrides(args: argparse.Namespace, exp_config: dict[str, Any]) -> None:
    """Fold an experiment YAML into ``args``.

    Runtime settings (model, agent, corpus) land in the shared config cache; the loop
    parameters (samples, seed, data locations) stay with this script.
    """
    apply_experiment_config(exp_config)
    model_section = exp_config.get("model", {})
    args.model_id = model_section.get("model_id", args.model_id)
    args.extra_headers = model_section.get("extra_headers", {})
    if "context" in exp_config:
        args.context_condition = exp_config["context"].get("condition", args.context_condition)
    if "inference" not in exp_config:
        return
    inf_cfg = exp_config["inference"]
    args.samples = inf_cfg.get("samples", args.samples)
    args.seed = inf_cfg.get("seed", args.seed)
    if "data_sources" in inf_cfg:
        args.data_sources = inf_cfg["data_sources"]
    elif "data_dirs" in inf_cfg:
        args.data_dirs = [Path(d) for d in inf_cfg["data_dirs"]]
    elif "data_dir" in inf_cfg:
        args.data_dirs = [Path(inf_cfg["data_dir"])]


def resolve_output_dir(requested: Path | None, exp_config: dict[str, Any]) -> Path:
    """Explicit --output-dir, else the experiment's directory, else ``inference.output_dir``."""
    if requested is not None:
        return requested
    if exp_config:
        return experiment_output_dir(exp_config)
    return Path(get_value("inference.output_dir", default="results"))


def open_cache(args: argparse.Namespace) -> InferenceCache | None:
    cache = open_inference_cache(CACHE_DIR, enabled=args.cache, clear=args.clear_cache)
    if cache is None:
        return None
    if args.clear_cache:
        log.info("[yellow]Cache cleared[/yellow]")
    log.info(f"[cyan]Cache enabled: {CACHE_DIR}[/cyan]")
    return cache


def discover_samples(args: argparse.Namespace) -> list[dict]:
    """Per-source sampling when the experiment names data_sources, else one global sample."""
    if args.data_sources:
        return get_sample_files_from_sources(args.data_sources, args.seed)
    return get_sample_files(args.samples, args.seed, args.data_dirs)


def describe_formats(samples: list[dict]) -> str:
    """``"3 CCDA, 2 FHIR"`` for the loaded-samples log line."""
    format_counts: dict[str, int] = {}
    for sample in samples:
        fmt = sample["format"]
        format_counts[fmt] = format_counts.get(fmt, 0) + 1
    return ", ".join(f"{count} {fmt.upper()}" for fmt, count in sorted(format_counts.items()))


def load_custom_instructions(preset_name: str) -> str:
    """Instructions text of ``config/custom_instructions/<preset>.yaml`` (empty when the key is absent).

    Raises:
        FileNotFoundError: when no preset file exists under that name.
    """
    preset_path = Path("config/custom_instructions") / f"{preset_name}.yaml"
    if not preset_path.exists():
        raise FileNotFoundError(f"Custom instructions preset not found: {preset_path}")
    import yaml

    with open(preset_path) as f:
        preset = yaml.safe_load(f)
    instructions_text = preset.get("instructions", "")
    if instructions_text:
        log.info(f"[bold cyan]Custom instructions loaded from preset: {preset_name}[/bold cyan]")
    else:
        log.warning(f"Preset {preset_name} has no 'instructions' key")
    return instructions_text


def make_sample_runner(
    args: argparse.Namespace, visit_context: dict, enable_trace: bool, profile: str | None
) -> Callable[[dict], tuple[InferenceResult, dict | None]]:
    """The per-sample call for the chosen mode; per-source visit context wins over the global one."""

    def run_sample(sample: dict) -> tuple[InferenceResult, dict | None]:
        if args.mode == "local":
            return run_local(
                sample,
                sample.get("visit_context", visit_context),
                enable_trace=enable_trace,
                model_id=args.model_id,
                context_condition=args.context_condition,
                extra_headers=args.extra_headers or None,
            )
        return run_agentcore(sample, visit_context, args.agent_arn, args.region, profile), None

    return run_sample


def make_cache_key(args: argparse.Namespace, visit_context: dict) -> Callable[[dict], str]:
    def cache_key(sample: dict) -> str:
        return build_inference_cache_key(
            args.model_id,
            Path(sample["file_path"]).read_bytes(),
            args.context_condition,
            sample.get("visit_context", visit_context),
        )

    return cache_key


def log_sample_result(result: InferenceResult, cache_hit: bool) -> None:
    """Verbose per-sample line: status, nudge count, latency, and any error."""
    if cache_hit:
        log.info("  [dim]Cache hit[/dim]")
    color = {"success": "green", "partial": "yellow", "error": "red"}.get(result.status, "white")
    log.info(f"  [{color}]{result.status}[/] - {result.nudge_count} nudges - {result.latency_ms}ms")
    if result.error:
        log.error(f"    {result.error}")


def progress_reporter(
    progress: Progress, task: TaskID, verbose: bool
) -> Callable[[dict, InferenceResult, bool], None]:
    def record_progress(sample: dict, result: InferenceResult, cache_hit: bool) -> None:
        progress.update(task, description=f"{sample['format'].upper()}: {sample['sample_id']}")
        if verbose:
            log_sample_result(result, cache_hit)
        progress.advance(task)

    return record_progress


def publish_metrics(
    report: Any,
    results: list[InferenceResult],
    traces: list[tuple[str, dict | None]],
    mode: str,
    region: str,
    profile: str | None,
) -> None:
    """Push the batch summary and per-inference timings to CloudWatch; a failure only warns."""
    try:
        from medical_nudging.tracing.cloudwatch_metrics import CloudWatchMetricsPublisher

        metrics_publisher = CloudWatchMetricsPublisher(region=region, profile=profile)
        metrics_publisher.publish_batch_summary(
            total_samples=report.total_samples,
            success_count=report.success_count,
            error_count=report.error_count,
            avg_latency_ms=report.avg_latency_ms,
            mode=mode,
        )
        publish_inference_metrics(metrics_publisher, results, traces, mode)
        log.info("[cyan]Metrics published to CloudWatch namespace 'MedicalNudging'[/cyan]")
    except Exception as e:
        log.warning(f"[yellow]Failed to publish CloudWatch metrics: {e}[/yellow]")


def publish_inference_metrics(
    metrics_publisher: Any,
    results: list[InferenceResult],
    traces: list[tuple[str, dict | None]],
    mode: str,
) -> None:
    """One metric record per sample that has both a trace and a result."""
    by_sample_id = {result.sample_id: result for result in results}
    for sample_id, trace_dict in traces:
        result = by_sample_id.get(sample_id)
        if not trace_dict or result is None:
            continue
        timing = trace_dict.get("timing", {})
        metrics_publisher.publish_inference_metrics(
            processing_time_ms=result.latency_ms,
            tool_duration_ms=timing.get("tool_ms", 0),
            model_duration_ms=timing.get("model_ms", 0),
            nudge_count=result.nudge_count,
            status=result.status,
            mode=mode,
        )


def resolve_visit_context(args: argparse.Namespace) -> dict | None:
    """Global ``inference.visit_context`` plus any preset instructions.

    Returns ``None`` (after logging why) when neither a global context nor per-source
    contexts exist, or when the named preset is missing.
    """
    visit_context = get_value("inference.visit_context", default={})
    if not visit_context and not args.data_sources:
        log.error("inference.visit_context not set in config/settings.yaml")
        return None
    if visit_context:
        log.info(f"[bold]Visit context: {visit_context}[/bold]")
    else:
        log.info("[bold]Visit context: per-source (from data_sources config)[/bold]")

    if args.custom_instructions_preset:
        try:
            instructions_text = load_custom_instructions(args.custom_instructions_preset)
        except FileNotFoundError as error:
            log.error(str(error))
            return None
        if instructions_text:
            visit_context["custom_instructions"] = instructions_text
    return visit_context


def run_and_save(
    args: argparse.Namespace,
    samples: list[dict],
    visit_context: dict,
    cache: InferenceCache | None,
    profile: str | None,
) -> None:
    """Run the batch with progress output, then report, save, and optionally publish."""
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"inference_{args.mode}_{run_timestamp}"
    enable_trace = not args.no_trace
    if enable_trace:
        log.info("[cyan]Trace capture enabled[/cyan]")

    with (
        otel_span_log(args.output_dir / f"otel_traces_{stem}.jsonl") as span_log,
        progress_bar(console) as progress,
    ):
        log.info(f"[cyan]OTEL trace JSONL: {span_log}[/cyan]")
        task = progress.add_task("Processing...", total=len(samples))
        outcome = run_batch(
            samples,
            make_sample_runner(args, visit_context, enable_trace, profile),
            cache=cache,
            cache_key=make_cache_key(args, visit_context),
            on_complete=progress_reporter(progress, task, args.verbose),
        )

    results = outcome.results
    traces = outcome.traces
    if cache is not None:
        log.info(f"[cyan]Cache: {outcome.cache_hits} hits, {outcome.cache_misses} misses[/cyan]")

    # Fetch AgentCore traces from CloudWatch after all invocations complete
    if args.mode == "agentcore" and enable_trace:
        traces = fetch_agentcore_traces(
            results, args.log_group, args.region, profile, agent_arn=args.agent_arn
        )

    report = generate_report(results, args.mode)
    for table in summary_tables(report, f"Inference Summary ({report.mode} mode)"):
        console.print(table)

    saved = save_run(
        report,
        args.output_dir / f"{stem}.json",
        traces if enable_trace else (),
        traces_dir=args.output_dir / "traces" / stem,
    )
    if saved.traces_dir:
        log.info(f"[cyan]Traces saved to {saved.traces_dir}[/cyan]")
    log.info(f"[green]Results saved to {saved.results_path}[/green]")

    if args.publish_metrics:
        publish_metrics(report, results, traces, args.mode, args.region, profile)


def main():
    config = load_config()

    # Set AWS_PROFILE from config to ensure consistency
    if config.get("aws_profile"):
        os.environ["AWS_PROFILE"] = config["aws_profile"]

    args = build_parser(config).parse_args()
    args.extra_headers = {}
    args.data_sources = None

    if args.mode == "agentcore" and not args.agent_arn:
        log.error("--agent-arn required (set in config/settings.yaml or via CLI)")
        return 1

    exp_config: dict[str, Any] = {}
    if args.exp_config:
        try:
            exp_config = load_experiment_config(args.exp_config)
        except (OSError, ValueError) as error:
            log.error(f"Experiment config not usable: {error}")
            return 1
        log.info(f"[bold]Loaded experiment config: {exp_config['experiment']['name']}[/bold]")
        apply_experiment_overrides(args, exp_config)

    args.output_dir = resolve_output_dir(args.output_dir, exp_config)
    log.info(f"[bold]Output directory: {args.output_dir}[/bold]")
    cache = open_cache(args)

    samples = discover_samples(args)
    if not samples:
        log.error("No sample files found")
        return 1
    log.info(f"[bold]Loaded {len(samples)} samples ({describe_formats(samples)})[/bold]")

    visit_context = resolve_visit_context(args)
    if visit_context is None:
        return 1

    run_and_save(args, samples, visit_context, cache, config.get("aws_profile"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
