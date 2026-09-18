"""Tool ledger: raw tool results turned into evidence spans and query coverage.

The contract-enforcing steering handler cannot verify a drafted nudge against the
model's own narration — it has to verify against what the tools actually returned.  A
:class:`ToolLedger` is that captured record: raw FHIR results become patient evidence
spans, retrieved passages become guideline evidence spans, and the executed tool
queries become the recorded query coverage an absence claim may be checked against.

:func:`ledger_from_steering_context` builds it during live generation from the ledger
that Strands' ``LedgerProvider`` accumulates on tool-call hook events.

A generated patient summary is never admitted here.  Only tool results are.

**Ledger fidelity.**  ``query_patient_fhir`` returns formatted prose, not raw JSON, so
a ledger built from its return value alone would be a single blob span — document-level
provenance, which the evidence contract forbids.  Every ledger therefore records the
fidelity of the patient evidence it holds:

``raw_fhir``
    Exact raw FHIR resources, one span each, taken from a JSON payload or from the
    :mod:`medical_nudging.tools.raw_resource_channel` side channel.  Every check runs.
``formatted_summary``
    The tool's ``### ResourceType (n results)`` output parsed into one span per
    summarised resource.  Values and dates are preserved verbatim by the formatter, so
    value and date checks still run; checks that need raw FHIR fields (medication
    status) are not evaluable.
``degraded``
    A payload that is neither raw FHIR nor the recognised formatted output.  It is kept
    whole as one ``RawToolResult`` span and every patient-side check that would depend
    on field-level evidence is not evaluable — the verifier never silently proceeds
    against evidence it could not decompose.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .schema import GuidelineEvidenceSpan, PatientEvidenceSpan, QueryCoverage

LedgerFidelity = Literal["raw_fhir", "formatted_summary", "degraded"]

_FIDELITY_ORDER: dict[str, int] = {"raw_fhir": 0, "formatted_summary": 1, "degraded": 2}

DEFAULT_PATIENT_TOOL_NAMES = frozenset({"query_patient_fhir", "get_patient_data"})
DEFAULT_GUIDELINE_TOOL_NAMES = frozenset(
    {"search_guidelines", "search_guidelines_custom", "list_guidelines", "list_sources"}
)

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_PASSAGE_HEADER_RE = re.compile(r"^===\s*(?P<source>.+?)\s*===\s*$")
_FIELD_RE = re.compile(
    r"^(?P<key>Section|Section Number|Page|Relevance)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE,
)
# The formatted ``query_patient_fhir`` output: one heading per queried resource type,
# one bullet per resource, and an explicit line for a type that returned nothing.
_SECTION_HEADER_RE = re.compile(r"^###\s+(?P<resource_type>[A-Za-z]+)\s*(?:\(.*\))?\s*$")
_EMPTY_SECTION_RE = re.compile(
    r"^No\s+(?P<resource_type>[A-Za-z]+)\s+resources\s+found\.?$", re.IGNORECASE
)
# Truncation markers emitted by query_patient_fhir. Either way the retrieved window is
# incomplete, so the query does not count as coverage for an absence claim
# (coverage-v2): the search stopped at the FHIR page cap, or the recent-history window
# dropped older results the server did return.
_PAGE_CAP_RE = re.compile(r"^Page cap reached for\s+(?P<resource_type>[A-Za-z]+)\b", re.MULTILINE)
_HISTORY_WINDOW_RE = re.compile(
    r"^History window excluded \d+ older\s+(?P<resource_type>[A-Za-z]+)\b", re.MULTILINE
)


def normalize_identifier(value: Any) -> str:
    """Fold a source or section label for tolerant identity comparisons."""
    return _NORMALIZE_RE.sub(" ", str(value or "").lower()).strip()


def citation_page(citation: Any) -> int | None:
    """Read either page spelling used by nudges, traces, and persisted passages.

    A key that is present but ``None`` falls through to the other spelling, so
    ``{"page_number": None, "page": 62}`` reads as page 62 rather than as no page.
    """
    if not isinstance(citation, dict):
        return None
    value = citation.get("page_number")
    if value is None:
        value = citation.get("page")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def citation_matches_passage(citation: Any, passage: Any) -> bool:
    """Whether citation metadata identifies one supplied passage.

    Shared by the deterministic verifier's passage-identity check and the citation-control
    construction, so "the citation resolves" means one thing in this repository.
    Source, page, and section must all be present and agree: a passage with no recorded
    section cannot confirm a cited section, and treating a missing side as a match is
    what let a fabricated citation resolve against a section-less chunk.
    """
    if not isinstance(citation, dict) or not isinstance(passage, dict):
        return False
    cited_source = normalize_identifier(citation.get("source"))
    if not cited_source or cited_source != normalize_identifier(passage.get("source")):
        return False
    cited_page = citation_page(citation)
    if cited_page is None or cited_page != citation_page(passage):
        return False
    cited_section = normalize_identifier(citation.get("section"))
    passage_section = normalize_identifier(passage.get("section"))
    if not cited_section or not passage_section:
        return False
    return cited_section in passage_section or passage_section in cited_section


def synthesize_chunk_id(
    *,
    source: str,
    section: str | None,
    page_number: int | None,
    content: str,
) -> str:
    """Derive a deterministic chunk id for a retrieved passage.

    The guideline search tool returns formatted passages without the corpus chunk id
    the design's schema names, so the ledger content-addresses each passage instead:
    the same passage text from the same source/section/page always yields the same
    chunk id, which is what citation resolution needs.
    """
    digest = hashlib.sha256(f"{normalize_identifier(section)}|{content}".encode("utf-8"))
    page = "na" if page_number is None else str(page_number)
    return f"{_NORMALIZE_RE.sub('-', source.lower()).strip('-')}:p{page}:{digest.hexdigest()[:12]}"


@dataclass
class ToolLedger:
    """Captured raw tool results for one generation attempt."""

    patient_evidence: list[PatientEvidenceSpan] = field(default_factory=list)
    guideline_evidence: list[GuidelineEvidenceSpan] = field(default_factory=list)
    queries_executed: list[str] = field(default_factory=list)
    coverage_rule_version: str = ""
    patient_evidence_fidelity: LedgerFidelity = "raw_fhir"
    fidelity_reasons: list[str] = field(default_factory=list)
    absent_resource_types: list[str] = field(default_factory=list)
    truncated_resource_types: list[str] = field(default_factory=list)

    def note_fidelity(self, fidelity: LedgerFidelity, reason: str) -> None:
        """Record the fidelity of one admitted payload, keeping the worst seen.

        A ledger is only as trustworthy as its weakest patient-evidence payload, so the
        recorded fidelity never improves once something has been degraded.
        """
        if _FIDELITY_ORDER[fidelity] > _FIDELITY_ORDER[self.patient_evidence_fidelity]:
            self.patient_evidence_fidelity = fidelity
        if fidelity != "raw_fhir" and reason not in self.fidelity_reasons:
            self.fidelity_reasons.append(reason)

    @property
    def has_raw_fhir_resources(self) -> bool:
        """True when patient evidence holds exact raw FHIR resources."""
        return self.patient_evidence_fidelity == "raw_fhir"

    @property
    def is_degraded(self) -> bool:
        """True when a patient-evidence payload could not be decomposed at all."""
        return self.patient_evidence_fidelity == "degraded"

    def fidelity_record(self) -> dict[str, Any]:
        """Serialisable fidelity record for persisted artifacts and audit trails."""
        return {
            "patient_evidence_fidelity": self.patient_evidence_fidelity,
            "fidelity_reasons": list(self.fidelity_reasons),
            "absent_resource_types": list(self.absent_resource_types),
        }

    def query_coverage(self) -> QueryCoverage:
        """The recorded query coverage for absence claims."""
        return QueryCoverage(
            queries_executed=list(self.queries_executed),
            coverage_rule_version=self.coverage_rule_version,
            truncated_resource_types=list(self.truncated_resource_types),
        )

    @property
    def is_empty(self) -> bool:
        """True when no tool returned any evidence."""
        return not self.patient_evidence and not self.guideline_evidence

    def guideline_chunk_ids(self) -> set[str]:
        """Chunk ids of every supplied guideline passage."""
        return {span.chunk_id for span in self.guideline_evidence}

    def span_ids(self) -> set[str]:
        """Every evidence span id in the ledger."""
        return {span.span_id for span in self.patient_evidence} | {
            span.span_id for span in self.guideline_evidence
        }

    def patient_evidence_text(self) -> str:
        """Flattened patient evidence text, for value and date containment checks."""
        parts: list[str] = []
        for span in self.patient_evidence:
            content = span.content
            text = content.get("text")
            parts.append(text if isinstance(text, str) else json.dumps(content, sort_keys=True))
        return "\n".join(parts)

    def medication_resources(self) -> list[dict[str, Any]]:
        """Raw FHIR medication resources captured in the patient evidence spans."""
        wanted = {
            "medicationrequest",
            "medicationstatement",
            "medicationdispense",
            "medicationadministration",
        }
        return [
            span.content
            for span in self.patient_evidence
            if str(span.content.get("resourceType", "")).lower() in wanted
        ]


def _content_blocks_to_text(blocks: Any) -> list[str]:
    """Flatten Strands tool-result content blocks to text payloads."""
    if not isinstance(blocks, list):
        return []
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
            continue
        payload = block.get("json")
        if payload is not None:
            texts.append(json.dumps(payload, sort_keys=True))
    return texts


def parse_guideline_passages(text: str) -> list[dict[str, Any]]:
    """Parse the ``=== SOURCE ===`` passage format the guideline tools emit.

    Returns one dict per passage with ``source``, ``section``, ``section_number``,
    ``page``, ``relevance``, and ``content`` keys.  Unparseable text yields no
    passages rather than a partial guess.
    """
    passages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            return
        current["content"] = "\n".join(body).strip()
        if current["content"]:
            passages.append(current)

    for line in text.splitlines():
        header = _PASSAGE_HEADER_RE.match(line.strip())
        if header:
            flush()
            current = {
                "source": header.group("source"),
                "section": None,
                "section_number": None,
                "page": None,
                "relevance": None,
            }
            body = []
            continue
        if current is None:
            continue
        matched_field = _FIELD_RE.match(line.strip())
        if matched_field and not body:
            key = matched_field.group("key").lower()
            value = matched_field.group("value").strip()
            if key == "section":
                current["section"] = value
            elif key == "section number":
                current["section_number"] = value
            elif key == "page":
                current["page"] = int(value) if value.isdigit() else None
            elif key == "relevance":
                try:
                    current["relevance"] = float(value)
                except ValueError:
                    current["relevance"] = None
            continue
        body.append(line)

    flush()
    return passages


def _passage_to_span(passage: dict[str, Any], span_index: int) -> GuidelineEvidenceSpan:
    """Convert one parsed or persisted passage dict into a guideline evidence span."""
    source = str(passage.get("source", "")).strip()
    section = passage.get("section")
    page = passage.get("page_number", passage.get("page"))
    page_number = int(page) if isinstance(page, (int, str)) and str(page).isdigit() else None
    content = str(passage.get("content", ""))
    chunk_id = passage.get("chunk_id") or synthesize_chunk_id(
        source=source,
        section=section if isinstance(section, str) else None,
        page_number=page_number,
        content=content,
    )
    return GuidelineEvidenceSpan(
        span_id=f"guideline-{span_index:03d}",
        chunk_id=str(chunk_id),
        source=source,
        content=content,
        section=section if isinstance(section, str) else None,
        page_number=page_number,
    )


def _fhir_resources(payload: Any) -> list[dict[str, Any]]:
    """Pull FHIR resources out of a tool result: a Bundle, a list, or one resource."""
    if isinstance(payload, list):
        resources: list[dict[str, Any]] = []
        for item in payload:
            resources.extend(_fhir_resources(item))
        return resources
    if not isinstance(payload, dict):
        return []
    if payload.get("resourceType") == "Bundle":
        entries = payload.get("entry")
        resources = []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    resources.extend(_fhir_resources(entry.get("resource")))
        return resources
    if payload.get("resourceType"):
        return [payload]
    return []


def parse_fhir_tool_output(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse the ``query_patient_fhir`` formatted output into per-resource entries.

    The tool renders one ``### ResourceType (n results)`` heading per queried type and
    one ``- summary`` bullet per resource, and an empty result as
    ``No ResourceType resources found.``.

    Returns:
        ``(entries, absent_resource_types)`` where each entry is a
        ``(resource_type, summary_line)`` pair.  An unrecognised payload yields two
        empty lists rather than a partial guess, which is what marks a ledger degraded.
    """
    entries: list[tuple[str, str]] = []
    absent: list[str] = []
    current: str | None = None
    recognised = False

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        empty = _EMPTY_SECTION_RE.match(line)
        if empty:
            recognised = True
            current = None
            resource_type = empty.group("resource_type").strip()
            if resource_type not in absent:
                absent.append(resource_type)
            continue
        heading = _SECTION_HEADER_RE.match(line)
        if heading:
            recognised = True
            current = heading.group("resource_type").strip()
            continue
        if current is None:
            continue
        if line.startswith("- "):
            summary = line[2:].strip()
            if summary:
                entries.append((current, summary))
        elif line.lower().startswith("error:"):
            # An errored query executed but returned nothing usable; coverage records
            # the attempt, evidence records nothing.
            continue

    if not recognised:
        return [], []
    return entries, absent


def _patient_spans_from_text(
    text: str,
    *,
    query: str,
    start_index: int,
    retrieved_at: str | None,
    ledger: ToolLedger,
) -> list[PatientEvidenceSpan]:
    """Build patient evidence spans from one tool result payload.

    A JSON payload is split into one span per raw FHIR resource so a claim can name the
    exact resource it rests on.  The formatted ``query_patient_fhir`` output is parsed
    into one span per summarised resource and the ledger's fidelity drops to
    ``formatted_summary``.  Anything else is kept whole as a single ``RawToolResult``
    span and the ledger is marked ``degraded`` — never dropped, never mistaken for raw
    evidence.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    resources = _fhir_resources(payload) if payload is not None else []
    if resources:
        ledger.note_fidelity("raw_fhir", "")
        return [
            PatientEvidenceSpan(
                span_id=f"patient-{start_index + offset:03d}",
                query=query,
                resource_type=str(resource.get("resourceType", "Unknown")),
                content=resource,
                retrieved_at=retrieved_at,
            )
            for offset, resource in enumerate(resources)
        ]

    entries, absent = parse_fhir_tool_output(text)
    if entries or absent:
        ledger.note_fidelity(
            "formatted_summary",
            "patient evidence came from the formatted query_patient_fhir output, so each "
            "span is a per-resource summary line rather than a raw FHIR resource",
        )
        for resource_type in absent:
            if resource_type not in ledger.absent_resource_types:
                ledger.absent_resource_types.append(resource_type)
        return [
            PatientEvidenceSpan(
                span_id=f"patient-{start_index + offset:03d}",
                query=query,
                resource_type=resource_type,
                content={"text": summary, "evidence_fidelity": "formatted_summary"},
                retrieved_at=retrieved_at,
            )
            for offset, (resource_type, summary) in enumerate(entries)
        ]

    ledger.note_fidelity(
        "degraded",
        "a patient-evidence payload was neither raw FHIR nor the recognised "
        "query_patient_fhir output, so it could not be decomposed into per-resource spans",
    )
    return [
        PatientEvidenceSpan(
            span_id=f"patient-{start_index:03d}",
            query=query,
            resource_type="RawToolResult",
            content={"text": text, "evidence_fidelity": "degraded"},
            retrieved_at=retrieved_at,
        )
    ]


def _describe_query(tool_name: str, tool_args: Any) -> str:
    """Render a recorded tool call as a query string for the coverage record."""
    if isinstance(tool_args, dict) and tool_args:
        rendered = ", ".join(f"{key}={tool_args[key]!r}" for key in sorted(tool_args))
        return f"{tool_name}({rendered})"
    return f"{tool_name}()"


def ledger_from_steering_context(
    steering_ledger: dict[str, Any] | None,
    *,
    coverage_rule_version: str,
    patient_tool_names: frozenset[str] = DEFAULT_PATIENT_TOOL_NAMES,
    guideline_tool_names: frozenset[str] = DEFAULT_GUIDELINE_TOOL_NAMES,
    raw_resources: list[dict[str, Any]] | None = None,
) -> ToolLedger:
    """Build a tool ledger from Strands' ``LedgerProvider`` steering context.

    Only successful tool calls contribute evidence spans; every attempted call
    contributes to the recorded query coverage, because a query that errored was
    still executed and its emptiness proves nothing.

    Args:
        steering_ledger: The ``LedgerProvider`` payload from the steering context.
        coverage_rule_version: Version of the coverage rule this ledger is recorded for.
        patient_tool_names: Tool names whose results are patient evidence.
        guideline_tool_names: Tool names whose results are guideline evidence.
        raw_resources: Raw FHIR resources published by the patient tool's side channel
            (:mod:`medical_nudging.tools.raw_resource_channel`).  When supplied, these
            become the patient evidence spans and the ledger keeps ``raw_fhir``
            fidelity; the tool's formatted return value is only parsed as a fallback.
    """
    ledger = ToolLedger(coverage_rule_version=coverage_rule_version)
    if not isinstance(steering_ledger, dict):
        return ledger

    calls = steering_ledger.get("tool_calls")
    if not isinstance(calls, list):
        return ledger

    for call in calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or "")
        query = _describe_query(tool_name, call.get("tool_args"))
        ledger.queries_executed.append(query)

        if call.get("status") != "success":
            continue
        texts = _content_blocks_to_text(call.get("result"))
        retrieved_at = call.get("completion_timestamp")

        if tool_name in patient_tool_names:
            for text in texts:
                # The absent-resource-type and page-cap records only exist in the
                # formatted output, so parse them for coverage even when raw resources
                # carry the evidence.
                _, absent = parse_fhir_tool_output(text)
                for resource_type in absent:
                    if resource_type not in ledger.absent_resource_types:
                        ledger.absent_resource_types.append(resource_type)
                for marker_re in (_PAGE_CAP_RE, _HISTORY_WINDOW_RE):
                    for truncated in marker_re.finditer(text):
                        resource_type = truncated.group("resource_type")
                        if resource_type not in ledger.truncated_resource_types:
                            ledger.truncated_resource_types.append(resource_type)
                if raw_resources:
                    continue
                ledger.patient_evidence.extend(
                    _patient_spans_from_text(
                        text,
                        query=query,
                        start_index=len(ledger.patient_evidence),
                        retrieved_at=retrieved_at,
                        ledger=ledger,
                    )
                )
        elif tool_name in guideline_tool_names:
            for text in texts:
                for passage in parse_guideline_passages(text):
                    ledger.guideline_evidence.append(
                        _passage_to_span(passage, len(ledger.guideline_evidence))
                    )

    if raw_resources:
        ledger.patient_evidence.extend(
            PatientEvidenceSpan(
                span_id=f"patient-{offset:03d}",
                query="raw_resource_channel",
                resource_type=str(resource.get("resourceType", "Unknown")),
                content=resource,
                retrieved_at=None,
            )
            for offset, resource in enumerate(raw_resources)
        )
        ledger.note_fidelity("raw_fhir", "")
    return ledger


def append_injected_guideline_passages(
    ledger: ToolLedger,
    formatted_text: str,
    *,
    query: str,
) -> int:
    """Append passages that were injected into the prompt rather than tool-retrieved.

    Used by a single-pass retrieval condition: one fixed retrieval happens outside the
    agent loop and its passages are injected into the generation context, so they never
    appear as tool calls in the steering context. The verifier still needs them as
    guideline evidence, and the coverage record still needs the retrieval named, so this
    parses the same ``=== SOURCE ===`` format the search tool emits and records ``query``
    as an executed query.

    Returns the number of passages appended.
    """
    ledger.queries_executed.append(query)
    appended = 0
    for passage in parse_guideline_passages(formatted_text):
        ledger.guideline_evidence.append(_passage_to_span(passage, len(ledger.guideline_evidence)))
        appended += 1
    return appended
