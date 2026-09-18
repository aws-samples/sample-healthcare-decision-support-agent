"""Patient-related Pydantic schemas."""

from typing import Literal
from pydantic import BaseModel


class PatientSummary(BaseModel):
    """Summary of a patient for listing."""

    id: str
    name: str
    dob: str | None = None
    gender: str | None = None
    source: Literal["synthea", "medicare"]
    format: Literal["ccda", "fhir"]
    file_path: str


class PatientListResponse(BaseModel):
    """Response for patient list endpoint."""

    patients: list[PatientSummary]
    total: int
    limit: int
    offset: int


class DemographicsResponse(BaseModel):
    """Patient demographics."""

    name: dict | None = None
    dob: str | None = None
    gender: str | None = None


class ProblemResponse(BaseModel):
    """A clinical problem/condition."""

    description: str
    code: str | None = None
    code_system: str | None = None
    status: str | None = None
    onset_date: str | None = None


class MedicationResponse(BaseModel):
    """A medication."""

    name: str
    dosage: str | None = None
    route: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class AllergyResponse(BaseModel):
    """An allergy."""

    substance: str
    reaction: str | None = None
    severity: str | None = None


class VitalResponse(BaseModel):
    """A vital sign measurement."""

    name: str
    value: str
    unit: str | None = None
    date: str | None = None


class LabResponse(BaseModel):
    """A lab result."""

    test: str
    value: str
    unit: str | None = None
    status: str | None = None
    date: str | None = None


class ImmunizationResponse(BaseModel):
    """An immunization record."""

    vaccine: str
    date: str | None = None


class EncounterResponse(BaseModel):
    """A clinical encounter (FHIR only)."""

    type: str | None = None
    date: str | None = None
    provider: str | None = None
    location: str | None = None


class ProcedureResponse(BaseModel):
    """A procedure (FHIR only)."""

    description: str
    code: str | None = None
    date: str | None = None
    status: str | None = None


class ParsedPatientResponse(BaseModel):
    """Full parsed patient data."""

    id: str
    format: Literal["ccda", "fhir"]
    demographics: DemographicsResponse | None = None
    problems: list[ProblemResponse] = []
    medications: list[MedicationResponse] = []
    allergies: list[AllergyResponse] = []
    vitals: list[VitalResponse] = []
    labs: list[LabResponse] = []
    immunizations: list[ImmunizationResponse] = []
    encounters: list[EncounterResponse] = []  # FHIR only
    procedures: list[ProcedureResponse] = []  # FHIR only
    clinical_notes: list[str] = []
