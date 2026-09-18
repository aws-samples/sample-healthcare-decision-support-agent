"""Generate one patient run, retaining evidence for all four evaluation layers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from medical_nudging.config import (
    get_fhir_config,
    get_opensearch_config,
    load_config,
)
from medical_nudging.steered_generation import serialize_ledger, steered_generation_record

__all__ = ["configure_generation", "generate_patient", "serialize_ledger"]


def configure_generation(config: dict[str, Any]) -> None:
    """Apply one arm's explicit settings before its patient workers start."""
    app = load_config()
    for key in ("model", "agent"):
        if key in config:
            app[key] = dict(config[key])
    corpus = config["generator_config"]["corpus_version"]
    app["opensearch_index"] = corpus
    if config.get("fhir_api"):
        app["fhir_api"] = dict(config["fhir_api"])
    if not get_opensearch_config().get("opensearch_endpoint"):
        raise ValueError("Configure the OpenSearch endpoint before generating.")
    if get_opensearch_config()["opensearch_index"] != corpus:
        raise ValueError("The configured OpenSearch index differs from the frozen corpus.")
    if any(not patient.get("patient_file") for patient in config["patients"]):
        fhir = get_fhir_config()
        if not fhir.get("enabled") or not fhir.get("datastore_endpoint"):
            raise ValueError("Configure an enabled FHIR endpoint before generating.")


def generate_patient(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable run, including failed/empty runs and excluded drafts.

    The record is built by :func:`medical_nudging.steered_generation.steered_generation_record`,
    the same code the deployed runtime runs for the candidate-acceptance gate.
    """
    patient_file = entry.get("patient_file")
    return steered_generation_record(
        patient_id=entry["patient_id"],
        visit_context=entry.get("visit_context") or {},
        data_source="fhir_api" if not patient_file else entry.get("format", "fhir"),
        patient_data=Path(patient_file).read_text() if patient_file else None,
        generator_config=config["generator_config"],
        generation_settings=config,
        specialty=config.get("specialty", "general"),
    )
