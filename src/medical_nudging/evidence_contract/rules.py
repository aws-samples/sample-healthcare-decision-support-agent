"""Versioned rule artifacts for the deterministic verifier (decision 16, design doc §5).

Medication status and absence coverage are deterministic only *relative to a codified
rule*.  Those rules therefore ship as versioned JSON artifacts under ``rule_artifacts/``
rather than as inline Python conditionals, so that:

* every verifier result can name the rule version it replayed;
* the freeze rule (design doc §5) can content-hash them before any test case is scored.

The loaders are cached, so a run reads each artifact once and reports one hash for it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

RULES_DIR = Path(__file__).parent / "rule_artifacts"

MEDICATION_STATUS_RULE_FILE = "medication_status_rule_v2.json"
QUERY_COVERAGE_RULE_FILE = "query_coverage_rule_v2.json"

MedicationStatus = Literal["active", "not_active", "indeterminate"]

_KEYWORD_CACHE: dict[str, re.Pattern[str]] = {}


def _keyword_matches(keyword: str, text: str) -> bool:
    """Whether a rule-artifact keyword occurs in the text on word boundaries."""
    pattern = _KEYWORD_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])")
        _KEYWORD_CACHE[keyword] = pattern
    return pattern.search(text) is not None


class RuleArtifactError(RuntimeError):
    """Raised when a versioned rule artifact is missing or malformed."""


def _load_artifact(filename: str) -> tuple[dict[str, Any], str]:
    """Read one rule artifact and its content hash."""
    path = RULES_DIR / filename
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuleArtifactError(f"Cannot read rule artifact {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuleArtifactError(f"Rule artifact {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("rule_version"):
        raise RuleArtifactError(f"Rule artifact {path} has no rule_version")
    return payload, hashlib.sha256(raw).hexdigest()


def _parse_iso_date(value: Any) -> date | None:
    """Parse the leading date of an ISO-8601 timestamp, tolerating a trailing offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    head = value.strip()[:10]
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class MedicationStatusRule:
    """Codified dispense/administration/statement medication status rule.

    Resolves a recorded medication status to ``active`` / ``not_active`` /
    ``indeterminate``.  The rule interprets *documentation*, not clinical reality: a
    resource that is merely stale yields ``indeterminate``, never ``not_active``, so
    that missing evidence drives abstention rather than a violation (decision 20).
    """

    rule_version: str
    content_hash: str
    payload: dict[str, Any]

    @property
    def resource_precedence(self) -> list[str]:
        precedence = self.payload.get("resource_precedence")
        return list(precedence) if isinstance(precedence, list) else []

    def resolve_resource(
        self,
        *,
        resource_type: str,
        status: str | None,
        effective_date: str | None,
        as_of: date,
    ) -> tuple[MedicationStatus, str]:
        """Resolve one medication resource to a status plus a replayable reason."""
        mappings = self.payload.get("status_mappings", {})
        resource_map = mappings.get(resource_type)
        unmapped: MedicationStatus = self.payload.get("unmapped_status_result", "indeterminate")
        if not isinstance(resource_map, dict):
            return unmapped, f"{resource_type} is not covered by {self.rule_version}"

        normalized = (status or "").strip().lower()
        resolved: MedicationStatus | None = None
        for bucket in ("active", "not_active", "indeterminate"):
            values = resource_map.get(bucket) or []
            if isinstance(values, list) and normalized in {str(v).lower() for v in values}:
                resolved = bucket  # type: ignore[assignment]
                break
        if resolved is None:
            return unmapped, f"status {status!r} is unmapped for {resource_type}"

        if resolved != "active":
            return resolved, f"{resource_type} status {normalized!r} maps to {resolved}"

        window = (self.payload.get("recency_window_days") or {}).get(resource_type)
        if not isinstance(window, int):
            return resolved, f"{resource_type} status {normalized!r} maps to active"

        parsed = _parse_iso_date(effective_date)
        if parsed is None:
            missing: MedicationStatus = self.payload.get("missing_date_result", "indeterminate")
            return missing, f"{resource_type} has no parseable effective date"
        age_days = (as_of - parsed).days
        if age_days > window:
            return (
                "indeterminate",
                f"{resource_type} is {age_days}d old, outside the {window}d recency window",
            )
        return resolved, (
            f"{resource_type} status {normalized!r} maps to active within the {window}d window"
        )

    def resolve(
        self,
        resources: list[dict[str, Any]],
        *,
        as_of: date,
    ) -> tuple[MedicationStatus, str]:
        """Resolve a medication status across resources using the rule's precedence.

        The highest-precedence resource with a determinate result wins.
        """
        if not resources:
            return "indeterminate", "no medication resources in the recorded evidence"

        precedence = self.resource_precedence
        ordered = sorted(
            resources,
            key=lambda resource: (
                precedence.index(str(resource.get("resourceType")))
                if str(resource.get("resourceType")) in precedence
                else len(precedence)
            ),
        )
        reasons: list[str] = []
        for resource in ordered:
            status, reason = self.resolve_resource(
                resource_type=str(resource.get("resourceType", "")),
                status=resource.get("status"),
                effective_date=(
                    resource.get("effectiveDateTime")
                    or resource.get("occurrenceDateTime")
                    or resource.get("whenHandedOver")
                    or resource.get("authoredOn")
                ),
                as_of=as_of,
            )
            reasons.append(reason)
            if status != "indeterminate":
                return status, reason
        return "indeterminate", "; ".join(reasons)


@dataclass(frozen=True)
class QueryCoverageRule:
    """Declares which queries must be recorded before absence may be established."""

    rule_version: str
    content_hash: str
    payload: dict[str, Any]

    def categorise(self, claim_text: str) -> str | None:
        """Match an absence claim to a declared coverage category, if any.

        Keywords match on word boundaries.  A bare substring match let the artifact
        keyword ``off`` fire on "offer" and "cutoff", miscategorising claims that assert
        nothing about a medication being stopped.  The artifact itself is frozen and
        content-hashed, so the fix belongs here rather than in the keyword list.
        """
        lowered = claim_text.lower()
        for category, spec in (self.payload.get("categories") or {}).items():
            keywords = spec.get("keywords") if isinstance(spec, dict) else None
            if not isinstance(keywords, list):
                continue
            if any(_keyword_matches(str(keyword).lower(), lowered) for keyword in keywords):
                return str(category)
        return None

    def required_resource_types(self, category: str) -> list[str]:
        """Resource types whose queries the category requires."""
        spec = (self.payload.get("categories") or {}).get(category)
        if not isinstance(spec, dict):
            return []
        required = spec.get("required_resource_types")
        return [str(item) for item in required] if isinstance(required, list) else []

    def missing_resource_types(self, category: str, queries_executed: list[str]) -> list[str]:
        """Required resource types absent from the recorded query coverage."""
        recorded = " ".join(queries_executed).lower()
        return [
            resource_type
            for resource_type in self.required_resource_types(category)
            if resource_type.lower() not in recorded
        ]


@lru_cache(maxsize=1)
def load_medication_status_rule() -> MedicationStatusRule:
    """Load the versioned medication status rule artifact."""
    payload, content_hash = _load_artifact(MEDICATION_STATUS_RULE_FILE)
    return MedicationStatusRule(
        rule_version=str(payload["rule_version"]),
        content_hash=content_hash,
        payload=payload,
    )


@lru_cache(maxsize=1)
def load_query_coverage_rule() -> QueryCoverageRule:
    """Load the versioned absence-coverage rule artifact."""
    payload, content_hash = _load_artifact(QUERY_COVERAGE_RULE_FILE)
    return QueryCoverageRule(
        rule_version=str(payload["rule_version"]),
        content_hash=content_hash,
        payload=payload,
    )


def rule_versions() -> dict[str, str]:
    """Rule versions for the generator configuration and run manifest."""
    return {
        "medication_status_rule_version": load_medication_status_rule().rule_version,
        "query_coverage_rule_version": load_query_coverage_rule().rule_version,
    }


def rule_content_hashes() -> dict[str, str]:
    """Content hashes of the rule artifacts, for the freeze manifest (design doc §5)."""
    return {
        MEDICATION_STATUS_RULE_FILE: load_medication_status_rule().content_hash,
        QUERY_COVERAGE_RULE_FILE: load_query_coverage_rule().content_hash,
    }
