"""Steering entailment check: span-entailment for guideline-attributed claims.

The v3 exact-span audit excluded 15/22 candidates for one failure mode the
deterministic verifier cannot express: the nudge cites the right source and page but
attributes directives (monitoring frequencies, "document X", hold/switch instructions)
that the cited span never states.  Whether a span *entails* a directive is not a
replayable string check, so this check calls a model at generation time.

**This is a generator-side component, not an evaluator component.**  It is distinct
from the bounded judge by construction: its prompt is defined here, versioned and
content-hashed into the generator configuration, and it shares no code or prompt with
the evidence contract evaluator.  Reusing the bounded judge here would mean every
benchmark original had been pre-filtered by the evaluator under test — a circularity
the benchmark cannot carry.

Failure semantics match the deterministic verifier's: a non-entailed claim is a
contract failure that feeds the Guide retry loop and, on retry exhaustion, the
``contract_failures_unresolved`` exclusion.  A model or parse error never fails a
draft — the check reports itself as errored and steering proceeds with the
deterministic verdict alone, so an outage degrades enforcement, not correctness.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .schema import ClaimLedgerEntry
from .tool_ledger import ToolLedger

logger = logging.getLogger(__name__)

ENTAILMENT_PROMPT_VERSION = "steering-entailment-v1"

#: Claim types the check applies to. Factual claims are already covered by the
#: deterministic verifier against patient evidence; the model check earns its cost only
#: where determinism cannot reach (span -> directive entailment).
ENTAILED_CLAIM_TYPES = ("guideline", "recommended_action")

SYSTEM_PROMPT = """\
You are a strict entailment checker for clinical guideline citations. You are given
claims from a drafted clinical nudge and the exact guideline passages each claim cites.
For each claim, decide whether the cited passages STATE the content the claim attributes
to the guideline.

Rules:
- A claim is entailed only if every directive it attributes to the guideline (actions,
  monitoring frequencies, thresholds, documentation requirements, hold/switch/dose
  instructions) is stated by the cited passage text. Reasonable paraphrase is fine;
  content the passage does not state is not.
- General clinical plausibility is NOT entailment. If the directive is standard practice
  but the cited passages do not state it, the claim is not entailed.
- Judge only guideline attribution. Do not judge whether the claim is clinically
  appropriate for the patient.

Respond with a JSON array only, no other text. One object per claim:
[{"claim_id": "<id>", "entailed": true|false, "unsupported_content": "<the attributed \
content the cited passages do not state, empty string when entailed>"}]
"""


@dataclass(frozen=True)
class EntailmentOutcome:
    """Result of one entailment pass over a drafted nudge's claims."""

    failures: list[str]
    checked_claim_ids: list[str]
    errored: bool
    error_detail: str = ""


def entailment_prompt_hash() -> str:
    """Content hash of the versioned prompt, for the generator configuration record."""
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


class SteeringEntailmentCheck:
    """Model-backed span-entailment check for the contract-enforcing steering handler.

    One Bedrock ``converse`` call per drafted nudge, covering every guideline and
    recommended-action claim that cites guideline evidence spans present in the tool
    ledger.  Claims without cited spans are left to the deterministic citation checks.
    """

    def __init__(self, model_id: str, region: str | None = None) -> None:
        self.model_id = model_id
        self.region = region
        self._client = None

    @property
    def client(self) -> Any:  # boto3 client has no importable static type
        """Lazily created bedrock-runtime client, so import needs no AWS session."""
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def config_record(self) -> dict[str, str]:
        """Provenance fields for the generator configuration."""
        return {
            "entailment_model_id": self.model_id,
            "entailment_prompt_version": ENTAILMENT_PROMPT_VERSION,
            "entailment_prompt_hash": entailment_prompt_hash(),
        }

    @staticmethod
    def _eligible_claims(
        claims: list[ClaimLedgerEntry], ledger: ToolLedger
    ) -> list[tuple[ClaimLedgerEntry, str]]:
        """Claims this check applies to, paired with their cited span text."""
        guideline_spans = {span.span_id: span.content for span in ledger.guideline_evidence}
        eligible: list[tuple[ClaimLedgerEntry, str]] = []
        for claim in claims:
            if claim.claim_type not in ENTAILED_CLAIM_TYPES:
                continue
            if claim.citation_provenance is None:
                continue
            cited = [
                guideline_spans[span_id]
                for span_id in claim.evidence_span_ids
                if span_id in guideline_spans
            ]
            if not cited:
                # No resolvable guideline span: the deterministic source/passage/
                # citation checks own that failure mode.
                continue
            eligible.append((claim, "\n---\n".join(cited)))
        return eligible

    @staticmethod
    def _build_user_prompt(eligible: list[tuple[ClaimLedgerEntry, str]]) -> str:
        parts: list[str] = []
        for claim, span_text in eligible:
            parts.append(
                f"CLAIM {claim.claim_id} ({claim.claim_type}):\n{claim.claim_text}\n\n"
                f"CITED PASSAGES for {claim.claim_id}:\n{span_text}"
            )
        parts.append("Return the JSON array now.")
        return "\n\n====\n\n".join(parts)

    @staticmethod
    def _parse_verdicts(text: str) -> list[dict[str, object]]:
        """Parse the model's JSON array, tolerating surrounding prose."""
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("no JSON array in the entailment response")
        parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, list):
            raise ValueError("entailment response is not a JSON array")
        return [item for item in parsed if isinstance(item, dict)]

    def check_nudge(
        self,
        claims: list[ClaimLedgerEntry],
        ledger: ToolLedger,
    ) -> EntailmentOutcome:
        """Check one drafted nudge's guideline-attributed claims against cited spans."""
        eligible = self._eligible_claims(claims, ledger)
        if not eligible:
            return EntailmentOutcome(failures=[], checked_claim_ids=[], errored=False)
        checked_ids = [claim.claim_id for claim, _ in eligible]

        try:
            response = self.client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[
                    {"role": "user", "content": [{"text": self._build_user_prompt(eligible)}]}
                ],
                # No temperature: Opus 5 rejects the parameter as deprecated
                # (ValidationException on Converse).
                inferenceConfig={"maxTokens": 2000},
            )
            blocks = response["output"]["message"]["content"]
            text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
            verdicts = self._parse_verdicts(text)
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "steering entailment check errored; proceeding on the deterministic "
                "verdict alone: %s",
                exc,
            )
            return EntailmentOutcome(
                failures=[],
                checked_claim_ids=checked_ids,
                errored=True,
                error_detail=f"{type(exc).__name__}: {exc}",
            )

        by_id = {str(item.get("claim_id")): item for item in verdicts}
        failures: list[str] = []
        unanswered: list[str] = []
        for claim, _ in eligible:
            verdict = by_id.get(claim.claim_id)
            if verdict is None or not isinstance(verdict.get("entailed"), bool):
                unanswered.append(claim.claim_id)
                continue
            if verdict["entailed"]:
                continue
            unsupported = str(verdict.get("unsupported_content") or "").strip()
            failures.append(
                f"[steering_entailment] claim {claim.claim_id}: the cited guideline "
                "passages do not state the attributed content"
                + (f": {unsupported}" if unsupported else "")
                + ". Quote or paraphrase only what the cited passage states, cite a "
                "retrieved passage that states it, or drop the attribution."
            )
        if unanswered:
            # A verdict the model failed to return is an error for those claims, not a
            # pass: say so, but never fail a claim on a missing answer.
            logger.warning("steering entailment returned no verdict for claims %s", unanswered)
            return EntailmentOutcome(
                failures=failures,
                checked_claim_ids=checked_ids,
                errored=True,
                error_detail=f"no verdict for claims {unanswered}",
            )
        return EntailmentOutcome(failures=failures, checked_claim_ids=checked_ids, errored=False)
