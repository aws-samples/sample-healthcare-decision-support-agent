"""Strands evaluators for operational reliability and evidence-contract checks."""

from __future__ import annotations

import json
import math
import copy
from typing import Any

from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from medical_nudging.evidence_contract import (
    DeterministicVerifier,
    GuidelineEvidenceSpan,
    PatientEvidenceSpan,
    ToolLedger,
    build_draft_claim_ledger,
)
from medical_nudging.evidence_contract.chart_state import check_redundant_order

VERDICTS = ("satisfies_contract", "violates_contract", "abstain_insufficient_evidence")
BLOG_CHECKS = (
    "citation_resolution",
    "cited_passage_identity",
    "medication_status",
    "absence_within_coverage",
    "redundant_order",
)


class OperationalChecks(Evaluator):
    """Apply the existing sanity gates to a captured generation response/trace."""

    def __init__(self, config: dict[str, Any]):
        super().__init__()
        self.config = copy.deepcopy(config)
        limits = self.config.setdefault("agent", {}).setdefault("tool_limits", {})
        for tool in ("guideline_search", "fhir_query"):
            limits.setdefault(tool, {}).setdefault("max_calls", None)
        self.config["context"] = {"data_source": "fhir_api"}

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        from evals.gates import ExecutionHealth, OutputFormatSuccess, ReviewerReady

        record = evaluation_case.actual_output or {}
        if not record.get("response") or not record.get("trace"):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    label="not_recorded",
                    reason="The saved run lacks response/trace fields; these gates cannot be reconstructed.",
                )
            ]
        data = EvaluationData(
            input=evaluation_case.input,
            actual_output=record["response"],
            metadata={"trace": record["trace"], "run_config": self.config},
        )
        checks = []
        for gate in (OutputFormatSuccess(), ExecutionHealth(), ReviewerReady()):
            for check in gate.evaluate(data):
                check.label = f"{type(gate).__name__}:{check.label}"
                checks.append(check)
        return checks


def ledger_from_record(record: dict[str, Any]) -> ToolLedger:
    raw = record.get("tool_ledger") or {}
    coverage = record.get("query_coverage") or {}
    return ToolLedger(
        patient_evidence=[
            PatientEvidenceSpan.model_validate(p) for p in raw.get("patient_evidence", [])
        ],
        guideline_evidence=[
            GuidelineEvidenceSpan.model_validate(p) for p in raw.get("guideline_evidence", [])
        ],
        queries_executed=coverage.get("queries_executed", []),
        coverage_rule_version=coverage.get("coverage_rule_version", ""),
        truncated_resource_types=coverage.get("truncated_resource_types", []),
        patient_evidence_fidelity=raw.get("patient_evidence_fidelity", "raw_fhir"),
        fidelity_reasons=raw.get("fidelity_reasons", []),
        absent_resource_types=raw.get("absent_resource_types", []),
    )


def _verdict(checks: list[dict[str, Any]]) -> str:
    if any(c["status"] == "fail" for c in checks):
        return "violates_contract"
    if not checks or any(c["status"] == "not_evaluable" for c in checks):
        return "abstain_insufficient_evidence"
    return "satisfies_contract"


def score_nudge(
    nudge: dict[str, Any], ledger: ToolLedger, *, verifier: DeterministicVerifier | None = None
) -> dict[str, Any]:
    """Rebuild claims from the output; never trust a saved reference answer key."""
    verifier = verifier or DeterministicVerifier()
    claims, chain = build_draft_claim_ledger(nudge, ledger)
    report = verifier.verify(claims=claims, ledger=ledger, action_evidence_chain=chain).as_dict()
    by_check: dict[str, list[dict[str, Any]]] = {}
    for result in report["results"]:
        by_check.setdefault(result["check"], []).append(result)
    mapping = {
        "citation_resolution": "source_identity",
        "cited_passage_identity": "passage_identity",
        "medication_status": "medication_status",
        "absence_within_coverage": "absence_within_query_coverage",
    }
    checks = []
    citation = nudge.get("guideline_citation")
    for name, raw_name in mapping.items():
        evidence = by_check.get(raw_name, [])
        applicable = bool(citation) if name.startswith(("citation", "cited")) else bool(evidence)
        checks.append(
            {
                "check": name,
                "verdict": _verdict(evidence),
                "applicable": applicable,
                "details": evidence,
            }
        )
    checks.append(check_redundant_order(nudge, ledger))
    active = [c for c in checks if c["applicable"]]
    additional_failures = [
        result
        for result in report["results"]
        if result["check"] not in mapping.values() and result["status"] == "fail"
    ]
    violation = bool(additional_failures) or any(
        c["verdict"] == "violates_contract" for c in active
    )
    abstention = (
        report["verdict"] == "abstain_insufficient_evidence"
        or not active
        or any(c["verdict"] == "abstain_insufficient_evidence" for c in active)
    )
    return {
        "checks": checks,
        "verdict": (
            "violates_contract"
            if violation
            else ("abstain_insufficient_evidence" if abstention else "satisfies_contract")
        ),
        "no_violation": not violation,
        "additional_failures": additional_failures,
        "unresolved_checks": [
            result for result in report["results"] if result["status"] == "not_evaluable"
        ],
        "full_verifier": report,
    }


class EvidenceContract(Evaluator):
    """One output per nudge/check; abstentions retain their own label."""

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        record = evaluation_case.actual_output or {}
        ledger = ledger_from_record(record)
        outputs = []
        for artifact in record.get("nudges", []):
            if artifact.get("steering_outcome", {}).get("flags"):
                continue
            score = score_nudge(artifact["nudge"], ledger)
            for failure in score["additional_failures"]:
                outputs.append(
                    EvaluationOutput(
                        score=0.0,
                        test_pass=False,
                        label=f"n{artifact['nudge_idx']}:repository:{failure['check']}:violates_contract",
                        reason=json.dumps(failure, sort_keys=True),
                    )
                )
            for check in score["checks"]:
                if not check["applicable"]:
                    continue
                verdict = check["verdict"]
                outputs.append(
                    EvaluationOutput(
                        score=1.0 if verdict == "satisfies_contract" else 0.0,
                        test_pass=verdict != "violates_contract",
                        label=f"n{artifact['nudge_idx']}:{check['check']}:{verdict}",
                        reason=json.dumps(check, sort_keys=True),
                    )
                )
        if outputs:
            return outputs
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=True,
                label="abstain_insufficient_evidence",
                reason="No applicable nudge checks; this is not an evidence pass.",
            )
        ]


def token_usage_is_measured(record: dict[str, Any]) -> bool:
    """Failed requests with zero-filled counters have no measured token usage."""
    value = (record.get("run_metrics") or {}).get("total_tokens")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        and (record.get("response_status") in {"success", "partial"} or value > 0)
    )


class BudgetGate(Evaluator):
    metric: str

    def __init__(self, limit: int | float):
        super().__init__()
        if isinstance(limit, bool) or not math.isfinite(limit) or limit <= 0:
            raise ValueError("Budget limit must be a finite positive number.")
        self.limit = limit

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        record = evaluation_case.actual_output or {}
        metrics = record.get("run_metrics") or {}
        value = metrics.get(self.metric)
        measured = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
        if self.metric == "total_tokens":
            measured = token_usage_is_measured(record)
        passed = measured and value <= self.limit
        return [
            EvaluationOutput(
                score=float(passed),
                test_pass=passed,
                label=(
                    ("within_budget" if passed else "over_budget") if measured else "missing_metric"
                ),
                reason=f"{self.metric}={value}; configured ceiling={self.limit}",
            )
        ]


class LatencyBudget(BudgetGate):
    metric = "latency_ms"


class TokenBudget(BudgetGate):
    metric = "total_tokens"
