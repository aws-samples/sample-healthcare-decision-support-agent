"""Deterministic verifier for the evidence contract (design doc §4).

The replayable, code-only half of the evidence contract evaluator.  It checks source
and passage identity, exact values, dates and units, medication status relative to a
codified versioned rule, arithmetic/trends/intervals, citation resolution against the
supplied corpus chunk ids, temporal conditions, absence within recorded query coverage,
and derivation-chain integrity.

**This module makes no model calls.**  It imports no model client, no agent, and no
judge.  Bounded judgment lives elsewhere and cannot override a deterministic
failure.  Every check returns a :class:`CheckResult` naming the claim, the check, a
three-state outcome, and the evidence spans it used, so any result can be replayed.

Reading the verdict: :attr:`DeterministicVerifierReport.verdict` is a
:data:`~.schema.DeterministicLayerVerdict`, which has no ``satisfies_contract`` state.
The strongest deterministic conclusion is ``deterministic_checks_passed`` — every
replayable check that applied passed.  ``satisfies_contract`` is the shared conclusion of
the *whole* evaluator (design §4, decision 12) and requires bounded judgment of
substantive support and applicability, so :attr:`DeterministicVerifierReport.case_verdict`
maps a passing deterministic report onto abstention until the bounded judge runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from .rules import (
    MedicationStatusRule,
    QueryCoverageRule,
    load_medication_status_rule,
    load_query_coverage_rule,
)
from .schema import (
    ActionEvidenceChain,
    ClaimLedgerEntry,
    DerivationStep,
    DeterministicLayerVerdict,
    EvidenceContractVerdict,
    case_verdict_from_deterministic,
)
from .tool_ledger import ToolLedger, citation_matches_passage, normalize_identifier

CheckStatus = Literal["pass", "fail", "not_evaluable"]

CheckName = Literal[
    "source_identity",
    "passage_identity",
    "citation_resolution",
    "exact_values",
    "dates",
    "units",
    "medication_status",
    "arithmetic_and_trends",
    "temporal_conditions",
    "absence_within_query_coverage",
    "derivation_chain_integrity",
    "action_evidence_chain",
]

_NUMBER_RE = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)(?![\w.])")
# The trailing lookahead (rather than \b) is what lets a date inside a full ISO
# timestamp match: in "2140-10-04T15:00" there is no word boundary after "04".
_ISO_DATE_RE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?!\d)")
# Date-shaped tokens, masked before numeric extraction: slash dates (``7/12``,
# ``10/08/2140``) and ISO dates or timestamps.  A two-part slash token is only masked
# when both parts are calendar-plausible, so a blood pressure written ``80/50`` keeps its
# two asserted values instead of being mistaken for a month and a day.
_DATE_TOKEN_RE = re.compile(
    r"(?<![\w./-])(?:\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)(?![\w/-])"
)
_SLASH_PAIR_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Absence markers, matched on word boundaries.  Bare substrings ("no ", "not ") also fire
# on "normal", "notable", "cannot", which drove near-universal abstention.
_ABSENCE_MARKER_RE = re.compile(
    r"\b(?:no|not|none|never|without|absent|absence|lacks|lacking|denies|negative)\b",
    re.IGNORECASE,
)
_WINDOW_RE = re.compile(
    r"within\s+(?P<count>\d+)\s*(?P<unit>hours?|days?|weeks?|months?|years?)",
    re.IGNORECASE,
)

# Design §4 names units alongside exact values and dates.  The vocabulary is deliberately
# small and closed: an unrecognised trailing token is "no unit", never a unit mismatch.
_UNIT_SYNONYMS = {
    "mmhg": "mmhg",
    "mg/dl": "mg/dl",
    "g/dl": "g/dl",
    "mg": "mg",
    "mcg": "mcg",
    "g": "g",
    "kg": "kg",
    "lb": "lb",
    "lbs": "lb",
    "ml": "ml",
    "l": "l",
    "meq/l": "meq/l",
    "mmol/l": "mmol/l",
    "umol/l": "umol/l",
    "ml/min": "ml/min",
    "ml/min/1.73m2": "ml/min",
    "iu": "iu",
    "units": "unit",
    "unit": "unit",
    "%": "%",
    "bpm": "bpm",
    "mg/day": "mg/day",
    "mg/kg": "mg/kg",
}
_KNOWN_UNITS = frozenset(_UNIT_SYNONYMS)
_VALUE_UNIT_RE = re.compile(
    r"(?<![\w.-])(?P<value>\d+(?:\.\d+)?)\s*" r"(?P<unit>%|[A-Za-z]+(?:/[A-Za-z0-9.]+)*)?(?![\w.])",
)

_UNIT_DAYS = {
    "hour": 1 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
    "year": 365.0,
}

ACTIVE_MEDICATION_MARKERS = ("on ", "taking", "receiving", "currently prescribed", "active on")
INACTIVE_MEDICATION_MARKERS = ("discontinued", "stopped", "not on", "no longer", "off ")
MEDICATION_CLAIM_MARKERS = (
    "dose",
    "insulin",
    "medication",
    "mg",
    "prescrib",
    "regimen",
    "therapy",
    "units",
)

SUPPORTED_DERIVATION_RULES = ("difference", "sum", "count", "trend", "interval_days")


@dataclass(frozen=True)
class CheckResult:
    """One deterministic check applied to one claim."""

    claim_id: str
    check: CheckName
    status: CheckStatus
    detail: str
    spans_used: tuple[str, ...] = ()
    rule_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for persisted artifacts."""
        return {
            "claim_id": self.claim_id,
            "check": self.check,
            "status": self.status,
            "detail": self.detail,
            "spans_used": list(self.spans_used),
            "rule_version": self.rule_version,
        }


@dataclass
class DeterministicVerifierReport:
    """All check results for one nudge, plus the deterministic-layer verdict."""

    results: list[CheckResult] = field(default_factory=list)
    rule_versions: dict[str, str] = field(default_factory=dict)

    @property
    def failures(self) -> list[CheckResult]:
        """Checks that established a violation."""
        return [result for result in self.results if result.status == "fail"]

    @property
    def not_evaluable(self) -> list[CheckResult]:
        """Checks that could not be decided from the recorded evidence."""
        return [result for result in self.results if result.status == "not_evaluable"]

    @property
    def verdict(self) -> DeterministicLayerVerdict:
        """Deterministic-layer verdict (design doc §4, decisions 12 and 20).

        One violated claim makes the whole nudge violate the contract.  Otherwise
        unresolved required evidence produces abstention.  When every applicable check
        passed the answer is ``deterministic_checks_passed`` — not ``satisfies_contract``,
        which only the full evaluator can conclude.
        """
        if self.failures:
            return "violates_contract"
        if self.not_evaluable:
            return "abstain_insufficient_evidence"
        return "deterministic_checks_passed"

    @property
    def case_verdict(self) -> EvidenceContractVerdict:
        """The verdict this report contributes to the shared case-level verdict space."""
        return case_verdict_from_deterministic(self.verdict)

    def failure_summary(self) -> list[str]:
        """One line per failure, for steering feedback and audit records."""
        return [
            f"[{result.check}] claim {result.claim_id}: {result.detail}" for result in self.failures
        ]

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for persisted artifacts."""
        return {
            "verdict": self.verdict,
            "case_verdict": self.case_verdict,
            "rule_versions": dict(self.rule_versions),
            "results": [result.as_dict() for result in self.results],
        }


# --- Extraction helpers (pure) ----------------------------------------------


def _mask_date_tokens(match: re.Match[str]) -> str:
    """Blank a date-shaped token, keeping calendar-implausible slash pairs intact."""
    token = match.group(0)
    pair = _SLASH_PAIR_RE.match(token)
    if pair is not None:
        first, second = int(pair.group(1)), int(pair.group(2))
        if not (1 <= first <= 12 and 1 <= second <= 31):
            return token
    return " "


def mask_date_tokens(text: str) -> str:
    """Replace calendar-plausible date-shaped tokens with whitespace."""
    return _DATE_TOKEN_RE.sub(_mask_date_tokens, text or "")


def extract_numbers(text: str) -> set[str]:
    """Numeric tokens in normalised form, so ``5.40`` and ``5.4`` compare equal.

    Date tokens are masked out first: ``bilirubin 34.5 mg/dL (7/12)`` asserts one value
    and one date, not the values 7 and 12.  Dates belong to the ``dates`` check.  A slash
    pair that cannot be a calendar date (``80/50``) is not masked, so a blood pressure
    still asserts its two values.
    """
    values: set[str] = set()
    masked = mask_date_tokens(text)
    for match in _NUMBER_RE.finditer(masked):
        try:
            values.add(f"{float(match.group(1)):g}")
        except ValueError:  # pragma: no cover - regex guarantees a float
            continue
    return values


def extract_numbers_unmasked(text: str) -> set[str]:
    """Every numeric token in the text, with no date or identifier masking applied.

    Used only to tell "this claim asserts no value" apart from "masking removed every
    value this claim asserts", so the second case can abstain instead of passing.
    """
    values: set[str] = set()
    for match in _NUMBER_RE.finditer(text or ""):
        try:
            values.add(f"{float(match.group(1)):g}")
        except ValueError:  # pragma: no cover - regex guarantees a float
            continue
    return values


def extract_value_units(text: str) -> dict[str, set[str]]:
    """Map each numeric token in the text to the units written immediately after it.

    Only the unit token adjacent to the number counts, and only when it is a recognised
    clinical unit: ``eGFR 45 mL/min`` attaches ``ml/min`` to ``45``, while ``45 patients``
    attaches nothing.  A value written without a unit contributes an empty unit set, which
    is what makes "unit on one side only" distinguishable from "units disagree".
    """
    found: dict[str, set[str]] = {}
    for match in _VALUE_UNIT_RE.finditer(mask_date_tokens(text)):
        try:
            key = f"{float(match.group('value')):g}"
        except ValueError:  # pragma: no cover - regex guarantees a float
            continue
        unit = (match.group("unit") or "").strip().lower()
        found.setdefault(key, set())
        if unit in _KNOWN_UNITS:
            found[key].add(_UNIT_SYNONYMS.get(unit, unit))
    return found


def extract_dates(text: str) -> set[date]:
    """ISO-8601 calendar dates appearing in the text."""
    found: set[date] = set()
    for match in _ISO_DATE_RE.finditer(text or ""):
        try:
            found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    return found


def extract_stated_windows(text: str) -> list[tuple[str, float]]:
    """Explicit ``within N units`` time windows stated in guideline text.

    Returns ``(rendered, days)`` pairs.  An implied window is not a stated window: the
    temporal-conditions check reports ``not_evaluable`` when nothing matches here.
    """
    windows: list[tuple[str, float]] = []
    for match in _WINDOW_RE.finditer(text or ""):
        unit = match.group("unit").lower().rstrip("s")
        scale = _UNIT_DAYS.get(unit)
        if scale is None:
            continue
        count = int(match.group("count"))
        windows.append((f"within {count} {unit}(s)", count * scale))
    return windows


def span_numeric_value(content: dict[str, Any]) -> float | None:
    """Numeric value of a FHIR resource span, when it carries one."""
    quantity = content.get("valueQuantity")
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, (int, float)):
            return float(value)
    value = content.get("value")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def span_date(content: dict[str, Any]) -> date | None:
    """Effective date of a FHIR resource span, when it carries one."""
    for key in (
        "effectiveDateTime",
        "occurrenceDateTime",
        "issued",
        "authoredOn",
        "whenHandedOver",
        "date",
    ):
        value = content.get(key)
        if isinstance(value, str):
            parsed = extract_dates(value)
            if parsed:
                return min(parsed)
    return None


def _replay_rule(kind: str, step: DerivationStep, contents: list[dict[str, Any]]) -> str | None:
    """Recompute one supported derivation rule; ``None`` when the spans lack the values."""
    if kind == "count":
        return str(len(step.inputs))
    if kind == "interval_days":
        return _replay_interval_days(contents)
    if kind == "trend":
        return _replay_trend(contents)
    values = [
        value
        for value in (span_numeric_value(content) for content in contents)
        if value is not None
    ]
    if kind == "difference":
        return f"{abs(values[0] - values[1]):g}" if len(values) >= 2 else None
    return f"{sum(values):g}" if values else None  # sum


def _replay_interval_days(contents: list[dict[str, Any]]) -> str | None:
    dates = [value for value in (span_date(content) for content in contents) if value]
    if len(dates) < 2:
        return None
    return str(abs((max(dates) - min(dates)).days))


def _replay_trend(contents: list[dict[str, Any]]) -> str | None:
    dated = [(span_date(content), span_numeric_value(content)) for content in contents]
    ordered = [
        value
        for when, value in sorted(
            (item for item in dated if item[0] and item[1] is not None),
            key=lambda item: item[0],  # type: ignore[arg-type,return-value]
        )
    ]
    if len(ordered) < 2:
        return None
    if ordered[-1] > ordered[0]:  # type: ignore[operator]
        return "increasing"
    if ordered[-1] < ordered[0]:  # type: ignore[operator]
        return "decreasing"
    return "flat"


def infer_as_of_date(ledger: ToolLedger) -> date | None:
    """Reference date for recency and interval arithmetic, or ``None``.

    The latest date recorded anywhere in the patient evidence, so replaying a persisted
    trace produces the same answer it produced when the trace was captured.  There is no
    wall-clock fallback: a verifier that consulted ``datetime.now()`` would give a
    different verdict on a re-run of the same evidence, which is exactly what a replayable
    verifier may not do.  When the evidence carries no date and the caller supplied no
    ``as_of``, the date-dependent checks report ``not_evaluable``.
    """
    dates = extract_dates(ledger.patient_evidence_text())
    return max(dates) if dates else None


# --- Verifier ---------------------------------------------------------------


class DeterministicVerifier:
    """Replayable, code-only checks over a claim ledger and a tool ledger.

    No method on this class calls a model.
    """

    def __init__(
        self,
        *,
        medication_status_rule: MedicationStatusRule | None = None,
        query_coverage_rule: QueryCoverageRule | None = None,
    ) -> None:
        self.medication_status_rule = medication_status_rule or load_medication_status_rule()
        self.query_coverage_rule = query_coverage_rule or load_query_coverage_rule()

    @property
    def rule_versions(self) -> dict[str, str]:
        """Versions of the rule artifacts this verifier instance replays."""
        return {
            "medication_status_rule_version": self.medication_status_rule.rule_version,
            "query_coverage_rule_version": self.query_coverage_rule.rule_version,
        }

    def verify(
        self,
        *,
        claims: list[ClaimLedgerEntry],
        ledger: ToolLedger,
        action_evidence_chain: ActionEvidenceChain | None = None,
        as_of: date | None = None,
    ) -> DeterministicVerifierReport:
        """Run every applicable deterministic check over a claim ledger.

        ``as_of`` is the reference date for recency and interval arithmetic.  When the
        caller supplies none it is derived from the recorded evidence; when the evidence
        carries no date either it stays ``None`` and the date-dependent checks abstain
        rather than consulting the wall clock.
        """
        report = DeterministicVerifierReport(rule_versions=self.rule_versions)
        reference_date = as_of if as_of is not None else infer_as_of_date(ledger)

        if ledger.is_empty:
            report.results.append(
                CheckResult(
                    claim_id="-",
                    check="derivation_chain_integrity",
                    status="not_evaluable",
                    detail="the tool ledger recorded no evidence spans",
                )
            )
            return report

        for claim in claims:
            report.results.extend(self._check_claim(claim, ledger, reference_date))

        if action_evidence_chain is not None:
            report.results.append(self._check_action_chain(action_evidence_chain, claims))
        return report

    # --- per-claim dispatch -------------------------------------------------

    def _check_claim(
        self,
        claim: ClaimLedgerEntry,
        ledger: ToolLedger,
        reference_date: date | None,
    ) -> list[CheckResult]:
        results = [self._check_span_integrity(claim, ledger)]
        if claim.citation_provenance is not None:
            results.append(self._check_source_identity(claim, ledger))
            results.append(self._check_passage_identity(claim, ledger))
            results.append(self._check_citation_resolution(claim, ledger))
        if claim.derivation_chain:
            results.extend(self._check_derivations(claim, ledger))
        results.append(self._check_exact_values(claim, ledger))
        results.append(self._check_dates(claim, ledger))
        units = self._check_units(claim, ledger)
        if units is not None:
            results.append(units)
        if self._is_medication_claim(claim.claim_text):
            results.append(self._check_medication_status(claim, ledger, reference_date))
        if self._is_absence_claim(claim.claim_text):
            results.append(self._check_absence_coverage(claim, ledger))
        temporal = self._check_temporal_conditions(claim, ledger, reference_date)
        if temporal is not None:
            results.append(temporal)
        return results

    # --- identity and citation ---------------------------------------------

    def _check_source_identity(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        citation = claim.citation_provenance
        assert citation is not None  # guarded by caller
        wanted = normalize_identifier(citation.source)
        matches = [
            span
            for span in ledger.guideline_evidence
            if normalize_identifier(span.source) == wanted
        ]
        if not wanted:
            return CheckResult(
                claim_id=claim.claim_id,
                check="source_identity",
                status="fail",
                detail="the claim carries citation provenance with an empty source",
            )
        if not matches:
            supplied = sorted({span.source for span in ledger.guideline_evidence})
            return CheckResult(
                claim_id=claim.claim_id,
                check="source_identity",
                status="fail",
                detail=(
                    f"cited source {citation.source!r} is not among the retrieved passages "
                    f"({supplied})"
                ),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="source_identity",
            status="pass",
            detail=f"cited source {citation.source!r} matches {len(matches)} retrieved passage(s)",
            spans_used=tuple(span.span_id for span in matches),
        )

    def _check_passage_identity(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        citation = claim.citation_provenance
        assert citation is not None
        wanted_source = normalize_identifier(citation.source)
        same_source = [
            span
            for span in ledger.guideline_evidence
            if normalize_identifier(span.source) == wanted_source
        ]
        if not same_source:
            return CheckResult(
                claim_id=claim.claim_id,
                check="passage_identity",
                status="fail",
                detail=f"no retrieved passage comes from {citation.source!r}",
            )

        # One shared definition of "this citation identifies that passage", also used by
        # the citation-control construction (:func:`citation_matches_passage`), so a
        # fabricated citation cannot resolve in one place and fail in the other.
        cited = {
            "source": citation.source,
            "section": citation.section,
            "page_number": citation.page_number,
        }
        matches = [
            span
            for span in same_source
            if citation_matches_passage(
                cited,
                {
                    "source": span.source,
                    "section": span.section,
                    "page_number": span.page_number,
                },
            )
        ]

        if not matches:
            return CheckResult(
                claim_id=claim.claim_id,
                check="passage_identity",
                status="fail",
                detail=(
                    f"citation {citation.source!r} section {citation.section!r} "
                    f"page {citation.page_number} identifies no retrieved passage"
                ),
                spans_used=tuple(span.span_id for span in same_source),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="passage_identity",
            status="pass",
            detail=f"citation identifies {len(matches)} retrieved passage(s)",
            spans_used=tuple(span.span_id for span in matches),
        )

    def _check_citation_resolution(
        self, claim: ClaimLedgerEntry, ledger: ToolLedger
    ) -> CheckResult:
        citation = claim.citation_provenance
        assert citation is not None
        if not citation.chunk_ids:
            return CheckResult(
                claim_id=claim.claim_id,
                check="citation_resolution",
                status="not_evaluable",
                detail="the citation declares no chunk ids to resolve against the corpus",
            )
        supplied = ledger.guideline_chunk_ids()
        unresolved = [chunk_id for chunk_id in citation.chunk_ids if chunk_id not in supplied]
        if unresolved:
            return CheckResult(
                claim_id=claim.claim_id,
                check="citation_resolution",
                status="fail",
                detail=f"cited chunk ids do not resolve to retrieved passages: {unresolved}",
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="citation_resolution",
            status="pass",
            detail=f"all {len(citation.chunk_ids)} cited chunk id(s) resolve",
            spans_used=tuple(
                span.span_id
                for span in ledger.guideline_evidence
                if span.chunk_id in set(citation.chunk_ids)
            ),
        )

    # --- values, dates ------------------------------------------------------

    def _claim_evidence_text(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> str:
        """Text of exactly the spans this claim names.

        Atomic provenance means a claim rests on the spans it names and on nothing else,
        so a claim naming no span yields no evidence text.  Falling back to every span in
        the ledger would let a value that appears anywhere in the record support any
        claim, which is the failure mode atomic provenance exists to prevent.
        """
        named = set(claim.evidence_span_ids)
        if not named:
            return ""
        parts: list[str] = []
        for span in ledger.patient_evidence:
            if span.span_id in named:
                content = span.content
                text = content.get("text")
                parts.append(text if isinstance(text, str) else str(sorted(content.items())))
        for passage in ledger.guideline_evidence:
            if passage.span_id in named:
                parts.append(passage.content)
        return "\n".join(parts)

    @staticmethod
    def _identifier_numbers(claim: ClaimLedgerEntry) -> set[str]:
        """Numbers belonging to *this claim's* citation identifier, not to asserted values.

        A publication year inside a source name (``ACC_AHA_..._2025``) or a numbered
        section heading asserts nothing about the patient; whether the nudge names the
        right source and passage is what source identity and passage identity check.

        Scoped to the claim's own citation on purpose.  Pooling the labels of every
        retrieved passage let an unrelated ``Table 4`` heading exempt the number 4 from
        every claim in the run, which is a silent hole in the exact-values check.
        """
        citation = claim.citation_provenance
        if citation is None:
            return set()
        label = f"{citation.source} {citation.section or ''}"
        # Identifiers glue their year on with punctuation (``..._2025``), which the number
        # pattern deliberately will not split, so fold separators to spaces first.
        return extract_numbers(normalize_identifier(label))

    @staticmethod
    def _guideline_evidence_text(ledger: ToolLedger) -> str:
        """Text of every retrieved guideline passage, regardless of which claim names it."""
        return "\n".join(span.content for span in ledger.guideline_evidence)

    def _check_exact_values(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        raw_numbers = extract_numbers_unmasked(claim.claim_text)
        claimed = extract_numbers(claim.claim_text) - self._identifier_numbers(claim)
        if not claimed:
            if raw_numbers:
                # Masking is a heuristic; a claim whose every number was masked away is a
                # claim this check could not read, not a claim that asserts nothing.
                # Passing here is how a masking bug turns into a silent false pass.
                return CheckResult(
                    claim_id=claim.claim_id,
                    check="exact_values",
                    status="not_evaluable",
                    detail=(
                        f"every number in the claim ({sorted(raw_numbers)}) was masked as a "
                        "date token or a citation identifier, so no asserted value remains "
                        "to check"
                    ),
                    spans_used=tuple(claim.evidence_span_ids),
                )
            return CheckResult(
                claim_id=claim.claim_id,
                check="exact_values",
                status="pass",
                detail="the claim asserts no numeric value",
            )
        if not claim.evidence_span_ids:
            return CheckResult(
                claim_id=claim.claim_id,
                check="exact_values",
                status="not_evaluable",
                detail=(
                    f"the claim asserts values {sorted(claimed)} but names no evidence span, "
                    "so there is nothing to check them against"
                ),
            )
        evidence_text = self._claim_evidence_text(claim, ledger)
        if ledger.is_degraded:
            return CheckResult(
                claim_id=claim.claim_id,
                check="exact_values",
                status="not_evaluable",
                detail=(
                    "the patient evidence could not be decomposed into per-resource spans "
                    f"({'; '.join(ledger.fidelity_reasons)}), so a missing value cannot be "
                    "distinguished from a value the payload never carried"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        supported = extract_numbers(evidence_text)
        missing = sorted(claimed - supported)
        if missing:
            # A nudge sentence routinely mixes a patient value with a guideline-side
            # number (a dose range, a recheck interval).  A number absent from the
            # claim's own spans but present in a retrieved guideline passage is not an
            # established violation: which link it belongs to is a bounded judgment, so
            # the deterministic layer leaves it unresolved rather than failing it.
            from_guideline = extract_numbers(self._guideline_evidence_text(ledger))
            unresolved = sorted(value for value in missing if value in from_guideline)
            if len(unresolved) == len(missing):
                return CheckResult(
                    claim_id=claim.claim_id,
                    check="exact_values",
                    status="not_evaluable",
                    detail=(
                        f"values {unresolved} appear in retrieved guideline evidence but not in "
                        "the spans this claim names; attribution is a bounded judgment"
                    ),
                    spans_used=tuple(claim.evidence_span_ids),
                )
            return CheckResult(
                claim_id=claim.claim_id,
                check="exact_values",
                status="fail",
                detail=(
                    f"values {sorted(set(missing) - set(unresolved))} do not appear in the named "
                    "evidence spans"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="exact_values",
            status="pass",
            detail=f"all {len(claimed)} asserted value(s) appear in the named evidence spans",
            spans_used=tuple(claim.evidence_span_ids),
        )

    def _check_dates(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        claimed = extract_dates(claim.claim_text)
        if not claimed:
            return CheckResult(
                claim_id=claim.claim_id,
                check="dates",
                status="pass",
                detail="the claim asserts no date",
            )
        if not claim.evidence_span_ids:
            return CheckResult(
                claim_id=claim.claim_id,
                check="dates",
                status="not_evaluable",
                detail=(
                    "the claim asserts dates but names no evidence span, so there is nothing "
                    "to check them against"
                ),
            )
        if ledger.is_degraded:
            return CheckResult(
                claim_id=claim.claim_id,
                check="dates",
                status="not_evaluable",
                detail=(
                    "the patient evidence could not be decomposed into per-resource spans "
                    f"({'; '.join(ledger.fidelity_reasons)}), so a missing date cannot be "
                    "distinguished from a date the payload never carried"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        supported = extract_dates(self._claim_evidence_text(claim, ledger))
        missing = sorted(claimed - supported)
        if missing:
            return CheckResult(
                claim_id=claim.claim_id,
                check="dates",
                status="fail",
                detail=(
                    "dates "
                    f"{[value.isoformat() for value in missing]} do not appear in the named "
                    "evidence spans"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="dates",
            status="pass",
            detail=f"all {len(claimed)} asserted date(s) appear in the named evidence spans",
            spans_used=tuple(claim.evidence_span_ids),
        )

    def _check_units(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult | None:
        """Unit agreement for values the claim and its named evidence both quantify.

        Design §4 names units alongside exact values and dates.  Deliberately minimal:
        a mismatch only counts when both sides attach a recognised unit to the *same*
        numeric value, and a unit present on one side only is ``not_evaluable`` — unit
        normalisation across a clinical corpus is not a deterministic problem.
        """
        if not claim.evidence_span_ids or ledger.is_degraded:
            return None
        claimed = extract_value_units(claim.claim_text)
        if not claimed:
            return None
        evidence = extract_value_units(self._claim_evidence_text(claim, ledger))
        mismatched: list[str] = []
        one_sided: list[str] = []
        compared = 0
        for value, claim_units in claimed.items():
            evidence_units = evidence.get(value)
            if evidence_units is None:
                continue
            if not claim_units or not evidence_units:
                if claim_units or evidence_units:
                    one_sided.append(value)
                continue
            compared += 1
            if not (claim_units & evidence_units):
                mismatched.append(
                    f"{value}: claim {sorted(claim_units)} vs evidence {sorted(evidence_units)}"
                )
        if mismatched:
            return CheckResult(
                claim_id=claim.claim_id,
                check="units",
                status="fail",
                detail=f"unit mismatch on {mismatched}",
                spans_used=tuple(claim.evidence_span_ids),
            )
        if one_sided:
            return CheckResult(
                claim_id=claim.claim_id,
                check="units",
                status="not_evaluable",
                detail=(
                    f"values {sorted(one_sided)} carry a unit on only one side, so unit "
                    "agreement cannot be established deterministically"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        if not compared:
            return None
        return CheckResult(
            claim_id=claim.claim_id,
            check="units",
            status="pass",
            detail=f"units agree on all {compared} value(s) quantified on both sides",
            spans_used=tuple(claim.evidence_span_ids),
        )

    # --- medication status --------------------------------------------------

    @staticmethod
    def _is_medication_claim(claim_text: str) -> bool:
        lowered = claim_text.lower()
        asserts_status = any(
            marker in lowered
            for marker in (*ACTIVE_MEDICATION_MARKERS, *INACTIVE_MEDICATION_MARKERS)
        )
        return asserts_status and any(marker in lowered for marker in MEDICATION_CLAIM_MARKERS)

    @staticmethod
    def _medication_names(resource: dict[str, Any]) -> set[str]:
        """Display names and codes naming the drug a medication resource is about."""
        names: set[str] = set()
        for key in ("medicationCodeableConcept", "medicationReference"):
            value = resource.get(key)
            if not isinstance(value, dict):
                continue
            for text in (value.get("text"), value.get("display"), value.get("reference")):
                if isinstance(text, str) and text.strip():
                    names.add(text.strip().lower())
            for coding in value.get("coding") or []:
                if isinstance(coding, dict) and isinstance(coding.get("display"), str):
                    names.add(coding["display"].strip().lower())
        return {name for name in names if name}

    @classmethod
    def _resources_for_named_drug(
        cls, claim_text: str, resources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The medication resources whose drug the claim actually names.

        Resolving a status against *every* medication in the chart is what made "the
        patient is not on a statin" fail because of an unrelated active prescription.
        A resource counts only when a word of its drug name appears in the claim.
        """
        claim_words = set(_WORD_RE.findall(claim_text.lower()))
        if not claim_words:
            return []
        matched: list[dict[str, Any]] = []
        for resource in resources:
            words = {
                word
                for name in cls._medication_names(resource)
                for word in _WORD_RE.findall(name)
                if len(word) > 3
            }
            if words & claim_words:
                matched.append(resource)
        return matched

    def _check_medication_status(
        self,
        claim: ClaimLedgerEntry,
        ledger: ToolLedger,
        reference_date: date | None,
    ) -> CheckResult:
        rule_version = self.medication_status_rule.rule_version
        if not ledger.has_raw_fhir_resources:
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="not_evaluable",
                detail=(
                    "the patient evidence is not raw FHIR "
                    f"(fidelity {ledger.patient_evidence_fidelity!r}), so {rule_version} "
                    "cannot read the status fields it resolves against"
                ),
                rule_version=rule_version,
            )
        if reference_date is None:
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="not_evaluable",
                detail=(
                    "no as-of date could be derived from the recorded evidence, so "
                    f"{rule_version}'s recency window has no reference point"
                ),
                rule_version=rule_version,
            )
        all_resources = ledger.medication_resources()
        if not all_resources:
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="not_evaluable",
                detail=(
                    "the recorded evidence contains no raw FHIR medication resources, so "
                    f"{rule_version} cannot resolve a status"
                ),
                rule_version=rule_version,
            )
        resources = self._resources_for_named_drug(claim.claim_text, all_resources)
        if not resources:
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="not_evaluable",
                detail=(
                    f"none of the {len(all_resources)} recorded medication resource(s) names "
                    "the drug this claim is about, so the claim's status cannot be resolved "
                    "against them"
                ),
                rule_version=rule_version,
            )
        resolved, reason = self.medication_status_rule.resolve(resources, as_of=reference_date)
        lowered = claim.claim_text.lower()
        asserts_inactive = any(marker in lowered for marker in INACTIVE_MEDICATION_MARKERS)
        asserted = "not_active" if asserts_inactive else "active"

        if resolved == "indeterminate":
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="not_evaluable",
                detail=f"{rule_version} resolved to indeterminate: {reason}",
                rule_version=rule_version,
            )
        if resolved != asserted:
            return CheckResult(
                claim_id=claim.claim_id,
                check="medication_status",
                status="fail",
                detail=(
                    f"the claim asserts {asserted} but {rule_version} resolves {resolved}: {reason}"
                ),
                rule_version=rule_version,
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="medication_status",
            status="pass",
            detail=f"{rule_version} resolves {resolved}, matching the claim: {reason}",
            rule_version=rule_version,
        )

    # --- absence within recorded query coverage -----------------------------

    def _is_absence_claim(self, claim_text: str) -> bool:
        """Whether the claim asserts that something is absent from the record.

        A negation marker alone is not enough: "the eGFR is not stable" negates a
        property, not a record entry.  The claim must also fall into a coverage-rule
        category, which is what names the resource types absence would have to be
        established over.
        """
        if self.query_coverage_rule.categorise(claim_text) is None:
            return False
        return _ABSENCE_MARKER_RE.search(claim_text) is not None

    def _check_absence_coverage(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        rule_version = self.query_coverage_rule.rule_version
        category = self.query_coverage_rule.categorise(claim.claim_text)
        if category is None:
            return CheckResult(
                claim_id=claim.claim_id,
                check="absence_within_query_coverage",
                status="not_evaluable",
                detail=(
                    f"the absence claim matches no {rule_version} category, so recorded "
                    "coverage cannot establish absence"
                ),
                rule_version=rule_version,
            )
        queries = ledger.queries_executed
        missing = self.query_coverage_rule.missing_resource_types(category, queries)
        if missing:
            return CheckResult(
                claim_id=claim.claim_id,
                check="absence_within_query_coverage",
                status="not_evaluable",
                detail=(
                    f"{category} coverage requires queries for {missing}, which the recorded "
                    "query coverage does not contain"
                ),
                rule_version=rule_version,
            )
        truncated_lower = {name.lower() for name in ledger.truncated_resource_types}
        truncated = sorted(
            name
            for name in self.query_coverage_rule.required_resource_types(category)
            if name.lower() in truncated_lower
        )
        if truncated:
            # coverage-v2: a search that stopped at the page cap, or whose history
            # window dropped results, saw a truncated window — its emptiness proves
            # nothing.  Abstention, never a violation.
            return CheckResult(
                claim_id=claim.claim_id,
                check="absence_within_query_coverage",
                status="not_evaluable",
                detail=(
                    f"{category} coverage queries for {truncated} returned a truncated "
                    "window (page cap or history filter), which cannot establish absence"
                ),
                rule_version=rule_version,
            )
        required = {
            name.lower() for name in self.query_coverage_rule.required_resource_types(category)
        }
        present = sorted(
            {
                span.resource_type
                for span in ledger.patient_evidence
                if span.resource_type.lower() in required
            }
        )
        if present:
            return CheckResult(
                claim_id=claim.claim_id,
                check="absence_within_query_coverage",
                status="not_evaluable",
                detail=(
                    f"{category} coverage is satisfied but the covered queries returned "
                    f"{present} resources, whose match against the claim needs audit"
                ),
                rule_version=rule_version,
                spans_used=tuple(span.span_id for span in ledger.patient_evidence),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="absence_within_query_coverage",
            status="pass",
            detail=(
                f"{category} coverage is satisfied by the recorded queries and returned no "
                "resources of the required types"
            ),
            rule_version=rule_version,
        )

    # --- temporal conditions ------------------------------------------------

    def _check_temporal_conditions(
        self,
        claim: ClaimLedgerEntry,
        ledger: ToolLedger,
        reference_date: date | None,
    ) -> CheckResult | None:
        claimed_dates = extract_dates(claim.claim_text)
        if not claimed_dates:
            return None
        if reference_date is None:
            return CheckResult(
                claim_id=claim.claim_id,
                check="temporal_conditions",
                status="not_evaluable",
                detail=(
                    "no as-of date could be derived from the recorded evidence, so a claimed "
                    "date has no reference point to be inside or outside a window of"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )

        named = set(claim.evidence_span_ids)
        if not named:
            return CheckResult(
                claim_id=claim.claim_id,
                check="temporal_conditions",
                status="not_evaluable",
                detail=(
                    "the claim carries dates but names no cited passage, so no stated window "
                    "belongs to it"
                ),
            )
        # Only the passages this claim cites. A window stated anywhere in the retrieved
        # corpus is not a condition on this claim, and applying the tightest one found
        # anywhere manufactured staleness failures out of unrelated guideline text.
        cited_text = "\n".join(
            span.content for span in ledger.guideline_evidence if span.span_id in named
        )
        windows = extract_stated_windows(cited_text)
        if not windows:
            return CheckResult(
                claim_id=claim.claim_id,
                check="temporal_conditions",
                status="not_evaluable",
                detail=(
                    "the claim carries dates but the passages it cites state no explicit "
                    "time window to check them against"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        distinct = {days for _, days in windows}
        if len(distinct) > 1:
            return CheckResult(
                claim_id=claim.claim_id,
                check="temporal_conditions",
                status="not_evaluable",
                detail=(
                    f"the cited passages state {len(distinct)} different time windows "
                    f"({sorted(rendered for rendered, _ in windows)}); which one conditions "
                    "this claim is a bounded judgment"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )

        rendered, window_days = windows[0]
        newest = max(claimed_dates)
        age_days = (reference_date - newest).days
        if age_days > window_days:
            return CheckResult(
                claim_id=claim.claim_id,
                check="temporal_conditions",
                status="fail",
                detail=(
                    f"the newest claimed date {newest.isoformat()} is {age_days}d before the "
                    f"reference date {reference_date.isoformat()}, outside the stated "
                    f"{rendered} window"
                ),
                spans_used=tuple(claim.evidence_span_ids),
            )
        return CheckResult(
            claim_id=claim.claim_id,
            check="temporal_conditions",
            status="pass",
            detail=(
                f"the newest claimed date {newest.isoformat()} is {age_days}d before the "
                f"reference date, inside the stated {rendered} window"
            ),
            spans_used=tuple(claim.evidence_span_ids),
        )

    # --- derivation chains --------------------------------------------------

    def _check_span_integrity(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> CheckResult:
        known = ledger.span_ids()
        if not claim.evidence_span_ids:
            return CheckResult(
                claim_id=claim.claim_id,
                check="derivation_chain_integrity",
                status="not_evaluable",
                detail="the claim names no evidence spans",
            )
        unknown = [span_id for span_id in claim.evidence_span_ids if span_id not in known]
        if unknown:
            return CheckResult(
                claim_id=claim.claim_id,
                check="derivation_chain_integrity",
                status="fail",
                detail=f"named evidence spans are absent from the tool ledger: {unknown}",
            )
        for step in claim.derivation_chain:
            missing = [span_id for span_id in step.inputs if span_id not in known]
            if missing:
                return CheckResult(
                    claim_id=claim.claim_id,
                    check="derivation_chain_integrity",
                    status="fail",
                    detail=(
                        f"derivation step {step.rule!r} names inputs absent from the tool "
                        f"ledger: {missing}"
                    ),
                )
            if not step.output.strip():
                return CheckResult(
                    claim_id=claim.claim_id,
                    check="derivation_chain_integrity",
                    status="fail",
                    detail=f"derivation step {step.rule!r} records no output",
                )
        return CheckResult(
            claim_id=claim.claim_id,
            check="derivation_chain_integrity",
            status="pass",
            detail=(
                f"{len(claim.evidence_span_ids)} named span(s) and "
                f"{len(claim.derivation_chain)} derivation step(s) resolve in the tool ledger"
            ),
            spans_used=tuple(claim.evidence_span_ids),
        )

    def _check_derivations(self, claim: ClaimLedgerEntry, ledger: ToolLedger) -> list[CheckResult]:
        return [self._replay_derivation(claim, step, ledger) for step in claim.derivation_chain]

    def _replay_derivation(
        self,
        claim: ClaimLedgerEntry,
        step: DerivationStep,
        ledger: ToolLedger,
    ) -> CheckResult:
        """Recompute one derivation step and compare against its recorded output."""
        kind = step.rule.split("(")[0].strip().lower()
        if kind not in SUPPORTED_DERIVATION_RULES:
            return self._derivation_result(
                claim,
                step,
                "not_evaluable",
                f"derivation rule {step.rule!r} is not one of the replayable rules "
                f"{list(SUPPORTED_DERIVATION_RULES)}",
            )

        by_id = {span.span_id: span.content for span in ledger.patient_evidence}
        contents = [by_id[span_id] for span_id in step.inputs if span_id in by_id]
        computed = _replay_rule(kind, step, contents)
        if computed is None:
            return self._derivation_result(
                claim,
                step,
                "not_evaluable",
                f"derivation rule {step.rule!r} needs values the named spans do not supply",
            )

        recorded = step.output.strip()
        matched = recorded.lower() == computed.lower() or computed in extract_numbers(recorded)
        if not matched:
            return self._derivation_result(
                claim,
                step,
                "fail",
                f"replaying {step.rule!r} over {list(step.inputs)} yields {computed!r}, "
                f"not the recorded output {recorded!r}",
            )
        return self._derivation_result(
            claim,
            step,
            "pass",
            f"replaying {step.rule!r} reproduces the recorded output {recorded!r}",
        )

    @staticmethod
    def _derivation_result(
        claim: ClaimLedgerEntry, step: DerivationStep, status: CheckStatus, detail: str
    ) -> CheckResult:
        return CheckResult(
            claim_id=claim.claim_id,
            check="arithmetic_and_trends",
            status=status,
            detail=detail,
            spans_used=tuple(step.inputs),
        )

    # --- action evidence chain ---------------------------------------------

    def _check_action_chain(
        self,
        chain: ActionEvidenceChain,
        claims: list[ClaimLedgerEntry],
    ) -> CheckResult:
        """Chain-integrity check (decision 14).

        A missing link is not-evaluable, never a violation: abstention is the correct
        outcome unless another claim has already established a violation.  A link
        naming a claim that does not exist *is* a violation — the chain is broken.
        """
        known = {claim.claim_id for claim in claims}
        dangling = [claim_id for claim_id in chain.all_claim_ids() if claim_id not in known]
        if dangling:
            return CheckResult(
                claim_id="-",
                check="action_evidence_chain",
                status="fail",
                detail=f"the action evidence chain names claims that do not exist: {dangling}",
            )
        missing = chain.missing_links()
        if missing:
            return CheckResult(
                claim_id="-",
                check="action_evidence_chain",
                status="not_evaluable",
                detail=f"the action evidence chain has no claims for links: {missing}",
            )
        return CheckResult(
            claim_id="-",
            check="action_evidence_chain",
            status="pass",
            detail="every action evidence chain link names at least one existing claim",
        )
