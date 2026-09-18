"""Normalize FHIR datasets to import-ready NDJSON.

HealthLake bulk import wants NDJSON — one FHIR resource per line — with every
resource carrying an ``id`` (import is upsert-by-id). Two sources are supported:

* **MIMIC-IV on FHIR demo (primary)** — already NDJSON grouped per resource
  profile, gzip-compressed. Normalizing is a gunzip, plus optional patient and
  resource-subset filtering.
* **Synthea sample bundles (fallback)** — per-patient FHIR *transaction*
  Bundles. Normalizing unwraps ``entry.resource``, rewrites ``urn:uuid:<id>``
  references to ``<Type>/<id>``, and resolves the conditional references
  (``Practitioner?identifier=...``) that the companion hospital and
  practitioner bundles satisfy.

Reference resolution is a *data-quality* step, not an import gate: HealthLake
bulk import stores reference strings literally and imports dangling references
without complaint (verified empirically). It matters because an unresolved
``Practitioner?identifier=...`` reference never resolves on a read.

Load-order tiering is deliberately absent — import is upsert-by-id, so there is
nothing to order.
"""

import gzip
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

logger = logging.getLogger(__name__)

# Pinned Synthea sample artifact. The synthea-sample-data repo has no tags or
# releases, so the pin is a commit SHA on an immutable raw URL.
SYNTHEA_SAMPLE_COMMIT = "a59acd49f50d240ce6bacc22a719fff8b25b8dc3"
SYNTHEA_SAMPLE_URL = (
    f"https://raw.githubusercontent.com/synthetichealth/synthea-sample-data/"
    f"{SYNTHEA_SAMPLE_COMMIT}/downloads/synthea_sample_data_fhir_r4_nov2021.zip"
)

# MIMIC-IV on FHIR demo v2.1.0 (open access, ODbL v1.0).
MIMIC_DEMO_VERSION = "2.1.0"
MIMIC_DEMO_PAGE = f"https://physionet.org/content/mimic-iv-fhir-demo/{MIMIC_DEMO_VERSION}/"
MIMIC_DEMO_ZIP_URL = (
    f"https://physionet.org/content/mimic-iv-fhir-demo/get-zip/{MIMIC_DEMO_VERSION}/"
)
MIMIC_ODBL_NOTICE = (
    "Contains information from MIMIC-IV Clinical Database Demo on FHIR, which is "
    "made available here under the Open Database License (ODbL v1.0)."
)

# MimicObservationChartevents is 35 MB of the demo's 49.5 MB. The "core" subset
# drops it so a quickstart import stays small; "all" imports every file.
MimicSubset = Literal["all", "core"]
MIMIC_HIGH_VOLUME_FILES = frozenset({"MimicObservationChartevents"})

# Resource types that are shared rather than patient-scoped, and so survive
# patient filtering.
SHARED_RESOURCE_TYPES = frozenset({"Organization", "Location", "Medication"})

_URN_UUID = re.compile(r"^urn:uuid:(?P<id>.+)$")
_CONDITIONAL_REF = re.compile(r"^(?P<type>[A-Za-z]+)\?identifier=(?P<system>[^|]*)\|(?P<value>.+)$")


@dataclass
class NormalizeStats:
    """Outcome of a normalize run."""

    files_written: int = 0
    resources_written: int = 0
    resources_skipped_no_id: int = 0
    urn_references_rewritten: int = 0
    conditional_references_resolved: int = 0
    residual_urn_references: int = 0
    residual_conditional_references: int = 0
    by_resource_type: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.resources_written} resources in {self.files_written} files; "
            f"rewrote {self.urn_references_rewritten} urn:uuid and resolved "
            f"{self.conditional_references_resolved} conditional references; "
            f"residual urn:uuid={self.residual_urn_references}, "
            f"conditional={self.residual_conditional_references}, "
            f"skipped(no id)={self.resources_skipped_no_id}"
        )


# ---------------------------------------------------------------------------
# MIMIC-IV on FHIR
# ---------------------------------------------------------------------------


def mimic_source_files(input_dir: Path, subset: MimicSubset = "all") -> list[Path]:
    """List the MIMIC demo NDJSON files to normalize, in stable order."""
    if not input_dir.is_dir():
        raise FileNotFoundError(f"MIMIC input directory not found: {input_dir}")

    candidates = sorted(input_dir.glob("*.ndjson.gz")) + sorted(input_dir.glob("*.ndjson"))
    if subset == "core":
        candidates = [p for p in candidates if _stem(p) not in MIMIC_HIGH_VOLUME_FILES]
    return candidates


def normalize_mimic(
    input_dir: Path,
    output_dir: Path,
    subset: MimicSubset = "all",
    patient_ids: set[str] | None = None,
) -> NormalizeStats:
    """Decompress MIMIC demo NDJSON into an import-ready directory.

    Args:
        input_dir: Directory holding the demo's ``Mimic*.ndjson.gz`` files
            (the ``fhir/`` folder of the PhysioNet download).
        output_dir: Directory to write plain ``.ndjson`` files into.
        subset: ``"all"`` for every file, ``"core"`` to drop the 35 MB
            ``MimicObservationChartevents`` file.
        patient_ids: When given, keep only resources for these patients (plus
            shared Organization/Location/Medication resources). Use it to
            pre-filter a curated cohort before import.

    Returns:
        NormalizeStats for the run.
    """
    sources = mimic_source_files(input_dir, subset)
    if not sources:
        raise FileNotFoundError(f"No MIMIC NDJSON files found under {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stats = NormalizeStats()

    for source in sources:
        name = _stem(source)
        destination = output_dir / f"{name}.ndjson"
        written = 0
        with destination.open("w", encoding="utf-8") as out:
            for resource in iter_ndjson(source):
                if not resource.get("id"):
                    stats.resources_skipped_no_id += 1
                    continue
                if patient_ids is not None and not _belongs_to_patients(resource, patient_ids):
                    continue
                out.write(json.dumps(resource, separators=(",", ":")) + "\n")
                written += 1

        if written == 0:
            destination.unlink()
            logger.debug(f"{name}: no resources after filtering, no file written")
            continue

        stats.files_written += 1
        stats.resources_written += written
        stats.by_resource_type[name] = written
        logger.info(f"{name}.ndjson: {written} resources")

    return stats


def mimic_patient_ids(input_dir: Path) -> set[str]:
    """Read every Patient id from the MIMIC demo's MimicPatient file."""
    for candidate in ("MimicPatient.ndjson.gz", "MimicPatient.ndjson"):
        path = input_dir / candidate
        if path.exists():
            return {r["id"] for r in iter_ndjson(path) if r.get("id")}
    raise FileNotFoundError(f"MimicPatient.ndjson[.gz] not found under {input_dir}")


# ---------------------------------------------------------------------------
# Synthea
# ---------------------------------------------------------------------------


def normalize_synthea(
    input_dir: Path,
    output_dir: Path,
    limit: int = 0,
) -> NormalizeStats:
    """Convert Synthea transaction Bundles into per-resource-type NDJSON.

    The converter is whole-corpus by necessity: conditional references inside a
    patient bundle only resolve against the companion ``hospitalInformation*``
    and ``practitionerInformation*`` bundles, so the id and identifier maps are
    built over every file before any NDJSON is emitted.

    Args:
        input_dir: Directory of Synthea bundle JSON files (the ``fhir/`` folder
            of the pinned sample zip).
        output_dir: Directory to write ``<ResourceType>.ndjson`` files into.
        limit: Maximum number of *patient* bundles to process (0 = all). The
            companion bundles are always processed.

    Returns:
        NormalizeStats for the run.
    """
    bundle_files = _synthea_bundle_files(input_dir, limit)
    if not bundle_files:
        raise FileNotFoundError(f"No Synthea bundle JSON files found under {input_dir}")

    resources: list[dict[str, Any]] = []
    id_to_type: dict[str, str] = {}
    identifier_to_reference: dict[tuple[str, str, str], str] = {}

    for path in bundle_files:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping {path.name}: {e}")
            continue
        for entry in bundle.get("entry", []):
            if not isinstance(entry, dict):
                continue
            resource = entry.get("resource")
            if not isinstance(resource, dict):
                continue
            resource_type = resource.get("resourceType")
            resource_id = resource.get("id")
            if isinstance(resource_type, str) and isinstance(resource_id, str):
                id_to_type[resource_id] = resource_type
                for key in _identifier_keys(resource_type, resource):
                    identifier_to_reference[key] = f"{resource_type}/{resource_id}"
            resources.append(resource)

    stats = NormalizeStats()
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in resources:
        _rewrite_references(resource, id_to_type, identifier_to_reference, stats)
        if not resource.get("id"):
            stats.resources_skipped_no_id += 1
            continue
        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str):
            stats.resources_skipped_no_id += 1
            continue
        by_type[resource_type].append(resource)

    output_dir.mkdir(parents=True, exist_ok=True)
    for resource_type, items in sorted(by_type.items()):
        destination = output_dir / f"{resource_type}.ndjson"
        with destination.open("w", encoding="utf-8") as out:
            for item in items:
                out.write(json.dumps(item, separators=(",", ":")) + "\n")
        stats.files_written += 1
        stats.resources_written += len(items)
        stats.by_resource_type[resource_type] = len(items)
        logger.info(f"{resource_type}.ndjson: {len(items)} resources")

    for items in by_type.values():
        for item in items:
            _count_residual_references(item, stats)

    return stats


def _synthea_bundle_files(input_dir: Path, limit: int) -> list[Path]:
    """Companion bundles first, then up to ``limit`` patient bundles."""
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Synthea input directory not found: {input_dir}")

    companions: list[Path] = []
    patients: list[Path] = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name.startswith(("hospitalInformation", "practitionerInformation")):
            companions.append(path)
        else:
            patients.append(path)

    if limit > 0:
        patients = patients[:limit]
    return companions + patients


def _identifier_keys(
    resource_type: str, resource: dict[str, Any]
) -> Iterator[tuple[str, str, str]]:
    """Yield (resourceType, system, value) keys for a resource's identifiers."""
    identifiers = resource.get("identifier")
    if not isinstance(identifiers, list):
        return
    for identifier in identifiers:
        if not isinstance(identifier, dict):
            continue
        system = identifier.get("system")
        value = identifier.get("value")
        if isinstance(system, str) and isinstance(value, str):
            yield (resource_type, system, value)


def _rewrite_references(
    node: Any,
    id_to_type: dict[str, str],
    identifier_to_reference: dict[tuple[str, str, str], str],
    stats: NormalizeStats,
) -> None:
    """Recursively rewrite urn:uuid and conditional references in place."""
    if isinstance(node, dict):
        reference = node.get("reference")
        if isinstance(reference, str):
            resolved = _resolve_reference(reference, id_to_type, identifier_to_reference, stats)
            if resolved is not None:
                node["reference"] = resolved
        for value in node.values():
            _rewrite_references(value, id_to_type, identifier_to_reference, stats)
    elif isinstance(node, list):
        for value in node:
            _rewrite_references(value, id_to_type, identifier_to_reference, stats)


def _resolve_reference(
    reference: str,
    id_to_type: dict[str, str],
    identifier_to_reference: dict[tuple[str, str, str], str],
    stats: NormalizeStats,
) -> str | None:
    urn = _URN_UUID.match(reference)
    if urn:
        target_id = urn.group("id")
        target_type = id_to_type.get(target_id)
        if target_type is None:
            return None
        stats.urn_references_rewritten += 1
        return f"{target_type}/{target_id}"

    conditional = _CONDITIONAL_REF.match(reference)
    if conditional:
        key = (
            conditional.group("type"),
            conditional.group("system"),
            conditional.group("value"),
        )
        resolved = identifier_to_reference.get(key)
        if resolved is None:
            return None
        stats.conditional_references_resolved += 1
        return resolved

    return None


def _count_residual_references(node: Any, stats: NormalizeStats) -> None:
    """Tally references that could not be resolved, for the run report."""
    if isinstance(node, dict):
        reference = node.get("reference")
        if isinstance(reference, str):
            if reference.startswith("urn:uuid:"):
                stats.residual_urn_references += 1
            elif _CONDITIONAL_REF.match(reference):
                stats.residual_conditional_references += 1
        for value in node.values():
            _count_residual_references(value, stats)
    elif isinstance(node, list):
        for value in node:
            _count_residual_references(value, stats)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stem(path: Path) -> str:
    """``MimicPatient.ndjson.gz`` -> ``MimicPatient``."""
    name = path.name
    for suffix in (".ndjson.gz", ".ndjson"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def iter_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    """Yield resources from an NDJSON or NDJSON.gz file, skipping bad lines."""
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    resource = json.loads(stripped)
                except json.JSONDecodeError as e:
                    logger.warning(f"{path.name}:{line_number} is not valid JSON — skipped ({e})")
                    continue
                if isinstance(resource, dict):
                    yield resource
                else:
                    logger.warning(f"{path.name}:{line_number} is not a FHIR resource — skipped")
    except (OSError, gzip.BadGzipFile) as e:
        raise OSError(f"Cannot read {path}: {e}") from e


def patient_id_of(resource: dict[str, Any]) -> str | None:
    """Return the Patient id a resource belongs to, or None."""
    if resource.get("resourceType") == "Patient":
        resource_id = resource.get("id")
        return resource_id if isinstance(resource_id, str) else None

    for field_name in ("subject", "patient"):
        candidate = resource.get(field_name)
        if not isinstance(candidate, dict):
            continue
        reference = candidate.get("reference")
        if isinstance(reference, str) and reference.startswith("Patient/"):
            return reference.split("/", 1)[1]
    return None


def _belongs_to_patients(resource: dict[str, Any], patient_ids: set[str]) -> bool:
    if resource.get("resourceType") in SHARED_RESOURCE_TYPES:
        return True
    patient_id = patient_id_of(resource)
    # Resources with no patient reference at all are kept: dropping them would
    # silently delete Encounter-scoped or catalogue-style resources.
    return patient_id is None or patient_id in patient_ids
