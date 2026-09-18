"""Offline checks over saved baseline runs.

These answer "did the run work, and is the output ready for a clinician to
inspect?", deliberately *not* "was the advice clinically correct?".

Each gate is a `strands_evals.evaluators.Evaluator` and reads only saved
artifacts -- no model calls, no AWS calls -- so the whole harness is replayable
and testable offline.

`evals.layers` wraps these gates for the Strands `Experiment` runner and populates
`EvaluationData` as:
    input            -> {"sample_id", "patient_id", ...}  (identifying metadata)
    actual_output    -> the saved NudgeResponse dict
    metadata         -> {"trace": <ExecutionTrace.to_dict() or None>}

Gates return one `EvaluationOutput` per checked unit (nudge, tool call, ...), so
the framework's default aggregator turns the list into a pass-fraction score plus
an all-must-pass boolean.
"""

from __future__ import annotations

import re
from typing import Any, TypeAlias

from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from evals.catalog import GuidelineCatalog, normalize_source

# `response_parser.create_error_response("Structured output", ...)` formats the
# error as f"{error_type} failed: {error}".
STRUCTURED_OUTPUT_ERROR_PREFIX = "Structured output failed"

# `orchestrator.emit_error(..., stage="output_format")` marks the same condition
# on the observability event.
OUTPUT_FORMAT_STAGE = "output_format"

HEALTHY_STATUSES = frozenset({"success", "partial"})

EvalData: TypeAlias = EvaluationData[dict[str, Any], dict[str, Any]]

DEFAULT_MAX_NUDGES = 3
DEFAULT_MAX_GUIDELINE_SEARCHES = 3
DEFAULT_MAX_FHIR_QUERIES = 3
MAX_KEY_FINDING_WORDS = 18
MIN_SUMMARY_WORDS = 40
MAX_SUMMARY_WORDS = 70
MAX_TITLE_WORDS = 8
MAX_DESCRIPTION_WORDS = 30
MAX_RATIONALE_WORDS = 30

_NORMALIZE_TEXT_RE = re.compile(r"[^a-z0-9]+")
_SEARCH_SOURCE_RE = re.compile(r"^===\s*(.+?)\s*===\s*$", re.MULTILINE)
_REFERENCE_SECTION_RE = re.compile(r"^(?:references?|bibliograph(?:y|ies))\b", re.IGNORECASE)


def _output_of(case: EvalData) -> dict[str, Any]:
    return case.actual_output if isinstance(case.actual_output, dict) else {}


def _trace_of(case: EvalData) -> dict[str, Any]:
    metadata = case.metadata or {}
    trace = metadata.get("trace")
    return trace if isinstance(trace, dict) else {}


def _run_config_of(case: EvalData) -> dict[str, Any]:
    metadata = case.metadata or {}
    run_config = metadata.get("run_config")
    return run_config if isinstance(run_config, dict) else {}


def _data_source_of(output: dict[str, Any], run_config: dict[str, Any]) -> str | None:
    context = run_config.get("context")
    context = context if isinstance(context, dict) else {}
    data_source = context.get("data_source")
    if isinstance(data_source, str):
        return data_source

    visit_context = context.get("visit_context")
    visit_context = visit_context if isinstance(visit_context, dict) else {}
    data_source = visit_context.get("data_source")
    if isinstance(data_source, str):
        return data_source

    output_format = output.get("format")
    return output_format if isinstance(output_format, str) else None


def _nudges_of(output: dict[str, Any]) -> list[dict[str, Any]]:
    nudges = output.get("nudges")
    if not isinstance(nudges, list):
        return []
    return [n for n in nudges if isinstance(n, dict)]


def _word_count(value: Any) -> int:
    return len(value.split()) if isinstance(value, str) else 0


def _positive_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default


def _source_matches(candidate: str, retrieved: set[str]) -> bool:
    needle = normalize_source(candidate)
    if not needle:
        return False
    return any(
        needle == source or needle in source or source in needle
        for source in retrieved
        if len(source) >= 6
    )


def _retrieved_guideline_sources(trace: dict[str, Any]) -> set[str]:
    tool_calls = trace.get("tool_calls")
    if not isinstance(tool_calls, list):
        return set()
    search_ids = {
        call.get("tool_use_id")
        for call in tool_calls
        if isinstance(call, dict) and call.get("tool_name") == "search_guidelines"
    }
    search_ids.discard(None)

    sources: set[str] = set()
    messages = trace.get("messages")
    if not isinstance(messages, list):
        return sources
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            if tool_result.get("toolUseId") not in search_ids:
                continue
            result_content = tool_result.get("content")
            if not isinstance(result_content, list):
                continue
            for result_block in result_content:
                if not isinstance(result_block, dict):
                    continue
                text = result_block.get("text")
                if not isinstance(text, str):
                    continue
                sources.update(normalize_source(match) for match in _SEARCH_SOURCE_RE.findall(text))
    return {source for source in sources if source}


def _tool_result_texts(trace: dict[str, Any]) -> dict[str, str]:
    """Collect serialized tool-result text by tool-use id."""
    texts: dict[str, str] = {}
    messages = trace.get("messages")
    if not isinstance(messages, list):
        return texts
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            tool_use_id = tool_result.get("toolUseId")
            result_content = tool_result.get("content")
            if not isinstance(tool_use_id, str) or not isinstance(result_content, list):
                continue
            parts = [
                item["text"]
                for item in result_content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            texts[tool_use_id] = "\n".join(parts)
    return texts


def _external_tool_call_count(trace: dict[str, Any], tool_name: str) -> int:
    """Count calls that reached the external boundary.

    Schema-invalid attempts and calls blocked by the request-scoped budget do
    not execute an external request and therefore do not consume that budget.
    """
    tool_calls = trace.get("tool_calls")
    if not isinstance(tool_calls, list):
        return 0
    result_texts = _tool_result_texts(trace)
    count = 0
    for call in tool_calls:
        if not isinstance(call, dict) or call.get("tool_name") != tool_name:
            continue
        if call.get("success") is False or call.get("error"):
            continue
        result_text = result_texts.get(call.get("tool_use_id"), "")
        if "BUDGET_EXHAUSTED:" in result_text or "BUDGET_EXCEEDED:" in result_text:
            continue
        count += 1
    return count


class OutputFormatSuccess(Evaluator):
    """Gate 1: did structured output generation succeed?

    The Strands SDK validates the `GeneratedNudgeOutput` payload against its
    Pydantic schema at generation time, so re-validating the saved output here
    would be dead code. What *can* still go wrong upstream of the save is the
    format step itself: truncation, a refusal, or the forcing pass failing to
    produce the structured-output tool call. Those surface as
    `status == "error"` with a "Structured output failed" message and a
    `stage == "output_format"` observability marker.

    Anything else -- including a run that errored for a non-format reason -- passes
    this gate; execution health is gate 3's job.
    """

    def evaluate(self, evaluation_case: EvalData) -> list[EvaluationOutput]:
        output = _output_of(evaluation_case)
        status = output.get("status")
        error = output.get("error") or ""
        stage = self._stage_of(output)

        is_format_failure = status == "error" and (
            error.startswith(STRUCTURED_OUTPUT_ERROR_PREFIX) or stage == OUTPUT_FORMAT_STAGE
        )

        if is_format_failure:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    label="output_format_failure",
                    reason=f"Structured output did not materialize: {error or stage}",
                )
            ]
        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="output_format_ok",
                reason=f"Structured output produced (status={status!r})",
            )
        ]

    @staticmethod
    def _stage_of(output: dict[str, Any]) -> str | None:
        """Read the observability stage marker, wherever the writer put it."""
        stage = output.get("stage")
        if isinstance(stage, str):
            return stage
        metadata = output.get("metadata")
        if isinstance(metadata, dict):
            nested = metadata.get("stage")
            if isinstance(nested, str):
                return nested
        return None


class CitationPresent(Evaluator):
    """Gate 2: does every guideline-grounded nudge carry a resolvable citation?

    A nudge that claims `grounding == "guideline"` must name a source the agent
    could actually have read -- i.e. one that is in the guideline catalog. Nudges
    grounded in `clinical_reasoning` are exempt by design; they are not claiming
    documentary support.

    This checks *presence and resolvability*, not whether the cited passage
    supports the claim. Citation-content faithfulness is a quality judgement and
    stays out of the sanity harness.
    """

    def __init__(self, catalog: GuidelineCatalog | None = None, name: str | None = None) -> None:
        super().__init__(name=name)
        self.catalog = catalog if catalog is not None else GuidelineCatalog.load()

    def evaluate(self, evaluation_case: EvalData) -> list[EvaluationOutput]:
        nudges = _nudges_of(_output_of(evaluation_case))
        if not nudges:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    label="no_guideline_citations",
                    reason="No nudges claim guideline grounding",
                )
            ]
        return [self._check(index, nudge) for index, nudge in enumerate(nudges)]

    def _check(self, index: int, nudge: dict[str, Any]) -> EvaluationOutput:
        grounding = nudge.get("grounding")
        if grounding != "guideline":
            return EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="not_guideline_grounded",
                reason=f"nudge[{index}]: grounding={grounding!r}, citation not required",
            )

        citation = nudge.get("guideline_citation")
        if not isinstance(citation, dict):
            return EvaluationOutput(
                score=0.0,
                test_pass=False,
                label="citation_missing",
                reason=f"nudge[{index}]: grounding=guideline but no guideline_citation",
            )

        source = citation.get("source")
        if not isinstance(source, str) or not source.strip():
            return EvaluationOutput(
                score=0.0,
                test_pass=False,
                label="citation_missing",
                reason=f"nudge[{index}]: guideline_citation has no source",
            )

        if self.catalog.is_empty:
            # No catalog to check against -- report presence only rather than
            # failing every nudge and manufacturing a false signal.
            return EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="citation_present_unverified",
                reason=f"nudge[{index}]: cites {source!r} (catalog empty, source not verified)",
            )

        if not self.catalog.contains(source):
            return EvaluationOutput(
                score=0.0,
                test_pass=False,
                label="citation_unknown_source",
                reason=f"nudge[{index}]: cites {source!r}, which is not in the guideline catalog",
            )

        return EvaluationOutput(
            score=1.0,
            test_pass=True,
            label="citation_present",
            reason=f"nudge[{index}]: cites {source!r}",
        )


class ExecutionHealth(Evaluator):
    """Gate 3: did the run and its tool calls complete without erroring?

    Two independent things can go wrong: the run itself can come back with
    `status == "error"`, and individual tool calls can fail while the run still
    reports success (a partial). Both matter for error analysis, so both are
    reported -- one `EvaluationOutput` for the run plus one per tool call, which
    makes the aggregated score a health fraction rather than a single bit.

    Tool failure is read from the trace's `tool_calls[].success`/`.error` and from
    `messages[].content[].toolResult.status`, because a tool can return an error
    payload to the model without the callback marking the call unsuccessful.
    """

    def evaluate(self, evaluation_case: EvalData) -> list[EvaluationOutput]:
        output = _output_of(evaluation_case)
        trace = _trace_of(evaluation_case)

        outputs = [self._check_run(output)]
        outputs.extend(self._check_tool_calls(trace))
        outputs.extend(self._check_tool_results(trace))
        return outputs

    @staticmethod
    def _check_run(output: dict[str, Any]) -> EvaluationOutput:
        status = output.get("status")
        error = output.get("error")
        if status in HEALTHY_STATUSES and not error:
            return EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="run_ok",
                reason=f"run status={status!r}",
            )
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            label="run_error",
            reason=f"run status={status!r} error={error!r}",
        )

    @staticmethod
    def _check_tool_calls(trace: dict[str, Any]) -> list[EvaluationOutput]:
        tool_calls = trace.get("tool_calls")
        if not isinstance(tool_calls, list):
            return []
        results = []
        for index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            tool_name = call.get("tool_name", "<unknown>")
            incomplete = call.get("completed") is False
            failed = incomplete or call.get("success") is False or call.get("error")
            if failed:
                label = "tool_incomplete" if incomplete else "tool_error"
                detail = "call did not complete" if incomplete else repr(call.get("error"))
                results.append(
                    EvaluationOutput(
                        score=0.0,
                        test_pass=False,
                        label=label,
                        reason=f"tool_calls[{index}] {tool_name}: {detail}",
                    )
                )
            else:
                results.append(
                    EvaluationOutput(
                        score=1.0,
                        test_pass=True,
                        label="tool_ok",
                        reason=f"tool_calls[{index}] {tool_name}: ok",
                    )
                )
        return results

    @staticmethod
    def _check_tool_results(trace: dict[str, Any]) -> list[EvaluationOutput]:
        """Flag toolResult blocks the model saw as errors."""
        messages = trace.get("messages")
        if not isinstance(messages, list):
            return []
        results = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                tool_result = block.get("toolResult")
                if not isinstance(tool_result, dict):
                    continue
                if tool_result.get("status") == "error":
                    tool_use_id = tool_result.get("toolUseId", "<unknown>")
                    results.append(
                        EvaluationOutput(
                            score=0.0,
                            test_pass=False,
                            label="tool_result_error",
                            reason=f"toolResult {tool_use_id} returned status=error",
                        )
                    )
        return results


class ReviewerReady(Evaluator):
    """Catch obvious output problems before a clinician sees the result.

    This check uses only facts recorded in the saved output, run configuration,
    and trace. It checks compact-output limits, configured call budgets, exact
    duplicate nudges, and whether guideline citations name a source that the
    run actually retrieved.

    It does not judge clinical correctness, citation entailment, prioritization,
    or semantic duplication.
    """

    def evaluate(self, evaluation_case: EvalData) -> list[EvaluationOutput]:
        output = _output_of(evaluation_case)
        trace = _trace_of(evaluation_case)
        run_config = _run_config_of(evaluation_case)

        checks = [self._check_trace(trace)]
        checks.extend(self._check_output_limits(output, run_config))
        checks.extend(self._check_tool_budgets(output, trace, run_config))
        checks.extend(self._check_citation_provenance(output, trace))
        checks.append(self._check_exact_duplicates(output))
        return checks

    @staticmethod
    def _check_trace(trace: dict[str, Any]) -> EvaluationOutput:
        if trace:
            return EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="trace_present",
                reason="Execution trace is available",
            )
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            label="trace_missing",
            reason="No execution trace is available for provenance checks",
        )

    @staticmethod
    def _check_output_limits(
        output: dict[str, Any], run_config: dict[str, Any]
    ) -> list[EvaluationOutput]:
        agent_config = run_config.get("agent")
        agent_config = agent_config if isinstance(agent_config, dict) else {}
        max_nudges = _positive_int(agent_config.get("max_nudges"), DEFAULT_MAX_NUDGES)
        nudges = _nudges_of(output)

        checks = [
            ReviewerReady._limit_check(
                label="nudge_count",
                value=len(nudges),
                minimum=0,
                maximum=max_nudges,
                unit="nudges",
            )
        ]

        key_findings = output.get("key_findings")
        if not isinstance(key_findings, list):
            key_findings = []
        checks.append(
            ReviewerReady._limit_check(
                label="key_finding_count",
                value=len(key_findings),
                minimum=3,
                maximum=3,
                unit="findings",
            )
        )
        for index, finding in enumerate(key_findings):
            checks.append(
                ReviewerReady._limit_check(
                    label=f"key_finding_{index}_words",
                    value=_word_count(finding),
                    minimum=1,
                    maximum=MAX_KEY_FINDING_WORDS,
                    unit="words",
                )
            )

        checks.append(
            ReviewerReady._limit_check(
                label="patient_summary_words",
                value=_word_count(output.get("patient_summary")),
                minimum=MIN_SUMMARY_WORDS,
                maximum=MAX_SUMMARY_WORDS,
                unit="words",
            )
        )

        for index, nudge in enumerate(nudges):
            for field, maximum in (
                ("title", MAX_TITLE_WORDS),
                ("description", MAX_DESCRIPTION_WORDS),
                ("rationale", MAX_RATIONALE_WORDS),
            ):
                checks.append(
                    ReviewerReady._limit_check(
                        label=f"nudge_{index}_{field}_words",
                        value=_word_count(nudge.get(field)),
                        minimum=1,
                        maximum=maximum,
                        unit="words",
                    )
                )
        return checks

    @staticmethod
    def _limit_check(
        *,
        label: str,
        value: int,
        minimum: int,
        maximum: int | None,
        unit: str,
    ) -> EvaluationOutput:
        passed = value >= minimum and (maximum is None or value <= maximum)
        if maximum is None:
            expected = f"at least {minimum} {unit}; no upper ceiling configured"
        elif minimum == 0:
            expected = f"at most {maximum} {unit}"
        elif minimum == maximum:
            expected = f"exactly {maximum} {unit}"
        else:
            expected = f"{minimum}-{maximum} {unit}"
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            label=f"{label}_{'ok' if passed else 'failed'}",
            reason=f"{label}: {value} {unit}; expected {expected}",
        )

    @staticmethod
    def _check_tool_budgets(
        output: dict[str, Any],
        trace: dict[str, Any],
        run_config: dict[str, Any],
    ) -> list[EvaluationOutput]:
        agent_config = run_config.get("agent")
        agent_config = agent_config if isinstance(agent_config, dict) else {}
        tool_limits = agent_config.get("tool_limits")
        tool_limits = tool_limits if isinstance(tool_limits, dict) else {}
        search_limits = tool_limits.get("guideline_search")
        search_limits = search_limits if isinstance(search_limits, dict) else {}
        fhir_limits = tool_limits.get("fhir_query")
        fhir_limits = fhir_limits if isinstance(fhir_limits, dict) else {}

        max_searches = _positive_int(search_limits.get("max_calls"), DEFAULT_MAX_GUIDELINE_SEARCHES)
        max_fhir_queries = _positive_int(fhir_limits.get("max_calls"), DEFAULT_MAX_FHIR_QUERIES)
        if "max_calls" in search_limits and search_limits["max_calls"] is None:
            max_searches = None
        if "max_calls" in fhir_limits and fhir_limits["max_calls"] is None:
            max_fhir_queries = None

        counts = {
            "search_guidelines": _external_tool_call_count(trace, "search_guidelines"),
            "query_patient_fhir": _external_tool_call_count(trace, "query_patient_fhir"),
        }
        checks = [
            ReviewerReady._limit_check(
                label="guideline_search_calls",
                value=counts["search_guidelines"],
                minimum=0,
                maximum=max_searches,
                unit="calls",
            )
        ]

        is_fhir_api = _data_source_of(output, run_config) == "fhir_api"
        if is_fhir_api and max_fhir_queries == 0:
            checks.append(
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    label="fhir_query_budget_disabled",
                    reason=(
                        "FHIR API run requires at least one patient query, but "
                        "fhir_query.max_calls is 0"
                    ),
                )
            )
        else:
            checks.append(
                ReviewerReady._limit_check(
                    label="fhir_query_calls",
                    value=counts["query_patient_fhir"],
                    minimum=1 if is_fhir_api else 0,
                    maximum=max_fhir_queries,
                    unit="calls",
                )
            )
        return checks

    @staticmethod
    def _check_citation_provenance(
        output: dict[str, Any], trace: dict[str, Any]
    ) -> list[EvaluationOutput]:
        retrieved_sources = _retrieved_guideline_sources(trace)
        checks: list[EvaluationOutput] = []
        for index, nudge in enumerate(_nudges_of(output)):
            if nudge.get("grounding") != "guideline":
                continue
            citation = nudge.get("guideline_citation")
            source = citation.get("source") if isinstance(citation, dict) else None
            section = citation.get("section") if isinstance(citation, dict) else None

            if isinstance(section, str) and _REFERENCE_SECTION_RE.match(section.strip()):
                checks.append(
                    EvaluationOutput(
                        score=0.0,
                        test_pass=False,
                        label="citation_reference_section",
                        reason=f"nudge[{index}] cites reference section {section!r}",
                    )
                )
                continue

            retrieved = isinstance(source, str) and _source_matches(source, retrieved_sources)
            checks.append(
                EvaluationOutput(
                    score=1.0 if retrieved else 0.0,
                    test_pass=bool(retrieved),
                    label="citation_retrieved" if retrieved else "citation_not_retrieved",
                    reason=(
                        f"nudge[{index}] cites {source!r}, which was retrieved"
                        if retrieved
                        else f"nudge[{index}] cites {source!r}, absent from search results"
                    ),
                )
            )
        if checks:
            return checks
        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                label="no_guideline_provenance_required",
                reason="No nudges claim guideline grounding",
            )
        ]

    @staticmethod
    def _check_exact_duplicates(output: dict[str, Any]) -> EvaluationOutput:
        seen: set[str] = set()
        duplicates: list[int] = []
        for index, nudge in enumerate(_nudges_of(output)):
            text = f"{nudge.get('title', '')} {nudge.get('description', '')}"
            normalized = _NORMALIZE_TEXT_RE.sub("", text.casefold())
            if not normalized:
                continue
            if normalized in seen:
                duplicates.append(index)
            seen.add(normalized)
        if duplicates:
            return EvaluationOutput(
                score=0.0,
                test_pass=False,
                label="exact_duplicate_nudges",
                reason=f"Repeated title and description at nudge indexes {duplicates}",
            )
        return EvaluationOutput(
            score=1.0,
            test_pass=True,
            label="no_exact_duplicate_nudges",
            reason="No exact repeated title and description",
        )
