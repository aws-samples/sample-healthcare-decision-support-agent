"""CCDA XML Parser using lxml.

This module parses CCDA (Consolidated Clinical Document Architecture) XML documents
into structured Python objects.
"""

from typing import Any, cast

from lxml import etree
from lxml.etree import _Element
from pydantic import BaseModel, Field


def _get_secure_parser() -> etree.XMLParser:
    """Create a secure XML parser that prevents XXE attacks.

    Returns:
        XMLParser configured to:
        - Disable external entity resolution (prevents XXE)
        - Disable network access (prevents SSRF)
        - Disable DTD loading (prevents Billion Laughs DoS)
        - Limit document size (prevents memory exhaustion)
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )


class Demographics(BaseModel):
    """Patient demographics."""

    name: dict[str, str] = Field(default_factory=dict)
    dob: str | None = None
    gender: str | None = None


class Problem(BaseModel):
    """Problem/diagnosis entry."""

    description: str
    code: str | None = None
    code_system: str | None = None
    status: str | None = None  # active, resolved, inactive
    onset_date: str | None = None  # When the problem started


class Medication(BaseModel):
    """Medication entry."""

    name: str
    dosage: str | None = None
    route: str | None = None
    status: str | None = None  # active, completed, on-hold, stopped
    start_date: str | None = None  # When the medication was started
    end_date: str | None = None  # When the medication was stopped (if applicable)


class Allergy(BaseModel):
    """Allergy entry."""

    substance: str
    reaction: str | None = None
    severity: str | None = None


class Vital(BaseModel):
    """Vital sign entry."""

    name: str
    value: str
    unit: str | None = None
    date: str | None = None


class Lab(BaseModel):
    """Lab result entry."""

    test: str
    value: str
    unit: str | None = None
    status: str | None = None
    date: str | None = None


class Immunization(BaseModel):
    """Immunization entry."""

    vaccine: str
    date: str | None = None


class ParsedCCDA(BaseModel):
    """Parsed CCDA document."""

    demographics: Demographics
    problems: list[Problem] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    vitals: list[Vital] = Field(default_factory=list)
    labs: list[Lab] = Field(default_factory=list)
    immunizations: list[Immunization] = Field(default_factory=list)
    clinical_notes: list[str] = Field(default_factory=list)


class CCDAParseError(Exception):
    """Raised when CCDA parsing fails."""

    pass


def _get_text(element: Any, xpath: str, namespaces: dict[str, str]) -> str | None:
    """Get text from XML element using XPath."""
    result = element.xpath(xpath, namespaces=namespaces)
    if result and len(result) > 0:
        el = result[0]
        return el.text if hasattr(el, "text") else str(el)
    return None


def _get_attr(element: Any, xpath: str, attr: str, namespaces: dict[str, str]) -> str | None:
    """Get attribute from XML element using XPath."""
    result = element.xpath(xpath, namespaces=namespaces)
    if result and len(result) > 0:
        val = result[0].get(attr)
        return str(val) if val is not None else None
    return None


def _parse_demographics(root: Any, ns: dict[str, str]) -> Demographics:
    """Parse patient demographics section."""
    name_parts: dict[str, str] = {}

    given = _get_text(root, ".//hl7:recordTarget//hl7:given", ns)
    family = _get_text(root, ".//hl7:recordTarget//hl7:family", ns)

    if given:
        name_parts["given"] = given
    if family:
        name_parts["family"] = family

    dob = _get_attr(root, ".//hl7:recordTarget//hl7:birthTime", "value", ns)
    gender = _get_attr(root, ".//hl7:recordTarget//hl7:administrativeGenderCode", "code", ns)

    return Demographics(name=name_parts, dob=dob, gender=gender)


def _parse_problems(root: Any, ns: dict[str, str]) -> list[Problem]:
    """Parse problems/diagnoses section."""
    problems = []

    # Look for problem section entries
    problem_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='11450-4']]//hl7:entry",
        namespaces=ns,
    )

    for entry in problem_entries:
        # Get problem value from observation
        display_name = _get_attr(
            entry,
            ".//hl7:observation/hl7:value[@xsi:type='CD']",
            "displayName",
            ns,
        )
        code = _get_attr(
            entry,
            ".//hl7:observation/hl7:value[@xsi:type='CD']",
            "code",
            ns,
        )
        code_system = _get_attr(
            entry,
            ".//hl7:observation/hl7:value[@xsi:type='CD']",
            "codeSystem",
            ns,
        )

        # Extract status from the act or observation statusCode
        # CCDA uses: active, completed, aborted, suspended
        status = _get_attr(entry, ".//hl7:act/hl7:statusCode", "code", ns)
        if not status:
            status = _get_attr(entry, ".//hl7:observation/hl7:statusCode", "code", ns)

        # Map CCDA status codes to our status values
        status_map = {
            "active": "active",
            "completed": "resolved",
            "aborted": "resolved",
            "suspended": "inactive",
        }
        mapped_status = status_map.get(status, status) if status else None

        # Extract onset date from effectiveTime/low
        onset_date = _get_attr(entry, ".//hl7:effectiveTime/hl7:low", "value", ns)
        if not onset_date:
            onset_date = _get_attr(
                entry, ".//hl7:observation/hl7:effectiveTime/hl7:low", "value", ns
            )

        if display_name:
            problems.append(
                Problem(
                    description=display_name,
                    code=code,
                    code_system=code_system,
                    status=mapped_status,
                    onset_date=onset_date,
                )
            )

    return problems


def _parse_medications(root: Any, ns: dict[str, str]) -> list[Medication]:
    """Parse medications section."""
    medications = []

    med_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='10160-0']]//hl7:entry",
        namespaces=ns,
    )

    for entry in med_entries:
        name = _get_attr(
            entry,
            ".//hl7:manufacturedProduct//hl7:name",
            "displayName",
            ns,
        )
        if not name:
            name = _get_text(entry, ".//hl7:manufacturedProduct//hl7:name", ns)

        dosage = _get_attr(entry, ".//hl7:doseQuantity", "value", ns)
        route = _get_attr(entry, ".//hl7:routeCode", "displayName", ns)

        # Extract status from substanceAdministration statusCode
        # CCDA uses: active, completed, aborted, on-hold
        status = _get_attr(entry, ".//hl7:substanceAdministration/hl7:statusCode", "code", ns)

        # Map CCDA status codes to our status values
        status_map = {
            "active": "active",
            "completed": "completed",
            "aborted": "stopped",
            "on-hold": "on-hold",
        }
        mapped_status = status_map.get(status, status) if status else None

        # Extract start date from effectiveTime/low
        start_date = _get_attr(entry, ".//hl7:effectiveTime/hl7:low", "value", ns)
        if not start_date:
            start_date = _get_attr(
                entry, ".//hl7:substanceAdministration/hl7:effectiveTime/hl7:low", "value", ns
            )

        # Extract end date from effectiveTime/high
        end_date = _get_attr(entry, ".//hl7:effectiveTime/hl7:high", "value", ns)
        if not end_date:
            end_date = _get_attr(
                entry, ".//hl7:substanceAdministration/hl7:effectiveTime/hl7:high", "value", ns
            )

        if name:
            medications.append(
                Medication(
                    name=name,
                    dosage=dosage,
                    route=route,
                    status=mapped_status,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

    return medications


def _parse_allergies(root: Any, ns: dict[str, str]) -> list[Allergy]:
    """Parse allergies section."""
    allergies = []

    allergy_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='48765-2']]//hl7:entry",
        namespaces=ns,
    )

    for entry in allergy_entries:
        substance = _get_attr(
            entry,
            ".//hl7:participant//hl7:playingEntity/hl7:code",
            "displayName",
            ns,
        )
        reaction = _get_attr(
            entry,
            ".//hl7:observation[hl7:code[@code='ASSERTION']]//hl7:value",
            "displayName",
            ns,
        )
        severity = _get_attr(
            entry,
            ".//hl7:observation[hl7:code[@code='SEV']]//hl7:value",
            "displayName",
            ns,
        )

        if substance:
            allergies.append(Allergy(substance=substance, reaction=reaction, severity=severity))

    return allergies


def _parse_vitals(root: Any, ns: dict[str, str]) -> list[Vital]:
    """Parse vital signs section."""
    vitals = []

    vital_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='8716-3']]//hl7:observation",
        namespaces=ns,
    )

    for obs in vital_entries:
        name = _get_attr(obs, "./hl7:code", "displayName", ns)
        value = _get_attr(obs, "./hl7:value", "value", ns)
        unit = _get_attr(obs, "./hl7:value", "unit", ns)
        date = _get_attr(obs, "./hl7:effectiveTime", "value", ns)

        if name and value:
            vitals.append(Vital(name=name, value=value, unit=unit, date=date))

    return vitals


def _parse_labs(root: Any, ns: dict[str, str]) -> list[Lab]:
    """Parse lab results section."""
    labs = []

    lab_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='30954-2']]//hl7:observation",
        namespaces=ns,
    )

    for obs in lab_entries:
        test = _get_attr(obs, "./hl7:code", "displayName", ns)
        value = _get_attr(obs, "./hl7:value", "value", ns)
        unit = _get_attr(obs, "./hl7:value", "unit", ns)
        status = _get_attr(obs, "./hl7:statusCode", "code", ns)
        date = _get_attr(obs, "./hl7:effectiveTime", "value", ns)

        if test and value:
            labs.append(Lab(test=test, value=value, unit=unit, status=status, date=date))

    return labs


def _parse_immunizations(root: Any, ns: dict[str, str]) -> list[Immunization]:
    """Parse immunizations section."""
    immunizations = []

    imm_entries = root.xpath(
        ".//hl7:component//hl7:section[hl7:code[@code='11369-6']]//hl7:substanceAdministration",
        namespaces=ns,
    )

    for entry in imm_entries:
        vaccine = _get_attr(
            entry,
            ".//hl7:manufacturedProduct//hl7:code",
            "displayName",
            ns,
        )
        date = _get_attr(entry, "./hl7:effectiveTime", "value", ns)

        if vaccine:
            immunizations.append(Immunization(vaccine=vaccine, date=date))

    return immunizations


def parse_ccda(xml: str) -> ParsedCCDA:
    """Parse CCDA XML string into structured data.

    Args:
        xml: CCDA XML document as string

    Returns:
        ParsedCCDA object with all parsed sections

    Raises:
        CCDAParseError: If XML is malformed or cannot be parsed
    """
    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=_get_secure_parser())
    except etree.XMLSyntaxError as e:
        raise CCDAParseError(f"Invalid XML: {e}") from e

    # Define namespace
    ns = {"hl7": "urn:hl7-org:v3", "xsi": "http://www.w3.org/2001/XMLSchema-instance"}

    # Parse all sections
    demographics = _parse_demographics(root, ns)
    problems = _parse_problems(root, ns)
    medications = _parse_medications(root, ns)
    allergies = _parse_allergies(root, ns)
    vitals = _parse_vitals(root, ns)
    labs = _parse_labs(root, ns)
    immunizations = _parse_immunizations(root, ns)

    # Extract clinical notes from text sections
    clinical_notes = []
    text_sections = root.xpath(".//hl7:section/hl7:text", namespaces=ns)
    if isinstance(text_sections, list):
        for section in text_sections:
            if hasattr(section, "itertext"):
                elem = cast(_Element, section)
                text = "".join(str(t) for t in elem.itertext()).strip()
                if text:
                    clinical_notes.append(text)

    return ParsedCCDA(
        demographics=demographics,
        problems=problems,
        medications=medications,
        allergies=allergies,
        vitals=vitals,
        labs=labs,
        immunizations=immunizations,
        clinical_notes=clinical_notes,
    )
