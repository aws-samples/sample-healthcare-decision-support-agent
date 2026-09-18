"""Versioned fabricated-citation control built from the reader's own evidence."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .tool_ledger import ToolLedger, normalize_identifier

PERTURB_VERSION = "blog-citation-fabrication-v1"


@dataclass
class PerturbationOutcome:
    status: str
    nudge: dict[str, Any] | None = None
    reason: str = ""


def perturb_citation_fabrication(nudge: dict[str, Any], ledger: ToolLedger) -> PerturbationOutcome:
    """Replace only the citation source with one absent from retrieved evidence.

    This blog control has no dependency on the paper's reference claim ledger.
    It tests provenance, not semantic clinical support. The input and all
    retrieved passages remain unchanged.
    """
    citation = nudge.get("guideline_citation")
    if not isinstance(citation, dict) or not citation.get("source"):
        return PerturbationOutcome("inapplicable", reason="No citation to corrupt.")
    sources = {normalize_identifier(span.source) for span in ledger.guideline_evidence}
    candidates = (
        "International Consensus Recommendations for Acute Care",
        "Society Practice Guideline for Inpatient Management",
        "Clinical Standards for Hospital Care",
    )
    source = next((s for s in candidates if normalize_identifier(s) not in sources), None)
    if source is None:
        return PerturbationOutcome("inapplicable", reason="No absent candidate source.")
    changed = copy.deepcopy(nudge)
    changed["guideline_citation"]["source"] = source
    return PerturbationOutcome("constructed", nudge=changed)
