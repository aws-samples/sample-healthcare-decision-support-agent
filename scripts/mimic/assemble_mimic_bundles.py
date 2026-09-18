#!/usr/bin/env python3
"""Assemble per-patient FHIR Bundles from MIMIC-IV FHIR Demo NDJSON files.

PhysioNet distributes MIMIC-IV FHIR as NDJSON.gz files grouped by resource
type. This script groups patient-dependent resources by patient UUID and
writes self-contained per-patient FHIR Bundles compatible with parse_fhir().

This feeds the zero-cost local file mode: the assembled bundles are parsed by
parse_fhir() with no FHIR server involved. For the HealthLake API path use
scripts/healthlake_import.py instead — bulk import wants NDJSON, not Bundles.

Usage:
    uv run scripts/mimic/fetch_demo.py
    uv run scripts/mimic/assemble_mimic_bundles.py \
        --input-dir data/mimic-iv-fhir-demo \
        --output-dir data/mimic-fhir

Attribution (ODbL v1.0): Contains information from MIMIC-IV Clinical Database
Demo on FHIR, which is made available here under the Open Database License
(ODbL). See scripts/mimic/fetch_demo.py for the full citation list.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///

import argparse
import gzip
import json
import logging
import sys
from collections import defaultdict
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
log = logging.getLogger("assemble_mimic")
console = Console()

# Data-only resources (not patient-specific)
DATA_RESOURCE_FILES = {
    "MimicMedication",
    "MimicMedicationMix",
    "MimicOrganization",
    "MimicLocation",
}

# Patient resource file
PATIENT_FILE = "MimicPatient"


def read_ndjson(path: Path) -> list[dict]:
    """Read NDJSON or NDJSON.gz file, returning list of parsed JSON objects."""
    resources = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                resources.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning(f"Skipped malformed JSON at {path.name}:{line_num}")
    return resources


def extract_patient_id(resource: dict) -> str | None:
    """Extract patient UUID from subject.reference ('Patient/{uuid}')."""
    subject = resource.get("subject")
    if not isinstance(subject, dict):
        return None
    reference = subject.get("reference", "")
    if reference.startswith("Patient/"):
        return reference.split("/", 1)[1]
    return None


def load_all_resources(input_dir: Path) -> tuple[
    dict[str, dict],  # patient_id -> Patient resource
    dict[str, list[dict]],  # patient_id -> [dependent resources]
    dict[str, dict],  # medication_id -> Medication resource
    dict[str, int],  # file_stem -> resource count
]:
    """Load all NDJSON files and group by patient."""
    patients: dict[str, dict] = {}
    patient_resources: dict[str, list[dict]] = defaultdict(list)
    medications: dict[str, dict] = {}
    file_stats: dict[str, int] = {}

    ndjson_files = sorted(list(input_dir.glob("*.ndjson.gz")) + list(input_dir.glob("*.ndjson")))

    if not ndjson_files:
        log.error(f"No NDJSON files found in {input_dir}")
        sys.exit(1)

    for ndjson_path in ndjson_files:
        stem = ndjson_path.stem
        if stem.endswith(".ndjson"):
            stem = stem[: -len(".ndjson")]

        resources = read_ndjson(ndjson_path)
        file_stats[stem] = len(resources)
        log.info(f"  {stem}: [bold]{len(resources)}[/bold] resources")

        if stem == PATIENT_FILE:
            for r in resources:
                pid = r.get("id")
                if pid:
                    patients[str(pid)] = r
        elif stem in DATA_RESOURCE_FILES:
            for r in resources:
                rid = r.get("id")
                if rid:
                    medications[str(rid)] = r
        else:
            for r in resources:
                pid = extract_patient_id(r)
                if pid:
                    patient_resources[pid].append(r)

    return patients, dict(patient_resources), medications, file_stats


def assemble_bundle(
    patient_id: str,
    patient_resource: dict,
    dependent_resources: list[dict],
    medication_index: dict[str, dict],
) -> dict:
    """Assemble a FHIR Bundle for one patient."""
    entries = [{"resource": patient_resource}]

    # Collect referenced medication IDs
    referenced_med_ids: set[str] = set()
    for r in dependent_resources:
        entries.append({"resource": r})
        med_ref = r.get("medicationReference", {})
        ref_str = med_ref.get("reference", "") if isinstance(med_ref, dict) else ""
        if ref_str.startswith("Medication/"):
            referenced_med_ids.add(ref_str.split("/", 1)[1])

    # Include referenced Medication resources
    for med_id in referenced_med_ids:
        med_resource = medication_index.get(med_id)
        if med_resource:
            entries.append({"resource": med_resource})

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": entries,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Assemble per-patient FHIR Bundles from MIMIC-IV NDJSON files"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/mimic-iv-fhir-demo"),
        help="Directory containing NDJSON(.gz) files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/mimic-fhir"),
        help="Output directory for per-patient Bundle JSON files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report statistics without writing files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max patients to assemble (0 = all)",
    )
    parser.add_argument(
        "--max-observations",
        type=int,
        default=0,
        help="Cap observations per patient (0 = no cap)",
    )
    args = parser.parse_args()

    console.rule("[bold]MIMIC-IV FHIR Bundle Assembly[/bold]")
    log.info(f"Input:  {args.input_dir}")
    log.info(f"Output: {args.output_dir}")

    # Load all resources
    log.info("Loading NDJSON files...")
    patients, patient_resources, medications, file_stats = load_all_resources(args.input_dir)
    log.info(
        f"Loaded [bold]{len(patients)}[/bold] patients, "
        f"[bold]{len(medications)}[/bold] medication resources"
    )

    # Assemble bundles
    patient_ids = sorted(patients.keys())
    if args.limit > 0:
        patient_ids = patient_ids[: args.limit]

    bundle_sizes: list[int] = []
    resource_counts: list[int] = []

    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    for pid in patient_ids:
        deps = patient_resources.get(pid, [])

        # Optionally cap observations
        if args.max_observations > 0:
            obs = [r for r in deps if r.get("resourceType") == "Observation"]
            non_obs = [r for r in deps if r.get("resourceType") != "Observation"]
            if len(obs) > args.max_observations:
                log.info(
                    f"  Patient {pid[:12]}...: capping observations "
                    f"{len(obs)} -> {args.max_observations}"
                )
                obs = obs[: args.max_observations]
            deps = non_obs + obs

        bundle = assemble_bundle(pid, patients[pid], deps, medications)
        bundle_json = json.dumps(bundle)
        bundle_sizes.append(len(bundle_json))
        resource_counts.append(len(bundle["entry"]))

        if not args.dry_run:
            out_path = args.output_dir / f"{pid}.json"
            out_path.write_text(bundle_json)

    # Report
    console.rule("[bold]Summary[/bold]")
    table = Table(title="Bundle Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Patients", str(len(patient_ids)))
    if bundle_sizes:
        table.add_row(
            "Bundle size (min/mean/max)",
            f"{min(bundle_sizes):,} / {sum(bundle_sizes)//len(bundle_sizes):,} / {max(bundle_sizes):,} bytes",
        )
        table.add_row(
            "Resources per patient (min/mean/max)",
            f"{min(resource_counts)} / {sum(resource_counts)//len(resource_counts)} / {max(resource_counts)}",
        )
        table.add_row("Total size", f"{sum(bundle_sizes)/1_048_576:.1f} MB")

        # Flag large bundles
        large = [(pid, sz) for pid, sz in zip(patient_ids, bundle_sizes) if sz > 5_000_000]
        if large:
            table.add_row(
                "Large bundles (>5MB)",
                f"{len(large)} patients",
            )
    console.print(table)

    if args.dry_run:
        log.info("[yellow]Dry run — no files written[/yellow]")
    else:
        log.info(f"Wrote {len(patient_ids)} bundles to {args.output_dir}")


if __name__ == "__main__":
    main()
