"""Evidence contract: shared schema, deterministic verifier, and contract-enforcing steering.

The evidence contract is the required link between a nudge and its patient evidence,
guideline evidence, citation provenance, temporal conditions, and proposed action.  This
package holds the parts of that contract shared by generation time and evaluation time:

* :mod:`.schema` — the shared evidence schema, including the three-state
  ``EvidenceContractVerdict``, claim ledger entries, derivation chains, query coverage,
  and the action evidence chain;
* :mod:`.rules` — versioned rule artifacts (medication status, absence coverage) with
  content hashes for run provenance;
* :mod:`.tool_ledger` — raw tool results turned into evidence spans and query coverage;
* :mod:`.draft_ledger` — code-only drafted claim ledger used at generation time;
* :mod:`.verifier` — the deterministic verifier (no model calls anywhere);
* :mod:`.steering` — bounded retries with deterministic and optional entailment checks;
* :mod:`.case_naming` — the Strands Evals case-name namespace;
* :mod:`.artifact_paths` — the guard keeping persisted patient evidence out of the tree.

The blog evaluation runner and citation-control judge live in ``evals/``.
"""

from .artifact_paths import ArtifactPathError, ensure_output_dir_outside_repo
from .case_naming import (
    CaseName,
    CaseNameError,
    build_case_name,
    parse_case_name,
    patient_short,
    result_store_subpath,
)
from .draft_ledger import build_draft_claim_ledger
from .rules import (
    MedicationStatusRule,
    QueryCoverageRule,
    load_medication_status_rule,
    load_query_coverage_rule,
    rule_content_hashes,
    rule_versions,
)
from .schema import (
    ActionEvidenceChain,
    CitationProvenance,
    ClaimLedgerEntry,
    DerivationStep,
    EvidenceContractVerdict,
    GuidelineEvidenceSpan,
    PatientEvidenceSpan,
    QueryCoverage,
)
from .steering import (
    ContractEnforcingSteeringHandler,
    GeneratorConfiguration,
    SteeringAttempt,
    extract_drafted_nudges,
)
from .tool_ledger import (
    ToolLedger,
    append_injected_guideline_passages,
    ledger_from_steering_context,
    parse_guideline_passages,
)
from .verifier import (
    CheckResult,
    DeterministicVerifier,
    DeterministicVerifierReport,
)

__all__ = [
    "ActionEvidenceChain",
    "ArtifactPathError",
    "CaseName",
    "CaseNameError",
    "CheckResult",
    "CitationProvenance",
    "ClaimLedgerEntry",
    "ContractEnforcingSteeringHandler",
    "DerivationStep",
    "DeterministicVerifier",
    "DeterministicVerifierReport",
    "EvidenceContractVerdict",
    "GeneratorConfiguration",
    "GuidelineEvidenceSpan",
    "MedicationStatusRule",
    "PatientEvidenceSpan",
    "QueryCoverage",
    "QueryCoverageRule",
    "SteeringAttempt",
    "ToolLedger",
    "append_injected_guideline_passages",
    "build_case_name",
    "build_draft_claim_ledger",
    "ensure_output_dir_outside_repo",
    "extract_drafted_nudges",
    "ledger_from_steering_context",
    "load_medication_status_rule",
    "load_query_coverage_rule",
    "parse_case_name",
    "parse_guideline_passages",
    "patient_short",
    "result_store_subpath",
    "rule_content_hashes",
    "rule_versions",
]
