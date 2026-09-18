"""Import saved generation artifacts without making a generator call."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

# Attested settings of the saved system baseline, absent from its legacy files.
# Keep this independent of the comparison config so replay cannot relabel settings.
LEGACY_OPUS_MODEL = {
    "model_id": "us.anthropic.claude-opus-5",
    "thinking_type": "adaptive",
    "effort": "high",
    "budget_tokens": None,
    "temperature": None,
    "max_tokens": 32000,
    "cache_system_prompt": True,
    "extra_headers": {},
}
LEGACY_OPUS_AGENT = {"max_nudges": 5, "search_backend": "opensearch", "tool_limits": {}}


def _reconcile_generation_requirements(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover exclusions omitted by the legacy per-nudge outcome serializer.

    The saved final steering attempt already rejected missing required citations.
    This changes replay/filter bookkeeping, never the deterministic verdict or
    the source files. New records carry these failures directly.
    """
    for record in records:
        nudges = record.get("nudges", [])
        record.setdefault(
            "source_excluded_nudge_count",
            sum(bool(n.get("steering_outcome", {}).get("flags")) for n in nudges),
        )
        steering = record.get("patient_steering_outcome", {})
        attempts = [
            a for a in steering.get("attempts", []) if a.get("action") != "proceed_no_draft"
        ]
        if (
            not steering.get("generator_config", {}).get("require_citation")
            or not attempts
            or attempts[-1].get("action") != "proceed_unresolved"
            or not any(
                failure.startswith("[generation_requirement]")
                for failure in attempts[-1].get("failures", [])
            )
        ):
            continue
        added = 0
        for item in nudges:
            if item["nudge"].get("guideline_citation"):
                continue
            outcome = item.setdefault("steering_outcome", {})
            if outcome.get("generation_requirement_failures"):
                continue
            failure = (
                "[generation_requirement] Missing required guideline_citation; "
                "reconciled from the saved final steering attempt."
            )
            flags = outcome.setdefault("flags", [])
            added += not flags
            if "contract_failures_unresolved" not in flags:
                flags.append("contract_failures_unresolved")
            outcome.setdefault("failures", []).append(failure)
            outcome["generation_requirement_failures"] = [failure]
        if added:
            record["replay_adjustments"] = {"missing_required_citation_exclusions": added}
            record["excluded_nudge_count"] = sum(
                bool(n.get("steering_outcome", {}).get("flags")) for n in nudges
            )
            if record.get("response") is not None:
                record["response"]["nudges"] = [
                    n["nudge"] for n in nudges if not n.get("steering_outcome", {}).get("flags")
                ]
    return records


def _validate_model_settings(
    saved: dict[str, Any], record: dict[str, Any], config: dict[str, Any]
) -> None:
    source = {"extra_headers": {}, **saved}
    expected = {"extra_headers": {}, **config["model"]}
    actual_transport = record.get("generation_settings", {}).get("transport", {})
    source_timeout = source.pop("read_timeout", actual_transport.get("read_timeout", 120))
    expected_timeout = expected.pop("read_timeout", 600)
    if source != expected:
        raise ValueError("Saved effective generation settings differ at model.")
    if actual_transport and actual_transport["read_timeout"] != source_timeout:
        raise ValueError("Saved model and actual client timeout disagree.")
    if source_timeout == expected_timeout:
        return
    retained = config.get("retained_run_transport", {})
    if (
        retained.get("read_timeout") != source_timeout
        or retained.get("replacement_read_timeout") != expected_timeout
    ):
        raise ValueError("Saved Bedrock timeout differs without an explicit retention decision.")
    error = str((record.get("response") or {}).get("error", "")).lower()
    if any(message in error for message in ("read timeout", "read timed out", "readtimeouterror")):
        raise ValueError("A request that hit the discarded timeout must not enter the comparison.")


def validate_provenance(records: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Reject a model/config mismatch before attributing results to an arm."""
    expected = config["generator_config"]
    for record in records:
        saved = record.get("patient_steering_outcome", {}).get("generator_config", {})
        for key in (
            "model_id",
            "config_version",
            "corpus_version",
            "retry_cap",
            "require_citation",
            "nudge_tool_name",
            "entailment_model_id",
        ):
            if saved.get(key) != expected.get(key):
                raise ValueError(f"Saved generator provenance differs at {key}.")
        instructions_hash = hashlib.sha256(
            expected.get("custom_instructions", "").encode()
        ).hexdigest()
        if saved.get("custom_instructions_hash") != instructions_hash:
            raise ValueError("Saved generator custom instructions differ.")
        settings = record.get("generation_settings")
        if settings is not None:
            _validate_model_settings(settings["model"], record, config)
            for key in ("agent", "generator_config", "specialty"):
                if settings.get(key) != config.get(key):
                    raise ValueError(f"Saved effective generation settings differ at {key}.")
        elif record.get("missing_legacy_fields"):
            _validate_model_settings(LEGACY_OPUS_MODEL, record, config)
            if (
                config["agent"] != LEGACY_OPUS_AGENT
                or expected.get("fhir_max_pages") != 50
                or config.get("specialty", "general") != "general"
            ):
                raise ValueError(
                    "Comparison settings differ from the attested legacy Opus runtime."
                )
        else:
            raise ValueError("Run lacks effective generation settings; provenance is incomplete.")


def load_runs(path: Path) -> list[dict[str, Any]]:
    """Read an Experiment task store, or the legacy two-file source artifact."""
    if (path / "tasks").is_dir():
        manifest = json.loads((path / "manifest.json").read_text())
        records = []
        for task in sorted((path / "tasks").glob("*.json")):
            payload = json.loads(task.read_text())
            record = payload.get("actual_output")
            if not isinstance(record, dict) or "patient_id" not in record:
                raise ValueError(f"{task} has no patient run output.")
            saved_config = manifest["config"]
            if record.get("generation_settings") is not None:
                for key in ("model", "agent", "generator_config", "specialty"):
                    if record["generation_settings"].get(key) != saved_config.get(key):
                        raise ValueError(f"Task settings differ from source manifest at {key}.")
            records.append(record)
        return _reconcile_generation_requirements(records)
    runs = json.loads((path / "patient_runs.json").read_text())
    source = json.loads((path / "source_nudges.json").read_text())
    manifest = json.loads((path / "summary.json").read_text())["run_manifest"]
    by_patient = {run["patient_id"]: dict(run, nudges=[]) for run in runs}
    if len(by_patient) != len(runs):
        raise ValueError("Duplicate legacy patient run records.")
    for artifact in source:
        record = by_patient[artifact["patient_id"]]
        ledger = artifact["tool_ledger"]
        if "tool_ledger" in record and ledger != record["tool_ledger"]:
            raise ValueError(
                "Nudges for one patient carry different ledgers; explicit migration required."
            )
        record["tool_ledger"] = ledger
        record["query_coverage"] = artifact["query_coverage"]
        record["nudges"].append(
            {
                key: artifact[key]
                for key in (
                    "nudge_idx",
                    "nudge",
                    "steering_outcome",
                    "draft_claim_ledger",
                    "action_evidence_chain",
                )
                if key in artifact
            }
        )
    for record in by_patient.values():
        producer = record["patient_steering_outcome"]["generator_config"]
        for source_key, producer_key in (
            ("generator_model_id", "model_id"),
            ("generator_config_version", "config_version"),
            ("corpus_version", "corpus_version"),
        ):
            if manifest.get(source_key) != producer.get(producer_key):
                raise ValueError(f"Legacy source manifest differs at {source_key}.")
        record["excluded_nudge_count"] = sum(
            bool(nudge.get("steering_outcome", {}).get("flags")) for nudge in record["nudges"]
        )
        record["missing_legacy_fields"] = ["response", "trace"]
    return _reconcile_generation_requirements(list(by_patient.values()))
