"""Run one contract-steered generation and retain evidence for every evaluation layer.

The local evaluation runner (``evals.generation``) and the AgentCore runtime's opt-in
evidence path (``agent.py``) both call :func:`steered_generation_record`, so a deployed
candidate and a local arm produce the same auditable record: the response, the
execution trace, the tool ledger, query coverage, and one deterministic verifier
report per drafted nudge.

The record carries raw patient evidence. Callers persist it only outside the
repository working tree (see ``evidence_contract.artifact_paths``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime, timezone
from typing import Any

from medical_nudging.config_bundle import prompt_provenance
from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator
from medical_nudging.config import get_model_config
from medical_nudging.evidence_contract import (
    ContractEnforcingSteeringHandler,
    DeterministicVerifier,
    GeneratorConfiguration,
    ToolLedger,
    build_draft_claim_ledger,
)
from medical_nudging.tools.raw_resource_channel import capture_raw_resources


def build_generator_configuration(options: Mapping[str, Any]) -> GeneratorConfiguration:
    """Build the frozen steering configuration, ignoring keys it does not define.

    ``fhir_max_pages`` is such a key: it is a retrieval setting recorded alongside
    the generator configuration, not part of the steering handler.
    """
    allowed = {item.name for item in fields(GeneratorConfiguration)}
    return GeneratorConfiguration(**{key: val for key, val in options.items() if key in allowed})


def serialize_ledger(ledger: ToolLedger) -> dict[str, Any]:
    return {
        "patient_evidence": [span.model_dump(mode="json") for span in ledger.patient_evidence],
        "guideline_evidence": [span.model_dump(mode="json") for span in ledger.guideline_evidence],
        **ledger.fidelity_record(),
    }


def _visit_context(
    visit_context: Mapping[str, Any] | None,
    *,
    patient_id: str,
    data_source: str,
    generator: GeneratorConfiguration,
) -> dict[str, Any]:
    visit = dict(visit_context or {})
    visit.update(patient_id=patient_id, data_source=data_source, search_mode="knowledge_base")
    if generator.custom_instructions:
        visit["custom_instructions"] = generator.custom_instructions
    return visit


def _nudge_artifacts(
    drafts: list[dict[str, Any]],
    handler: ContractEnforcingSteeringHandler,
    verifier: DeterministicVerifier,
    ledger: ToolLedger,
) -> list[dict[str, Any]]:
    """Pair each draft with its claim ledger and verifier report.

    Reports come from the steering pass when it saw exactly these drafts; otherwise
    they are re-derived and labelled as such.
    """
    aligned = len(handler.last_reports) == len(drafts) == len(handler.last_draft_ledgers)
    artifacts: list[dict[str, Any]] = []
    for index, nudge in enumerate(drafts):
        if aligned:
            claims, chain = handler.last_draft_ledgers[index]
            report = handler.last_reports[index]
        else:
            claims, chain = build_draft_claim_ledger(nudge, ledger)
            report = verifier.verify(claims=claims, ledger=ledger, action_evidence_chain=chain)
        artifacts.append(
            {
                "nudge_idx": index,
                "nudge": nudge,
                "draft_claim_ledger": [claim.model_dump(mode="json") for claim in claims],
                "action_evidence_chain": chain.model_dump(mode="json"),
                "steering_outcome": handler.nudge_outcome_record(index if aligned else -1),
                "deterministic_verifier_report": report.as_dict(),
                "deterministic_verifier_report_source": "steering" if aligned else "re_derived",
            }
        )
    return artifacts


def _run_metrics(trace: Any) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for call in trace.tool_calls:
        counts[call.tool_name] = counts.get(call.tool_name, 0) + 1
    return {
        **(trace.token_usage or {}),
        "latency_ms": trace.total_duration_ms,
        "tool_duration_ms": trace.tool_duration_ms,
        "tool_call_counts": counts,
    }


def _generation_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **{key: settings.get(key) for key in ("model", "agent", "generator_config", "specialty")},
        "transport": {
            "read_timeout": get_model_config()["read_timeout"],
            "retries": {"max_attempts": 2},
        },
    }


def steered_generation_record(
    *,
    patient_id: str,
    visit_context: Mapping[str, Any] | None,
    data_source: str,
    patient_data: str | None,
    generator_config: Mapping[str, Any],
    generation_settings: Mapping[str, Any],
    specialty: str = "general",
    request_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable run, including failed/empty runs and excluded drafts.

    Args:
        patient_id: FHIR patient id (or a stable sample id for document input).
        visit_context: Visit fields; ``patient_id``, ``data_source``, and
            ``search_mode`` are set here so every caller sends the same request.
        data_source: ``"fhir_api"`` or the document format (``"fhir"``/``"ccda"``).
        patient_data: Document text for file-based input, ``None`` for FHIR API mode.
        generator_config: Frozen steering configuration (``config_version``,
            ``retry_cap``, ``corpus_version``, ``model_id``, ``entailment_model_id``,
            ``require_citation``, ``custom_instructions``).
        generation_settings: ``model``/``agent``/``generator_config``/``specialty``
            settings recorded verbatim on the artifact for provenance and drift checks.
        specialty: Orchestrator specialty.
        request_config: Optional request-level orchestrator config (``max_nudges``,
            ``tool_limits``, ``model_version``) used by the deployed runtime.
    """
    generator = build_generator_configuration(generator_config)
    verifier = DeterministicVerifier()
    with capture_raw_resources() as channel:
        handler = ContractEnforcingSteeringHandler(
            config=generator, verifier=verifier, raw_channel=channel
        )
        orchestrator = MedicalNudgingOrchestrator(
            specialty=specialty,
            model_id=generator.model_id,
            context_condition="full",
            plugins=[handler],
        )
        response, trace = orchestrator.generate_nudges_with_trace(
            patient_data=patient_data,
            visit_context=_visit_context(
                visit_context, patient_id=patient_id, data_source=data_source, generator=generator
            ),
            config=dict(request_config) if request_config else None,
        )
        ledger = (
            handler.last_ledger if handler.last_ledger is not None else handler.build_tool_ledger()
        )
        drafts = [nudge.model_dump(mode="json") for nudge in response.nudges]
        artifacts = _nudge_artifacts(drafts, handler, verifier, ledger)
        record: dict[str, Any] = {
            "patient_id": patient_id,
            "generation_settings": _generation_settings(generation_settings),
            "prompt": prompt_provenance(),
            "response_status": response.status,
            "response": response.model_dump(mode="json"),
            "trace": trace.to_dict(),
            "run_metrics": _run_metrics(trace),
            "tool_ledger": serialize_ledger(ledger),
            "query_coverage": ledger.query_coverage().model_dump(mode="json"),
            "patient_steering_outcome": handler.outcome_record(),
            "nudges": artifacts,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        # Retain failed candidates in artifacts; readers only see eligible output.
        excluded = {
            item["nudge_idx"] for item in artifacts if item["steering_outcome"].get("flags")
        }
        record["response"]["nudges"] = [
            nudge for index, nudge in enumerate(drafts) if index not in excluded
        ]
        record["excluded_nudge_count"] = len(excluded)
        return record
