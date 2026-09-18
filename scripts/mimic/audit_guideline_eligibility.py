#!/usr/bin/env python3
"""Audit which MIMIC-IV demo patients are eligible for which corpus guideline.

Answers three questions that gate a guideline-grounded generation run:

1. **Eligible-patient count.** How many of the demo patients present at least one
   clinical situation that some source in the frozen guideline corpus governs?
   A patient nobody's guideline covers cannot produce a citable nudge, so this is
   the effective n for guideline-grounded evaluation.
2. **Per-guideline distribution.** Which sources have enough eligible patients to
   be exercised at all, and which are dead weight in the corpus?
3. **Index-event date quality.** Does each eligible pair have a resolvable, dated
   index event? Temporal perturbations rewrite that date, so a pair without one
   cannot carry a temporal-staleness control.

Eligibility criteria are declared in ``CRITERIA`` below, *before* any counting, so
the thresholds are not tuned to make the count look good. Each criterion is a
coarse, deliberately over-inclusive screen on codes and resource presence: it says
"this guideline plausibly applies", not "this guideline was indicated". A clinician
reading the criteria table should be able to disagree with a specific row.

Because a few criteria can be met by monitoring evidence alone (two creatinine
results, an INR on an anticoagulant), every count is reported at two strictness
tiers: the declared criterion, and a **diagnosis/procedure-anchored** tier that
requires a condition code or a procedure (``ANCHORED_OVERRIDES``). The declared
criteria are never retuned to improve a count; the anchored tier is reported
alongside them so the effective n can be read either way.

Five corpus sources are institution-level rather than patient-level (surveillance
definitions, stewardship program structure, unit-level infection control). They
govern hospital programs, not individual patients, so they are marked
``institution_level`` and are structurally ineligible by construction, not by a
failure to match. That is a corpus-composition finding, not a data gap.

Usage:
    uv run scripts/mimic/audit_guideline_eligibility.py \
        --input-dir data/mimic-iv-fhir-demo \
        --catalog guidelines/catalog.json \
        --output results/cohort/guideline_eligibility.json

Prerequisites:
    uv run scripts/mimic/fetch_demo.py
"""
# /// script
# requires-python = ">=3.12"
# # boto3 is not used directly here, but importing medical_nudging.healthlake.normalize
# # runs the package __init__, which imports import_job -> botocore.
# dependencies = ["rich", "boto3"]
# ///

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

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
log = logging.getLogger("audit_eligibility")
console = Console()

Scope = Literal["patient_level", "institution_level"]

MEDICATION_RESOURCE_TYPES = frozenset(
    {
        "MedicationRequest",
        "MedicationAdministration",
        "MedicationDispense",
        "MedicationStatement",
    }
)
DATED_FIELDS = (
    "effectiveDateTime",
    "onsetDateTime",
    "recordedDate",
    "performedDateTime",
    "authoredOn",
    "occurrenceDateTime",
)

# ---------------------------------------------------------------------------
# Code and text screens
#
# MIMIC-IV carries both ICD-9 and ICD-10 diagnosis codes, so every condition
# screen lists both families. Prefix matching is intentional: I48 catches every
# atrial-fibrillation subtype without enumerating them.
# ---------------------------------------------------------------------------

CONDITION_CODES: dict[str, tuple[str, ...]] = {
    "acute_coronary_syndrome": ("I21", "I22", "I24", "410", "411"),
    "chronic_coronary_disease": ("I25", "414"),
    "peripheral_artery_disease": ("I70", "I73.9", "440", "443.9"),
    "atrial_fibrillation": ("I48", "427.3"),
    "heart_failure": ("I50", "428"),
    "hypertension": ("I10", "I11", "I12", "I13", "I15", "401", "402", "403", "404", "405"),
    "type_2_diabetes": ("E11", "250"),
    "intracerebral_hemorrhage": ("I61", "431"),
    "traumatic_brain_injury": ("S06", "800", "801", "803", "804", "850", "851", "852", "853"),
    "sepsis": ("A41", "R65", "038", "995.9"),
    "pneumonia": ("J13", "J14", "J15", "J16", "J17", "J18", "481", "482", "483", "485", "486"),
    "influenza": ("J09", "J10", "J11", "487", "488"),
    "acute_kidney_injury": ("N17", "584"),
    "ards": ("J80", "518.5", "518.82"),
    "asthma": ("J45", "493"),
    "migraine": ("G43", "346"),
    "food_allergy": ("T78.0", "T78.1", "Z91.01", "Z91.02", "995.6", "693.1"),
    "cirrhosis": ("K70.3", "K74", "K76.6", "571.2", "571.5", "572.3"),
    "ascites": ("R18", "789.5"),
    "anemia": ("D50", "D62", "D64", "285", "280"),
}

OBSERVATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "inr": re.compile(r"\bINR\b|international normalized", re.IGNORECASE),
    "creatinine": re.compile(r"creatinine", re.IGNORECASE),
    "lactate": re.compile(r"lactate|lactic", re.IGNORECASE),
    "glucose": re.compile(r"glucose", re.IGNORECASE),
    "hemoglobin": re.compile(r"h(a)?emoglobin\b|\bhgb\b", re.IGNORECASE),
}

MEDICATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "warfarin": re.compile(r"warfarin|coumadin", re.IGNORECASE),
    "doac": re.compile(r"apixaban|rivaroxaban|dabigatran|edoxaban", re.IGNORECASE),
    "metformin": re.compile(r"metformin|glucophage", re.IGNORECASE),
    "insulin": re.compile(r"insulin", re.IGNORECASE),
    "systemic_steroid": re.compile(
        r"prednisone|prednisolone|methylprednisolone|dexamethasone|hydrocortisone", re.IGNORECASE
    ),
    "antibiotic": re.compile(
        r"ceftriaxone|vancomycin|piperacillin|tazobactam|meropenem|"
        r"cefepime|ciprofloxacin|levofloxacin|azithromycin|metronidazole|"
        r"ampicillin|sulbactam|gentamicin|tobramycin|cefazolin|clindamycin",
        re.IGNORECASE,
    ),
    "sedative_analgesic": re.compile(
        r"propofol|dexmedetomidine|midazolam|lorazepam|fentanyl|"
        r"hydromorphone|morphine|ketamine",
        re.IGNORECASE,
    ),
    "antiplatelet": re.compile(r"aspirin|clopidogrel|ticagrelor|prasugrel", re.IGNORECASE),
    "diuretic": re.compile(r"furosemide|bumetanide|spironolactone|torsemide", re.IGNORECASE),
    "bronchodilator": re.compile(r"albuterol|salbutamol|ipratropium|formoterol", re.IGNORECASE),
    "triptan": re.compile(r"sumatriptan|rizatriptan|zolmitriptan", re.IGNORECASE),
}

PROCEDURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "mechanical_ventilation": re.compile(
        r"mechanical ventilation|invasive ventilation|intubation|endotracheal", re.IGNORECASE
    ),
    "contrast_imaging": re.compile(
        r"with contrast|contrast[- ]enhanced|angiograph|angiogram|"
        r"\bCT\b.*contrast|coronary catheter",
        re.IGNORECASE,
    ),
    "transfusion": re.compile(
        r"transfusion|packed (red )?(blood )?cell|\bPRBC\b|blood product", re.IGNORECASE
    ),
    "paracentesis": re.compile(r"paracentesis|peritoneal (fluid )?(tap|aspiration)", re.IGNORECASE),
    "cardiac_surgery": re.compile(
        r"coronary artery bypass|\bCABG\b|cardiopulmonary bypass|valve replacement|sternotomy",
        re.IGNORECASE,
    ),
    "dialysis": re.compile(r"dialysis|\bCRRT\b|continuous renal replacement", re.IGNORECASE),
}


@dataclass
class PatientEvidence:
    """Everything one patient contributes to an eligibility decision.

    Sets rather than booleans so an unmatched criterion can be explained by
    reading back what the patient actually had.
    """

    conditions: set[str] = field(default_factory=set)
    observations: set[str] = field(default_factory=set)
    observation_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    medications: set[str] = field(default_factory=set)
    procedures: set[str] = field(default_factory=set)
    has_icu_stay: bool = False
    has_emergency_encounter: bool = False
    has_inpatient_encounter: bool = False
    resource_count: int = 0
    # Latest parseable date on any resource, per resource type. Index-event date
    # quality is judged from these rather than from a single "admission" field,
    # which MIMIC-on-FHIR does not expose uniformly.
    dated_resource_types: set[str] = field(default_factory=set)
    undated_resource_types: set[str] = field(default_factory=set)
    earliest_date: str | None = None
    latest_date: str | None = None


def has_condition(evidence: PatientEvidence, *names: str) -> bool:
    return any(name in evidence.conditions for name in names)


def has_medication(evidence: PatientEvidence, *names: str) -> bool:
    return any(name in evidence.medications for name in names)


def has_procedure(evidence: PatientEvidence, *names: str) -> bool:
    return any(name in evidence.procedures for name in names)


# ---------------------------------------------------------------------------
# ELIGIBILITY CRITERIA — declared before counting.
#
# Each entry maps a corpus source id to (scope, prose criterion, predicate).
# The prose is what goes in the paper appendix and in front of a clinician; the
# predicate must be a faithful reading of the prose.
# ---------------------------------------------------------------------------

CRITERIA: dict[str, tuple[Scope, str, Callable[[PatientEvidence], bool] | None]] = {
    "ACC_AHA_Acute_Coronary_Syndromes_2025": (
        "patient_level",
        "An acute coronary syndrome or acute myocardial infarction diagnosis.",
        lambda e: has_condition(e, "acute_coronary_syndrome"),
    ),
    "AHA_ACC_Chronic_Coronary_Disease_2023": (
        "patient_level",
        "Chronic ischemic heart disease, or a coronary revascularization procedure "
        "(secondary prevention and post-CABG antiplatelet therapy).",
        lambda e: has_condition(e, "chronic_coronary_disease")
        or has_procedure(e, "cardiac_surgery"),
    ),
    "ACC_AHA_Peripheral_Artery_Disease_2024": (
        "patient_level",
        "A peripheral arterial disease diagnosis.",
        lambda e: has_condition(e, "peripheral_artery_disease"),
    ),
    "NICE_Atrial_Fibrillation_NG196_2021": (
        "patient_level",
        "Atrial fibrillation or flutter, or an oral anticoagulant with INR monitoring "
        "(anticoagulation indication and INR management).",
        lambda e: has_condition(e, "atrial_fibrillation")
        or (has_medication(e, "warfarin", "doac") and "inr" in e.observations),
    ),
    "NICE_Chronic_Heart_Failure_NG106_2018": (
        "patient_level",
        "A heart failure diagnosis.",
        lambda e: has_condition(e, "heart_failure"),
    ),
    "NICE_Hypertension_NG136_2019": (
        "patient_level",
        "A hypertension diagnosis.",
        lambda e: has_condition(e, "hypertension"),
    ),
    "NICE_Type_2_Diabetes_NG28_2022": (
        "patient_level",
        "Type 2 diabetes with a glucose-lowering medication (metformin or insulin).",
        lambda e: has_condition(e, "type_2_diabetes") and has_medication(e, "metformin", "insulin"),
    ),
    "ESUR_Contrast_Agents_2018": (
        "patient_level",
        "A contrast-enhanced imaging or angiographic procedure together with either "
        "metformin exposure or at least two creatinine results (contrast-associated "
        "AKI risk and metformin withholding).",
        lambda e: has_procedure(e, "contrast_imaging")
        and (has_medication(e, "metformin") or e.observation_counts.get("creatinine", 0) >= 2),
    ),
    "JBDS_Steroid_Hyperglycaemia_2023": (
        "patient_level",
        "A systemic glucocorticoid together with glucose monitoring.",
        lambda e: has_medication(e, "systemic_steroid") and "glucose" in e.observations,
    ),
    "KDIGO_AKI_2012": (
        "patient_level",
        "An acute kidney injury diagnosis, dialysis or CRRT, or at least two "
        "creatinine results (the minimum needed to stage AKI by trend).",
        lambda e: has_condition(e, "acute_kidney_injury")
        or has_procedure(e, "dialysis")
        or e.observation_counts.get("creatinine", 0) >= 2,
    ),
    "SCCM_ESICM_Surviving_Sepsis_2026": (
        "patient_level",
        "A sepsis diagnosis with a lactate result or an antibiotic.",
        lambda e: has_condition(e, "sepsis")
        and ("lactate" in e.observations or has_medication(e, "antibiotic")),
    ),
    "SCCM_PADIS_2018": (
        "patient_level",
        "An ICU stay with a sedative or analgesic infusion (pain, agitation, "
        "delirium, immobility, sleep).",
        lambda e: e.has_icu_stay and has_medication(e, "sedative_analgesic"),
    ),
    "AHA_ASA_Spontaneous_ICH_2022": (
        "patient_level",
        "A spontaneous intracerebral hemorrhage diagnosis.",
        lambda e: has_condition(e, "intracerebral_hemorrhage"),
    ),
    "BTF_Severe_TBI_4th_Edition_2017": (
        "patient_level",
        "A traumatic brain injury diagnosis with an ICU stay (the guideline is "
        "scoped to severe TBI).",
        lambda e: has_condition(e, "traumatic_brain_injury") and e.has_icu_stay,
    ),
    "ATS_IDSA_CAP_2019": (
        "patient_level",
        "A pneumonia diagnosis without mechanical ventilation "
        "(community-acquired presentation).",
        lambda e: has_condition(e, "pneumonia") and not has_procedure(e, "mechanical_ventilation"),
    ),
    "IDSA_ATS_HAP_VAP_2016": (
        "patient_level",
        "A pneumonia diagnosis with mechanical ventilation (ventilator-associated "
        "or hospital-acquired presentation).",
        lambda e: has_condition(e, "pneumonia") and has_procedure(e, "mechanical_ventilation"),
    ),
    "NHLBI_ARDSNet_Ventilator_Protocol": (
        "patient_level",
        "Mechanical ventilation, with an ARDS diagnosis or an ICU stay.",
        lambda e: has_procedure(e, "mechanical_ventilation")
        and (has_condition(e, "ards") or e.has_icu_stay),
    ),
    "CDC_Influenza_Treatment": (
        "patient_level",
        "An influenza diagnosis.",
        lambda e: has_condition(e, "influenza"),
    ),
    "EASL_Decompensated_Cirrhosis_2018": (
        "patient_level",
        "Cirrhosis with ascites, a paracentesis, or a diuretic "
        "(ascites, SBP, hepatorenal syndrome).",
        lambda e: has_condition(e, "cirrhosis")
        and (
            has_condition(e, "ascites")
            or has_procedure(e, "paracentesis")
            or has_medication(e, "diuretic")
        ),
    ),
    "NICE_Blood_Transfusion_NG24_2015": (
        "patient_level",
        "A transfusion procedure, or anemia with at least two hemoglobin results "
        "(a threshold decision requires a trend).",
        lambda e: has_procedure(e, "transfusion")
        or (has_condition(e, "anemia") and e.observation_counts.get("hemoglobin", 0) >= 2),
    ),
    "NHLBI_NAEPP_Asthma_EPR3_2007": (
        "patient_level",
        "An asthma diagnosis with a bronchodilator.",
        lambda e: has_condition(e, "asthma") and has_medication(e, "bronchodilator"),
    ),
    "AAFP_Acute_Migraine_Management_2002": (
        "patient_level",
        "A migraine diagnosis, or a triptan.",
        lambda e: has_condition(e, "migraine") or has_medication(e, "triptan"),
    ),
    "NIAID_Food_Allergy_Guidelines_2010": (
        "patient_level",
        "A documented food allergy.",
        lambda e: has_condition(e, "food_allergy"),
    ),
    # ---- institution-level sources: no patient predicate by construction ----
    "CDC_NHSN_PSC_Surveillance_Definitions": (
        "institution_level",
        "Surveillance case definitions for hospital infection reporting. Governs an "
        "infection-prevention program, not a patient's care.",
        None,
    ),
    "CDC_Core_Elements_Hospital_Antibiotic_Stewardship": (
        "institution_level",
        "Structural requirements for a hospital antibiotic stewardship program "
        "(leadership, accountability, reporting). Not patient-directed.",
        None,
    ),
    "CDC_HICPAC_NICU_CLABSI": (
        "institution_level",
        "Neonatal ICU central-line bundle guidance. The demo cohort is adult, and "
        "the content is unit-level practice rather than per-patient recommendation.",
        None,
    ),
    "CDC_Outpatient_Oncology_Infection_Control_2011": (
        "institution_level",
        "Infection-control plan for outpatient oncology facilities. Wrong care "
        "setting for an ICU/inpatient cohort and facility-level in scope.",
        None,
    ),
    "CDC_HICPAC_Healthcare_Pneumonia_2003": (
        "institution_level",
        "Healthcare-associated pneumonia *prevention* guidance directed at "
        "institutional practice; treatment decisions are covered by the "
        "IDSA/ATS HAP-VAP guideline instead.",
        None,
    ),
}

# Some declared criteria can be satisfied by monitoring evidence alone -- KDIGO
# matches any patient with two creatinine results, which in an ICU cohort is
# nearly everyone. That is a defensible reading of "this guideline plausibly
# applies" but a useless denominator, so a second, stricter tier requires a
# diagnosis code or a procedure. Both tiers are reported; neither replaces the
# other, and the declared criteria above are not retuned to improve the count.
ANCHORED_OVERRIDES: dict[str, Callable[[PatientEvidence], bool]] = {
    "KDIGO_AKI_2012": lambda e: has_condition(e, "acute_kidney_injury")
    or has_procedure(e, "dialysis"),
    "NICE_Atrial_Fibrillation_NG196_2021": lambda e: has_condition(e, "atrial_fibrillation"),
    "NICE_Blood_Transfusion_NG24_2015": lambda e: has_procedure(e, "transfusion"),
}


def predicate_for(source: str, anchored: bool) -> Callable[[PatientEvidence], bool] | None:
    """Return the criterion predicate for a source under the requested tier."""
    if anchored and source in ANCHORED_OVERRIDES:
        return ANCHORED_OVERRIDES[source]
    return CRITERIA[source][2]


# ---------------------------------------------------------------------------
# Resource inspection
# ---------------------------------------------------------------------------


def codeable_texts(concept: object) -> Iterator[str]:
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


def coding_codes(concept: object) -> Iterator[str]:
    """Yield the raw `code` values in a CodeableConcept."""
    if not isinstance(concept, dict):
        return
    for coding in concept.get("coding", []):
        if isinstance(coding, dict):
            code = coding.get("code")
            if isinstance(code, str):
                yield code


def medication_texts(resource: dict[str, Any]) -> Iterator[str]:
    """Yield every string that might name the medication in a resource."""
    yield from codeable_texts(resource.get("medicationCodeableConcept"))
    reference = resource.get("medicationReference")
    if isinstance(reference, dict):
        display = reference.get("display")
        if isinstance(display, str):
            yield display
    for identifier in resource.get("identifier", []):
        if isinstance(identifier, dict):
            value = identifier.get("value")
            if isinstance(value, str):
                yield value


def resource_date(resource: dict[str, Any]) -> str | None:
    """Return the first ISO-ish date on a resource, or None."""
    for field_name in DATED_FIELDS:
        value = resource.get(field_name)
        if isinstance(value, str) and value:
            return value
    period = resource.get("period") or resource.get("effectivePeriod")
    if isinstance(period, dict):
        start = period.get("start")
        if isinstance(start, str) and start:
            return start
    return None


def matched_condition_names(resource: dict[str, Any]) -> set[str]:
    """Return every condition screen a Condition resource satisfies."""
    codes = [code.upper().replace(".", "") for code in coding_codes(resource.get("code"))]
    matched: set[str] = set()
    for name, prefixes in CONDITION_CODES.items():
        normalized = tuple(prefix.upper().replace(".", "") for prefix in prefixes)
        if any(code.startswith(normalized) for code in codes):
            matched.add(name)
    return matched


def _record_date(evidence: PatientEvidence, resource_type: str, resource: dict[str, Any]) -> None:
    """Track which resource types carry dates and the patient's overall date span."""
    date = resource_date(resource)
    if date is None:
        evidence.undated_resource_types.add(resource_type)
        return
    evidence.dated_resource_types.add(resource_type)
    if evidence.earliest_date is None or date < evidence.earliest_date:
        evidence.earliest_date = date
    if evidence.latest_date is None or date > evidence.latest_date:
        evidence.latest_date = date


def _matched_names(texts: list[str], patterns: dict[str, re.Pattern[str]]) -> set[str]:
    """Names whose pattern matches any of the texts."""
    return {name for name, pattern in patterns.items() if any(pattern.search(t) for t in texts)}


def _record_medication(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    evidence.medications |= _matched_names(list(medication_texts(resource)), MEDICATION_PATTERNS)


def _record_observation(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    texts = list(codeable_texts(resource.get("code")))
    for name in _matched_names(texts, OBSERVATION_PATTERNS):
        evidence.observations.add(name)
        evidence.observation_counts[name] += 1


def _record_condition(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    evidence.conditions |= matched_condition_names(resource)


def _record_procedure(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    texts = list(codeable_texts(resource.get("code")))
    evidence.procedures |= _matched_names(texts, PROCEDURE_PATTERNS)


def _record_encounter(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    encounter_class = resource.get("class")
    code = encounter_class.get("code") if isinstance(encounter_class, dict) else None
    if code == "EMER":
        evidence.has_emergency_encounter = True
    elif code in {"IMP", "ACUTE", "NONAC"}:
        evidence.has_inpatient_encounter = True
    for text in codeable_texts(resource.get("type")):
        if re.search(r"intensive care|\bICU\b|critical care", text, re.IGNORECASE):
            evidence.has_icu_stay = True


_EVIDENCE_RECORDERS: dict[str, Callable[[PatientEvidence, dict[str, Any]], None]] = {
    **{resource_type: _record_medication for resource_type in MEDICATION_RESOURCE_TYPES},
    "Observation": _record_observation,
    "Condition": _record_condition,
    "Procedure": _record_procedure,
    "Encounter": _record_encounter,
}


def update_evidence(evidence: PatientEvidence, resource: dict[str, Any]) -> None:
    """Fold one FHIR resource into a patient's evidence."""
    resource_type = resource.get("resourceType", "")
    evidence.resource_count += 1
    _record_date(evidence, resource_type, resource)
    recorder = _EVIDENCE_RECORDERS.get(resource_type)
    if recorder is not None:
        recorder(evidence, resource)


def scan(input_dir: Path) -> dict[str, PatientEvidence]:
    """Stream every NDJSON file and accumulate evidence per patient."""
    files = mimic_source_files(input_dir)
    if not files:
        raise FileNotFoundError(f"No MIMIC NDJSON files under {input_dir}")

    by_patient: dict[str, PatientEvidence] = {}
    for path in files:
        counted = 0
        # ICU encounters live in their own file and are not typed as ICU inside
        # Encounter.type, so file identity is the reliable ICU signal.
        is_icu_file = "EncounterICU" in path.name or "ICU" in path.name
        for resource in iter_ndjson(path):
            patient_id = patient_id_of(resource)
            if patient_id is None:
                continue
            evidence = by_patient.get(patient_id)
            if evidence is None:
                evidence = PatientEvidence()
                by_patient[patient_id] = evidence
            update_evidence(evidence, resource)
            if is_icu_file:
                evidence.has_icu_stay = True
            counted += 1
        log.info(f"  {path.name}: {counted} patient-linked resources")

    return by_patient


def corpus_sources(catalog_path: Path) -> list[str]:
    """Return the source ids actually present in the frozen index."""
    payload = json.loads(catalog_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{catalog_path}: expected a JSON object")
    entries = payload.get("guidelines")
    if not isinstance(entries, list):
        raise ValueError(f"{catalog_path}: missing a 'guidelines' list")
    sources = [entry["source"] for entry in entries if isinstance(entry, dict)]
    missing = [source for source in sources if source not in CRITERIA]
    if missing:
        raise ValueError(
            "Catalog sources have no declared eligibility criterion: "
            + ", ".join(sorted(missing))
            + ". Add them to CRITERIA before running the audit -- silently skipping a "
            "source would understate the corpus and overstate coverage."
        )
    return sources


def evaluate(
    by_patient: dict[str, PatientEvidence], sources: list[str], anchored: bool = False
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (eligible patients per source, eligible sources per patient)."""
    per_source: dict[str, list[str]] = {source: [] for source in sources}
    per_patient: dict[str, list[str]] = {patient: [] for patient in by_patient}
    for patient_id, evidence in sorted(by_patient.items()):
        for source in sources:
            predicate = predicate_for(source, anchored)
            if predicate is not None and predicate(evidence):
                per_source[source].append(patient_id)
                per_patient[patient_id].append(source)
    return per_source, per_patient


def date_quality(by_patient: dict[str, PatientEvidence], eligible: set[str]) -> dict[str, Any]:
    """Audit whether eligible patients carry usable index-event dates.

    A temporal-staleness perturbation rewrites the date of the evidence a nudge
    rests on, so it needs a dated clinical resource and a span between the
    earliest and latest date wide enough for "stale" to mean something.
    """
    dated: list[str] = []
    undated: list[str] = []
    spans: list[tuple[str, str, str]] = []
    for patient_id in sorted(eligible):
        evidence = by_patient[patient_id]
        clinical_dated = evidence.dated_resource_types - {"Patient"}
        if clinical_dated and evidence.earliest_date and evidence.latest_date:
            dated.append(patient_id)
            spans.append((patient_id, evidence.earliest_date, evidence.latest_date))
        else:
            undated.append(patient_id)
    return {
        "eligible_with_dated_clinical_resources": len(dated),
        "eligible_without_dated_clinical_resources": len(undated),
        "undated_patient_ids": undated,
        "temporal_perturbation_ready": len(dated),
        "date_span_examples": [
            {"patient_id": pid, "earliest": lo, "latest": hi} for pid, lo, hi in spans[:5]
        ],
    }


def render(
    by_patient: dict[str, PatientEvidence],
    sources: list[str],
    per_source: dict[str, list[str]],
    per_patient: dict[str, list[str]],
    per_source_anchored: dict[str, list[str]],
) -> None:
    """Print the per-guideline distribution and the headline counts."""
    total = len(by_patient)
    patient_level = [source for source in sources if CRITERIA[source][0] == "patient_level"]
    institution_level = [source for source in sources if CRITERIA[source][0] == "institution_level"]

    table = Table(title=f"Patient-level guideline eligibility across {total} demo patients")
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Eligible", justify="right")
    table.add_column("%", justify="right", style="dim")
    table.add_column("Anchored", justify="right", style="dim")
    for source in sorted(patient_level, key=lambda s: (-len(per_source[s]), s)):
        count = len(per_source[source])
        style = "green" if count >= 10 else "yellow" if count > 0 else "red"
        table.add_row(
            source,
            f"[{style}]{count}[/{style}]",
            f"{100 * count / total:.0f}%",
            str(len(per_source_anchored[source])),
        )
    console.print(table)

    console.print(
        f"\n[dim]{len(institution_level)} institution-level sources are structurally "
        f"patient-ineligible by construction: "
        f"{', '.join(sorted(institution_level))}[/dim]"
    )

    covered = [pid for pid, matched in per_patient.items() if matched]
    depth = Table(title="How many guidelines cover each patient")
    depth.add_column("Guidelines matched", justify="right")
    depth.add_column("Patients", justify="right")
    histogram: dict[int, int] = defaultdict(int)
    for matched in per_patient.values():
        histogram[len(matched)] += 1
    for bucket in sorted(histogram):
        depth.add_row(str(bucket), str(histogram[bucket]))
    console.print(depth)

    console.print(
        f"\n[bold]Eligible patients: {len(covered)}/{total}[/bold] "
        f"(at least one patient-level guideline applies)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit MIMIC demo patient eligibility against the guideline corpus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/mimic-iv-fhir-demo"),
        help="Directory of MIMIC demo NDJSON (default: data/mimic-iv-fhir-demo)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("guidelines/catalog.json"),
        help="Catalog of the frozen index being audited (default: guidelines/catalog.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/cohort/guideline_eligibility.json"),
        help="Where to write the audit report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing the report",
    )
    args = parser.parse_args()

    console.rule("[bold]Guideline eligibility audit[/bold]")
    console.print(f"[yellow]{MIMIC_ODBL_NOTICE}[/yellow]")

    try:
        sources = corpus_sources(args.catalog)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        log.error(f"Catalog read failed: {error}")
        return 1

    catalog = json.loads(args.catalog.read_text())
    log.info(f"Auditing index [bold]{catalog.get('index')}[/bold] ({len(sources)} sources)")

    try:
        by_patient = scan(args.input_dir)
    except (FileNotFoundError, OSError) as error:
        log.error(f"Scan failed: {error}")
        return 1

    per_source, per_patient = evaluate(by_patient, sources)
    per_source_anchored, per_patient_anchored = evaluate(by_patient, sources, anchored=True)
    render(by_patient, sources, per_source, per_patient, per_source_anchored)

    eligible = {pid for pid, matched in per_patient.items() if matched}
    eligible_anchored = {pid for pid, matched in per_patient_anchored.items() if matched}
    console.print(
        f"[bold]Diagnosis/procedure-anchored eligible: {len(eligible_anchored)}"
        f"/{len(by_patient)}[/bold] (stricter tier: monitoring evidence alone does not count)"
    )
    dates = date_quality(by_patient, eligible)
    console.print(
        f"[bold]Temporal-perturbation ready: {dates['temporal_perturbation_ready']}"
        f"/{len(eligible)}[/bold] eligible patients have dated clinical resources"
    )

    if args.dry_run:
        log.info("[yellow]Dry run -- report not written[/yellow]")
        return 0

    report = {
        "index": catalog.get("index"),
        "catalog": str(args.catalog),
        "source": str(args.input_dir),
        "patients_scanned": len(by_patient),
        "eligible_patients": len(eligible),
        "eligible_patient_ids": sorted(eligible),
        "eligible_patients_anchored": len(eligible_anchored),
        "eligible_patient_ids_anchored": sorted(eligible_anchored),
        "criteria": {
            source: {"scope": CRITERIA[source][0], "criterion": CRITERIA[source][1]}
            for source in sources
        },
        "anchored_overrides": sorted(ANCHORED_OVERRIDES),
        "per_source": {
            source: {
                "scope": CRITERIA[source][0],
                "eligible_count": len(per_source[source]),
                "patient_ids": per_source[source],
                "eligible_count_anchored": len(per_source_anchored[source]),
                "patient_ids_anchored": per_source_anchored[source],
            }
            for source in sources
        },
        "per_patient": {pid: sorted(matched) for pid, matched in sorted(per_patient.items())},
        "per_patient_anchored": {
            pid: sorted(matched) for pid, matched in sorted(per_patient_anchored.items())
        },
        "index_event_date_quality": dates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    log.info(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
