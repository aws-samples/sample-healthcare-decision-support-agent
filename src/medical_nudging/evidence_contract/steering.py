"""Verify draft nudges against retrieved evidence before final output.

Established deterministic failures guide the model to redraft, up to a configured
retry cap. Unresolved drafts retain explicit exclusion flags. The optional,
versioned entailment checker contributes to the same retry loop; its errors are
recorded and leave the deterministic verdict in force. Configuration and outcomes
are retained with every patient run for replay and attrition reporting.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from strands.vended_plugins.steering import (
    Guide,
    LedgerProvider,
    ModelSteeringAction,
    Proceed,
    SteeringHandler,
)

from ..tools.raw_resource_channel import RawResourceChannel, default_channel
from .schema import ActionEvidenceChain, ClaimLedgerEntry, DeterministicLayerVerdict
from .draft_ledger import build_draft_claim_ledger
from .entailment import SteeringEntailmentCheck
from .rules import rule_content_hashes
from .tool_ledger import (
    ToolLedger,
    append_injected_guideline_passages,
    ledger_from_steering_context,
)
from .verifier import DeterministicVerifier, DeterministicVerifierReport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from strands.agent import Agent
    from strands.types.content import Message
    from strands.types.streaming import StopReason

logger = logging.getLogger(__name__)

DEFAULT_RETRY_CAP = 3
DEFAULT_NUDGE_TOOL_NAME = "GeneratedNudgeOutput"

UNRESOLVED_FLAG = "contract_failures_unresolved"
VERIFICATION_ERROR_FLAG = "verification_error"

#: What the handler did on one pass over a model response.
SteeringActionName = Literal[
    "proceed_no_draft",
    "proceed",
    "proceed_unresolved",
    "proceed_verification_error",
    "guide",
]

#: The verdict recorded on one pass.  ``not_applicable`` means there was no draft to
#: verify and ``not_verified`` means verification did not complete — neither is a
#: statement about the evidence, and neither may be read as a clean draft.
SteeringAttemptVerdict = Literal[
    "satisfies_contract",
    "violates_contract",
    "abstain_insufficient_evidence",
    "deterministic_checks_passed",
    "not_applicable",
    "not_verified",
]

GUIDE_PREAMBLE = (
    "Your drafted nudges do not satisfy the evidence contract. A deterministic verifier "
    "replayed your draft against the raw tool results and retrieved guideline passages "
    "and established the following contract failures. Re-draft so that every remaining "
    "claim rests on exact evidence spans you actually retrieved: correct the claim, cite "
    "a passage that is present in the retrieved evidence, or drop the claim. Do not "
    "restate an unsupported claim, and do not treat your own patient summary as evidence."
)


@dataclass(frozen=True)
class GeneratorConfiguration:
    """Versioned configuration of the contract-enforcing steered generator.

    Recorded in every output artifact so that a source nudge, and any regeneration of
    it, always names the configuration that produced it (decision 3/15).
    """

    config_version: str
    retry_cap: int = DEFAULT_RETRY_CAP
    nudge_tool_name: str = DEFAULT_NUDGE_TOOL_NAME
    corpus_version: str = ""
    model_id: str | None = None
    # Generation requirement, not a contract clause: a candidate without citation
    # provenance can never satisfy the evidence contract, so guiding on it at
    # generation time saves dead-weight candidates from reaching the validity audit.
    require_citation: bool = False
    # Model id for the steering entailment check (v4). None disables the check and
    # keeps the handler fully deterministic.
    entailment_model_id: str | None = None
    # Versioned generation-time prompt constraints, injected into the user prompt by
    # the runner. Carried here so the configuration record hashes them.
    custom_instructions: str = ""

    def as_dict(self, verifier: DeterministicVerifier) -> dict[str, Any]:
        """Serialisable configuration record, including verifier rule provenance."""
        record: dict[str, Any] = {
            "config_version": self.config_version,
            "retry_cap": self.retry_cap,
            "nudge_tool_name": self.nudge_tool_name,
            "corpus_version": self.corpus_version,
            "model_id": self.model_id,
            "require_citation": self.require_citation,
            "custom_instructions_hash": (
                hashlib.sha256(self.custom_instructions.encode("utf-8")).hexdigest()
                if self.custom_instructions
                else None
            ),
            "verifier_rule_versions": verifier.rule_versions,
            "verifier_rule_content_hashes": rule_content_hashes(),
        }
        if self.entailment_model_id:
            record.update(SteeringEntailmentCheck(self.entailment_model_id).config_record())
        else:
            record["entailment_model_id"] = None
        return record


@dataclass
class SteeringAttempt:
    """One recorded pass of the deterministic verifier over a drafted nudge set."""

    attempt: int
    action: SteeringActionName
    verdict: SteeringAttemptVerdict
    failures: list[str] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    recorded_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for persisted artifacts."""
        return {
            "attempt": self.attempt,
            "action": self.action,
            "verdict": self.verdict,
            "failures": list(self.failures),
            "reports": list(self.reports),
            "recorded_at": self.recorded_at,
        }


def _combined_verdict(
    reports: list[DeterministicVerifierReport],
    *,
    has_failures: bool,
) -> DeterministicLayerVerdict:
    """Combine per-nudge deterministic verdicts across a drafted nudge set (decision 12).

    One violated claim anywhere makes the whole draft violate the contract; otherwise an
    unresolved required check produces abstention.  A clean pass is
    ``deterministic_checks_passed``: steering cannot conclude ``satisfies_contract``,
    because that conclusion needs the bounded judge.
    """
    if has_failures:
        return "violates_contract"
    if any(report.verdict == "abstain_insufficient_evidence" for report in reports):
        return "abstain_insufficient_evidence"
    return "deterministic_checks_passed"


def extract_drafted_nudges(message: Any, nudge_tool_name: str) -> list[dict[str, Any]]:
    """Pull drafted nudges out of a model response.

    The nudge payload arrives as input to the structured-output tool, not as assistant
    text, so this reads ``toolUse`` blocks whose name matches the configured tool.
    """
    if not isinstance(message, dict):
        return []
    nudges: list[dict[str, Any]] = []
    for block in message.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        tool_use = block.get("toolUse")
        if not isinstance(tool_use, dict) or tool_use.get("name") != nudge_tool_name:
            continue
        payload = tool_use.get("input")
        if not isinstance(payload, dict):
            continue
        drafted = payload.get("nudges")
        if isinstance(drafted, list):
            nudges.extend(item for item in drafted if isinstance(item, dict))
    return nudges


class ContractEnforcingSteeringHandler(SteeringHandler):
    """``SteeringHandler`` that enforces the evidence contract at generation.

    Every replayable decision comes from :class:`DeterministicVerifier`.  When the
    configuration names an ``entailment_model_id`` the handler additionally runs the
    versioned :class:`SteeringEntailmentCheck` over guideline-attributed claims; its
    failures join the deterministic failures in the same Guide retry loop, and an
    entailment error is recorded and degrades to the deterministic verdict alone.
    """

    name: str = "evidence_contract_steering"

    def __init__(
        self,
        *,
        config: GeneratorConfiguration,
        verifier: DeterministicVerifier | None = None,
        raw_channel: RawResourceChannel | None = None,
        injected_guideline_context: str | None = None,
        injected_query_label: str = "singlepass_retrieval",
    ) -> None:
        """Initialise the handler with a versioned generator configuration.

        Args:
            config: Versioned generator configuration (retry cap, nudge tool name).
            verifier: Deterministic verifier; a default instance loads the current
                versioned rule artifacts.
            raw_channel: Side channel carrying the raw FHIR resources the patient tool
                retrieved; defaults to the process-local channel the tool publishes to.
            injected_guideline_context: Formatted ``=== SOURCE ===`` passages that were
                injected into the generation context instead of being tool-retrieved
                (single-pass RAG condition). Appended to every ledger this handler
                builds so the verifier sees the same guideline evidence the model saw.
            injected_query_label: Query string recorded in the ledger's coverage record
                for the injected retrieval.
        """
        super().__init__(context_providers=[LedgerProvider()])
        self.config = config
        self.verifier = verifier or DeterministicVerifier()
        self.raw_channel = raw_channel
        self.injected_guideline_context = injected_guideline_context
        self.injected_query_label = injected_query_label
        self.entailment_check = (
            SteeringEntailmentCheck(config.entailment_model_id)
            if config.entailment_model_id
            else None
        )
        self.attempts: list[SteeringAttempt] = []
        # The reports steering actually acted on, kept so the runner can persist them
        # instead of re-deriving claims and re-running verify (which can diverge).
        self.last_reports: list[DeterministicVerifierReport] = []
        self.last_ledger: ToolLedger | None = None
        # The drafted claim ledgers those reports were produced from, in draft order.
        self.last_draft_ledgers: list[tuple[list[ClaimLedgerEntry], ActionEvidenceChain]] = []
        # Per-nudge entailment failures for the last verified draft set, in draft order.
        self.last_entailment_failures: list[list[str]] = []
        self.last_generation_failures: list[list[str]] = []
        self.entailment_errors: list[str] = []

    # --- recorded outcome ---------------------------------------------------

    @property
    def guided_attempts(self) -> int:
        """How many times the handler has discarded a draft and retried."""
        return sum(1 for attempt in self.attempts if attempt.action == "guide")

    @property
    def contract_failures_unresolved(self) -> bool:
        """True when the last verified draft still carried deterministic failures."""
        verified = [attempt for attempt in self.attempts if attempt.action != "proceed_no_draft"]
        return bool(verified) and verified[-1].action == "proceed_unresolved"

    @property
    def verification_error(self) -> bool:
        """True when a verification pass raised instead of producing a verdict."""
        return any(attempt.action == "proceed_verification_error" for attempt in self.attempts)

    def outcome_record(self) -> dict[str, Any]:
        """Patient-level steering outcome, recorded once per patient run.

        This is *not* a per-nudge record: the counters here are run totals.  Copying it
        into every nudge artifact multiplies the retry totals by the nudge count and
        attributes a patient-level exclusion flag to that patient's clean nudges.  Use
        :meth:`nudge_outcome_record` for the per-nudge fields.
        """
        verified = [attempt for attempt in self.attempts if attempt.action != "proceed_no_draft"]
        flags: list[str] = []
        if self.contract_failures_unresolved:
            flags.append(UNRESOLVED_FLAG)
        if self.verification_error:
            flags.append(VERIFICATION_ERROR_FLAG)
        # "nothing was verified" is not a verified abstention.  ``not_verified`` says the
        # deterministic layer never reached this draft, which is a different claim from
        # "the deterministic layer resolved nothing against it".
        final_verdict: SteeringAttemptVerdict = verified[-1].verdict if verified else "not_verified"
        return {
            "generator_config": self.config.as_dict(self.verifier),
            "verified_draft_count": len(verified),
            "guided_attempt_count": self.guided_attempts,
            "retry_cap": self.config.retry_cap,
            "flags": flags,
            "verification_error": self.verification_error,
            "entailment_errors": list(self.entailment_errors),
            "final_verdict": final_verdict,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }

    def nudge_outcome_record(self, nudge_index: int) -> dict[str, Any]:
        """Per-nudge steering record: this nudge's own report and its own flags.

        ``nudge_index`` is the zero-based position of the nudge in the last verified
        draft set.  Only flags established for *this* nudge appear here.
        """
        reports = self.last_reports
        if nudge_index < 0 or nudge_index >= len(reports):
            return {
                "verified": False,
                "flags": [VERIFICATION_ERROR_FLAG] if self.verification_error else [],
                "verification_error": self.verification_error,
                "deterministic_verdict": "not_verified",
                "case_verdict": "abstain_insufficient_evidence",
                "failures": [],
                "report": None,
            }
        report = reports[nudge_index]
        entailment_failures = (
            list(self.last_entailment_failures[nudge_index])
            if nudge_index < len(self.last_entailment_failures)
            else []
        )
        generation_failures = (
            list(self.last_generation_failures[nudge_index])
            if nudge_index < len(self.last_generation_failures)
            else []
        )
        has_failures = (
            bool(report.failures) or bool(entailment_failures) or bool(generation_failures)
        )
        unresolved = has_failures and self.contract_failures_unresolved
        return {
            "verified": True,
            "flags": [UNRESOLVED_FLAG] if unresolved else [],
            "verification_error": False,
            "deterministic_verdict": report.verdict,
            "case_verdict": report.case_verdict,
            "failures": [*report.failure_summary(), *entailment_failures, *generation_failures],
            "entailment_failures": entailment_failures,
            "generation_requirement_failures": generation_failures,
            "report": report.as_dict(),
        }

    def reset(self) -> None:
        """Clear recorded attempts between patients so records stay per-run."""
        self.attempts = []
        self.last_reports = []
        self.last_ledger = None
        self.last_draft_ledgers = []
        self.last_entailment_failures = []
        self.last_generation_failures = []
        self.entailment_errors = []

    # --- verification -------------------------------------------------------

    def build_tool_ledger(self) -> ToolLedger:
        """Build the tool ledger from the captured steering context.

        The FHIR tool returns formatted prose to the model, so the raw resources come
        from the side channel instead (:mod:`medical_nudging.tools.raw_resource_channel`).
        When the channel is empty the ledger falls back to parsing the formatted output
        and records the reduced fidelity, so a degraded ledger is visible rather than
        indistinguishable from raw evidence.
        """
        raw = (self.raw_channel if self.raw_channel is not None else default_channel()).resources()
        ledger = ledger_from_steering_context(
            self.steering_context.data.get("ledger"),
            coverage_rule_version=self.verifier.query_coverage_rule.rule_version,
            raw_resources=raw or None,
        )
        if self.injected_guideline_context:
            append_injected_guideline_passages(
                ledger,
                self.injected_guideline_context,
                query=self.injected_query_label,
            )
        return ledger

    def verify_drafts(
        self,
        nudges: list[dict[str, Any]],
        ledger: ToolLedger,
    ) -> list[tuple[dict[str, Any], DeterministicVerifierReport]]:
        """Verify every drafted nudge against the tool ledger.

        Records the drafted claim ledgers alongside the reports so a runner can persist
        exactly what steering acted on instead of re-deriving claims and re-verifying,
        which can diverge from the verdict that actually drove the retry decision.
        """
        verified: list[tuple[dict[str, Any], DeterministicVerifierReport]] = []
        drafted: list[tuple[list[ClaimLedgerEntry], ActionEvidenceChain]] = []
        for index, nudge in enumerate(nudges):
            claims, chain = build_draft_claim_ledger(
                nudge, ledger, claim_id_prefix=f"n{index + 1}c"
            )
            report = self.verifier.verify(
                claims=claims,
                ledger=ledger,
                action_evidence_chain=chain,
            )
            verified.append((nudge, report))
            drafted.append((claims, chain))
        self.last_draft_ledgers = drafted
        return verified

    @staticmethod
    def build_guidance(
        verified: list[tuple[dict[str, Any], DeterministicVerifierReport]],
        extra_failures: list[list[str]] | None = None,
    ) -> str:
        """Render the specific contract failures as retry feedback.

        ``extra_failures`` carries the per-nudge non-deterministic failures (entailment,
        generation requirements) so the feedback names the nudge each one belongs to.
        """
        per_nudge_extra = extra_failures or []
        lines = [GUIDE_PREAMBLE, ""]
        for index, (nudge, report) in enumerate(verified, start=1):
            failures = list(report.failure_summary())
            if index - 1 < len(per_nudge_extra):
                failures.extend(per_nudge_extra[index - 1])
            if not failures:
                continue
            title = str(nudge.get("title") or f"nudge {index}")
            lines.append(f"Nudge {index} — {title}:")
            lines.extend(f"  - {failure}" for failure in failures)
        return "\n".join(lines)

    # --- SteeringHandler hook ----------------------------------------------

    async def steer_after_model(
        self,
        *,
        agent: "Agent",
        message: "Message",
        stop_reason: "StopReason",
        **kwargs: Any,
    ) -> ModelSteeringAction:
        """Verify the drafted nudge against the tool ledger, then proceed or guide."""
        nudges = extract_drafted_nudges(message, self.config.nudge_tool_name)
        if not nudges:
            self._record_attempt("proceed_no_draft", "not_applicable")
            return Proceed(reason="No drafted nudge in this model response")

        # The SDK's hook wrapper catches handler exceptions and logs them at debug, which
        # would let a crash in ledger build or verification look like a clean draft.  Own
        # the failure here instead: log loudly and record it as an unverified draft.
        try:
            ledger = self.build_tool_ledger()
            verified = self.verify_drafts(nudges, ledger)
        except Exception:
            logger.exception(
                "config=<%s> | evidence-contract verification failed; recording the draft as "
                "unverified rather than clean",
                self.config.config_version,
            )
            self._forget_verification()
            self._record_attempt("proceed_verification_error", "not_verified")
            return Proceed(
                reason=(
                    "Evidence-contract verification raised; the draft is recorded "
                    f"{VERIFICATION_ERROR_FLAG} and unverified, not clean"
                )
            )

        self.last_ledger = ledger
        self.last_reports = [report for _, report in verified]
        deterministic_failures = [
            failure for _, report in verified for failure in report.failure_summary()
        ]
        self.last_entailment_failures = self._entailment_failures(len(verified), ledger)
        self.last_generation_failures = self._generation_failures(verified)
        extra_failures = [
            [*self.last_generation_failures[index], *self.last_entailment_failures[index]]
            for index in range(len(verified))
        ]
        failures = [
            *deterministic_failures,
            *(f for per_nudge in extra_failures for f in per_nudge),
        ]
        # The recorded verdict is the *deterministic* layer's conclusion only: an
        # entailment or generation-requirement failure drives the retry decision but
        # must never masquerade as a replayable deterministic violation.
        verdict = _combined_verdict(
            [report for _, report in verified], has_failures=bool(deterministic_failures)
        )
        reports = [report.as_dict() for _, report in verified]

        if not failures:
            self._record_attempt("proceed", verdict, reports=reports)
            logger.info(
                "config=<%s> | drafted nudges established no deterministic contract failure",
                self.config.config_version,
            )
            return Proceed(reason="Deterministic verifier established no contract failure")

        if self.guided_attempts >= self.config.retry_cap:
            self._record_attempt("proceed_unresolved", verdict, failures=failures, reports=reports)
            logger.warning(
                "config=<%s>, retry_cap=<%d> | emitting nudge flagged %s with %d failure(s)",
                self.config.config_version,
                self.config.retry_cap,
                UNRESOLVED_FLAG,
                len(failures),
            )
            return Proceed(
                reason=(
                    f"Retry cap {self.config.retry_cap} exhausted; emitting flagged "
                    f"{UNRESOLVED_FLAG} as an excluded source nudge"
                )
            )

        self._record_attempt("guide", verdict, failures=failures, reports=reports)
        logger.info(
            "config=<%s>, guided=<%d>/<%d> | guiding retry on %d contract failure(s)",
            self.config.config_version,
            self.guided_attempts,
            self.config.retry_cap,
            len(failures),
        )
        return Guide(reason=self.build_guidance(verified, extra_failures))

    def _record_attempt(
        self,
        action: SteeringActionName,
        verdict: SteeringAttemptVerdict,
        *,
        failures: list[str] | None = None,
        reports: list[dict[str, Any]] | None = None,
    ) -> None:
        self.attempts.append(
            SteeringAttempt(
                attempt=len(self.attempts) + 1,
                action=action,
                verdict=verdict,
                failures=failures or [],
                reports=reports or [],
            )
        )

    def _forget_verification(self) -> None:
        """Drop every per-draft record so a failed verification cannot read as clean."""
        self.last_reports = []
        self.last_ledger = None
        self.last_draft_ledgers = []
        self.last_entailment_failures = []
        self.last_generation_failures = []

    def _entailment_failures(self, draft_count: int, ledger: ToolLedger) -> list[list[str]]:
        """Steering entailment check (v4): guideline-attributed content must be stated
        by the cited spans.  An errored check is recorded and contributes nothing —
        it must never pass or fail a claim."""
        failures: list[list[str]] = [[] for _ in range(draft_count)]
        if self.entailment_check is None:
            return failures
        for index, (claims, _chain) in enumerate(self.last_draft_ledgers):
            outcome = self.entailment_check.check_nudge(claims, ledger)
            failures[index] = list(outcome.failures)
            if outcome.errored:
                self.entailment_errors.append(
                    f"attempt {len(self.attempts) + 1}, nudge {index + 1}: "
                    f"{outcome.error_detail}"
                )
        return failures

    def _generation_failures(
        self, verified: list[tuple[dict[str, Any], DeterministicVerifierReport]]
    ) -> list[list[str]]:
        """Per-nudge generation requirements, distinct from the verifier's verdict.

        Missing citation provenance is abstention for the contract (decision 20), but a
        candidate without it is dead weight for the audit, so guide a redraft.  Recorded
        per nudge so the Guide feedback names the nudge it belongs to.
        """
        failures: list[list[str]] = [[] for _ in verified]
        if not self.config.require_citation:
            return failures
        for index, (nudge, _report) in enumerate(verified):
            if nudge.get("guideline_citation"):
                continue
            title = str(nudge.get("title", f"nudge {index}"))[:80]
            failures[index].append(
                f"[generation_requirement] nudge '{title}' names no "
                "guideline_citation (source/section/page). Cite the retrieved "
                "guideline passage that supports the recommendation, or drop "
                "the nudge if no retrieved passage supports it."
            )
        return failures
