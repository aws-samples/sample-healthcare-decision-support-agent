"""Deterministic evidence-presentation rule for model judges (evidence-view-v1).

A benchmark case's evaluator-visible payload runs to roughly a million tokens —
3,000+ patient evidence spans — which no judge model context fits.  This module
is the single place that reduces it to a judge-sized view:

* every guideline evidence span and the query-coverage record pass through
  verbatim;
* patient evidence spans are ranked by a fixed lexical-overlap score (BM25 over
  the case's own spans) against the nudge text, proposed action, and citation
  provenance, then selected greedily under a token budget, tie-broken by
  ``span_id``;
* two structural guarantees precede the global ranking: every ``Patient``
  resource span, and the top ``_PER_TYPE_FLOOR`` spans of every other resource
  type.  Pure top-K selection systematically dropped the context spans that
  support applicability claims — Encounter, Patient demographics, Condition,
  Procedure — which score low on lexical overlap with the nudge text
  (measured on the dev split: 38/48 audited reference-supporting spans
  in-view without the guarantees, 45/48 with them);
* selected spans are presented in their original payload order so rank carries
  no cue.

The rule consumes only the evaluator-visible payload (hidden fields already
stripped), so it cannot read them, and it is applied identically to both
evaluator conditions and every control type — otherwise the view itself would
confound the comparison or leak the perturbation.  The deterministic verifier
does NOT use this view: it replays checks over the full payload.  Only model
judge calls (baseline holistic and bounded) receive the filtered view; the
asymmetry is disclosed in the paper.

The module's own source bytes are the versioned artifact:
:func:`evidence_view_content_hash` joins run manifests the same way the
verifier rule hashes do, and a post-freeze change to this file invalidates a
test run (design doc §5).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

EVIDENCE_VIEW_VERSION = "evidence-view-v1"

# Provisional until the MedGemma vLLM max_model_len is verified on the serving
# endpoint; frozen (with that check recorded) before any dev scoring.
DEFAULT_PATIENT_SPAN_TOKEN_BUDGET = 32_000

# Structural floor per non-Patient resource type: the judge always sees the
# strongest few spans of every type, whatever the global ranking says.
_PER_TYPE_FLOOR = 5

_BM25_K1 = 1.2
_BM25_B = 0.75

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def evidence_view_content_hash() -> str:
    """SHA-256 of this module's source bytes — the freeze-manifest identity."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _estimate_tokens(obj: Any) -> int:
    """Deterministic token estimate: serialized bytes / 4, minimum 1."""
    serialized = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return max(1, len(serialized.encode("utf-8")) // 4)


def _span_document(span: dict[str, Any]) -> str:
    """The text a patient span is scored on: resource type plus raw content."""
    content = span.get("content", {})
    resource_type = span.get("resource_type", "")
    return f"{resource_type} {json.dumps(content, sort_keys=True, ensure_ascii=False)}"


def _query_text(payload: dict[str, Any]) -> str:
    """The scoring query: nudge text, proposed action, citation provenance."""
    original = payload.get("benchmark_original", {})
    parts = [original.get("nudge_text", ""), original.get("proposed_action", "")]
    provenance = original.get("citation_provenance")
    if isinstance(provenance, dict):
        parts.append(provenance.get("source", ""))
        parts.append(provenance.get("section", ""))
    return " ".join(part for part in parts if part)


def _bm25_scores(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    """BM25 of each document against the query, idf computed over ``documents``."""
    doc_count = len(documents)
    if doc_count == 0:
        return []
    avg_len = sum(len(doc) for doc in documents) / doc_count
    query_terms = sorted(set(query_tokens))
    doc_frequency = {term: sum(1 for doc in documents if term in set(doc)) for term in query_terms}
    scores: list[float] = []
    for doc in documents:
        doc_len = len(doc) or 1
        term_counts: dict[str, int] = {}
        for token in doc:
            term_counts[token] = term_counts.get(token, 0) + 1
        score = 0.0
        for term in query_terms:
            tf = term_counts.get(term, 0)
            if tf == 0:
                continue
            df = doc_frequency[term]
            idf = math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))
            denominator = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / (avg_len or 1))
            score += idf * tf * (_BM25_K1 + 1) / denominator
        scores.append(score)
    return scores


def select_patient_spans(
    payload: dict[str, Any],
    patient_span_token_budget: int = DEFAULT_PATIENT_SPAN_TOKEN_BUDGET,
) -> list[dict[str, Any]]:
    """Pack the token budget in priority order; spans that do not fit are skipped.

    Priority order: every ``Patient`` span, then the top ``_PER_TYPE_FLOOR``
    spans of each other resource type (types in sorted-name order), then the
    remaining spans by global BM25 rank.  A span that does not fit the
    remaining budget is skipped, not a stopping point, so one oversized span
    cannot exclude everything ranked after it.  Returned spans keep their
    original payload order.
    """
    spans = payload.get("patient_evidence", [])
    if not spans:
        return []
    query_tokens = _tokenize(_query_text(payload))
    documents = [_tokenize(_span_document(span)) for span in spans]
    scores = _bm25_scores(query_tokens, documents)
    ranked_indices = sorted(
        range(len(spans)),
        key=lambda i: (-scores[i], str(spans[i].get("span_id", ""))),
    )
    patient_indices: list[int] = []
    by_resource_type: dict[str, list[int]] = {}
    for index in ranked_indices:
        resource_type = str(spans[index].get("resource_type", ""))
        if resource_type == "Patient":
            patient_indices.append(index)
        else:
            by_resource_type.setdefault(resource_type, []).append(index)
    floor_indices = [
        index
        for resource_type in sorted(by_resource_type)
        for index in by_resource_type[resource_type][:_PER_TYPE_FLOOR]
    ]
    prioritized = patient_indices + floor_indices
    remainder = [i for i in ranked_indices if i not in set(prioritized)]
    remaining = patient_span_token_budget
    selected: set[int] = set()
    for index in prioritized + remainder:
        cost = _estimate_tokens(spans[index])
        if cost <= remaining:
            selected.add(index)
            remaining -= cost
    return [span for i, span in enumerate(spans) if i in selected]


def build_evidence_view(
    payload: dict[str, Any],
    patient_span_token_budget: int = DEFAULT_PATIENT_SPAN_TOKEN_BUDGET,
) -> dict[str, Any]:
    """The judge-visible view of an evaluator-visible payload.

    ``payload`` must be the evaluator-visible payload with hidden fields already
    stripped; passing a full case dict would defeat the blinding this rule relies on.
    """
    hidden_fields = {"reference_claim_ledger", "control", "provenance", "split"}
    leaked = hidden_fields & payload.keys()
    if leaked:
        raise ValueError(
            "build_evidence_view expects an evaluator-visible payload; "
            f"got hidden fields {sorted(leaked)}"
        )
    selected_spans = select_patient_spans(payload, patient_span_token_budget)
    view = {
        "benchmark_original": payload.get("benchmark_original", {}),
        "patient_evidence": selected_spans,
        "guideline_evidence": payload.get("guideline_evidence", []),
        "query_coverage": payload.get("query_coverage", {}),
    }
    view["view_manifest"] = {
        "view_version": EVIDENCE_VIEW_VERSION,
        "view_content_hash": evidence_view_content_hash(),
        "patient_span_token_budget": patient_span_token_budget,
        "per_type_floor": _PER_TYPE_FLOOR,
        "patient_spans_total": len(payload.get("patient_evidence", [])),
        "patient_spans_selected": len(selected_spans),
        "estimated_view_tokens": _estimate_tokens(
            {key: view[key] for key in view if key != "view_manifest"}
        ),
    }
    return view
