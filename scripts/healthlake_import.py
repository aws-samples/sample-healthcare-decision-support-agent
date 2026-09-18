#!/usr/bin/env python3
"""Normalize a FHIR dataset to NDJSON and bulk-import it into AWS HealthLake.

Two sources, one back end:

    MIMIC-IV on FHIR demo (primary)   gunzip           ┐
    Synthea sample bundles (fallback) bundle → NDJSON  ┴→ S3 stage → StartFHIRImportJob

⚠️  Requires a running HealthLake datastore, which bills ~$0.27/hour (~$197/month)
    and cannot be paused. Enable it with `terraform apply -var="healthlake_enabled=true"`
    and destroy it when you are done.

Usage:
    # MIMIC (primary). Fetch the data first: uv run scripts/mimic/fetch_demo.py
    uv run scripts/healthlake_import.py --source mimic \
        --input-dir data/mimic-iv-fhir-demo

    # Skip the 35 MB charted-observations file for a fast quickstart
    uv run scripts/healthlake_import.py --source mimic \
        --input-dir data/mimic-iv-fhir-demo --subset core

    # Import only a curated cohort (see scripts/mimic/curate_mimic_experiment.py)
    uv run scripts/healthlake_import.py --source mimic \
        --input-dir data/mimic-iv-fhir-demo \
        --patients-file data/mimic-cohort.json

    # Synthea fallback (no PhysioNet dependency at all)
    curl -LO <pinned zip URL printed by --help>
    unzip -d data/synthea synthea_sample_data_fhir_r4_nov2021.zip
    uv run scripts/healthlake_import.py --source synthea \
        --input-dir data/synthea/fhir --limit 20

    # Normalize only, no AWS calls
    uv run scripts/healthlake_import.py --source mimic \
        --input-dir data/mimic-iv-fhir-demo --normalize-only

Terraform supplies every AWS argument; they are read from `terraform output` when
not passed explicitly.
"""
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "rich"]
# ///

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from medical_nudging.healthlake.import_job import (  # noqa: E402
    DEFAULT_VALIDATION_LEVEL,
    ImportJobError,
    run_import,
)
from medical_nudging.healthlake.normalize import (  # noqa: E402
    MIMIC_ODBL_NOTICE,
    SYNTHEA_SAMPLE_URL,
    normalize_mimic,
    normalize_synthea,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("healthlake_import")
console = Console()

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "terraform"

TERRAFORM_OUTPUTS = {
    "datastore_id": "healthlake_datastore_id",
    "bucket": "healthlake_staging_bucket",
    "role_arn": "healthlake_import_role_arn",
    "kms_key_arn": "healthlake_import_kms_key_arn",
}


def terraform_output(name: str) -> str | None:
    """Read a single terraform output, or None when unavailable."""
    if not TERRAFORM_DIR.is_dir():
        return None
    try:
        completed = subprocess.run(
            ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-raw", name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(f"terraform output {name} unavailable: {e}")
        return None
    if completed.returncode != 0:
        log.debug(f"terraform output {name} failed: {completed.stderr.strip()}")
        return None
    value = completed.stdout.strip()
    return value or None


def resolve_aws_args(args: argparse.Namespace) -> dict[str, str]:
    """Fill missing AWS arguments from terraform outputs. Exits if any is missing."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for attr, output_name in TERRAFORM_OUTPUTS.items():
        value = getattr(args, attr) or terraform_output(output_name)
        if not value:
            missing.append(f"--{attr.replace('_', '-')} (terraform output {output_name})")
        else:
            resolved[attr] = value

    if missing:
        log.error("Missing required HealthLake settings:")
        for item in missing:
            log.error(f"  {item}")
        log.error('Deploy with: terraform apply -var="healthlake_enabled=true"')
        sys.exit(2)
    return resolved


def load_patient_ids(path: Path) -> set[str]:
    """Read patient ids from a JSON list, a JSON cohort file, or a text file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        payload = json.loads(text)
        if isinstance(payload, list):
            return {str(item) for item in payload}
        if isinstance(payload, dict):
            if isinstance(payload.get("patient_ids"), list):
                return {str(item) for item in payload["patient_ids"]}
            if isinstance(payload.get("patients"), list):
                return {
                    str(entry["patient_id"])
                    for entry in payload["patients"]
                    if isinstance(entry, dict) and "patient_id" in entry
                }
        raise ValueError(f"{path} is not a patient-id list or cohort file")
    return {line.strip() for line in text.splitlines() if line.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Pinned Synthea sample: {SYNTHEA_SAMPLE_URL}",
    )
    parser.add_argument(
        "--source",
        choices=("mimic", "synthea"),
        required=True,
        help="Dataset shape: mimic (gzipped NDJSON) or synthea (transaction bundles)",
    )
    parser.add_argument("--input-dir", type=Path, required=True, help="Source data directory")
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=Path("data/.healthlake-ndjson"),
        help="Local directory for normalized NDJSON (default: data/.healthlake-ndjson)",
    )
    parser.add_argument(
        "--subset",
        choices=("all", "core"),
        default="all",
        help="MIMIC only: 'core' drops the 35 MB MimicObservationChartevents file",
    )
    parser.add_argument(
        "--patients-file",
        type=Path,
        default=None,
        help="MIMIC only: JSON/text file of patient ids to pre-filter before import",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Synthea only: maximum patient bundles to convert (0 = all)",
    )
    parser.add_argument("--datastore-id", default=None, help="HealthLake datastore id")
    parser.add_argument("--bucket", default=None, help="S3 staging bucket")
    parser.add_argument("--role-arn", default=None, help="HealthLake import role ARN")
    parser.add_argument("--kms-key-arn", default=None, help="KMS key ARN for job output")
    parser.add_argument(
        "--job-name", default=None, help="Import job name (default: <source>-<UTC>)"
    )
    parser.add_argument(
        "--validation-level",
        choices=("strict", "structure-only", "minimal"),
        default=DEFAULT_VALIDATION_LEVEL,
        help=(
            "FHIR validation level. Keep structure-only: MIMIC's custom IG profiles "
            "mass-reject under strict."
        ),
    )
    parser.add_argument("--region", default=None, help="AWS region (default: session region)")
    parser.add_argument("--profile", default=None, help="AWS profile")
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Seconds to wait for the import job (default: 3600)",
    )
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Write NDJSON locally and stop — makes no AWS calls",
    )
    args = parser.parse_args()

    console.rule(f"[bold]HealthLake import — {args.source}[/bold]")
    if args.source == "mimic":
        console.print(f"[yellow]{MIMIC_ODBL_NOTICE}[/yellow]")

    patient_ids: set[str] | None = None
    if args.patients_file is not None:
        if args.source != "mimic":
            parser.error("--patients-file applies to --source mimic")
        if not args.patients_file.exists():
            parser.error(f"--patients-file not found: {args.patients_file}")
        try:
            patient_ids = load_patient_ids(args.patients_file)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.error(f"Cannot read {args.patients_file}: {e}")
            return 1
        log.info(f"Pre-filtering to {len(patient_ids)} patients")

    # ---- Stage 1: normalize -------------------------------------------------
    console.rule("[cyan]Normalize[/cyan]")
    try:
        if args.source == "mimic":
            stats = normalize_mimic(
                input_dir=args.input_dir,
                output_dir=args.staging_dir,
                subset=args.subset,
                patient_ids=patient_ids,
            )
        else:
            stats = normalize_synthea(
                input_dir=args.input_dir,
                output_dir=args.staging_dir,
                limit=args.limit,
            )
    except (FileNotFoundError, OSError) as e:
        log.error(f"Normalization failed: {e}")
        return 1

    log.info(stats.summary())
    if args.source == "synthea" and stats.residual_conditional_references:
        log.warning(
            f"{stats.residual_conditional_references} conditional references remain unresolved. "
            "Import will still succeed (references are stored literally) but those links will "
            "not resolve on read — check that the hospitalInformation*/practitionerInformation* "
            "companion bundles are in --input-dir."
        )

    if args.normalize_only:
        console.print(f"\nNDJSON written to {args.staging_dir} (no AWS calls made)")
        return 0

    # ---- Stage 2: stage to S3 + import -------------------------------------
    aws_args = resolve_aws_args(args)
    job_name = args.job_name or f"{args.source}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        s3_client = session.client("s3")
        healthlake_client = session.client("healthlake")
    except (BotoCoreError, NoCredentialsError) as e:
        log.error(f"Cannot create AWS clients: {e}")
        return 1

    console.rule("[cyan]Import[/cyan]")
    log.info(f"Datastore {aws_args['datastore_id']} — job {job_name}")
    try:
        result = run_import(
            healthlake_client=healthlake_client,
            s3_client=s3_client,
            datastore_id=aws_args["datastore_id"],
            ndjson_dir=args.staging_dir,
            bucket=aws_args["bucket"],
            job_name=job_name,
            data_access_role_arn=aws_args["role_arn"],
            kms_key_arn=aws_args["kms_key_arn"],
            validation_level=args.validation_level,
            timeout_seconds=args.timeout,
        )
    except (ImportJobError, ClientError, FileNotFoundError) as e:
        log.error(f"Import failed: {e}")
        return 1

    console.rule("[bold]Summary[/bold]")
    table = Table()
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Job id", result.job_id)
    table.add_row("Job status", result.job_status)
    table.add_row("Files staged", str(result.staged_objects))
    if result.manifest is not None:
        table.add_row("Resources scanned", f"{result.manifest.resources_scanned:,}")
        table.add_row("Resources imported", f"{result.manifest.resources_imported:,}")
        table.add_row("Customer errors", str(result.manifest.resources_with_customer_error))
        table.add_row("Server errors", str(result.manifest.resources_with_server_error))
    table.add_row("FAILURE/ objects", str(len(result.failure_objects or [])))
    table.add_row("Output prefix", f"s3://{aws_args['bucket']}/{result.output_prefix}/")
    console.print(table)

    if not result.succeeded:
        log.error("Import did not complete cleanly — inspect Manifest.json and FAILURE/ in S3")
        return 1

    console.print("\n[bold green]Import completed cleanly.[/bold green]")
    console.print("Remember: the datastore keeps billing until you destroy it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
