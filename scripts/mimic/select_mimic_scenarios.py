#!/usr/bin/env python3
"""Identify MIMIC-IV patients matching curated clinical scenarios.

Streams the MIMIC-IV-on-FHIR demo NDJSON, accumulates per-patient clinical
flags, and writes a JSON manifest of the patient ids matching each scenario.

The output feeds `curate_mimic_experiment.py`, whose cohort file is passed to
`scripts/healthlake_import.py --patients-file` so only the cohort's resources are
imported. Selection therefore happens *before* import — no assembled per-patient
bundles, no symlink trees, and nothing extra loaded into a billed datastore.

Usage:
    uv run scripts/mimic/select_mimic_scenarios.py \
        --input-dir data/mimic-iv-fhir-demo

    uv run scripts/mimic/select_mimic_scenarios.py \
        --input-dir data/mimic-iv-fhir-demo \
        --output data/mimic-scenarios.json

Prerequisites:
    uv run scripts/mimic/fetch_demo.py
"""
# /// script
# requires-python = ">=3.12"
# # boto3 is not used directly here, but importing medical_nudging.healthlake.normalize
# # runs the package __init__, which imports import_job -> botocore.
# dependencies = ["rich", "boto3"]
# ///

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from medical_nudging.healthlake.normalize import (  # noqa: E402
    MIMIC_ODBL_NOTICE,
    iter_ndjson,
    mimic_source_files,
    patient_id_of,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("select_scenarios")
console = Console()

MEDICATION_RESOURCE_TYPES = frozenset(
    {
        "MedicationRequest",
        "MedicationAdministration",
        "MedicationDispense",
        "MedicationStatement",
    }
)
INPATIENT_ENCOUNTER_CLASSES = frozenset({"IMP", "ACUTE", "NONAC"})

WARFARIN_RE = re.compile(r"warfarin|coumadin", re.IGNORECASE)
AMIODARONE_RE = re.compile(r"amiodarone|cordarone", re.IGNORECASE)
INR_RE = re.compile(r"\bINR\b|international normalized", re.IGNORECASE)

METFORMIN_RE = re.compile(r"metformin|glucophage", re.IGNORECASE)
CREATININE_RE = re.compile(r"creatinine", re.IGNORECASE)

LACTATE_RE = re.compile(r"lactate|lactic", re.IGNORECASE)
ANTIBIOTIC_RE = re.compile(
    r"ceftriaxone|vancomycin|piperacillin|tazobactam|meropenem|"
    r"cefepime|ciprofloxacin|levofloxacin|azithromycin|metronidazole|"
    r"ampicillin|sulbactam|gentamicin|tobramycin|cefazolin|clindamycin",
    re.IGNORECASE,
)

DIABETES_ICD_PREFIXES = ("E11", "250")
SEPSIS_ICD_PREFIXES = ("A41", "R65", "995.9")


@dataclass
class PatientFlags:
    """Clinical flags accumulated for one patient while streaming NDJSON."""

    has_warfarin: bool = False
    has_amiodarone: bool = False
    has_inr: bool = False
    has_metformin: bool = False
    creatinine_observations: int = 0
    has_diabetes: bool = False
    has_sepsis: bool = False
    has_lactate: bool = False
    has_antibiotics: bool = False
    has_emergency_encounter: bool = False
    has_inpatient_encounter: bool = False
    has_home_medications: bool = False
    resource_count: int = 0
    seen_resource_types: set[str] = field(default_factory=set)


def scenario_b_drug_interaction(flags: PatientFlags) -> bool:
    """B: Drug-drug interaction — warfarin + amiodarone + INR monitoring."""
    return flags.has_warfarin and flags.has_amiodarone and flags.has_inr


def scenario_c_lab_trend(flags: PatientFlags) -> bool:
    """C: Acute lab trend — diabetes + metformin + repeated creatinine."""
    return flags.has_diabetes and flags.has_metformin and flags.creatinine_observations >= 2


def scenario_f_med_reconciliation(flags: PatientFlags) -> bool:
    """F: ED to inpatient medication reconciliation."""
    return (
        flags.has_emergency_encounter
        and flags.has_inpatient_encounter
        and flags.has_home_medications
    )


def scenario_g_sepsis(flags: PatientFlags) -> bool:
    """G: Sepsis indicators — sepsis diagnosis + lactate + antibiotics."""
    return flags.has_sepsis and flags.has_lactate and flags.has_antibiotics


SCENARIOS: dict[str, tuple[str, Callable[[PatientFlags], bool]]] = {
    "scenario_B": ("Drug-drug interaction (warfarin+amiodarone)", scenario_b_drug_interaction),
    "scenario_C": ("Acute lab trend (diabetes+metformin+creatinine)", scenario_c_lab_trend),
    "scenario_F": ("ED-inpatient med reconciliation", scenario_f_med_reconciliation),
    "scenario_G": ("Sepsis indicators", scenario_g_sepsis),
}


# ---------------------------------------------------------------------------
# Resource inspection
# ---------------------------------------------------------------------------


def _codeable_texts(concept: object) -> Iterator[str]:
    """Yield every human-readable string in a CodeableConcept."""
    if not isinstance(concept, dict):
        return
    text = concept.get("text")
    if isinstance(text, str):
        yield text
    for coding in concept.get("coding", []):
        if not isinstance(coding, dict):
            continue
        for key in ("display", "code"):
            value = coding.get(key)
            if isinstance(value, str):
                yield value


def _medication_matches(resource: dict, pattern: re.Pattern) -> bool:
    """Match a medication resource's name against a pattern."""
    for text in _codeable_texts(resource.get("medicationCodeableConcept")):
        if pattern.search(text):
            return True
    reference = resource.get("medicationReference")
    if isinstance(reference, dict):
        display = reference.get("display")
        if isinstance(display, str) and pattern.search(display):
            return True
    for identifier in resource.get("identifier", []):
        if not isinstance(identifier, dict):
            continue
        value = identifier.get("value")
        if isinstance(value, str) and pattern.search(value):
            return True
    return False


def _observation_matches(resource: dict, pattern: re.Pattern) -> bool:
    return any(pattern.search(text) for text in _codeable_texts(resource.get("code")))


def _has_icd_prefix(resource: dict, prefixes: tuple[str, ...]) -> bool:
    concept = resource.get("code")
    if not isinstance(concept, dict):
        return False
    for coding in concept.get("coding", []):
        if not isinstance(coding, dict):
            continue
        code = coding.get("code")
        if isinstance(code, str) and code.upper().startswith(prefixes):
            return True
    return False


def update_flags(flags: PatientFlags, resource: dict) -> None:
    """Fold one resource into a patient's flags."""
    resource_type = resource.get("resourceType", "")
    flags.resource_count += 1
    if resource_type:
        flags.seen_resource_types.add(resource_type)

    if resource_type in MEDICATION_RESOURCE_TYPES:
        if _medication_matches(resource, WARFARIN_RE):
            flags.has_warfarin = True
        if _medication_matches(resource, AMIODARONE_RE):
            flags.has_amiodarone = True
        if _medication_matches(resource, METFORMIN_RE):
            flags.has_metformin = True
        if _medication_matches(resource, ANTIBIOTIC_RE):
            flags.has_antibiotics = True
        if resource_type == "MedicationStatement":
            flags.has_home_medications = True
        return

    if resource_type == "Observation":
        if _observation_matches(resource, INR_RE):
            flags.has_inr = True
        if _observation_matches(resource, CREATININE_RE):
            flags.creatinine_observations += 1
        if _observation_matches(resource, LACTATE_RE):
            flags.has_lactate = True
        return

    if resource_type == "Condition":
        if _has_icd_prefix(resource, DIABETES_ICD_PREFIXES):
            flags.has_diabetes = True
        if _has_icd_prefix(resource, SEPSIS_ICD_PREFIXES):
            flags.has_sepsis = True
        return

    if resource_type == "Encounter":
        encounter_class = resource.get("class")
        code = encounter_class.get("code") if isinstance(encounter_class, dict) else None
        if code == "EMER":
            flags.has_emergency_encounter = True
        elif code in INPATIENT_ENCOUNTER_CLASSES:
            flags.has_inpatient_encounter = True


def scan(input_dir: Path) -> dict[str, PatientFlags]:
    """Stream every NDJSON file and accumulate flags per patient."""
    files = mimic_source_files(input_dir)
    if not files:
        raise FileNotFoundError(f"No MIMIC NDJSON files under {input_dir}")

    by_patient: dict[str, PatientFlags] = {}
    for path in files:
        counted = 0
        for resource in iter_ndjson(path):
            patient_id = patient_id_of(resource)
            if patient_id is None:
                continue
            flags = by_patient.get(patient_id)
            if flags is None:
                flags = PatientFlags()
                by_patient[patient_id] = flags
            update_flags(flags, resource)
            counted += 1
        log.info(f"  {path.name}: {counted} patient-linked resources")

    return by_patient


def match_scenarios(by_patient: dict[str, PatientFlags]) -> dict[str, list[str]]:
    """Evaluate every scenario predicate over the accumulated flags."""
    matches: dict[str, list[str]] = {name: [] for name in SCENARIOS}
    for patient_id, flags in sorted(by_patient.items()):
        for name, (_, predicate) in SCENARIOS.items():
            if predicate(flags):
                matches[name].append(patient_id)
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Identify MIMIC patients matching clinical scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/mimic-iv-fhir-demo"),
        help="Directory of MIMIC demo NDJSON (default: data/mimic-iv-fhir-demo)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/mimic-scenarios.json"),
        help="Where to write the scenario manifest (default: data/mimic-scenarios.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matches without writing the manifest",
    )
    args = parser.parse_args()

    console.rule("[bold]MIMIC-IV scenario selection[/bold]")
    console.print(f"[yellow]{MIMIC_ODBL_NOTICE}[/yellow]")

    try:
        by_patient = scan(args.input_dir)
    except (FileNotFoundError, OSError) as e:
        log.error(f"Scan failed: {e}")
        return 1

    matches = match_scenarios(by_patient)

    table = Table(title=f"Scenario matches across {len(by_patient)} patients")
    table.add_column("Scenario", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Matches", justify="right")
    table.add_column("Patient ids (first 3)", style="dim")
    for name, (description, _) in SCENARIOS.items():
        matched = matches[name]
        style = "green" if matched else "red"
        table.add_row(
            name,
            description,
            f"[{style}]{len(matched)}[/{style}]",
            ", ".join(pid[:8] + "..." for pid in matched[:3]) or "[red]none[/red]",
        )
    console.print(table)

    if args.dry_run:
        log.info("[yellow]Dry run — manifest not written[/yellow]")
        return 0

    manifest = {
        "source": str(args.input_dir),
        "patients_scanned": len(by_patient),
        "scenarios": {
            name: {"description": SCENARIOS[name][0], "patient_ids": matches[name]}
            for name in SCENARIOS
        },
        "all_patient_ids": sorted(by_patient),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info(f"Wrote {args.output}")
    console.print(
        f"\n[bold]Next:[/bold] uv run scripts/mimic/curate_mimic_experiment.py "
        f"--scenarios {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
