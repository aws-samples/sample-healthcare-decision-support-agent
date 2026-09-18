"""FHIR R4 Bundle Parser.

This module parses FHIR R4 Bundle JSON documents (Synthea and MIMIC-IV formats)
into structured Python objects compatible with the CCDA parser models.
"""

import json
from typing import Any

from pydantic import BaseModel, Field

# Reuse common models from CCDA parser
from medical_nudging.parsers.ccda_parser import (
    Demographics,
    Problem,
    Medication,
    Allergy,
    Vital,
    Lab,
    Immunization,
)


class Encounter(BaseModel):
    """Encounter entry (FHIR-specific)."""

    encounter_type: str
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    reason: str | None = None
    class_code: str | None = None  # AMB, IMP, EMER, etc.


class Procedure(BaseModel):
    """Procedure entry (FHIR-specific)."""

    description: str
    code: str | None = None
    code_system: str | None = None
    status: str | None = None
    performed_date: str | None = None


class ParsedFHIR(BaseModel):
    """Parsed FHIR Bundle document."""

    demographics: Demographics
    problems: list[Problem] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    vitals: list[Vital] = Field(default_factory=list)
    labs: list[Lab] = Field(default_factory=list)
    immunizations: list[Immunization] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)
    procedures: list[Procedure] = Field(default_factory=list)


class FHIRParseError(Exception):
    """Raised when FHIR parsing fails."""

    pass


def _get_coding_display(codeable_concept: dict[str, Any] | None) -> str | None:
    """Extract display text from a CodeableConcept."""
    if not codeable_concept:
        return None
    # Try text first
    text = codeable_concept.get("text")
    if text:
        return str(text)
    # Then try coding display
    codings = codeable_concept.get("coding", [])
    if codings and codings[0].get("display"):
        return str(codings[0]["display"])
    return None


def _get_coding_code(codeable_concept: dict[str, Any] | None) -> str | None:
    """Extract code from a CodeableConcept."""
    if not codeable_concept:
        return None
    codings = codeable_concept.get("coding", [])
    if codings:
        code = codings[0].get("code")
        return str(code) if code else None
    return None


def _get_coding_system(codeable_concept: dict[str, Any] | None) -> str | None:
    """Extract code system from a CodeableConcept."""
    if not codeable_concept:
        return None
    codings = codeable_concept.get("coding", [])
    if codings:
        system = codings[0].get("system")
        return str(system) if system else None
    return None


def _get_medication_name_from_concept(
    codeable_concept: dict[str, Any] | None,
) -> str | None:
    """Extract medication name from a CodeableConcept, falling back to code.

    MIMIC-IV often has coding with only a ``code`` field (e.g. formulary
    drug code) and no ``display`` or ``text``.  We fall back to the code
    value so these medications are not silently dropped.
    """
    if not codeable_concept:
        return None
    # Standard path: text or display
    name = _get_coding_display(codeable_concept)
    if name:
        return name
    # MIMIC fallback: use the code itself
    codings = codeable_concept.get("coding", [])
    if codings:
        code = codings[0].get("code")
        if code:
            return str(code)
    return None


def _get_medication_name_from_resource(med_resource: dict[str, Any]) -> str | None:
    """Extract a human-readable name from a Medication resource.

    MIMIC-IV stores the drug name in ``identifier[]`` with system
    ``mimic-medication-name``, while the ``code`` field holds an NDC.
    """
    # Try identifiers first (MIMIC pattern: medication-name system)
    for ident in med_resource.get("identifier", []):
        system = ident.get("system", "")
        if "medication-name" in system and ident.get("value"):
            return str(ident["value"])
    # Fall back to code display/text/code
    return _get_medication_name_from_concept(med_resource.get("code"))


def _resolve_medication_name(
    resource: dict[str, Any],
    medication_index: dict[str, dict[str, Any]],
) -> str | None:
    """Resolve medication name from medicationCodeableConcept or medicationReference.

    Synthea uses medicationCodeableConcept (inline). MIMIC-IV uses
    medicationReference pointing to a separate Medication resource.
    """
    # Try medicationCodeableConcept first (inline)
    med_concept = resource.get("medicationCodeableConcept")
    if med_concept:
        return _get_medication_name_from_concept(med_concept)

    # Try medicationReference (MIMIC pattern — points to Medication resource)
    med_ref = resource.get("medicationReference", {})
    if not isinstance(med_ref, dict):
        return None
    reference = med_ref.get("reference", "")
    if reference.startswith("Medication/"):
        med_id = reference.split("/", 1)[1]
        med_resource = medication_index.get(med_id)
        if med_resource:
            return _get_medication_name_from_resource(med_resource)

    # Fall back to display on the reference itself
    display = med_ref.get("display")
    if display:
        return str(display)

    return None


def _parse_patient(patient: dict | None) -> Demographics:
    """Parse Patient resource into Demographics."""
    if not patient:
        return Demographics()

    name_parts: dict[str, str] = {}
    names = patient.get("name", [])
    if names:
        name = names[0]
        given = name.get("given", [])
        if given:
            name_parts["given"] = given[0]
        if name.get("family"):
            name_parts["family"] = name["family"]

    dob = patient.get("birthDate")
    gender = patient.get("gender")

    return Demographics(name=name_parts, dob=dob, gender=gender)


def _parse_conditions(conditions: list[dict]) -> list[Problem]:
    """Parse Condition resources into Problems."""
    problems = []
    for condition in conditions:
        code_concept = condition.get("code")
        description = _get_coding_display(code_concept)
        if not description:
            continue

        # Get clinical status
        clinical_status = condition.get("clinicalStatus", {})
        status_codings = clinical_status.get("coding", [])
        status = status_codings[0].get("code") if status_codings else None

        problems.append(
            Problem(
                description=description,
                code=_get_coding_code(code_concept),
                code_system=_get_coding_system(code_concept),
                status=status,
                onset_date=condition.get("onsetDateTime"),
            )
        )
    return problems


def _parse_medication_requests(
    med_requests: list[dict],
    medication_index: dict[str, dict[str, Any]] | None = None,
) -> list[Medication]:
    """Parse MedicationRequest resources into Medications."""
    medications = []
    med_idx = medication_index or {}
    for med_req in med_requests:
        name = _resolve_medication_name(med_req, med_idx)
        if not name:
            continue

        # Get dosage if available
        dosage = None
        dosage_instructions = med_req.get("dosageInstruction", [])
        if dosage_instructions:
            dose_and_rate = dosage_instructions[0].get("doseAndRate", [])
            if dose_and_rate:
                dose_qty = dose_and_rate[0].get("doseQuantity", {})
                if dose_qty.get("value"):
                    dosage = f"{dose_qty.get('value')} {dose_qty.get('unit', '')}".strip()

        medications.append(
            Medication(
                name=name,
                dosage=dosage,
                status=med_req.get("status"),
                start_date=med_req.get("authoredOn"),
            )
        )
    return medications


def _parse_medication_administrations(
    med_admins: list[dict],
    medication_index: dict[str, dict[str, Any]] | None = None,
) -> list[Medication]:
    """Parse MedicationAdministration resources into Medications.

    Common in inpatient/ICU settings (MIMIC-IV). Represents medications
    that were actually administered to a patient.
    """
    medications = []
    med_idx = medication_index or {}
    for med_admin in med_admins:
        name = _resolve_medication_name(med_admin, med_idx)
        if not name:
            continue

        dosage = None
        dosage_info = med_admin.get("dosage", {})
        dose = dosage_info.get("dose", {})
        if dose.get("value") is not None:
            dosage = f"{dose.get('value')} {dose.get('unit', '')}".strip()

        start_date = med_admin.get("effectiveDateTime")
        if not start_date:
            period = med_admin.get("effectivePeriod", {})
            start_date = period.get("start")

        medications.append(
            Medication(
                name=name,
                dosage=dosage,
                status=med_admin.get("status"),
                start_date=start_date,
            )
        )
    return medications


def _parse_medication_dispenses(
    med_dispenses: list[dict],
    medication_index: dict[str, dict[str, Any]] | None = None,
) -> list[Medication]:
    """Parse MedicationDispense resources into Medications.

    Represents medications dispensed from a pharmacy or ED medication room.
    """
    medications = []
    med_idx = medication_index or {}
    for med_disp in med_dispenses:
        name = _resolve_medication_name(med_disp, med_idx)
        if not name:
            continue

        dosage = None
        quantity = med_disp.get("quantity", {})
        if quantity.get("value") is not None:
            dosage = f"{quantity.get('value')} {quantity.get('unit', '')}".strip()

        start_date = med_disp.get("whenHandedOver")
        if not start_date:
            start_date = med_disp.get("whenPrepared")

        medications.append(
            Medication(
                name=name,
                dosage=dosage,
                status=med_disp.get("status"),
                start_date=start_date,
            )
        )
    return medications


def _parse_medication_statements(
    med_statements: list[dict],
    medication_index: dict[str, dict[str, Any]] | None = None,
) -> list[Medication]:
    """Parse MedicationStatement resources into Medications.

    Captures reported medication use (e.g., home medications at ED triage).
    """
    medications = []
    med_idx = medication_index or {}
    for med_stmt in med_statements:
        name = _resolve_medication_name(med_stmt, med_idx)
        if not name:
            continue

        start_date = med_stmt.get("effectiveDateTime")
        if not start_date:
            period = med_stmt.get("effectivePeriod", {})
            start_date = period.get("start")

        medications.append(
            Medication(
                name=name,
                dosage=None,
                status=med_stmt.get("status"),
                start_date=start_date,
            )
        )
    return medications


def _parse_allergy_intolerances(allergies: list[dict]) -> list[Allergy]:
    """Parse AllergyIntolerance resources into Allergies."""
    result = []
    for allergy in allergies:
        code_concept = allergy.get("code")
        substance = _get_coding_display(code_concept)
        if not substance:
            continue

        # Get reaction
        reaction = None
        severity = None
        reactions = allergy.get("reaction", [])
        if reactions:
            manifestations = reactions[0].get("manifestation", [])
            if manifestations:
                reaction = _get_coding_display(manifestations[0])
            severity = reactions[0].get("severity")

        result.append(
            Allergy(
                substance=substance,
                reaction=reaction,
                severity=severity,
            )
        )
    return result


def _parse_vitals(observations: list[dict[str, Any]]) -> list[Vital]:
    """Parse Observation resources with vital-signs category."""
    return _parse_observations_internal(observations, "vital-signs", is_vital=True)  # type: ignore[return-value]


def _parse_labs(observations: list[dict[str, Any]]) -> list[Lab]:
    """Parse Observation resources with laboratory category."""
    return _parse_observations_internal(observations, "laboratory", is_vital=False)  # type: ignore[return-value]


def _parse_observations_internal(
    observations: list[dict[str, Any]], category: str, is_vital: bool
) -> list[Vital] | list[Lab]:
    """Parse Observation resources filtered by category."""
    results: list[Vital | Lab] = []

    for obs in observations:
        # Check category
        categories = obs.get("category", [])
        obs_category = None
        for cat in categories:
            codings = cat.get("coding", [])
            for coding in codings:
                if coding.get("code") in ["vital-signs", "laboratory"]:
                    obs_category = coding.get("code")
                    break
            if obs_category:
                break

        if obs_category != category:
            continue

        code_concept = obs.get("code")
        name = _get_coding_display(code_concept)
        if not name:
            continue

        # Get value
        value_qty = obs.get("valueQuantity", {})
        value = value_qty.get("value")
        if value is None:
            # Try valueString or valueCodeableConcept
            value = obs.get("valueString")
            if not value:
                value_concept = obs.get("valueCodeableConcept")
                value = _get_coding_display(value_concept)

        if value is None:
            continue

        value_str = str(value)
        unit = value_qty.get("unit")
        date = obs.get("effectiveDateTime")

        if is_vital:
            results.append(
                Vital(
                    name=name,
                    value=value_str,
                    unit=str(unit) if unit else None,
                    date=str(date) if date else None,
                )
            )
        else:
            status = obs.get("status")
            results.append(
                Lab(
                    test=name,
                    value=value_str,
                    unit=str(unit) if unit else None,
                    status=str(status) if status else None,
                )
            )

    return results  # type: ignore[return-value]


def _parse_immunizations(immunizations: list[dict]) -> list[Immunization]:
    """Parse Immunization resources."""
    result = []
    for imm in immunizations:
        vaccine_concept = imm.get("vaccineCode")
        vaccine = _get_coding_display(vaccine_concept)
        if not vaccine:
            continue

        date = imm.get("occurrenceDateTime")
        result.append(Immunization(vaccine=vaccine, date=date))

    return result


def _parse_encounters(encounters: list[dict[str, Any]]) -> list[Encounter]:
    """Parse Encounter resources."""
    result = []
    for enc in encounters:
        # Get encounter type
        types = enc.get("type", [])
        encounter_type = _get_coding_display(types[0]) if types else None
        if not encounter_type:
            encounter_type = "Unknown"

        # Get period
        period = enc.get("period", {})
        start_date = period.get("start")
        end_date = period.get("end")

        # Get reason
        reason_codes = enc.get("reasonCode", [])
        reason = _get_coding_display(reason_codes[0]) if reason_codes else None

        # Get class code (AMB, IMP, etc.)
        class_info = enc.get("class", {})
        class_code = class_info.get("code")

        result.append(
            Encounter(
                encounter_type=encounter_type,
                status=enc.get("status"),
                start_date=str(start_date) if start_date else None,
                end_date=str(end_date) if end_date else None,
                reason=reason,
                class_code=str(class_code) if class_code else None,
            )
        )
    return result


def _parse_procedures(procedures: list[dict]) -> list[Procedure]:
    """Parse Procedure resources."""
    result = []
    for proc in procedures:
        code_concept = proc.get("code")
        description = _get_coding_display(code_concept)
        if not description:
            continue

        # Get performed date
        performed_date = proc.get("performedDateTime")
        if not performed_date:
            performed_period = proc.get("performedPeriod", {})
            performed_date = performed_period.get("start")

        result.append(
            Procedure(
                description=description,
                code=_get_coding_code(code_concept),
                code_system=_get_coding_system(code_concept),
                status=proc.get("status"),
                performed_date=performed_date,
            )
        )
    return result


def parse_fhir(json_str: str) -> ParsedFHIR:
    """Parse FHIR Bundle JSON string into structured data.

    Args:
        json_str: FHIR Bundle JSON document as string

    Returns:
        ParsedFHIR object with all parsed resources

    Raises:
        FHIRParseError: If JSON is malformed or not a valid Bundle
    """
    try:
        bundle = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise FHIRParseError(f"Invalid JSON: {e}") from e

    if bundle.get("resourceType") != "Bundle":
        raise FHIRParseError(f"Expected FHIR Bundle, got {bundle.get('resourceType', 'unknown')}")

    # Index resources by type
    resources: dict[str, list[dict]] = {}
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType")
        if rtype:
            resources.setdefault(rtype, []).append(resource)

    # Build Medication resource index for reference resolution (MIMIC pattern)
    medication_index: dict[str, dict[str, Any]] = {}
    for med_resource in resources.get("Medication", []):
        med_id = med_resource.get("id")
        if med_id:
            medication_index[str(med_id)] = med_resource

    # Parse Patient (should be exactly one)
    patients = resources.get("Patient", [])
    demographics = _parse_patient(patients[0] if patients else None)

    # Parse medications from all four FHIR resource types
    medications = _parse_medication_requests(
        resources.get("MedicationRequest", []), medication_index
    )
    medications.extend(
        _parse_medication_administrations(
            resources.get("MedicationAdministration", []), medication_index
        )
    )
    medications.extend(
        _parse_medication_dispenses(resources.get("MedicationDispense", []), medication_index)
    )
    medications.extend(
        _parse_medication_statements(resources.get("MedicationStatement", []), medication_index)
    )

    # Parse other resources
    observations = resources.get("Observation", [])
    return ParsedFHIR(
        demographics=demographics,
        problems=_parse_conditions(resources.get("Condition", [])),
        medications=medications,
        allergies=_parse_allergy_intolerances(resources.get("AllergyIntolerance", [])),
        vitals=_parse_vitals(observations),
        labs=_parse_labs(observations),
        immunizations=_parse_immunizations(resources.get("Immunization", [])),
        encounters=_parse_encounters(resources.get("Encounter", [])),
        procedures=_parse_procedures(resources.get("Procedure", [])),
    )
