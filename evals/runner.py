"""One Strands Experiment entry point for generation, replay, and regression."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from strands_evals import Case, Experiment
from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from medical_nudging.evidence_contract.artifact_paths import ensure_output_dir_outside_repo
from medical_nudging.evidence_contract.case_naming import (
    build_case_name,
    patient_short,
    result_store_subpath,
)
from medical_nudging.evidence_contract.rules import rule_content_hashes


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def implementation_hash() -> str:
    """Invalidate on runtime, prompt, evaluator, or dependency changes."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = [root / "uv.lock"]
    for directory in ("src/medical_nudging", "evals", "prompts"):
        files.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".md", ".txt", ".json", ".html"}
        )
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_identity(config: dict[str, Any]) -> dict[str, Any]:
    from medical_nudging.config import get_fhir_config, get_opensearch_config, get_prompts_path

    prompts = get_prompts_path().resolve()
    prompt_hashes = {
        str(path.relative_to(prompts)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(prompts.rglob("*"))
        if path.is_file()
    }
    return {
        "config": config,
        "corpus_version": config["generator_config"]["corpus_version"],
        "rule_hashes": rule_content_hashes(),
        "implementation_sha256": implementation_hash(),
        "effective_prompts_sha256": content_hash(prompt_hashes),
        "retrieval_settings": {
            "fhir": get_fhir_config(),
            "opensearch": get_opensearch_config(),
        },
    }


def select_regression_dataset(config: dict[str, Any], path: Path) -> dict[str, Any]:
    """Use the predefined AgentCore scenarios with the same Experiment task."""
    import copy

    selected = copy.deepcopy(config)
    dataset = json.loads(path.read_text())
    scenarios = dataset.get("scenarios", [])
    if not scenarios:
        raise ValueError("Regression dataset has no predefined scenarios.")
    patients = []
    versions = set()
    for scenario in scenarios:
        metadata = scenario["metadata"]
        for key in ("model", "agent", "generator_config"):
            if metadata[key] != config[key]:
                raise ValueError(f"Regression dataset configuration differs at {key}.")
        versions.add(metadata["dataset_version"])
        turns = scenario["turns"]
        if len(turns) != 1:
            raise ValueError("This runner supports one request per patient scenario.")
        visit = turns[0]["input"]["visit_context"]
        patients.append(
            {
                "patient_id": visit["patient_id"],
                "data_source": "fhir_api",
                "visit_context": visit,
            }
        )
    if len(versions) != 1 or len({p["patient_id"] for p in patients}) != len(patients):
        raise ValueError("Regression scenarios need one version and distinct patients.")
    selected["patients"] = patients
    selected["dataset_version"] = versions.pop()
    selected["regression_dataset_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return selected


class FileEvaluationDataStore:
    """Atomic SDK task cache; paths contain the complete run identity."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def _path(self, case_name: str) -> Path:
        if not case_name or Path(case_name).name != case_name or case_name in {".", ".."}:
            raise ValueError("Case name must be a single filename component.")
        return self.path / f"{case_name}.json"

    def load(self, case_name: str) -> EvaluationData | None:
        path = self._path(case_name)
        if not path.exists():
            return None
        result = EvaluationData.model_validate_json(path.read_text())
        if result.name != case_name:
            raise ValueError("Cached result name does not match the requested case.")
        return result

    def save(self, case_name: str, result: EvaluationData) -> None:
        path = self._path(case_name)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(result.model_dump_json())
        temporary.replace(path)


class Completion(Evaluator):
    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        record = evaluation_case.actual_output or {}
        passed = record.get("response_status") in {"success", "partial"}
        return [
            EvaluationOutput(
                score=float(passed),
                test_pass=passed,
                label="completion",
                reason=f"Recorded status: {record.get('response_status', 'missing')}",
            )
        ]


def run_experiment(
    *,
    config: dict[str, Any],
    output_dir: Path,
    task: Callable,
    evaluators: list[Evaluator],
    cases: list[Case] | None = None,
    max_concurrency: int = 1,
) -> tuple[Any, Path]:
    """Run/replay each case through Experiment, then evaluate its persisted output."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive.")
    output_dir = ensure_output_dir_outside_repo(output_dir)
    identity = run_identity(config)
    if cases is not None:
        identity["case_inputs_sha256"] = content_hash([case.model_dump() for case in cases])
    digest = content_hash(identity)
    path = (
        output_dir
        / result_store_subpath(
            benchmark_version=config["dataset_version"], split="offline", evaluator_id="layers-v1"
        )
        / digest
    )
    store = FileEvaluationDataStore(path / "tasks")
    manifest = path / "manifest.json"
    manifest.write_text(json.dumps(identity, indent=2) + "\n")
    if cases is None:
        cases = [
            Case(
                name=build_case_name(
                    benchmark_version=config["dataset_version"],
                    split="offline",
                    control="original",
                    patient_short=patient_short(entry["patient_id"], 12),
                    nudge_idx=0,
                    evaluator_id="layers-v1",
                ),
                input=entry,
                session_id=entry["patient_id"],
                metadata={"dataset_version": config["dataset_version"], "config_sha256": digest},
            )
            for entry in config["patients"]
        ]
    experiment = Experiment(cases=cases, evaluators=evaluators)
    report = asyncio.run(
        experiment.run_evaluations_async(
            task,
            max_workers=max_concurrency,
            evaluation_data_store=store,
        )
    )
    (path / "report.json").write_text(report.model_dump_json())
    return report, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--regression-dataset", type=Path, help="Predefined AgentCore dataset JSON")
    parser.add_argument(
        "--eligible-cohort",
        type=Path,
        help="Use eligible_patient_ids_anchored from the downloaded-demo eligibility audit JSON",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    os.environ["AWS_REGION"] = args.region
    config = json.loads(args.config.read_text())
    if args.regression_dataset:
        if args.eligible_cohort or args.limit:
            parser.error("Regression dataset cannot be combined with cohort overrides.")
        config = select_regression_dataset(config, args.regression_dataset)
    if args.eligible_cohort:
        audit = json.loads(args.eligible_cohort.read_text())
        ids = audit["eligible_patient_ids_anchored"]
        if not ids or len(ids) != len(set(ids)) or not all(isinstance(pid, str) for pid in ids):
            parser.error("Eligibility audit must contain distinct patient identifiers.")
        config["patients"] = [
            {
                "patient_id": pid,
                "data_source": "fhir_api",
                "visit_context": {"visit_type": "inpatient", "specialty": "general"},
            }
            for pid in sorted(ids)
        ]
        config["dataset_version"] = "mimic-eligible-v1"
        config["eligibility_sha256"] = hashlib.sha256(args.eligible_cohort.read_bytes()).hexdigest()
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        config["patients"] = config["patients"][: args.limit]
    if config["generator_config"].get("fhir_max_pages"):
        os.environ["FHIR_MAX_PAGES"] = str(config["generator_config"]["fhir_max_pages"])
    from evals.generation import configure_generation, generate_patient

    configure_generation(config)

    def task(case: Case) -> dict[str, Any]:
        record = generate_patient(case.input, config)
        print(f"Completed {case.name}: {record['response_status']}", flush=True)
        return {"output": record}

    from evals.layers import EvidenceContract, LatencyBudget, OperationalChecks, TokenBudget

    evaluators = [Completion(), OperationalChecks(config), EvidenceContract()]
    thresholds = config.get("thresholds", {})
    if thresholds.get("latency_ms") is not None:
        evaluators.append(LatencyBudget(thresholds["latency_ms"]))
    if thresholds.get("total_tokens") is not None:
        evaluators.append(TokenBudget(thresholds["total_tokens"]))
    report, path = run_experiment(
        config=config,
        output_dir=args.output_dir,
        task=task,
        evaluators=evaluators,
        max_concurrency=args.concurrency,
    )
    print(f"Saved evaluation: {path}", flush=True)
    return 0 if all(report.test_passes) and report.test_passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
