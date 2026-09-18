"""Conservative redundant-order checks over retrieved FHIR request resources."""

from __future__ import annotations

import re
from typing import Any

from .tool_ledger import ToolLedger, normalize_identifier

ORDER_RULE_VERSION = "redundant-order-v2"
_ORDER_VERB = re.compile(r"\b(?:order|initiate|start|prescribe|obtain|request)\b", re.I)
_QUALIFIED = re.compile(
    r"\b(?:repeat|recheck|serial|daily|weekly|if|unless|pending|consider|review|continue|"
    r"increase|decrease|switch|stop|hold|discontinue)\b",
    re.I,
)
_NEGATED = re.compile(r"\b(?:do not|don't|avoid|no need to|not indicated)\b", re.I)
# Only a direct imperative's object establishes what is being ordered.
_IMPERATIVE_ORDER = re.compile(
    r"^\s*(?:please\s+)?(?:order|initiate|start|prescribe|obtain|request)\s+(.+)", re.I
)
_TARGET_TERMINATOR = re.compile(
    r"\b(?:before|after|because|while|given|for|to|as|and|with)\b", re.I
)
_SENTENCE_BREAK = re.compile(r"[.!?;\n]")
_REQUEST_RESOURCE_TYPES = {"MedicationRequest", "ServiceRequest"}
_ORDER_INTENTS = {"order", "original-order"}
_MIN_LABEL_LENGTH = 5


def check_redundant_order(nudge: dict[str, Any], ledger: ToolLedger) -> dict[str, Any]:
    """Flag a direct order for an exactly named, already active request.

    Repeat timing, conditional actions, dose changes, and fuzzy code/name
    equivalence abstain. Absence of a matching order also abstains: a retrieved
    window cannot prove none exists.
    """
    text = " ".join(str(nudge.get(key) or "") for key in ("title", "description"))
    result: dict[str, Any] = {
        "check": "redundant_order",
        "rule_version": ORDER_RULE_VERSION,
        "verdict": "abstain_insufficient_evidence",
        "applicable": bool(_ORDER_VERB.search(text) or "order" in (nudge.get("action_type") or [])),
        "reason": "No explicit order action.",
        "spans_used": [],
    }
    if not result["applicable"]:
        return result
    if _QUALIFIED.search(text) or _NEGATED.search(text):
        result["reason"] = "Conditional, repeated, or modified order requires interpretation."
        return result
    span_id = _redundant_order_span(ledger, _ordered_targets(nudge))
    if span_id is None:
        result["reason"] = (
            "No provably redundant active order; missing evidence is not a clean chart."
        )
        return result
    result.update(
        verdict="violates_contract",
        reason="The same named order is active in the retrieved active encounter.",
        spans_used=[span_id],
    )
    return result


def _ordered_targets(nudge: dict[str, Any]) -> list[str]:
    """Normalized objects of the nudge's imperative order sentences.

    Context, rationale, and subsequent clauses may describe a different order,
    so only the object of a sentence that opens with an order verb counts.
    """
    targets = []
    for field in ("title", "description"):
        for sentence in _SENTENCE_BREAK.split(str(nudge.get(field) or "")):
            action = _IMPERATIVE_ORDER.match(sentence)
            if action:
                target = _TARGET_TERMINATOR.split(action.group(1), maxsplit=1)[0]
                targets.append(normalize_identifier(target))
    return targets


def _redundant_order_span(ledger: ToolLedger, targets: list[str]) -> str | None:
    """Span id of the first active order that names a target inside an active encounter."""
    active_encounters = _active_encounter_refs(ledger)
    for span in ledger.patient_evidence:
        resource = span.content
        if not _is_active_order(resource) or not _names_a_target(resource, targets):
            continue
        if _encounter_ref(resource) in active_encounters:
            return span.span_id
    return None


def _active_encounter_refs(ledger: ToolLedger) -> set[str]:
    return {
        f"Encounter/{item.content.get('id')}"
        for item in ledger.patient_evidence
        if item.content.get("resourceType") == "Encounter"
        and item.content.get("status") == "in-progress"
    }


def _is_active_order(resource: dict[str, Any]) -> bool:
    if resource.get("resourceType") not in _REQUEST_RESOURCE_TYPES:
        return False
    if resource.get("status") != "active" or resource.get("intent") not in _ORDER_INTENTS:
        return False
    do_not_perform = resource.get("doNotPerform")
    return not (do_not_perform is True or do_not_perform == "true")


def _names_a_target(resource: dict[str, Any], targets: list[str]) -> bool:
    return any(
        len(label) >= _MIN_LABEL_LENGTH and label in targets
        for label in (normalize_identifier(name) for name in _order_names(resource))
    )


def _order_names(resource: dict[str, Any]) -> list[Any]:
    """Text and coding displays of what a request resource orders."""
    concept_key = (
        "medicationCodeableConcept"
        if resource.get("resourceType") == "MedicationRequest"
        else "code"
    )
    concept = resource.get(concept_key)
    if not isinstance(concept, dict):
        return []
    names = [concept.get("text")]
    names.extend(c.get("display") for c in concept.get("coding", []) if isinstance(c, dict))
    return names


def _encounter_ref(resource: dict[str, Any]) -> str | None:
    encounter = resource.get("encounter")
    return encounter.get("reference") if isinstance(encounter, dict) else None
