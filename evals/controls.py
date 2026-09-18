"""Validate a cross-family holistic judge with fabricated-citation controls."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from strands_evals import Case
from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from evals.layers import ledger_from_record, score_nudge
from medical_nudging.evidence_contract.evidence_view import build_evidence_view
from medical_nudging.evidence_contract.perturb import perturb_citation_fabrication

PROMPT_DIRECTORY = Path(__file__).resolve().parents[1] / "prompts/evaluators"


def model_family(model: str) -> str:
    for family in ("anthropic", "openai", "xai", "amazon", "google", "meta", "mistral"):
        if family in model.lower():
            return family
    raise ValueError(f"Unknown model family: {model}")


def visible_payload(nudge: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    citation = nudge.get("guideline_citation")
    if isinstance(citation, dict):
        citation = {
            **citation,
            "page_number": citation.get("page_number") or citation.get("page"),
        }
    ledger = record.get("tool_ledger") or {}
    return {
        "benchmark_original": {
            "nudge_text": "\n".join(
                str(nudge.get(k) or "") for k in ("title", "description", "rationale")
            ),
            "proposed_action": " ".join(str(nudge.get(k) or "") for k in ("title", "description")),
            "citation_provenance": citation,
        },
        "patient_evidence": ledger.get("patient_evidence", []),
        "guideline_evidence": ledger.get("guideline_evidence", []),
        "query_coverage": record.get("query_coverage") or {},
    }


def build_controls(
    records: list[dict[str, Any]], cap: int, *, excluded_names: set[str] | None = None
) -> tuple[list[Case], dict[str, int]]:
    if cap < 1:
        raise ValueError("Control cap must be positive.")
    controls = []
    counts = {"candidates": 0, "excluded": 0, "l2_failed": 0, "uncited": 0, "inapplicable": 0}
    if excluded_names:
        counts["previously_used"] = 0
    for record in sorted(records, key=lambda r: r["patient_id"]):
        ledger = ledger_from_record(record)
        for artifact in sorted(record.get("nudges", []), key=lambda a: a["nudge_idx"]):
            if len(controls) >= cap:
                return controls, counts
            name = f"{record['patient_id']}__{artifact['nudge_idx']}"
            if excluded_names and name in excluded_names:
                counts["previously_used"] += 1
                continue
            counts["candidates"] += 1
            if artifact.get("steering_outcome", {}).get("flags"):
                counts["excluded"] += 1
                continue
            nudge = artifact["nudge"]
            original = score_nudge(nudge, ledger)
            if (
                not original["no_violation"]
                or original["full_verifier"]["verdict"] == "violates_contract"
            ):
                counts["l2_failed"] += 1
                continue
            citation_checks = [
                c
                for c in original["checks"]
                if c["check"] in {"citation_resolution", "cited_passage_identity"}
            ]
            if not all(
                c["applicable"] and c["verdict"] == "satisfies_contract" for c in citation_checks
            ):
                counts["uncited"] += 1
                continue
            outcome = perturb_citation_fabrication(nudge, ledger)
            if outcome.status != "constructed":
                counts["inapplicable"] += 1
                continue
            verified = score_nudge(outcome.nudge, ledger)
            detected = any(
                c["verdict"] == "violates_contract"
                for c in verified["checks"]
                if c["check"] in {"citation_resolution", "cited_passage_identity"}
            )
            if not detected:
                raise ValueError("Constructed citation control was not detected by the verifier.")
            controls.append(
                Case(
                    name=name,
                    session_id=name,
                    input=visible_payload(outcome.nudge, record),
                    expected_output={"verdict": "violates_contract"},
                    metadata={
                        "original_verdict": original["verdict"],
                        "original_full_verifier_verdict": original["full_verifier"]["verdict"],
                        "original_unresolved_checks": original["unresolved_checks"],
                        "verifier_detected": True,
                    },
                )
            )
    return controls, counts


def load_control_exclusions(
    directory: Path, records: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[set[str], str]:
    """Accept only completed controls tied to these exact saved generations."""
    try:
        previous = json.loads((directory / "manifest.json").read_text())["config"]
        summary = json.loads((directory / "control_summary.json").read_text())
        tasks = sorted((directory / "tasks").glob("*.json"))
        if not tasks or summary["controls"] != len(tasks):
            raise ValueError("Previous control set is incomplete.")
        for key in ("replay_content_sha256", "generator_config", "model", "agent"):
            if previous[key] != config[key]:
                raise ValueError(f"Previous controls differ from this replay at {key}.")
        candidates = {
            f"{record['patient_id']}__{nudge['nudge_idx']}"
            for record in records for nudge in record.get("nudges", [])
        }
        names = set()
        digest = hashlib.sha256()
        for path in tasks:
            raw = path.read_bytes()
            task = json.loads(raw)
            name = task["name"]
            if (
                name not in candidates or name in names
                or task.get("expected_output") != {"verdict": "violates_contract"}
                or not task.get("metadata", {}).get("verifier_detected")
            ):
                raise ValueError("Previous task is not a distinct control from this replay.")
            names.add(name)
            digest.update(raw)
        return names, digest.hexdigest()
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid previous control result directory: {error}") from error


class HolisticJudge(Evaluator):
    """A strict three-state judge; malformed responses count as control misses."""

    def __init__(
        self,
        model_id: str,
        generator_model_id: str,
        *,
        region: str = "us-east-1",
        reasoning_effort: str = "high",
        max_concurrency: int = 1,
        cache_dir: Path | None = None,
        prompt_version: str = "v1",
    ):
        super().__init__()
        if model_family(model_id) == model_family(generator_model_id):
            raise ValueError("The blog judge must use a different model family from the generator.")
        if max_concurrency < 1:
            raise ValueError("Judge concurrency must be positive.")
        if prompt_version not in {"v1", "v2"}:
            raise ValueError("Judge prompt version must be v1 or v2.")
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.prompt = (PROMPT_DIRECTORY / f"holistic_judge_{prompt_version}.md").read_text()
        self.prompt_sha256 = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.cache_dir = cache_dir
        self.cache_locks: dict[str, Any] = {}
        self.cache_locks_guard = threading.Lock()
        if cache_dir is not None:
            from medical_nudging.evidence_contract.artifact_paths import (
                ensure_output_dir_outside_repo,
            )

            self.cache_dir = ensure_output_dir_outside_repo(cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = threading.BoundedSemaphore(max_concurrency)
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=900, retries={"max_attempts": 3, "mode": "adaptive"}),
        )

    def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        view = build_evidence_view(payload)
        cache_path = None
        cache_lock = nullcontext()
        if self.cache_dir is not None:
            key = self.cache_key(view)
            cache_path = self.cache_dir / f"{key}.json"
            with self.cache_locks_guard:
                cache_lock = self.cache_locks.setdefault(key, threading.Lock())
        with cache_lock, self.semaphore:
            try:
                if cache_path is not None and cache_path.exists():
                    cached = json.loads(cache_path.read_text())
                    if (
                        not isinstance(cached, dict)
                        or cached.get("verdict")
                        not in {
                            "satisfies_contract",
                            "violates_contract",
                            "abstain_insufficient_evidence",
                        }
                        or cached.get("prompt_sha256") != self.prompt_sha256
                        or cached.get("model_id") != self.model_id
                        or cached.get("reasoning_effort") != self.reasoning_effort
                    ):
                        raise ValueError(
                            "Cached judgment is malformed or has mismatched provenance."
                        )
                    return cached
                response = self.client.converse(
                    modelId=self.model_id,
                    system=[{"text": self.prompt}],
                    messages=[{"role": "user", "content": [{"text": json.dumps(view)}]}],
                    inferenceConfig={"maxTokens": 2048},
                    additionalModelRequestFields={"reasoning": {"effort": self.reasoning_effort}},
                )
                raw = "".join(
                    block.get("text", "") for block in response["output"]["message"]["content"]
                )
                clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                result = json.loads(clean)
                if not isinstance(result, dict) or result.get("verdict") not in {
                    "satisfies_contract",
                    "violates_contract",
                    "abstain_insufficient_evidence",
                }:
                    raise ValueError("Judge response has no recognized verdict.")
                judged = {
                    **result,
                    "usage": response.get("usage", {}),
                    "prompt_sha256": self.prompt_sha256,
                    "model_id": self.model_id,
                    "reasoning_effort": self.reasoning_effort,
                }
                if cache_path is not None:
                    with tempfile.NamedTemporaryFile(
                        mode="w", dir=cache_path.parent, suffix=".tmp", delete=False
                    ) as temporary:
                        temporary.write(json.dumps(judged))
                    Path(temporary.name).replace(cache_path)
                return judged
            except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, OSError) as error:
                return {
                    "verdict": "abstain_insufficient_evidence",
                    "error": f"{type(error).__name__}: {error}",
                    "prompt_sha256": self.prompt_sha256,
                }

    def cache_key(self, view: dict[str, Any]) -> str:
        identity = {
            "view": view,
            "prompt_sha256": self.prompt_sha256,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": 2048,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        result = self.judge(evaluation_case.actual_output)
        detected = result["verdict"] == "violates_contract"
        return [
            EvaluationOutput(
                score=float(detected),
                test_pass=detected,
                label=result["verdict"],
                reason=json.dumps(result, sort_keys=True),
            )
        ]


def control_summary(report: Any, count: int) -> dict[str, Any]:
    detected = sum(report.test_passes)
    return {
        "controls": count,
        "detected": detected,
        "trusted_for_citation_provenance": count > 0
        and len(report.test_passes) == count
        and detected == count,
        "criterion": "Every admitted control must receive violates_contract; abstentions and errors miss.",
    }
