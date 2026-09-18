"""Patient service for scanning and parsing patient files."""

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Literal

import diskcache

from medical_nudging.api.schemas.patient import (
    AllergyResponse,
    DemographicsResponse,
    EncounterResponse,
    ImmunizationResponse,
    LabResponse,
    MedicationResponse,
    ParsedPatientResponse,
    PatientSummary,
    ProblemResponse,
    ProcedureResponse,
    VitalResponse,
)
from medical_nudging.parsers.ccda_parser import ParsedCCDA, parse_ccda
from medical_nudging.parsers.fhir_parser import ParsedFHIR, parse_fhir

logger = logging.getLogger(__name__)

# Data directory paths
DATA_DIR = Path(__file__).parent.parent.parent.parent.parent / "data"
SYNTHEA_DIR = DATA_DIR / "sample-ccda"
MEDICARE_DIR = DATA_DIR / "sample-fhir"

# Disk cache for parsed patients (persists across restarts)
CACHE_DIR = Path(__file__).parent.parent.parent.parent.parent / ".cache" / "patients"
# JSONDisk instead of the default pickle serializer: a writable cache directory
# must never be a code-execution path (CVE-2025-69872). Values are model dumps.
cache = diskcache.Cache(str(CACHE_DIR), size_limit=500 * 1024 * 1024, disk=diskcache.JSONDisk)

# Response limits
MAX_LABS = 200
MAX_VITALS = 100
MAX_ENCOUNTERS = 100
MAX_PROCEDURES = 100
MAX_CLINICAL_NOTES = 10

# Pre-built patient index (lazy loaded)
_patient_index: dict[str, PatientSummary] | None = None


def _get_file_hash(file_path: Path) -> str:
    """Get hash of file mtime+size for cache invalidation."""
    stat = file_path.stat()
    # MD5 is used for cache key generation, not cryptographic security
    return hashlib.md5(
        f"{stat.st_mtime}:{stat.st_size}".encode(), usedforsecurity=False
    ).hexdigest()[:12]


def _format_value(value: str | None) -> str | None:
    """Format numeric values to max 3 decimal places."""
    if value is None:
        return None
    try:
        num = float(value)
        if num == int(num):
            return str(int(num))
        return f"{num:.3f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return value


def _extract_name_from_filename(filename: str) -> str:
    """Extract patient name from filename."""
    name = filename.rsplit(".", 1)[0]
    parts = name.split("_")
    if len(parts) >= 2:
        first = re.sub(r"\d+$", "", parts[0])
        last = re.sub(r"\d+$", "", parts[1])
        return f"{first} {last}"
    return name


def _extract_id_from_filename(filename: str) -> str:
    """Extract patient ID (UUID) from filename."""
    name = filename.rsplit(".", 1)[0]
    parts = name.split("_")
    if len(parts) >= 3:
        return parts[-1]
    return name


def _build_patient_index() -> dict[str, PatientSummary]:
    """Build index of all patients (runs once, cached in memory)."""
    index: dict[str, PatientSummary] = {}

    if SYNTHEA_DIR.exists():
        for file_path in SYNTHEA_DIR.glob("*.xml"):
            patient_id = _extract_id_from_filename(file_path.name)
            index[patient_id] = PatientSummary(
                id=patient_id,
                name=_extract_name_from_filename(file_path.name),
                source="synthea",
                format="ccda",
                file_path=str(file_path),
            )

    if MEDICARE_DIR.exists():
        for file_path in MEDICARE_DIR.glob("*.json"):
            patient_id = _extract_id_from_filename(file_path.name)
            index[patient_id] = PatientSummary(
                id=patient_id,
                name=_extract_name_from_filename(file_path.name),
                source="medicare",
                format="fhir",
                file_path=str(file_path),
            )

    return index


def _get_patient_index() -> dict[str, PatientSummary]:
    """Get or build patient index."""
    global _patient_index
    if _patient_index is None:
        _patient_index = _build_patient_index()
    return _patient_index


def scan_patients(
    source: Literal["synthea", "medicare", "all"] = "all",
    limit: int = 100,
    offset: int = 0,
    search: str | None = None,
) -> tuple[list[PatientSummary], int]:
    """Scan for patients using pre-built index."""
    index = _get_patient_index()

    # Filter by source
    if source == "all":
        patients = list(index.values())
    else:
        patients = [p for p in index.values() if p.source == source]

    # Filter by search
    if search:
        search_lower = search.lower()
        patients = [p for p in patients if search_lower in p.name.lower()]

    # Sort by name
    patients.sort(key=lambda p: p.name)

    total = len(patients)
    return patients[offset : offset + limit], total


def get_patient_by_id(patient_id: str) -> PatientSummary | None:
    """Find patient by ID using index."""
    return _get_patient_index().get(patient_id)


def parse_patient_file(file_path: str) -> ParsedPatientResponse:
    """Parse patient file with disk caching."""
    path = Path(file_path)
    patient_id = _extract_id_from_filename(path.name)

    # Cache key includes file hash for invalidation
    cache_key = f"patient:{patient_id}:{_get_file_hash(path)}"

    # Check cache
    cached = cache.get(cache_key)
    if cached is not None:
        return ParsedPatientResponse.model_validate(cached)

    # Parse file
    content = path.read_text()
    if path.suffix == ".xml":
        result = _to_response(patient_id, "ccda", parse_ccda(content))
    else:
        result = _to_response(patient_id, "fhir", parse_fhir(content))

    # Cache result
    cache.set(cache_key, result.model_dump(mode="json"), expire=86400)  # 24 hour expiry
    return result


def _to_response(
    patient_id: str, fmt: Literal["ccda", "fhir"], parsed: ParsedCCDA | ParsedFHIR
) -> ParsedPatientResponse:
    """Map a parsed document onto the API response, truncating long sections.

    Encounters and procedures exist only on FHIR documents; CCDA documents carry
    clinical notes instead. Missing sections map to empty lists.
    """
    return ParsedPatientResponse(
        id=patient_id,
        format=fmt,
        demographics=_demographics_response(parsed.demographics),
        problems=[
            ProblemResponse(
                description=p.description,
                code=p.code,
                code_system=p.code_system,
                status=p.status,
                onset_date=p.onset_date,
            )
            for p in parsed.problems
        ],
        medications=[
            MedicationResponse(
                name=m.name,
                dosage=m.dosage,
                route=m.route,
                status=m.status,
                start_date=m.start_date,
                end_date=m.end_date,
            )
            for m in parsed.medications
        ],
        allergies=[
            AllergyResponse(substance=a.substance, reaction=a.reaction, severity=a.severity)
            for a in parsed.allergies
        ],
        vitals=[
            VitalResponse(name=v.name, value=_format_value(v.value), unit=v.unit, date=v.date)
            for v in parsed.vitals[:MAX_VITALS]
        ],
        labs=[
            LabResponse(
                test=lab.test,
                value=_format_value(lab.value),
                unit=lab.unit,
                status=lab.status,
                date=lab.date,
            )
            for lab in parsed.labs[:MAX_LABS]
        ],
        immunizations=[
            ImmunizationResponse(vaccine=i.vaccine, date=i.date) for i in parsed.immunizations
        ],
        encounters=[
            EncounterResponse(
                type=e.encounter_type, date=e.start_date, provider=None, location=None
            )
            for e in getattr(parsed, "encounters", [])[:MAX_ENCOUNTERS]
        ],
        procedures=[
            ProcedureResponse(
                description=p.description, code=p.code, date=p.performed_date, status=p.status
            )
            for p in getattr(parsed, "procedures", [])[:MAX_PROCEDURES]
        ],
        clinical_notes=getattr(parsed, "clinical_notes", [])[:MAX_CLINICAL_NOTES],
    )


def _demographics_response(demographics: Any) -> DemographicsResponse | None:
    if not demographics:
        return None
    return DemographicsResponse(
        name=demographics.name, dob=demographics.dob, gender=demographics.gender
    )
