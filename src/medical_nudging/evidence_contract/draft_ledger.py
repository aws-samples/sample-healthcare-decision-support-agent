"""Derive candidate claim-to-evidence links without model calls.

Statements are split and classified mechanically, then linked to candidate patient
and guideline spans. The resulting ledger is drafted evidence for deterministic
checks, not an audited answer key. Ambiguous or incomplete links must abstain.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .schema import ActionEvidenceChain, CitationProvenance, ClaimLedgerEntry, ClaimType
from .tool_ledger import ToolLedger, citation_page, normalize_identifier

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+|\n+")

APPLICABILITY_MARKERS = (
    "adult",
    "age",
    "child",
    "contraindicat",
    "eligib",
    "elderly",
    "exclu",
    "indicat",
    "male",
    "paediatric",
    "patients with",
    "pediatric",
    "population",
    "pregnan",
    "version",
    "women",
)

TEMPORAL_MARKERS = (
    "day",
    "hour",
    "month",
    "recent",
    "since",
    "week",
    "within",
    "year",
)

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_TOKEN_RE = re.compile(r"[a-z]+")
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?(?:-\d{2}-\d{2})?")


def split_claim_statements(text: str) -> list[str]:
    """Split nudge prose into claim-sized statements."""
    statements = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "")]
    return [statement for statement in statements if len(statement) > 2]


def has_temporal_content(text: str) -> bool:
    """True when a statement carries a date or an explicit time expression."""
    lowered = text.lower()
    return bool(_DATE_RE.search(text)) or any(marker in lowered for marker in TEMPORAL_MARKERS)


def has_applicability_content(text: str) -> bool:
    """True when a statement asserts a population, version, or eligibility condition."""
    lowered = text.lower()
    return any(marker in lowered for marker in APPLICABILITY_MARKERS)


def _citation_from_nudge(nudge: dict[str, Any]) -> CitationProvenance | None:
    """Read the nudge's citation provenance, tolerating both page spellings."""
    citation = nudge.get("guideline_citation")
    if not isinstance(citation, dict):
        return None
    page_number = citation_page(citation)
    chunk_ids = citation.get("chunk_ids")
    return CitationProvenance(
        source=str(citation.get("source", "")),
        section=str(citation.get("section", "") or ""),
        page_number=page_number,
        chunk_ids=[str(item) for item in chunk_ids] if isinstance(chunk_ids, list) else [],
    )


def _candidate_guideline_span_ids(
    citation: CitationProvenance | None,
    ledger: ToolLedger,
) -> list[str]:
    """Guideline spans whose source matches the citation, as candidate evidence."""
    if citation is None:
        return []
    wanted = normalize_identifier(citation.source)
    return [
        span.span_id
        for span in ledger.guideline_evidence
        if normalize_identifier(span.source) == wanted
    ]


def _statement_tokens(text: str) -> set[str]:
    """Content tokens of a statement: numbers, dates, and words worth matching on.

    Short words are dropped because "the", "is", "of" appear in every span and would
    make every span look like a match.
    """
    tokens = {token for token in _TOKEN_RE.findall(text.lower()) if len(token) > 3}
    tokens |= {token for token in _NUMERIC_TOKEN_RE.findall(text)}
    return tokens


def _matching_patient_span_ids(statement: str, ledger: ToolLedger) -> list[str]:
    """Patient spans whose content shares a content token with the statement.

    Atomic provenance is a claim-level link to the exact evidence spans that claim rests
    on (CONTEXT.md).  Attaching every patient span to every factual claim is a
    document-level source list wearing a claim-level shape: it lets a value appearing
    anywhere in the record support a claim about something else entirely.  A claim that
    matches no span gets an empty span list, which the verifier reports as not_evaluable
    — an unattributable draft claim, not a violated one.
    """
    wanted = _statement_tokens(statement)
    if not wanted:
        return []
    matched: list[str] = []
    for span in ledger.patient_evidence:
        content = span.content
        text = content.get("text")
        rendered = text if isinstance(text, str) else json.dumps(content, sort_keys=True)
        if _statement_tokens(rendered) & wanted:
            matched.append(span.span_id)
    return matched


def build_draft_claim_ledger(
    nudge: dict[str, Any],
    ledger: ToolLedger,
    *,
    claim_id_prefix: str = "c",
) -> tuple[list[ClaimLedgerEntry], ActionEvidenceChain]:
    """Draft a claim ledger and action evidence chain for one nudge.

    Args:
        nudge: The drafted nudge payload (``title``, ``description``, ``rationale``,
            ``grounding``, ``guideline_citation``).
        ledger: Captured tool ledger supplying candidate evidence spans.
        claim_id_prefix: Prefix for generated claim ids.

    Returns:
        The drafted claim ledger entries and the action evidence chain linking them.
        Chain links the drafted ledger cannot fill stay empty, which makes the
        deterministic verifier report them as not-evaluable rather than violated.
    """
    citation = _citation_from_nudge(nudge)
    guideline_span_ids = _candidate_guideline_span_ids(citation, ledger)

    entries: list[ClaimLedgerEntry] = []
    trigger_ids: list[str] = []
    rule_ids: list[str] = []
    applicability_ids: list[str] = []
    temporal_ids: list[str] = []

    def add(
        *,
        claim_type: ClaimType,
        claim_text: str,
        evidence_span_ids: list[str],
        provenance: CitationProvenance | None = None,
    ) -> str:
        claim_id = f"{claim_id_prefix}{len(entries) + 1:02d}"
        entries.append(
            ClaimLedgerEntry(
                claim_id=claim_id,
                claim_type=claim_type,
                claim_text=claim_text,
                evidence_span_ids=list(evidence_span_ids),
                citation_provenance=provenance,
                audit_status="drafted",
            )
        )
        return claim_id

    # Factual claims come from the rationale and description: the patient-side
    # statements the nudge asserts.
    factual_source = " ".join(
        str(nudge.get(field, "") or "") for field in ("rationale", "description")
    )
    for statement in split_claim_statements(factual_source):
        claim_id = add(
            claim_type="factual",
            claim_text=statement,
            evidence_span_ids=_matching_patient_span_ids(statement, ledger),
        )
        trigger_ids.append(claim_id)
        if has_temporal_content(statement):
            temporal_ids.append(claim_id)

    # The guideline claim carries the citation provenance: what the nudge says the
    # guideline requires. Citation provenance is not verification.
    if citation is not None:
        rule_text = str(nudge.get("title", "") or "").strip() or citation.section
        rule_id = add(
            claim_type="guideline",
            claim_text=f"{citation.source} {citation.section}: {rule_text}".strip(),
            evidence_span_ids=guideline_span_ids,
            provenance=citation,
        )
        rule_ids.append(rule_id)
        if has_applicability_content(citation.section):
            applicability_ids.append(rule_id)

    # The recommended action.
    action_text = " ".join(
        part
        for part in (str(nudge.get("title", "") or ""), str(nudge.get("description", "") or ""))
        if part
    ).strip()
    if action_text:
        add(
            claim_type="recommended_action",
            claim_text=action_text,
            evidence_span_ids=[
                *_matching_patient_span_ids(action_text, ledger),
                *guideline_span_ids,
            ],
            provenance=citation,
        )

    chain = ActionEvidenceChain(
        trigger_claim_ids=trigger_ids,
        rule_claim_ids=rule_ids,
        applicability_claim_ids=applicability_ids,
        temporal_condition_claim_ids=temporal_ids,
    )
    return entries, chain
