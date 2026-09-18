#!/usr/bin/env python3
"""Curate a balanced MIMIC-IV cohort from the scenario manifest.

Reads the manifest written by `select_mimic_scenarios.py`, picks N patients per
clinical scenario plus N from the non-scenario pool with no overlap, and writes a
cohort file of patient ids. Selection is seeded, so the cohort is reproducible.

The cohort file is the input to the import pre-filter:

    uv run scripts/healthlake_import.py --source mimic \
        --input-dir data/mimic-iv-fhir-demo \
        --patients-file data/mimic-cohort.json

Only the cohort's resources are then normalized and imported, which keeps the
billed datastore small. Nothing here queries HealthLake.

Usage:
    uv run scripts/mimic/curate_mimic_experiment.py
    uv run scripts/mimic/curate_mimic_experiment.py --per-scenario 3 --general 4
    uv run scripts/mimic/curate_mimic_experiment.py --seed 7 --dry-run
"""
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich"]
# ///

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("curate_mimic")
console = Console()

GENERAL_POOL = "general"


@dataclass
class Selection:
    """One selected patient and the pool it came from."""

    pool: str
    patient_id: str


def load_manifest(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Return (scenario -> patient ids, all patient ids) from a scenario manifest."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "scenarios" not in payload:
        raise ValueError(f"{path} is not a scenario manifest from select_mimic_scenarios.py")

    scenarios: dict[str, list[str]] = {}
    for name, block in payload["scenarios"].items():
        if isinstance(block, dict) and isinstance(block.get("patient_ids"), list):
            scenarios[name] = [str(pid) for pid in block["patient_ids"]]
        else:
            scenarios[name] = []

    all_patients = payload.get("all_patient_ids")
    if not isinstance(all_patients, list):
        raise ValueError(f"{path} has no all_patient_ids list")
    return scenarios, [str(pid) for pid in all_patients]


def curate(
    scenarios: dict[str, list[str]],
    all_patients: list[str],
    per_scenario: int,
    general: int,
    seed: int,
) -> list[Selection]:
    """Pick a non-overlapping, seeded cohort across scenarios and the general pool."""
    rng = random.Random(seed)
    selected: list[Selection] = []
    used: set[str] = set()

    for name in sorted(scenarios):
        available = [pid for pid in scenarios[name] if pid not in used]
        take = min(per_scenario, len(available))
        if take < per_scenario:
            log.warning(f"{name}: only {len(available)} available, picking {take}")
        for patient_id in rng.sample(available, take):
            selected.append(Selection(name, patient_id))
            used.add(patient_id)

    scenario_patients = {pid for pids in scenarios.values() for pid in pids}
    non_scenario = sorted(set(all_patients) - scenario_patients)
    take = min(general, len(non_scenario))
    if take < general:
        log.warning(
            f"{GENERAL_POOL}: only {len(non_scenario)} non-scenario patients, picking {take}"
        )
    for patient_id in rng.sample(non_scenario, take):
        selected.append(Selection(GENERAL_POOL, patient_id))
        used.add(patient_id)

    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=Path("data/mimic-scenarios.json"),
        help="Scenario manifest from select_mimic_scenarios.py",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/mimic-cohort.json"),
        help="Cohort file to write (default: data/mimic-cohort.json)",
    )
    parser.add_argument("--per-scenario", type=int, default=2, help="Patients per scenario")
    parser.add_argument(
        "--general", type=int, default=2, help="Patients from the non-scenario pool"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the selection without writing the cohort file",
    )
    args = parser.parse_args()

    if not args.scenarios.exists():
        log.error(f"Scenario manifest not found: {args.scenarios}")
        log.error("Run: uv run scripts/mimic/select_mimic_scenarios.py")
        return 1

    try:
        scenarios, all_patients = load_manifest(args.scenarios)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log.error(f"Cannot read {args.scenarios}: {e}")
        return 1

    selected = curate(
        scenarios=scenarios,
        all_patients=all_patients,
        per_scenario=args.per_scenario,
        general=args.general,
        seed=args.seed,
    )

    table = Table(title=f"Selected {len(selected)} patients (seed={args.seed})")
    table.add_column("Pool", style="cyan")
    table.add_column("Patient id", style="green")
    for selection in selected:
        table.add_row(selection.pool, selection.patient_id)
    console.print(table)

    if args.dry_run:
        log.info("[yellow]Dry run — cohort file not written[/yellow]")
        return 0

    cohort = {
        "source_manifest": str(args.scenarios),
        "seed": args.seed,
        "per_scenario": args.per_scenario,
        "general": args.general,
        "patients": [{"pool": s.pool, "patient_id": s.patient_id} for s in selected],
        "patient_ids": [s.patient_id for s in selected],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cohort, indent=2) + "\n", encoding="utf-8")
    log.info(f"Wrote {args.output}")
    console.print(
        "\n[bold]Next:[/bold] uv run scripts/healthlake_import.py --source mimic "
        f"--input-dir data/mimic-iv-fhir-demo --patients-file {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
