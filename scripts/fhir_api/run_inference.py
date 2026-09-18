#!/usr/bin/env python
"""Run inference with the agent querying AWS HealthLake on demand.

Instead of reading patient files, each sample is a HealthLake patient id and the
agent pulls what it needs through the ``query_patient_fhir`` tool. Caching, OTEL
span export, trace saving, and reporting are the shared batch recipe in
``medical_nudging.batch``; this script only discovers patients and calls it.

Configuration:
    All inference parameters come from config/settings.yaml:
      - fhir_api.datastore_endpoint: HealthLake DatastoreEndpoint
      - inference.samples: number of patients
      - inference.seed: random seed
      - inference.fhir.patient_ids: explicit patient IDs (empty = auto-discover)
      - inference.fhir.visit_context: visit context dict
      - model.model_id: model to use

    Use --exp-config to override with an experiment YAML file.

Usage:
    # Run using settings.yaml defaults
    uv run scripts/fhir_api/run_inference.py -v

    # With caching
    uv run scripts/fhir_api/run_inference.py --cache

    # Override with experiment config
    uv run scripts/fhir_api/run_inference.py --exp-config config/exps/fhir_api_baseline.yaml

    # Dry run (list discovered patients, don't run inference)
    uv run scripts/fhir_api/run_inference.py --dry-run
"""

import argparse
import logging
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, TaskID
from rich.table import Table

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
    get_fhir_config,
    get_value,
    load_config,
    load_experiment_config,
)
from medical_nudging.inference import build_inference_cache_key, generate_report, run_fhir_api
from medical_nudging.models import InferenceResult
from medical_nudging.run_artifacts import save_run

CACHE_DIR = Path(".cache/fhir_inference")

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("fhir_inference")
console = Console()


def discover_patients(datastore_endpoint: str, region: str, count: int, seed: int) -> list[dict]:
    """Discover patient IDs from the HealthLake datastore.

    Args:
        datastore_endpoint: HealthLake DatastoreEndpoint (used verbatim)
        region: AWS region hosting the datastore
        count: Number of patients to return
        seed: Random seed for reproducible sampling

    Returns:
        List of dicts with id, name, gender, birthDate
    """
    from medical_nudging.search.fhir_client import create_healthlake_client

    client = create_healthlake_client(datastore_endpoint, region=region)
    # Fetch more than needed so random.sample has room. _count is capped at 100
    # by HealthLake and clamped by the client.
    fetch_count = max(count * 2, count + 10)
    patients_raw = client.search("Patient", {"_count": str(fetch_count)})

    patients = []
    for p in patients_raw:
        name = "Unknown"
        names = p.get("name", [])
        if names:
            given = " ".join(names[0].get("given", []))
            family = names[0].get("family", "")
            name = f"{given} {family}".strip()
        patients.append(
            {
                "id": p["id"],
                "name": name,
                "gender": p.get("gender", "unknown"),
                "birthDate": p.get("birthDate", "unknown"),
            }
        )

    random.seed(seed)
    if len(patients) > count:
        patients = random.sample(patients, count)

    return patients


@dataclass
class RunSettings:
    """Loop parameters for one FHIR API run: settings.yaml first, then the experiment YAML."""

    model_id: str
    context_condition: str = "full"
    agent_runtime_config: dict[str, Any] = field(default_factory=dict)
    opensearch_index: str = "guidelines"
    n_samples: int = 3
    max_concurrency: int = 1
    seed: int = 42
    patient_ids: list[str] = field(default_factory=list)
    visit_context: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, Any] = field(default_factory=dict)

    @property
    def max_nudges(self) -> int:
        return self.agent_runtime_config.get("max_nudges", 5)

    @classmethod
    def from_settings(cls) -> "RunSettings":
        return cls(
            model_id=get_value(
                "model.model_id", default="us.anthropic.claude-sonnet-5"
            ),
            agent_runtime_config=dict(get_value("agent", default={}) or {}),
            opensearch_index=get_value("opensearch_index", default="guidelines"),
            n_samples=get_value("inference.samples", default=3),
            max_concurrency=get_value("inference.concurrency", default=1),
            seed=get_value("inference.seed", default=42),
            patient_ids=get_value("inference.fhir.patient_ids", default=[]) or [],
            visit_context=get_value(
                "inference.fhir.visit_context",
                default={
                    "visit_type": "ambulatory",
                    "specialty": "general",
                    "chief_complaint": "Routine follow-up",
                },
            ),
        )

    def apply_experiment(self, exp_config: dict[str, Any]) -> None:
        """Fold an experiment YAML in.

        Runtime settings (model, agent, corpus, HealthLake endpoint) land in the shared
        config cache; the loop parameters stay here.
        """
        apply_experiment_config(exp_config)
        model_section = exp_config.get("model", {})
        self.model_id = model_section.get("model_id", self.model_id)
        self.extra_headers = model_section.get("extra_headers", {})
        if "context" in exp_config:
            self.context_condition = exp_config["context"].get("condition", self.context_condition)
        if "agent" in exp_config:
            self.agent_runtime_config.update(exp_config["agent"])
        self.opensearch_index = exp_config.get("opensearch_index", self.opensearch_index)
        if "inference" in exp_config:
            self._apply_inference_section(exp_config["inference"])

    def _apply_inference_section(self, inf_cfg: dict[str, Any]) -> None:
        self.n_samples = inf_cfg.get("samples", self.n_samples)
        self.max_concurrency = inf_cfg.get("concurrency", self.max_concurrency)
        self.seed = inf_cfg.get("seed", self.seed)
        if "fhir_api" in inf_cfg:
            fhir_cfg = inf_cfg["fhir_api"]
            self.n_samples = fhir_cfg.get("samples", self.n_samples)
            self.patient_ids = fhir_cfg.get("patient_ids", self.patient_ids) or []
        if "visit_context" in inf_cfg:
            self.visit_context = inf_cfg["visit_context"]

    def log_summary(self) -> None:
        log.info("[bold]Visit context: %s[/bold]", self.visit_context)
        log.info("[bold]Model: %s[/bold]", self.model_id)
        log.info("[bold]OpenSearch index: %s[/bold]", self.opensearch_index)
        log.info("[bold]Max nudges: %d[/bold]", self.max_nudges)
        log.info("[bold]Tool limits: %s[/bold]", self.agent_runtime_config.get("tool_limits", {}))
        log.info("[bold]Concurrency: %d[/bold]", self.max_concurrency)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run inference with the agent querying HealthLake on demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed progress")
    parser.add_argument("--dry-run", action="store_true", help="Discover patients without running")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to save results (default: results/experiments/<exp_name> or results/)",
    )
    parser.add_argument("--no-trace", action="store_true", help="Disable trace capture")
    parser.add_argument("--cache", action="store_true", help="Enable caching")
    parser.add_argument("--clear-cache", action="store_true", help="Clear cache before running")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Maximum concurrent patient runs (default: inference.concurrency or 1)",
    )
    parser.add_argument(
        "--exp-config",
        type=Path,
        help="Experiment config YAML (overrides settings.yaml values)",
    )
    return parser


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
    log.info("[cyan]Cache enabled: %s[/cyan]", CACHE_DIR)
    return cache


def resolve_patients(settings: RunSettings, fhir_url: str, aws_region: str) -> list[dict] | None:
    """Configured patient ids as-is, else a seeded sample from HealthLake; ``None`` when it fails."""
    if settings.patient_ids:
        return [
            {"id": pid, "name": "(configured)", "gender": "?", "birthDate": "?"}
            for pid in settings.patient_ids
        ]
    log.info("[bold]Discovering %d patients from %s...[/bold]", settings.n_samples, fhir_url)
    try:
        return discover_patients(fhir_url, aws_region, settings.n_samples, settings.seed)
    except (ConnectionError, OSError, RuntimeError) as e:
        log.error("[red]Failed to query HealthLake: %s[/red]", e)
        log.error(
            'Is the datastore deployed? terraform apply -var="healthlake_enabled=true", '
            "then import data with scripts/healthlake_import.py"
        )
        return None


def print_patient_table(patients: list[dict]) -> None:
    patient_table = Table(title=f"FHIR Patients ({len(patients)})")
    patient_table.add_column("ID", style="cyan")
    patient_table.add_column("Name")
    patient_table.add_column("Gender")
    patient_table.add_column("DOB")
    for p in patients:
        patient_table.add_row(p["id"], p["name"], p["gender"], p["birthDate"])
    console.print(patient_table)


def make_cache_key(settings: RunSettings) -> Callable[[dict], str]:
    runtime_identity = {
        "agent": settings.agent_runtime_config,
        "opensearch_index": settings.opensearch_index,
    }

    def cache_key(sample: dict) -> str:
        return build_inference_cache_key(
            settings.model_id,
            sample["patient_id"],
            settings.context_condition,
            settings.visit_context,
            runtime_identity,
        )

    return cache_key


def make_sample_runner(
    settings: RunSettings, enable_trace: bool
) -> Callable[[dict], tuple[InferenceResult, dict | None]]:
    def run_sample(sample: dict) -> tuple[InferenceResult, dict | None]:
        return run_fhir_api(
            sample,
            settings.visit_context,
            enable_trace=enable_trace,
            model_id=settings.model_id,
            context_condition=settings.context_condition,
            extra_headers=settings.extra_headers or None,
            response_config=settings.agent_runtime_config,
        )

    return run_sample


def log_sample_result(sample: dict, result: InferenceResult, cache_hit: bool) -> None:
    """Verbose per-sample line: status, nudge count, latency, and any error."""
    if cache_hit:
        log.info("  [dim]Cache hit: %s[/dim]", sample["sample_id"])
    color = {"success": "green", "partial": "yellow", "error": "red"}.get(result.status, "white")
    log.info(
        "  [%s]%s[/] - %d nudges - %dms",
        color,
        result.status,
        result.nudge_count,
        result.latency_ms,
    )
    if result.status == "error" and result.error:
        log.error("  [red]Error: %s[/red]", result.error)


def progress_reporter(
    progress: Progress, task: TaskID, verbose: bool
) -> Callable[[dict, InferenceResult, bool], None]:
    def record_progress(sample: dict, result: InferenceResult, cache_hit: bool) -> None:
        progress.update(task, description=f"FHIR: {sample['sample_id']}")
        if verbose:
            log_sample_result(sample, result, cache_hit)
        progress.advance(task)

    return record_progress


def run_config_record(settings: RunSettings) -> dict[str, Any]:
    """The provenance block saved alongside the report."""
    return {
        "model": {"model_id": settings.model_id},
        "opensearch_index": settings.opensearch_index,
        "agent": settings.agent_runtime_config,
        "context": {
            "data_source": "fhir_api",
            "condition": settings.context_condition,
            "visit_context": settings.visit_context,
        },
        "inference": {
            "concurrency": settings.max_concurrency,
            "seed": settings.seed,
        },
    }


def run_and_save(
    args: argparse.Namespace,
    settings: RunSettings,
    samples: list[dict],
    cache: InferenceCache | None,
) -> None:
    """Run the batch with progress output, then report and save."""
    log.info("[bold]Running %d FHIR API samples[/bold]", len(samples))

    enable_trace = not args.no_trace
    if enable_trace:
        log.info("[cyan]Trace capture enabled[/cyan]")

    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"inference_fhir_api_{run_timestamp}"

    with (
        otel_span_log(args.output_dir / f"otel_traces_{stem}.jsonl") as span_log,
        progress_bar(console) as progress,
    ):
        log.info("[cyan]OTEL trace JSONL: %s[/cyan]", span_log)
        task = progress.add_task("Processing...", total=len(samples))
        outcome = run_batch(
            samples,
            make_sample_runner(settings, enable_trace),
            cache=cache,
            cache_key=make_cache_key(settings),
            max_concurrency=settings.max_concurrency,
            on_complete=progress_reporter(progress, task, args.verbose),
        )

    if cache is not None:
        log.info("[cyan]Cache: %d hits, %d misses[/cyan]", outcome.cache_hits, outcome.cache_misses)

    report = generate_report(outcome.results, "fhir_api")
    for table in summary_tables(report, f"FHIR API Inference Summary ({report.mode} mode)"):
        console.print(table)
    report_payload = asdict(report)
    report_payload["run_config"] = run_config_record(settings)

    saved = save_run(
        report_payload,
        args.output_dir / f"{stem}.json",
        outcome.traces if enable_trace else (),
        traces_dir=args.output_dir / "traces" / stem,
    )
    if saved.traces_dir:
        log.info("[cyan]Traces saved to %s[/cyan]", saved.traces_dir)
    log.info("[green]Results saved to %s[/green]", saved.results_path)


def resolve_settings(args: argparse.Namespace) -> tuple[RunSettings, dict[str, Any]] | None:
    """Settings from settings.yaml, the experiment YAML, then CLI; ``None`` (logged) when unusable."""
    settings = RunSettings.from_settings()
    exp_config: dict[str, Any] = {}
    if args.exp_config:
        try:
            exp_config = load_experiment_config(args.exp_config)
        except (OSError, ValueError) as error:
            log.error("Experiment config not usable: %s", error)
            return None
        log.info("[bold]Loaded experiment config: %s[/bold]", exp_config["experiment"]["name"])
        settings.apply_experiment(exp_config)

    if args.concurrency is not None:
        settings.max_concurrency = args.concurrency
    if settings.max_concurrency < 1:
        log.error("Concurrency must be at least 1, got %s", settings.max_concurrency)
        return None
    return settings, exp_config


def main() -> int:
    config = load_config()

    if config.get("aws_profile"):
        os.environ["AWS_PROFILE"] = config["aws_profile"]

    args = build_parser().parse_args()

    resolved = resolve_settings(args)
    if resolved is None:
        return 1
    settings, exp_config = resolved
    args.output_dir = resolve_output_dir(args.output_dir, exp_config)

    fhir_config = get_fhir_config()
    if not fhir_config["enabled"]:
        log.error(
            "FHIR API not enabled. Provide inference.fhir_api in --exp-config "
            "or set fhir_api.enabled in settings.yaml."
        )
        return 1

    cache = open_cache(args)

    patients = resolve_patients(settings, fhir_config["datastore_endpoint"], fhir_config["region"])
    if patients is None:
        return 1
    if not patients:
        log.error("No patients found. Import data first with scripts/healthlake_import.py")
        return 1

    print_patient_table(patients)
    settings.log_summary()

    if args.dry_run:
        log.info("[yellow]Dry run — skipping inference.[/yellow]")
        return 0

    samples = [
        {
            "sample_id": p["id"],
            "patient_id": p["id"],
            "file_path": f"fhir://{p['id']}",
            "format": "fhir_api",
        }
        for p in patients
    ]
    run_and_save(args, settings, samples, cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
