"""Candidate-acceptance gate: run the frozen regression dataset against a deployed runtime.

The gate is the CI/CD step a delivery pipeline places between "candidate deployed" and
"traffic routed". It

1. loads an immutable regression dataset version (AgentCore Dataset Management, or the
   checked-in JSON with its SHA-256 recorded),
2. invokes the candidate AgentCore runtime once per scenario through the AgentCore
   ``OnDemandEvaluationDatasetRunner`` with ``return_evidence`` set, so the runtime
   returns its trace, tool ledger, and verifier reports next to the response,
3. lets the runner collect the runtime's CloudWatch spans and apply the configured
   AgentCore evaluators (``Builtin.GoalSuccessRate`` over the dataset assertions by
   default, as the worked example; trend signals, recorded, not gating),
4. applies the frozen deterministic evaluator set from ``evals.layers`` (Layer 1
   operational gates and every Layer 2 evidence-contract check) to the returned
   evidence, together with the frozen thresholds,
5. writes a machine-readable result artifact and exits nonzero when a threshold
   regresses or the runtime's effective configuration drifts from the dataset's.

No score here is evidence of clinical safety, correctness, or effectiveness. The gate
establishes that a candidate still completes the fixed scenarios inside budget and
keeps every nudge honest about its retrieved evidence.

Exit codes: 0 gate passed, 1 gate failed, 2 the gate could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData

from evals.layers import EvidenceContract, LatencyBudget, OperationalChecks, TokenBudget
from evals.runner import Completion, content_hash, implementation_hash
from medical_nudging.evidence_contract.artifact_paths import ensure_output_dir_outside_repo
from medical_nudging.evidence_contract.rules import rule_content_hashes
from medical_nudging.evidence_stream import (
    EVENT_STREAM_MEDIA_TYPE,
    EvidenceStreamAssembler,
    EvidenceStreamError,
)

log = logging.getLogger("evals.agentcore_gate")

DEFAULT_EVALUATORS = ("Builtin.GoalSuccessRate",)
DEFAULT_QUALIFIER = "DEFAULT"
EVALUATOR_SET_ID = "layers-v1"
GATE_SCHEMA_VERSION = "agentcore-gate-result-v1"

# Threshold keys and what they gate. Keys absent from the thresholds file are not applied.
DETERMINISTIC_THRESHOLDS = (
    "latency_ms",
    "total_tokens",
    "max_contract_violations",
    "min_completion_rate",
)


class GateError(RuntimeError):
    """The gate could not run to a verdict (configuration, transport, or dataset error)."""


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def scenario_plan(scenarios: Iterable[Any], config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the predefined scenarios against the arm config and plan one request each.

    Mirrors ``evals.runner.select_regression_dataset``: the dataset must carry the
    same ``model``/``agent``/``generator_config`` as the arm config, one dataset
    version, one turn per scenario, and distinct patients.
    """
    plan: list[dict[str, Any]] = []
    versions: set[str] = set()
    for scenario in scenarios:
        metadata = _field(scenario, "metadata") or {}
        for key in ("model", "agent", "generator_config"):
            if metadata.get(key) != config.get(key):
                raise GateError(
                    f"Scenario {_field(scenario, 'scenario_id')!r} differs from the arm config at {key}."
                )
        versions.add(metadata.get("dataset_version", ""))
        turns = _field(scenario, "turns") or []
        if len(turns) != 1:
            raise GateError("This gate supports one request per patient scenario.")
        turn_input = _field(turns[0], "input")
        if not isinstance(turn_input, Mapping) or "visit_context" not in turn_input:
            raise GateError("Each turn input must be an object with a visit_context.")
        visit = dict(turn_input["visit_context"])
        if not visit.get("patient_id"):
            raise GateError("Each scenario visit_context needs a patient_id.")
        plan.append(
            {
                "scenario_id": _field(scenario, "scenario_id"),
                "patient_id": visit["patient_id"],
                "visit_context": visit,
                "assertions": list(_field(scenario, "assertions") or []),
                "metadata": dict(metadata),
            }
        )
    if not plan:
        raise GateError("Regression dataset has no predefined scenarios.")
    if len(versions) != 1 or "" in versions:
        raise GateError("Regression scenarios need exactly one dataset_version.")
    if len({p["patient_id"] for p in plan}) != len(plan):
        raise GateError("Regression scenarios need distinct patients.")
    return plan


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _canonical(value: Any) -> Any:
    """Drop ``None`` fields recursively so SDK model dumps and raw JSON hash alike."""
    if isinstance(value, Mapping):
        return {k: _canonical(v) for k, v in value.items() if v is not None and k != "schema_type"}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def dataset_fingerprint(scenarios: Iterable[Any]) -> str:
    """Content hash of the scenarios, independent of load path and order.

    The service and the file provider both yield SDK scenario models; the checked-in
    JSON is raw dicts. Optional fields the models default to ``None`` are dropped and
    scenarios are sorted by id, so the same eight scenarios hash the same everywhere.
    """
    payload = []
    for scenario in scenarios:
        raw = scenario.model_dump(mode="json") if hasattr(scenario, "model_dump") else scenario
        payload.append(_canonical(raw))
    payload.sort(key=lambda item: str(item.get("scenario_id", "")))
    return content_hash(payload)


def load_dataset(
    *,
    dataset_file: Path | None,
    dataset_id: str | None,
    dataset_version: str | None,
    region: str,
) -> tuple[Any, dict[str, Any]]:
    """Load the dataset from the service (pinned version) or the checked-in file."""
    from bedrock_agentcore.evaluation import FileDatasetProvider

    if dataset_id:
        if not dataset_version:
            raise GateError(
                "--dataset-version is required with --dataset-id: pin an immutable version."
            )
        from bedrock_agentcore.evaluation import DatasetClient, DatasetManagementServiceProvider

        client = DatasetClient(region_name=region)
        dataset = DatasetManagementServiceProvider(
            dataset_id=dataset_id, version_id=str(dataset_version), client=client
        ).get_dataset()
        source = {
            "kind": "agentcore_dataset",
            "dataset_id": dataset_id,
            "dataset_version": str(dataset_version),
        }
    elif dataset_file:
        dataset = FileDatasetProvider(str(dataset_file)).get_dataset()
        source = {
            "kind": "file",
            "path": str(dataset_file),
            "sha256": hashlib.sha256(dataset_file.read_bytes()).hexdigest(),
        }
    else:
        raise GateError("Provide --dataset-id/--dataset-version or --dataset-file.")
    source["scenario_count"] = len(dataset.scenarios)
    source["content_sha256"] = dataset_fingerprint(dataset.scenarios)
    return dataset, source


# ---------------------------------------------------------------------------
# Runtime invocation
# ---------------------------------------------------------------------------


def build_payload(visit_context: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """The runtime request for one scenario: frozen generator settings, evidence on."""
    generator = dict(config["generator_config"])
    agent = config.get("agent") or {}
    return {
        "visit_context": dict(visit_context),
        "config": {
            "max_nudges": agent.get("max_nudges", 5),
            "model_version": generator.get("model_id")
            or (config.get("model") or {}).get("model_id"),
            "tool_limits": agent.get("tool_limits") or None,
            "return_evidence": True,
            "generator_config": generator,
        },
    }


class RuntimeInvoker:
    """Invoke the candidate runtime for one scenario turn and keep the evidence it returns."""

    def __init__(
        self,
        *,
        runtime_arn: str,
        region: str,
        config: Mapping[str, Any],
        qualifier: str = DEFAULT_QUALIFIER,
        profile: str | None = None,
        read_timeout: int = 960,
        baggage: str | None = None,
        stream_deadline: int = 1800,
    ) -> None:
        # The runtime streams evidence responses as server-sent events (keepalive
        # every 15 s, then the record in chunks), so the read timeout only has to
        # outlast one keepalive gap; it stays at 960 s so a runtime that predates
        # streaming and answers with one synchronous body still works up to the
        # AgentCore 15-minute synchronous cap. ``stream_deadline`` bounds a streamed
        # invocation end to end (AgentCore allows 60 minutes of streaming).
        import boto3
        from botocore.config import Config

        session = boto3.Session(profile_name=profile, region_name=region)
        self._client = session.client(
            "bedrock-agentcore",
            config=Config(read_timeout=read_timeout, retries={"max_attempts": 0}),
        )
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier
        self.config = config
        # W3C baggage naming the configuration bundle version the candidate must apply.
        self.baggage = baggage
        self.stream_deadline = stream_deadline
        self.records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def __call__(self, invoker_input: Any) -> Any:
        from bedrock_agentcore.evaluation import AgentInvokerOutput

        payload = invoker_input.payload
        if not isinstance(payload, Mapping) or "visit_context" not in payload:
            raise GateError("Turn input must be an object with a visit_context.")
        body = build_payload(payload["visit_context"], self.config)
        started = datetime.now(timezone.utc)
        request: dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "qualifier": self.qualifier,
            "runtimeSessionId": invoker_input.session_id,
            "contentType": "application/json",
            "accept": EVENT_STREAM_MEDIA_TYPE,
            "payload": json.dumps(body).encode(),
        }
        if self.baggage:
            request["baggage"] = self.baggage
        response = self._client.invoke_agent_runtime(**request)
        parsed = self._read_response(response)
        if not isinstance(parsed, Mapping) or "evidence" not in parsed:
            raise GateError(
                "Runtime response has no evidence block; deploy a runtime that supports "
                "config.return_evidence."
            )
        record = dict(parsed["evidence"])
        record["response"] = parsed.get("output")
        record["runtime"] = parsed.get("runtime") or {}
        record["invocation"] = {
            "session_id": invoker_input.session_id,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "runtime_arn": self.runtime_arn,
            "qualifier": self.qualifier,
            "baggage": self.baggage,
        }
        with self._lock:
            self.records[invoker_input.session_id] = record
        return AgentInvokerOutput(agent_output=parsed.get("output"))

    def _read_response(self, response: Mapping[str, Any]) -> Any:
        """Decode a streamed (SSE) or, for older runtimes, a single JSON body."""
        content_type = str(response.get("contentType") or "")
        body = response["response"]
        if not content_type.startswith(EVENT_STREAM_MEDIA_TYPE):
            raw = body.read()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GateError(f"Runtime returned non-JSON payload: {raw[:200]!r}") from exc
        assembler = EvidenceStreamAssembler()
        deadline = time.monotonic() + self.stream_deadline
        try:
            for raw_line in body.iter_lines():
                if time.monotonic() > deadline:
                    raise GateError(
                        f"Evidence stream exceeded {self.stream_deadline}s "
                        f"({assembler.keepalives} keepalive(s) received)."
                    )
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                assembler.feed_line(line)
                if assembler.done:
                    break
        except EvidenceStreamError as exc:
            raise GateError(str(exc)) from exc
        if not assembler.done:
            raise GateError(
                f"Evidence stream ended without an end event "
                f"({assembler.keepalives} keepalive(s) received)."
            )
        log.info("Evidence stream complete after %d keepalive(s)", assembler.keepalives)
        return assembler.result


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------


def deterministic_evaluators(
    config: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> list[Evaluator]:
    """The frozen evaluator set: Layer 1 gates, every Layer 2 check, and the budget gates."""
    evaluators: list[Evaluator] = [
        Completion(),
        OperationalChecks(dict(config)),
        EvidenceContract(),
    ]
    if thresholds.get("latency_ms") is not None:
        evaluators.append(LatencyBudget(thresholds["latency_ms"]))
    if thresholds.get("total_tokens") is not None:
        evaluators.append(TokenBudget(thresholds["total_tokens"]))
    return evaluators


def score_record(
    record: Mapping[str, Any], evaluators: Iterable[Evaluator]
) -> list[dict[str, Any]]:
    """Run every deterministic evaluator over one returned evidence record."""
    data = EvaluationData(
        input={"patient_id": record.get("patient_id")},
        actual_output=dict(record),
        metadata={"source": "agentcore_gate"},
    )
    checks: list[dict[str, Any]] = []
    for evaluator in evaluators:
        for output in evaluator.evaluate(data):
            checks.append(
                {
                    "evaluator": type(evaluator).__name__,
                    "label": output.label,
                    "test_pass": bool(output.test_pass),
                    "score": output.score,
                    "reason": output.reason,
                }
            )
    return checks


def contract_violations(checks: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for c in checks if c["evaluator"] == "EvidenceContract" and not c["test_pass"])


def configuration_drift(record: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """Compare the runtime's effective settings with the dataset's frozen settings.

    Only the keys the dataset pins are compared, so a runtime may report more
    settings than the dataset froze without failing the gate.
    """
    actual = record.get("generation_settings") or {}
    drift: list[str] = []
    for section in ("model", "agent", "generator_config"):
        pinned = expected.get(section) or {}
        reported = actual.get(section) or {}
        if not isinstance(pinned, Mapping):
            continue
        for key, value in pinned.items():
            if key == "fhir_max_pages":
                # Retrieval setting: the runtime reports it under runtime.fhir_max_pages.
                reported_value = (record.get("runtime") or {}).get("fhir_max_pages")
            else:
                reported_value = reported.get(key) if isinstance(reported, Mapping) else None
            if reported_value != value:
                drift.append(f"{section}.{key}: dataset={value!r} runtime={reported_value!r}")
    corpus = (expected.get("generator_config") or {}).get("corpus_version")
    reported_index = (record.get("runtime") or {}).get("opensearch_index")
    if corpus and reported_index is not None and reported_index != corpus:
        drift.append(f"runtime.opensearch_index: dataset={corpus!r} runtime={reported_index!r}")
    return drift


def prompt_drift(record: Mapping[str, Any], expected_bundle: Mapping[str, Any] | None) -> list[str]:
    """Compare the prompt the runtime reports with the bundle version the gate pinned.

    A gate run names at most one bundle version. The runtime must report that it
    applied exactly that version (and that prompt hash); a run without a pinned bundle
    must report the repository prompt. Either mismatch means the verdict would be
    attributed to the wrong prompt, so it fails the gate like any other drift.
    """
    reported = record.get("prompt")
    if not isinstance(reported, Mapping):
        if expected_bundle:
            return ["prompt: runtime reported no prompt provenance; a bundle version was pinned"]
        return []
    bundle = reported.get("config_bundle") or {}
    source = reported.get("prompt_source")
    if not expected_bundle:
        if source == "config_bundle":
            return [
                "prompt: runtime applied configuration bundle "
                f"{bundle.get('bundle_id')}@{bundle.get('bundle_version')} but none was pinned"
            ]
        return []
    drift: list[str] = []
    if source != "config_bundle":
        drift.append(f"prompt: source={source!r}, expected the pinned configuration bundle")
    for key in ("bundle_id", "bundle_version"):
        if bundle.get(key) != expected_bundle.get(key):
            drift.append(
                f"prompt.config_bundle.{key}: gate={expected_bundle.get(key)!r} "
                f"runtime={bundle.get(key)!r}"
            )
    expected_hash = expected_bundle.get("system_prompt_sha256")
    if expected_hash and reported.get("base_prompt_sha256") != expected_hash:
        drift.append(
            f"prompt.base_prompt_sha256: gate={expected_hash!r} "
            f"runtime={reported.get('base_prompt_sha256')!r}"
        )
    return drift


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize_agentcore_results(scenario_result: Any) -> list[dict[str, Any]]:
    """Flatten the runner's raw Evaluate API responses to value/label/explanation."""
    summary: list[dict[str, Any]] = []
    for evaluator in _field(scenario_result, "evaluator_results") or []:
        results = _field(evaluator, "results") or []
        summary.append(
            {
                "evaluator_id": _field(evaluator, "evaluator_id"),
                "results": [
                    {
                        "value": r.get("value"),
                        "label": r.get("label"),
                        "explanation": r.get("explanation"),
                        "error_code": r.get("errorCode"),
                        "error_message": r.get("errorMessage"),
                        "ignored_reference_input_fields": r.get("ignoredReferenceInputFields"),
                    }
                    for r in results
                    if isinstance(r, Mapping)
                ],
            }
        )
    return summary


def evaluate_gate(
    *,
    plan: list[dict[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    agentcore_results: Mapping[str, Any],
    config: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    expected_bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the frozen evaluator set and thresholds; return the per-scenario table and verdict.

    ``records`` and ``agentcore_results`` are keyed by scenario id. ``expected_bundle``
    is the configuration bundle version the run pinned (``describe_bundle``), if any.
    """
    evaluators = deterministic_evaluators(config, thresholds)
    scenarios: list[dict[str, Any]] = []
    failures: list[str] = []
    completed = 0
    total_violations = 0
    scores_by_evaluator: dict[str, list[float]] = {}

    for item in plan:
        scenario_id = item["scenario_id"]
        record = records.get(scenario_id)
        agentcore = agentcore_results.get(scenario_id)
        entry: dict[str, Any] = {
            "scenario_id": scenario_id,
            "patient_id": item["patient_id"],
            "session_id": (record or {}).get("invocation", {}).get("session_id")
            or _field(agentcore, "session_id"),
        }
        if record is None:
            entry["status"] = "not_invoked"
            entry["error"] = _field(agentcore, "error") if agentcore is not None else "no record"
            failures.append(f"{scenario_id}: no evidence record ({entry['error']})")
            scenarios.append(entry)
            continue
        checks = score_record(record, evaluators)
        expected = item["metadata"]
        drift = configuration_drift(record, expected) + prompt_drift(record, expected_bundle)
        violations = contract_violations(checks)
        total_violations += violations
        completion = any(c["evaluator"] == "Completion" and c["test_pass"] for c in checks)
        completed += int(completion)
        failed_checks = [c for c in checks if not c["test_pass"]]
        metrics = record.get("run_metrics") or {}
        entry.update(
            {
                "status": record.get("response_status"),
                "completion": completion,
                "latency_ms": metrics.get("latency_ms"),
                "total_tokens": metrics.get("total_tokens"),
                "nudges_returned": len((record.get("response") or {}).get("nudges") or []),
                "nudges_excluded": record.get("excluded_nudge_count"),
                "contract_violations": violations,
                "configuration_drift": drift,
                "failed_checks": failed_checks,
                "check_count": len(checks),
                "agentcore_evaluators": summarize_agentcore_results(agentcore) if agentcore else [],
                "agentcore_status": _field(agentcore, "status") if agentcore is not None else None,
                "agentcore_error": _field(agentcore, "error") if agentcore is not None else None,
            }
        )
        for evaluator in entry["agentcore_evaluators"]:
            values = [
                float(r["value"])
                for r in evaluator["results"]
                if isinstance(r.get("value"), (int, float))
            ]
            if values:
                scores_by_evaluator.setdefault(evaluator["evaluator_id"], []).extend(values)
        for check in failed_checks:
            failures.append(
                f"{scenario_id}: {check['evaluator']}:{check['label']} — {check['reason']}"
            )
        for line in drift:
            failures.append(f"{scenario_id}: configuration drift — {line}")
        scenarios.append(entry)

    completion_rate = completed / len(plan) if plan else 0.0
    threshold_results = {
        "completion_rate": {
            "value": completion_rate,
            "minimum": thresholds.get("min_completion_rate"),
            "passed": thresholds.get("min_completion_rate") is None
            or completion_rate >= float(thresholds["min_completion_rate"]),
        },
        "contract_violations": {
            "value": total_violations,
            "maximum": thresholds.get("max_contract_violations"),
            "passed": thresholds.get("max_contract_violations") is None
            or total_violations <= int(thresholds["max_contract_violations"]),
        },
    }
    for name, result in threshold_results.items():
        if not result["passed"]:
            failures.append(f"threshold {name}: {result}")

    agentcore_summary = {
        evaluator_id: {"mean": _mean(values), "count": len(values)}
        for evaluator_id, values in scores_by_evaluator.items()
    }
    minimums = thresholds.get("min_agentcore_scores") or {}
    for evaluator_id, minimum in minimums.items():
        mean = (agentcore_summary.get(evaluator_id) or {}).get("mean")
        passed = mean is not None and mean >= float(minimum)
        threshold_results[f"agentcore:{evaluator_id}"] = {
            "value": mean,
            "minimum": minimum,
            "passed": passed,
        }
        if not passed:
            failures.append(f"threshold agentcore:{evaluator_id}: mean={mean} minimum={minimum}")

    return {
        "scenarios": scenarios,
        "thresholds": threshold_results,
        "agentcore_evaluator_summary": agentcore_summary,
        "gate": {"passed": not failures, "failures": failures},
    }


# ---------------------------------------------------------------------------
# Result artifact
# ---------------------------------------------------------------------------


def result_metadata(
    *,
    config: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    thresholds_path: Path | None,
    dataset_source: Mapping[str, Any],
    dataset_version: str,
    runtime: Mapping[str, Any],
    evaluator_ids: list[str],
    evaluation_delay_seconds: int,
    mode: str,
) -> dict[str, Any]:
    """Everything a reader needs to reproduce or dispute the verdict, in one place."""
    from importlib.metadata import PackageNotFoundError, version

    def _version(package: str) -> str | None:
        try:
            return version(package)
        except PackageNotFoundError:
            return None

    return {
        "schema": GATE_SCHEMA_VERSION,
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"dataset_version": dataset_version, **dataset_source},
        "runtime": dict(runtime),
        "generator_config": config.get("generator_config"),
        "model": config.get("model"),
        "agent": config.get("agent"),
        "thresholds": {
            "values": dict(thresholds),
            "path": str(thresholds_path) if thresholds_path else None,
            "sha256": (
                hashlib.sha256(thresholds_path.read_bytes()).hexdigest()
                if thresholds_path
                else None
            ),
        },
        "evaluators": {
            "deterministic_set": EVALUATOR_SET_ID,
            "rule_hashes": rule_content_hashes(),
            "implementation_sha256": implementation_hash(),
            "agentcore_evaluator_ids": list(evaluator_ids),
            "agentcore_sdk_version": _version("bedrock-agentcore"),
            "strands_evals_version": _version("strands-agents-evals"),
            "evaluation_delay_seconds": evaluation_delay_seconds,
        },
        "sampling": {"kind": "fixed_dataset", "scenario_fraction": 1.0},
        "claim_boundary": (
            "Deterministic checks establish completion, budget, and evidence honesty. "
            "AgentCore evaluator scores are trend signals. None is evidence of clinical "
            "safety, correctness, or effectiveness."
        ),
    }


def write_artifacts(
    output_dir: Path,
    *,
    result: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> tuple[Path, Path]:
    """Write result.json (no patient evidence) and records.json (raw evidence)."""
    output_dir = ensure_output_dir_outside_repo(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"
    records_path = output_dir / "records.json"
    result_path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    records_path.write_text(json.dumps(records, indent=2, default=str) + "\n")
    return result_path, records_path


def describe_runtime(runtime_arn: str, region: str, profile: str | None) -> dict[str, Any]:
    """Record the candidate's runtime version and image; best effort."""
    info: dict[str, Any] = {"runtime_arn": runtime_arn, "qualifier": DEFAULT_QUALIFIER}
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    info["runtime_id"] = runtime_id
    try:
        import boto3

        client = boto3.Session(profile_name=profile, region_name=region).client(
            "bedrock-agentcore-control"
        )
        details = client.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:  # noqa: BLE001 - provenance is best effort, the gate is not
        log.warning("Could not describe runtime %s: %s", runtime_id, exc)
        info["describe_error"] = str(exc)
        return info
    artifact = details.get("agentRuntimeArtifact") or {}
    container = artifact.get("containerConfiguration") or {}
    info.update(
        {
            "runtime_name": details.get("agentRuntimeName"),
            "runtime_version": details.get("agentRuntimeVersion"),
            "container_uri": container.get("containerUri"),
            "status": details.get("status"),
        }
    )
    return info


def describe_bundle(
    bundle_id: str, bundle_version: str, runtime_arn: str, region: str, profile: str | None
) -> dict[str, Any]:
    """Resolve the pinned configuration bundle version for the candidate runtime.

    Returns the identity the gate records next to the runtime version (the pair
    (runtime version, bundle version) must never be ambiguous) and the baggage
    the invoker sends. Unlike ``describe_runtime`` this is not best effort: a bundle the
    gate cannot read is a gate error.
    """
    import boto3

    from medical_nudging.config_bundle import ConfigBundleError, component_configuration

    client = boto3.Session(profile_name=profile, region_name=region).client(
        "bedrock-agentcore-control"
    )
    try:
        version = client.get_configuration_bundle_version(
            bundleId=bundle_id, versionId=bundle_version
        )
        config = component_configuration(version, runtime_arn)
    except ConfigBundleError as exc:
        raise GateError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface the service error as a gate error
        raise GateError(
            f"Could not read configuration bundle {bundle_id} version {bundle_version}: {exc}"
        ) from exc
    if config.system_prompt is None:
        raise GateError(
            f"Configuration bundle {bundle_id} version {bundle_version} carries no "
            f"system_prompt for {runtime_arn}"
        )
    lineage = version.get("lineageMetadata") or {}
    return {
        **config.provenance(),
        "parent_version_ids": list(lineage.get("parentVersionIds") or []),
        "version_created_at": str(version.get("versionCreatedAt") or ""),
        "baggage": config.ref.baggage(),
    }


def runtime_log_group(runtime_arn: str, qualifier: str = DEFAULT_QUALIFIER) -> str:
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{qualifier}"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def run_live(args: argparse.Namespace, config: dict[str, Any], thresholds: dict[str, Any]) -> int:
    from bedrock_agentcore.evaluation import (
        CloudWatchAgentSpanCollector,
        EvaluationRunConfig,
        EvaluatorConfig,
        OnDemandEvaluationDatasetRunner,
    )

    dataset, source = load_dataset(
        dataset_file=args.dataset_file,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
        region=args.region,
    )
    plan = scenario_plan(dataset.scenarios, config)
    dataset_version = plan[0]["metadata"]["dataset_version"]
    log.info("Loaded %d scenarios (%s) from %s", len(plan), dataset_version, source["kind"])

    expected_bundle: dict[str, Any] | None = None
    if args.bundle_id:
        expected_bundle = describe_bundle(
            args.bundle_id, args.bundle_version, args.runtime_arn, args.region, args.profile
        )
        log.info(
            "Pinned configuration bundle %s version %s (%s)",
            expected_bundle["bundle_id"],
            expected_bundle["bundle_version"],
            expected_bundle.get("commit_message") or "no commit message",
        )

    invoker = RuntimeInvoker(
        runtime_arn=args.runtime_arn,
        region=args.region,
        config=config,
        qualifier=args.qualifier,
        profile=args.profile,
        baggage=expected_bundle["baggage"] if expected_bundle else None,
    )
    collector = CloudWatchAgentSpanCollector(
        log_group_name=args.log_group or runtime_log_group(args.runtime_arn, args.qualifier),
        region=args.region,
        max_wait_seconds=args.span_wait_seconds,
    )
    run_config = EvaluationRunConfig(
        evaluator_config=EvaluatorConfig(evaluator_ids=list(args.evaluators)),
        evaluation_delay_seconds=args.evaluation_delay_seconds,
        max_concurrent_scenarios=args.concurrency,
    )
    started = datetime.now(timezone.utc)
    runner = OnDemandEvaluationDatasetRunner(region=args.region)
    result = runner.run(
        config=run_config, dataset=dataset, agent_invoker=invoker, span_collector=collector
    )

    # Join evidence records to scenarios via the runner's session ids.
    by_session = {sr.session_id: sr for sr in result.scenario_results}
    records: dict[str, dict[str, Any]] = {}
    agentcore_results: dict[str, Any] = {}
    for scenario_result in result.scenario_results:
        agentcore_results[scenario_result.scenario_id] = scenario_result
        record = invoker.records.get(scenario_result.session_id)
        if record is not None:
            records[scenario_result.scenario_id] = record
    unmatched = set(invoker.records) - set(by_session)
    if unmatched:
        log.warning("%d evidence record(s) had no runner session: %s", len(unmatched), unmatched)

    verdict = evaluate_gate(
        plan=plan,
        records=records,
        agentcore_results=agentcore_results,
        config=config,
        thresholds=thresholds,
        expected_bundle=expected_bundle,
    )
    runtime = describe_runtime(args.runtime_arn, args.region, args.profile)
    runtime.update(
        {
            "qualifier": args.qualifier,
            "log_group": collector.log_group_name,
            "reported": _first_runtime_report(records),
            "config_bundle": (
                {k: v for k, v in expected_bundle.items() if k != "baggage"}
                if expected_bundle
                else None
            ),
            "prompt_source": "config_bundle" if expected_bundle else "repository",
        }
    )
    metadata = result_metadata(
        config=config,
        thresholds=thresholds,
        thresholds_path=args.thresholds,
        dataset_source=source,
        dataset_version=dataset_version,
        runtime=runtime,
        evaluator_ids=list(args.evaluators),
        evaluation_delay_seconds=args.evaluation_delay_seconds,
        mode="live",
    )
    metadata["started_at"] = started.isoformat()
    return _finish(args, metadata, verdict, records)


def run_replay(args: argparse.Namespace, config: dict[str, Any], thresholds: dict[str, Any]) -> int:
    """Score saved evidence records without invoking anything.

    Used for the known-bad fixture check in CI and for offline re-scoring of a
    gate run's ``records.json``. The file is either the gate's own ``records.json``
    (scenario id → record) or a list of ``{"scenario_id", "patient_id", "record"}``.
    """
    payload = json.loads(args.replay_records.read_text())
    records: dict[str, dict[str, Any]] = {}
    plan: list[dict[str, Any]] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, Mapping):
        entries = [
            {"scenario_id": sid, "patient_id": rec.get("patient_id"), "record": rec}
            for sid, rec in payload.items()
        ]
    else:
        raise GateError("Replay file must be a list of entries or a scenario-id map.")
    for entry in entries:
        record = entry["record"]
        records[entry["scenario_id"]] = record
        metadata = entry.get("metadata") or {
            "dataset_version": entry.get("dataset_version", "replay"),
            **{
                k: (record.get("generation_settings") or {}).get(k)
                for k in ("model", "agent", "generator_config")
            },
        }
        plan.append(
            {
                "scenario_id": entry["scenario_id"],
                "patient_id": entry.get("patient_id") or record.get("patient_id"),
                "visit_context": {},
                "assertions": [],
                "metadata": metadata,
            }
        )
    verdict = evaluate_gate(
        plan=plan, records=records, agentcore_results={}, config=config, thresholds=thresholds
    )
    metadata = result_metadata(
        config=config,
        thresholds=thresholds,
        thresholds_path=args.thresholds,
        dataset_source={
            "kind": "replay_records",
            "path": str(args.replay_records),
            "sha256": hashlib.sha256(args.replay_records.read_bytes()).hexdigest(),
            "scenario_count": len(plan),
        },
        dataset_version=plan[0]["metadata"].get("dataset_version", "replay") if plan else "replay",
        runtime={"runtime_arn": None, "reported": _first_runtime_report(records)},
        evaluator_ids=[],
        evaluation_delay_seconds=0,
        mode="replay",
    )
    return _finish(args, metadata, verdict, records)


def _first_runtime_report(records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    for record in records.values():
        reported = record.get("runtime")
        if isinstance(reported, Mapping):
            return dict(reported)
    return None


def _finish(
    args: argparse.Namespace,
    metadata: dict[str, Any],
    verdict: dict[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> int:
    result = {"metadata": metadata, **verdict}
    result_path, records_path = write_artifacts(args.output_dir, result=result, records=records)
    _print_summary(result, result_path, records_path)
    return 0 if verdict["gate"]["passed"] else 1


def _print_summary(result: Mapping[str, Any], result_path: Path, records_path: Path) -> None:
    print(f"\nCandidate acceptance gate — {result['metadata']['mode']} mode")
    dataset = result["metadata"]["dataset"]
    print(
        f"Dataset: {dataset.get('dataset_version')} ({dataset.get('kind')}, {dataset.get('scenario_count')} scenarios)"
    )
    runtime = result["metadata"].get("runtime") or {}
    if runtime.get("runtime_arn"):
        print(
            f"Runtime: {runtime.get('runtime_name') or runtime['runtime_arn']} v{runtime.get('runtime_version')} {runtime.get('container_uri') or ''}"
        )
        bundle = runtime.get("config_bundle")
        if bundle:
            print(
                f"Prompt:  configuration bundle {bundle.get('bundle_id')} "
                f"version {bundle.get('bundle_version')} "
                f"({bundle.get('commit_message') or 'no commit message'})"
            )
        else:
            print("Prompt:  repository prompts/orchestrator.md")
    print(
        f"{'scenario':<24}{'status':<10}{'latency_ms':>12}{'tokens':>10}{'nudges':>8}{'viol.':>7}  failed checks"
    )
    for row in result["scenarios"]:
        print(
            f"{str(row['scenario_id']):<24}{str(row.get('status')):<10}"
            f"{str(row.get('latency_ms')):>12}{str(row.get('total_tokens')):>10}"
            f"{str(row.get('nudges_returned')):>8}{str(row.get('contract_violations')):>7}  "
            f"{len(row.get('failed_checks') or [])}{' +drift' if row.get('configuration_drift') else ''}"
        )
    for name, threshold in result["thresholds"].items():
        print(f"threshold {name}: {'ok' if threshold['passed'] else 'FAILED'} {threshold}")
    for evaluator_id, summary in (result.get("agentcore_evaluator_summary") or {}).items():
        print(
            f"agentcore {evaluator_id}: mean={summary['mean']} n={summary['count']} (trend signal)"
        )
    gate = result["gate"]
    print(f"\nGATE {'PASSED' if gate['passed'] else 'FAILED'}")
    for failure in gate["failures"]:
        print(f"  - {failure}")
    print(f"Result artifact: {result_path}")
    print(f"Evidence records (raw patient evidence, keep private): {records_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="Arm config (model/agent/generator_config)"
    )
    parser.add_argument("--thresholds", type=Path, required=True, help="Frozen thresholds JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="Outside every worktree")
    parser.add_argument("--runtime-arn", default=os.environ.get("AGENT_RUNTIME_ARN"))
    parser.add_argument("--qualifier", default=DEFAULT_QUALIFIER)
    parser.add_argument("--dataset-id", default=os.environ.get("GATE_DATASET_ID") or None)
    parser.add_argument("--dataset-version", default=os.environ.get("GATE_DATASET_VERSION") or None)
    parser.add_argument("--dataset-file", type=Path, default=None)
    parser.add_argument(
        "--bundle-id",
        default=os.environ.get("GATE_BUNDLE_ID") or None,
        help="Configuration bundle whose pinned version the candidate must apply",
    )
    parser.add_argument(
        "--bundle-version",
        default=os.environ.get("GATE_BUNDLE_VERSION") or None,
        help="Immutable bundle version id (required with --bundle-id)",
    )
    parser.add_argument(
        "--replay-records", type=Path, default=None, help="Score saved records; no invocations"
    )
    parser.add_argument(
        "--evaluators",
        nargs="*",
        default=list(DEFAULT_EVALUATORS),
        help="AgentCore evaluator ids applied to the collected spans (trend signals)",
    )
    parser.add_argument("--evaluation-delay-seconds", type=int, default=180)
    parser.add_argument("--span-wait-seconds", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--log-group", default=None, help="Runtime log group (default derived from ARN)"
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        # Verbose covers the gate and the AgentCore SDK only; botocore debug output
        # would print signed request headers into CI logs.
        logging.getLogger("evals").setLevel(logging.DEBUG)
        logging.getLogger("bedrock_agentcore").setLevel(logging.DEBUG)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    os.environ.setdefault("AWS_REGION", args.region)
    try:
        config = json.loads(args.config.read_text())
        thresholds = json.loads(args.thresholds.read_text())
        if args.concurrency < 1:
            raise GateError("--concurrency must be positive")
        if bool(args.bundle_id) != bool(args.bundle_version):
            raise GateError("--bundle-id and --bundle-version must be given together")
        if args.replay_records:
            return run_replay(args, config, thresholds)
        if not args.runtime_arn:
            raise GateError("--runtime-arn (or AGENT_RUNTIME_ARN) is required for a live gate run.")
        return run_live(args, config, thresholds)
    except GateError as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - report, do not hide, an unexpected failure
        log.exception("Gate could not run: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
