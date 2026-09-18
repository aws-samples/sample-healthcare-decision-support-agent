"""Patient API routes."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from medical_nudging.api.schemas.patient import (
    PatientListResponse,
    ParsedPatientResponse,
)
from medical_nudging.api.services.patient_service import (
    scan_patients,
    get_patient_by_id,
    parse_patient_file,
)

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=PatientListResponse)
async def list_patients(
    source: Literal["synthea", "medicare", "all"] = Query(
        default="all",
        description="Data source to scan",
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum patients to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    search: str | None = Query(default=None, description="Search term for patient name"),
) -> PatientListResponse:
    """List available patients from data directories.

    Scans the synthea and medicare data directories and returns patient summaries.
    Supports pagination and filtering by data source.
    """
    patients, total = scan_patients(source=source, limit=limit, offset=offset, search=search)
    return PatientListResponse(
        patients=patients,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{patient_id}", response_model=ParsedPatientResponse)
async def get_patient(patient_id: str) -> ParsedPatientResponse:
    """Get parsed patient data by ID."""
    patient = get_patient_by_id(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail=f"Patient not found: {patient_id}")

    try:
        # Run sync I/O in thread pool to avoid blocking event loop
        return await asyncio.to_thread(parse_patient_file, patient.file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse patient file: {str(e)}")
