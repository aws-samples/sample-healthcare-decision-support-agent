"""Shared evidence spans, claim ledger, and three-state contract verdicts.

Generation and evaluation use the same raw-evidence types. Audit answer keys and
paper benchmark cases are deliberately not part of the public runtime schema.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Shared vocabulary -------------------------------------------------------

EvidenceContractVerdict = Literal[
    "satisfies_contract",
    "violates_contract",
    "abstain_insufficient_evidence",
]
"""Three-state verdict shared by both evaluator conditions (design doc §4, decision 12).

One violated claim makes the whole nudge violate the contract; unresolved required
evidence produces abstention when no violation has been established.
"""

DeterministicLayerVerdict = Literal[
    "violates_contract",
    "deterministic_checks_passed",
    "abstain_insufficient_evidence",
]
"""What the *deterministic* layer alone can conclude.

``satisfies_contract`` is deliberately absent.  That verdict is the conclusion shared by
both evaluator conditions and it requires bounded judgment of substantive support and
applicability, which no code-only check performs.  The strongest thing the deterministic
layer can say is ``deterministic_checks_passed``: every replayable check it could apply
passed and none established a violation.
"""


def case_verdict_from_deterministic(
    verdict: DeterministicLayerVerdict,
) -> EvidenceContractVerdict:
    """Map a deterministic-layer verdict onto the shared case-level verdict space.

    A deterministic violation is a violation of the evidence contract outright.
    Anything else abstains until a bounded judge has run: passing every
    replayable check is not the same as being substantively supported.
    """
    if verdict == "violates_contract":
        return "violates_contract"
    return "abstain_insufficient_evidence"


ClaimType = Literal["factual", "guideline", "recommended_action"]

# --- Evidence spans ----------------------------------------------------------


class PatientEvidenceSpan(BaseModel):
    """One exact portion of raw patient evidence.

    Raw FHIR results only.  A generated patient summary is a claim-bearing output and
    never a patient evidence span (design doc §1, decision 7).
    """

    span_id: str
    query: str = Field(description="The recorded tool query that returned this span")
    resource_type: str
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw FHIR payload as returned by the tool, unsummarised",
    )
    retrieved_at: str | None = None


class GuidelineEvidenceSpan(BaseModel):
    """One retrieved guideline passage, exact text."""

    span_id: str
    chunk_id: str
    source: str
    content: str
    section: str | None = None
    page_number: int | None = None


class CitationProvenance(BaseModel):
    """A pointer from a claim to a source location.

    Records where support is claimed to come from; it does not establish that the
    source supports the claim.
    """

    source: str
    section: str = ""
    page_number: int | None = None
    chunk_ids: list[str] = Field(default_factory=list)


class QueryCoverage(BaseModel):
    """The recorded query set an absence claim may be evaluated against.

    The deterministic verifier proves "queries Q were executed and returned no
    contraindication", never "the condition does not exist" (decision 16).
    """

    queries_executed: list[str] = Field(default_factory=list)
    coverage_rule_version: str = ""
    truncated_resource_types: list[str] = Field(
        default_factory=list,
        description=(
            "Resource types whose retrieved window was truncated — the search stopped "
            "at the FHIR page cap, or the recent-history window dropped older results. "
            "A truncated query does not count as coverage for an absence claim "
            "(coverage-v2)."
        ),
    )


# --- Claim ledger ------------------------------------------------------------


class DerivationStep(BaseModel):
    """One replayable step from evidence spans to a derived value."""

    rule: str
    inputs: list[str] = Field(default_factory=list, description="Evidence span ids")
    output: str


class ClaimLedgerEntry(BaseModel):
    """One claim mapped to its evidence spans, derivation chain, and audit status.

    ``derivation_chain`` is empty when a span states the claim directly.
    """

    claim_id: str
    claim_type: ClaimType
    claim_text: str
    evidence_span_ids: list[str] = Field(default_factory=list)
    derivation_chain: list[DerivationStep] = Field(default_factory=list)
    citation_provenance: CitationProvenance | None = None
    audit_status: Literal["admitted", "drafted", "disputed"] = "drafted"
    auditor: str | None = None
    audited_at: str | None = None


class ActionEvidenceChain(BaseModel):
    """The complete support path for a recommended action (decision 14).

    A citation alone is not an action evidence chain: the trigger, the rule, and the
    applicability and temporal conditions each need their own claims.
    """

    trigger_claim_ids: list[str] = Field(default_factory=list)
    rule_claim_ids: list[str] = Field(default_factory=list)
    applicability_claim_ids: list[str] = Field(default_factory=list)
    temporal_condition_claim_ids: list[str] = Field(default_factory=list)

    def all_claim_ids(self) -> list[str]:
        """Every claim id referenced by the chain, in link order."""
        return [
            *self.trigger_claim_ids,
            *self.rule_claim_ids,
            *self.applicability_claim_ids,
            *self.temporal_condition_claim_ids,
        ]

    def missing_links(self) -> list[str]:
        """Names of the chain links that have no claims at all."""
        links = {
            "trigger": self.trigger_claim_ids,
            "rule": self.rule_claim_ids,
            "applicability": self.applicability_claim_ids,
            "temporal_condition": self.temporal_condition_claim_ids,
        }
        return [name for name, ids in links.items() if not ids]
