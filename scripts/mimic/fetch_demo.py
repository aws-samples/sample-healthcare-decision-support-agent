#!/usr/bin/env python3
"""Download the MIMIC-IV Clinical Database Demo on FHIR (v2.1.0) from PhysioNet.

The demo is OPEN ACCESS under the Open Data Commons Open Database License v1.0
(ODbL v1.0): no PhysioNet account, no CITI training, no signed DUA. 100 patients,
a 28.9 MB ZIP, 49.5 MB uncompressed, 30 gzipped NDJSON files.

The data is NOT vendored in this repository on purpose. ODbL share-alike (§4.4)
and the §4.6 obligation to offer recipients the derivative database would attach
to any mirrored copy, making this an ODbL-licensed data repo. Fetch it yourself.

The full (non-demo) MIMIC-IV on FHIR dataset is credentialed access under the
PhysioNet Credentialed Health Data License and must never be mirrored. If you
have credentialed access, point --output-dir at your own local copy instead of
running this script.

Attribution required by ODbL §4.3 when publishing anything derived from this data:

    Contains information from MIMIC-IV Clinical Database Demo on FHIR, which is
    made available here under the Open Database License (ODbL).

Please also cite:
  Bennett, A., Ulrich, H., Wiedekopf, J., Szul, P., Grimes, J., & Johnson, A.
    (2025). MIMIC-IV Clinical Database Demo on FHIR (version 2.1.0). PhysioNet.
    RRID:SCR_007345. https://doi.org/10.13026/vphg-y548
  Bennett AM, Ulrich H, van Damme P, Wiedekopf J, Johnson AE. MIMIC-IV on FHIR:
    converting a decade of in-patient data into an exchangeable, interoperable
    format. JAMIA. 2023;30(4):718-25.
  Pollard, T., Moody, B. E., Lehman, L., Gow, B., Fernandes, C., Xie, C.,
    Johnson, A., Mark, R. G., & Heldt, T. (2026). PhysioNet as a global platform
    for biomedical research. Nature Health.
  Parent projects: MIMIC-IV Clinical Database Demo v2.2, MIMIC-IV-ED Demo v2.2.

Usage:
    uv run scripts/mimic/fetch_demo.py
    uv run scripts/mimic/fetch_demo.py --output-dir data/mimic-iv-fhir-demo
    uv run scripts/mimic/fetch_demo.py --keep-zip --verify
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "rich"]
# ///

import argparse
import hashlib
import logging
import shutil
import sys
import zipfile
from pathlib import Path

import requests
from rich.console import Console
from rich.logging import RichHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("fetch_mimic_demo")
console = Console()

DEMO_VERSION = "2.1.0"
DEMO_PAGE = f"https://physionet.org/content/mimic-iv-fhir-demo/{DEMO_VERSION}/"
DEMO_ZIP_URL = f"https://physionet.org/content/mimic-iv-fhir-demo/get-zip/{DEMO_VERSION}/"
DEMO_FILES_URL = f"https://physionet.org/files/mimic-iv-fhir-demo/{DEMO_VERSION}/"
SHA256SUMS_URL = f"{DEMO_FILES_URL}SHA256SUMS.txt"
EXPECTED_ZIP_BYTES = 28_884_938

ODBL_NOTICE = (
    "Contains information from MIMIC-IV Clinical Database Demo on FHIR, "
    "which is made available here under the Open Database License (ODbL)."
)

DOWNLOAD_CHUNK_BYTES = 1 << 20


def download(url: str, destination: Path, timeout: int = 300) -> int:
    """Stream a URL to disk. Returns the byte count written."""
    log.info(f"Downloading {url}")
    written = 0
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
    log.info(f"Wrote {written:,} bytes to {destination}")
    return written


def extract_fhir_files(zip_path: Path, output_dir: Path) -> int:
    """Extract the archive's NDJSON, license, and README into output_dir (flat)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = Path(member.filename).name
            keep = name.endswith((".ndjson.gz", ".ndjson")) or name in (
                "LICENSE.txt",
                "README_DEMO.md",
                "SHA256SUMS.txt",
            )
            if not keep:
                continue
            target = output_dir / name
            with archive.open(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted += 1
    log.info(f"Extracted {extracted} files into {output_dir}")
    return extracted


def verify_checksums(output_dir: Path) -> tuple[int, list[str]]:
    """Verify extracted files against the dataset's SHA256SUMS.txt.

    Returns (files_checked, mismatched_names).
    """
    sums_path = output_dir / "SHA256SUMS.txt"
    if not sums_path.exists():
        log.warning("SHA256SUMS.txt not present — skipping verification")
        return 0, []

    checked = 0
    mismatched: list[str] = []
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        expected, listed_path = parts
        local = output_dir / Path(listed_path.lstrip("*")).name
        if not local.exists():
            continue
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        checked += 1
        if digest != expected:
            mismatched.append(local.name)
    return checked, mismatched


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the open-access MIMIC-IV-on-FHIR demo (v2.1.0) from PhysioNet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/mimic-iv-fhir-demo"),
        help="Directory to extract the NDJSON into (default: data/mimic-iv-fhir-demo)",
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="Keep the downloaded ZIP after extraction",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify extracted files against the dataset's SHA256SUMS.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the output directory already holds NDJSON",
    )
    args = parser.parse_args()

    console.rule("[bold]MIMIC-IV on FHIR demo — open access (ODbL v1.0)[/bold]")
    console.print(f"Source: {DEMO_PAGE}")
    console.print(f"[yellow]{ODBL_NOTICE}[/yellow]")
    console.print("Data is not vendored in this repo — ODbL share-alike would attach to it.\n")

    existing = list(args.output_dir.glob("*.ndjson.gz"))
    if existing and not args.force:
        log.info(f"{len(existing)} NDJSON files already in {args.output_dir} — nothing to do")
        log.info("Pass --force to re-download")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = args.output_dir / f"mimic-iv-fhir-demo-{DEMO_VERSION}.zip"

    try:
        size = download(DEMO_ZIP_URL, zip_path)
    except requests.RequestException as e:
        log.error(f"Download failed: {e}")
        log.error(f"Download the ZIP manually from {DEMO_PAGE} and extract it to {args.output_dir}")
        return 1

    if size != EXPECTED_ZIP_BYTES:
        log.warning(
            f"ZIP is {size:,} bytes; expected {EXPECTED_ZIP_BYTES:,}. "
            "PhysioNet may have republished the archive — check the project page."
        )

    try:
        extracted = extract_fhir_files(zip_path, args.output_dir)
    except (zipfile.BadZipFile, OSError) as e:
        log.error(f"Extraction failed: {e}")
        return 1

    if extracted == 0:
        log.error("Archive contained no NDJSON — aborting")
        return 1

    if args.verify:
        checked, mismatched = verify_checksums(args.output_dir)
        if mismatched:
            log.error(f"Checksum mismatch in {len(mismatched)} file(s): {', '.join(mismatched)}")
            return 1
        log.info(f"Verified {checked} file(s) against SHA256SUMS.txt")

    if not args.keep_zip:
        zip_path.unlink()
        log.info("Removed the ZIP (pass --keep-zip to keep it)")

    console.print()
    console.print("[bold]Next:[/bold] normalize and import into HealthLake")
    console.print(
        f"  uv run scripts/healthlake_import.py --source mimic --input-dir {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
