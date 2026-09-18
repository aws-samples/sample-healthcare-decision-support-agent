"""Main orchestrator agent for medical nudging system."""

import atexit
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any

from strands.types.exceptions import StructuredOutputException

from medical_nudging.config import get_model_config
from medical_nudging.models import GeneratedNudgeOutput, NudgeResponse
from medical_nudging.parsers.ccda_parser import CCDAParseError, parse_ccda
from medical_nudging.parsers.fhir_parser import FHIRParseError, parse_fhir
from medical_nudging.parsers.preparsed_parser import PreParsedJSONError, parse_preparsed_json
from medical_nudging.agents.agent_builder import (
    CONTEXT_CONDITIONS,
    build_agent,
    build_tool_list,
    resolve_effective_mode,
)
from medical_nudging.agents.prompt_builder import (
    build_system_prompt,
    build_user_prompt,
    build_user_prompt_fhir,
    load_custom_instructions,
)
from medical_nudging.tools.specialty_instructions import load_specialty_guidance
from medical_nudging.agents.response_parser import (
    STRUCTURED_OUTPUT_TOOL_NAME,
    StructuredOutputMissingError,
    build_nudge_response,
    build_streaming_result,
    create_error_response,
    create_streaming_error_result,
    extract_structured_output,
)
from medical_nudging.tracing.request_observer import FailureStage, RequestObserver
from medical_nudging.tracing.trace_capture import ExecutionTrace

logger = logging.getLogger(__name__)


def _extract_age_months(parsed_data: dict[str, Any]) -> int | None:
    """Extract patient age in months from parsed patient data.

    Handles both CCDA format (``YYYYMMDD``) and FHIR format (``YYYY-MM-DD``).

    Returns:
        Age in months, or ``None`` if DOB is unavailable or unparseable.
    """
    demographics = parsed_data.get("demographics")
    if not isinstance(demographics, dict):
        return None

    dob_str = demographics.get("dob")
    if not dob_str or not isinstance(dob_str, str):
        return None

    try:
        from datetime import date

        dob_str = dob_str.strip()
        if len(dob_str) == 8 and dob_str.isdigit():
            # CCDA format: YYYYMMDD
            dob = date(int(dob_str[:4]), int(dob_str[4:6]), int(dob_str[6:8]))
        else:
            # FHIR format: YYYY-MM-DD (possibly with trailing time)
            dob = date.fromisoformat(dob_str[:10])

        today = date.today()
        months = (today.year - dob.year) * 12 + (today.month - dob.month)
        return max(0, months)
    except (ValueError, TypeError) as e:
        logger.debug(f"Could not parse DOB '{dob_str}' for age calculation: {e}")
        return None


def _structured_output_input_delta(event: dict[str, Any]) -> str:
    """Extract the incremental tool-input JSON from a Strands tool use stream event.

    Returns:
        The partial JSON fragment, or ``""`` if the event carries no input delta.
    """
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return ""
    tool_use = delta.get("toolUse")
    if not isinstance(tool_use, dict):
        return ""
    chunk = tool_use.get("input")
    return chunk if isinstance(chunk, str) else ""


# Human-readable labels for parse error types, used in error responses
_PARSE_ERROR_LABELS: dict[type, str] = {
    CCDAParseError: "CCDA parsing",
    FHIRParseError: "FHIR parsing",
    PreParsedJSONError: "Pre-parsed JSON parsing",
}

# Track temp files for cleanup on exit (handles edge cases like process interruption)
_temp_files: set[Path] = set()


def _cleanup_temp_files() -> None:
    """Clean up any remaining temp files on process exit."""
    for temp_path in list(_temp_files):
        try:
            if temp_path.exists():
                temp_path.unlink()
                _temp_files.discard(temp_path)
        except OSError as e:
            logger.debug(f"Failed to cleanup temp file {temp_path}: {e}")


atexit.register(_cleanup_temp_files)


def _release_temp_file(temp_path: Path | None) -> None:
    """Delete a request's temp file and stop tracking it; missing files are fine."""
    if temp_path is None:
        return
    try:
        if temp_path.exists():
            temp_path.unlink()
        _temp_files.discard(temp_path)
    except OSError as e:
        logger.debug(f"Failed to cleanup temp file {temp_path}: {e}")


def _token_usage(result: Any) -> dict[str, int] | None:
    """Read Bedrock token counters off a Strands ``AgentResult``, if it carries them."""
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) if metrics else None
    if not isinstance(usage, dict) or not usage:
        return None
    return {
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
        "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
    }


def _resolve_failure(error: Exception) -> tuple[FailureStage, str]:
    """Observer stage and client-facing code for a request that could not be resolved."""
    if isinstance(error, (CCDAParseError, FHIRParseError, PreParsedJSONError)):
        return "parsing", "PARSE_ERROR"
    return "invalid_input", "INVALID_INPUT"


def _log_streaming_tokens(token_usage: dict[str, int] | None) -> None:
    if token_usage is None:
        logger.debug("No accumulated_usage on the streaming result")
        return
    logger.info(
        "Streaming tokens: input=%s, output=%s",
        token_usage["input_tokens"],
        token_usage["output_tokens"],
    )


class _ToolTimer:
    """Pairs each streamed tool start with its result and times the interval."""

    def __init__(self) -> None:
        self._open: dict[str, tuple[str, float]] = {}  # tool_id → (name, start_time)
        self._closed: set[str] = set()

    def start(self, tool_id: str, tool_name: str) -> bool:
        """Record a tool start; ``False`` when the id is empty or already open."""
        if not tool_id or tool_id in self._open:
            return False
        self._open[tool_id] = (tool_name, time.perf_counter())
        return True

    def finish(self, tool_id: str) -> tuple[str, int] | None:
        """Close an open tool and return ``(name, duration_ms)``, once per id."""
        if not tool_id or tool_id not in self._open or tool_id in self._closed:
            return None
        tool_name, tool_start = self._open[tool_id]
        self._closed.add(tool_id)
        return tool_name, int((time.perf_counter() - tool_start) * 1000)

    def finish_open(self) -> list[tuple[str, int]]:
        """Close every tool that started but never reported a result."""
        finished = [self.finish(tool_id) for tool_id in list(self._open)]
        return [item for item in finished if item is not None]


def _tool_end_event(tool_name: str, duration_ms: int) -> dict[str, Any]:
    return {"event": "tool_end", "data": {"tool": tool_name, "duration_ms": duration_ms}}


def _translate_stream_event(event: dict[str, Any], tools: _ToolTimer) -> list[dict[str, Any]]:
    """Map one ``stream_async`` event onto zero or more WebSocket protocol events."""
    events: list[dict[str, Any]] = []
    if "data" in event and isinstance(event.get("data"), str):
        events.append({"event": "text", "data": event["data"]})
    events.extend(_tool_use_events(event, tools))
    events.extend(_tool_result_events(event.get("message"), tools))
    return events


def _tool_use_events(event: dict[str, Any], tools: _ToolTimer) -> list[dict[str, Any]]:
    """Tool use start (from ToolUseStreamEvent: has current_tool_use)."""
    current_tool = event.get("current_tool_use")
    if not isinstance(current_tool, dict):
        return []
    tool_id = current_tool.get("toolUseId", "")
    tool_name = current_tool.get("name", "unknown")
    if tool_name == STRUCTURED_OUTPUT_TOOL_NAME:
        # The final payload arrives as structured output tool input rather than
        # assistant text, so forward its input deltas as text to keep the
        # client-visible stream unchanged. It is not a clinical tool, so no
        # tool_start/tool_end is emitted.
        chunk = _structured_output_input_delta(event)
        return [{"event": "text", "data": chunk}] if chunk else []
    if tools.start(tool_id, tool_name):
        return [
            {
                "event": "tool_start",
                "data": {"tool": tool_name, "input": current_tool.get("input", {})},
            }
        ]
    return []


def _tool_result_events(message: Any, tools: _ToolTimer) -> list[dict[str, Any]]:
    """Tool completion (from ToolResultMessageEvent: message with toolResult)."""
    if not isinstance(message, dict):
        return []
    events: list[dict[str, Any]] = []
    for block in message.get("content", []):
        tool_result = block.get("toolResult") if isinstance(block, dict) else None
        if not isinstance(tool_result, dict):
            continue
        finished = tools.finish(tool_result.get("toolUseId", ""))
        if finished is not None:
            events.append(_tool_end_event(*finished))
    return events


def _streaming_nudge_result(
    agent_result: Any, structured_output_error: str | None, config: dict
) -> tuple[dict[str, Any], str | None]:
    """Completion payload body plus the structured-output error, if any."""
    if structured_output_error is not None:
        return create_streaming_error_result(structured_output_error), structured_output_error
    try:
        return build_streaming_result(extract_structured_output(agent_result), config), None
    except StructuredOutputMissingError as e:
        logger.warning(f"Structured output missing from stream result: {e}")
        return create_streaming_error_result(str(e)), str(e)


def _complete_payload(
    nudge_result: dict[str, Any],
    token_usage: dict[str, int] | None,
    processing_time_ms: int,
    request_id: str,
    structured_output_error: str | None,
) -> dict[str, Any]:
    complete_data: dict[str, Any] = {
        "status": nudge_result["status"],
        "key_findings": nudge_result["key_findings"],
        "patient_summary": nudge_result["patient_summary"],
        "nudges": nudge_result["nudges"],
        "usage": {
            "input_tokens": (token_usage or {}).get("input_tokens", 0),
            "output_tokens": (token_usage or {}).get("output_tokens", 0),
        },
        "processing_time_ms": processing_time_ms,
        "request_id": request_id,
    }
    if structured_output_error is not None:
        complete_data["error"] = structured_output_error
    return complete_data


def _write_temp_file(content: str) -> Path:
    """Write content to a secure temp file and track for cleanup.

    Returns:
        Path to the created temp file
    """
    fd, temp_path_str = tempfile.mkstemp(
        prefix="medical_nudging_",
        suffix=".xml",
        dir=None,
    )
    temp_path = Path(temp_path_str)
    _temp_files.add(temp_path)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return temp_path


@dataclass
class _ResolvedRequest:
    """Pre-resolved request context for inference.

    Bundles the results of data source resolution, tool selection, and
    prompt construction so callers don't duplicate this logic.
    """

    parsed_data: dict[str, Any]
    request_tools: list[Any]
    system_prompt: str
    user_prompt: str
    temp_path: Path | None
    specialty: str


class MedicalNudgingOrchestrator:
    """Main orchestrator with tool-based architecture."""

    def __init__(
        self,
        specialty: str = "general",
        model_id: str | None = None,
        context_condition: str = "full",
        extra_headers: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
    ):
        """Initialize orchestrator.

        Args:
            specialty: Default specialty context (can be overridden in visit_context)
            model_id: Override model ID (default: from config/settings.yaml)
            context_condition: Context condition for experiments:
                - "full": All tools enabled including search_guidelines (default)
                - "summaries_only": No search tools, summaries injected into prompt
                - "no_guidelines": No search tools, no summaries (model knowledge only)
            extra_headers: Additional fields for Bedrock additionalModelRequestFields
                (e.g., {"anthropic-beta": "context-1m-2025-08-07"} for 1M context)
            plugins: Optional Strands plugins attached to every agent this orchestrator
                builds, e.g. the contract-enforcing steering handler used for steered
                source-nudge generation
        """
        if context_condition not in CONTEXT_CONDITIONS:
            raise ValueError(
                f"Invalid context_condition: {context_condition}. "
                f"Must be one of: {CONTEXT_CONDITIONS}"
            )

        self.specialty = specialty
        self.model_id = model_id
        self.context_condition = context_condition
        self.extra_headers = extra_headers
        self.plugins = plugins

    def _runtime_config(self, config: dict | None) -> dict[str, Any]:
        """Return request config with the effective generator identity attached."""
        runtime_config = dict(config or {})
        runtime_config.setdefault(
            "model_version",
            self.model_id or get_model_config()["model_id"],
        )
        return runtime_config

    def _observer(self, request_id: str, visit_context: dict) -> RequestObserver:
        """Observer for one request; dimensions come from the visit context."""
        return RequestObserver(
            request_id=request_id,
            specialty=visit_context.get("specialty", self.specialty),
            visit_type=visit_context.get("visit_type") or "unknown",
        )

    def generate_nudges(
        self,
        patient_data: str | None = None,
        visit_context: dict | None = None,
        config: dict | None = None,
    ) -> NudgeResponse:
        """Generate clinical nudges using agent with tools.

        Args:
            patient_data: Patient document string (CCDA XML, FHIR JSON, or
                pre-parsed JSON).  ``None`` when ``data_source='fhir_api'``.
            visit_context: Visit type, specialty, chief complaint, data_source
            config: Optional configuration (model version, max_nudges, etc.)

        Returns:
            NudgeResponse with status, patient_summary, nudges, and metadata
        """
        visit_context = visit_context or {}
        response, _ = self._run_inference(patient_data, visit_context, config, enable_trace=False)
        return response

    def generate_nudges_with_trace(
        self,
        patient_data: str | None = None,
        visit_context: dict | None = None,
        config: dict | None = None,
    ) -> tuple[NudgeResponse, ExecutionTrace]:
        """Generate clinical nudges with full execution trace capture.

        Args:
            patient_data: Patient document string (CCDA XML, FHIR JSON, or
                pre-parsed JSON).  ``None`` when ``data_source='fhir_api'``.
            visit_context: Visit type, specialty, chief complaint, data_source
            config: Optional configuration (model version, max_nudges, etc.)

        Returns:
            Tuple of (NudgeResponse, ExecutionTrace) for analysis/debugging
        """
        visit_context = visit_context or {}
        response, trace = self._run_inference(
            patient_data, visit_context, config, enable_trace=True
        )
        # trace is guaranteed non-None when enable_trace=True
        return response, trace  # type: ignore[return-value]

    async def generate_nudges_streaming(
        self,
        patient_data: str | None = None,
        visit_context: dict | None = None,
        config: dict | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Generate clinical nudges with streaming output over WebSocket.

        Yields event dicts following the streaming protocol:
            - {"event": "text", "data": "..."}       incremental text chunks
            - {"event": "tool_start", "data": {...}}  tool invocation started
            - {"event": "tool_end", "data": {...}}    tool invocation completed
            - {"event": "error", "data": {...}}       recoverable error
            - {"event": "complete", "data": {...}}    final result with nudges
            - {"event": "fatal", "data": {...}}       unrecoverable error

        Args:
            patient_data: Patient document string (CCDA XML, FHIR JSON, or
                pre-parsed JSON).  ``None`` when ``data_source='fhir_api'``.
            visit_context: Visit type, specialty, chief complaint, data_source
            config: Optional configuration (model version, max_nudges, etc.)

        Yields:
            Event dicts for WebSocket transmission
        """
        visit_context = visit_context or {}
        start_time = time.perf_counter()
        config = self._runtime_config(config)
        temp_path: Path | None = None
        request_id = str(uuid.uuid4())
        observer = self._observer(request_id, visit_context)

        def elapsed_ms() -> int:
            return int((time.perf_counter() - start_time) * 1000)

        try:
            # Resolve data source, tools, and prompts (shared with _run_inference)
            try:
                resolved = self._resolve_request(patient_data, visit_context, config)
            except (CCDAParseError, FHIRParseError, PreParsedJSONError, ValueError) as e:
                stage, code = _resolve_failure(e)
                observer.failed(
                    error_type=type(e).__name__,
                    error_message=str(e),
                    stage=stage,
                    processing_time_ms=elapsed_ms(),
                )
                yield {"event": "fatal", "data": {"code": code, "message": str(e)}}
                return

            temp_path = resolved.temp_path
            observer.request_received(resolved.parsed_data, visit_context)

            agent = build_agent(
                model_id=self.model_id,
                extra_headers=self.extra_headers,
                system_prompt=resolved.system_prompt,
                tools=resolved.request_tools,
                enable_trace=False,
                trace=None,
                request_id=request_id,
                plugins=self.plugins,
            )

            # Stream events via Strands agent.stream_async()
            token_usage: dict[str, int] | None = None
            agent_result: Any = None
            structured_output_error: str | None = None
            tools = _ToolTimer()

            try:
                async for event in agent.stream_async(
                    resolved.user_prompt, structured_output_model=GeneratedNudgeOutput
                ):
                    # Final result event (emitted last by stream_async)
                    result = event.get("result")
                    if result is not None:
                        agent_result = result
                        token_usage = _token_usage(result)
                        _log_streaming_tokens(token_usage)
                        continue
                    for stream_event in _translate_stream_event(event, tools):
                        yield stream_event
            except StructuredOutputException as e:
                # The model never produced a schema-valid payload, even after the
                # SDK forced the structured output tool. Report it on the complete
                # event rather than aborting the stream.
                logger.warning(f"Structured output failed during streaming: {e}")
                structured_output_error = str(e)

            # Emit tool_end for any tools that started but didn't get a result event
            for tool_name, duration_ms in tools.finish_open():
                yield _tool_end_event(tool_name, duration_ms)

            # Map the SDK-validated structured output into the completion payload
            processing_time_ms = elapsed_ms()
            nudge_result, structured_output_error = _streaming_nudge_result(
                agent_result, structured_output_error, config
            )
            yield {
                "event": "complete",
                "data": _complete_payload(
                    nudge_result,
                    token_usage,
                    processing_time_ms,
                    request_id,
                    structured_output_error,
                ),
            }

            if structured_output_error is not None:
                observer.failed(
                    error_type="StructuredOutputError",
                    error_message=structured_output_error,
                    stage="output_format",
                    processing_time_ms=processing_time_ms,
                )
            else:
                observer.succeeded(
                    status=nudge_result["status"],
                    nudges=nudge_result["nudges"],
                    processing_time_ms=processing_time_ms,
                    token_usage=token_usage,
                )

        except Exception as e:
            logger.exception(f"Streaming error: {e}")
            observer.failed(
                error_type=type(e).__name__,
                error_message=str(e),
                stage="streaming",
                processing_time_ms=elapsed_ms(),
            )
            yield {"event": "fatal", "data": {"code": type(e).__name__, "message": str(e)}}

        finally:
            _release_temp_file(temp_path)

    def _resolve_request(
        self,
        patient_data: str | None,
        visit_context: dict,
        config: dict | None = None,
    ) -> _ResolvedRequest:
        """Resolve data source, build tools and prompts for a request.

        Handles file-based vs FHIR API data source routing, patient data
        parsing, tool list building, and prompt construction.  Shared by
        ``_run_inference`` and ``generate_nudges_streaming``.

        Raises:
            ValueError: If no patient data and no FHIR API config, or
                missing patient_id for FHIR API mode
            CCDAParseError: If CCDA XML parsing fails
            FHIRParseError: If FHIR JSON parsing fails
            PreParsedJSONError: If pre-parsed JSON parsing fails
        """
        data_source = visit_context.get("data_source")
        is_fhir_api = data_source == "fhir_api" and not patient_data
        fhir_patient_id: str | None = None
        temp_path: Path | None = None

        if is_fhir_api:
            fhir_patient_id = visit_context.get("patient_id")
            if not fhir_patient_id:
                raise ValueError("patient_id required in visit_context when data_source='fhir_api'")
            parsed_data: dict[str, Any] = {
                "data_source": "fhir_api",
                "patient_id": fhir_patient_id,
            }
        elif patient_data:
            if data_source == "fhir_api":
                logger.warning(
                    "Both patient_data and data_source='fhir_api' provided; "
                    "using patient_data (explicit data wins)"
                )
            parsed_data = self._parse_patient_data(patient_data)
        else:
            raise ValueError(
                "No patient data provided. Pass document string "
                "or set data_source='fhir_api' with patient_id in visit_context."
            )

        specialty = visit_context.get("specialty", self.specialty)
        # No default here: a missing search_mode must fall through to the experiment's
        # context_condition. Defaulting to "auto" silently re-enabled OpenSearch tools
        # under context_condition="no_guidelines" (an earlier search_mode
        # contamination bug), which invalidates any no-retrieval condition.
        search_mode = visit_context.get("search_mode")
        effective_mode = resolve_effective_mode(search_mode, self.context_condition)

        runtime_config = config or {}
        request_tools = build_tool_list(
            effective_mode,
            # Only effective_mode=="full" reads search_mode; a missing override keeps
            # the old "auto" availability probe there without touching the mode choice.
            search_mode if search_mode is not None else "auto",
            fhir_api=is_fhir_api,
            tool_limits=runtime_config.get("tool_limits"),
        )

        if patient_data and not is_fhir_api:
            temp_path = _write_temp_file(patient_data)

        enabled_sources = visit_context.get("enabled_sources")
        custom_instructions = load_custom_instructions(visit_context)

        # Pre-load specialty instructions (age unavailable in FHIR API mode)
        age_months = _extract_age_months(parsed_data) if not is_fhir_api else None
        specialty_instructions = load_specialty_guidance(specialty, patient_age_months=age_months)

        system_prompt = build_system_prompt(
            visit_context,
            enabled_sources=enabled_sources,
            runtime_config=runtime_config,
        )

        injected_guideline_context = visit_context.get("injected_guideline_context")
        if injected_guideline_context and not is_fhir_api:
            # The steering handler seeds injected passages into every ledger it
            # builds; dropping them from the prompt here would let the verifier
            # attest citations against passages the model never saw.
            raise ValueError(
                "injected_guideline_context is only supported with "
                "data_source='fhir_api'; the non-FHIR prompt path does not embed it"
            )

        if is_fhir_api:
            assert fhir_patient_id is not None  # validated above
            user_prompt = build_user_prompt_fhir(
                patient_id=fhir_patient_id,
                visit_context=visit_context,
                specialty=specialty,
                custom_instructions=custom_instructions,
                specialty_instructions=specialty_instructions,
                injected_guideline_context=injected_guideline_context,
            )
        else:
            user_prompt = build_user_prompt(
                parsed_data,
                visit_context,
                str(temp_path),
                specialty,
                custom_instructions=custom_instructions,
                specialty_instructions=specialty_instructions,
            )

        return _ResolvedRequest(
            parsed_data=parsed_data,
            request_tools=request_tools,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temp_path=temp_path,
            specialty=specialty,
        )

    def _run_inference(
        self,
        patient_data: str | None,
        visit_context: dict,
        config: dict | None,
        enable_trace: bool,
    ) -> tuple[NudgeResponse, ExecutionTrace | None]:
        """Core inference logic shared by generate_nudges and generate_nudges_with_trace.

        Args:
            patient_data: Patient document string (CCDA XML, FHIR JSON, or
                pre-parsed JSON).  ``None`` when ``data_source='fhir_api'``.
            visit_context: Visit type, specialty, chief complaint, data_source
            config: Optional configuration (model version, max_nudges, etc.)
            enable_trace: Whether to capture execution trace

        Returns:
            Tuple of (NudgeResponse, ExecutionTrace or None)
        """
        start_time = time.perf_counter()
        config = self._runtime_config(config)
        temp_path: Path | None = None
        trace: ExecutionTrace | None = None

        # Generate unique request ID for observability correlation
        request_id = str(uuid.uuid4())
        observer = self._observer(request_id, visit_context)

        if enable_trace:
            trace = ExecutionTrace()
            trace.start_time = start_time

        def fail(error: Exception, label: str, stage: FailureStage) -> NudgeResponse:
            if trace:
                trace.end_time = time.perf_counter()
            error_resp = create_error_response(label, error, start_time, config)
            observer.failed(
                error_type=type(error).__name__,
                error_message=str(error),
                stage=stage,
                processing_time_ms=error_resp.metadata.processing_time_ms,
            )
            return error_resp

        try:
            resolved = self._resolve_request(patient_data, visit_context, config)
            temp_path = resolved.temp_path
            observer.request_received(resolved.parsed_data, visit_context)

            # Set trace prompt metadata before building agent
            if enable_trace and trace:
                trace.system_prompt = resolved.system_prompt
                trace.user_prompt = resolved.user_prompt

            agent = build_agent(
                model_id=self.model_id,
                extra_headers=self.extra_headers,
                system_prompt=resolved.system_prompt,
                tools=resolved.request_tools,
                enable_trace=enable_trace,
                trace=trace,
                request_id=request_id,
                plugins=self.plugins,
            )
            response = agent(resolved.user_prompt, structured_output_model=GeneratedNudgeOutput)
            token_usage = _token_usage(response)

            if trace:
                trace.token_usage = token_usage
                trace.end_time = time.perf_counter()

            nudge_response = build_nudge_response(
                extract_structured_output(response), start_time, config
            )
            metadata = nudge_response.metadata
            observer.succeeded(
                status=nudge_response.status,
                nudges=nudge_response.nudges or [],
                processing_time_ms=metadata.processing_time_ms if metadata else 0,
                token_usage=token_usage,
                guidelines_used=metadata.guidelines_used if metadata else [],
            )
            return nudge_response, trace

        except (CCDAParseError, FHIRParseError, PreParsedJSONError) as e:
            return fail(e, _PARSE_ERROR_LABELS.get(type(e), "parsing"), "parsing"), trace
        except (StructuredOutputException, StructuredOutputMissingError) as e:
            # Format-compliance failure: the model never produced a schema-valid
            # payload, even after the SDK forced the structured output tool.
            return fail(e, "Structured output", "output_format"), trace
        except Exception as e:
            # Classify error: token limit vs other
            error_msg = str(e)
            is_token_limit = "prompt is too long" in error_msg or (
                "ValidationException" in error_msg and "tokens" in error_msg
            )
            if is_token_limit:
                return fail(e, "Token limit exceeded", "token_limit"), trace
            return fail(e, "Nudge generation", "inference"), trace
        finally:
            _release_temp_file(temp_path)

    def _parse_patient_data(self, patient_data: str) -> dict[str, Any]:
        """Parse patient data and return structured dict.

        Auto-detects CCDA XML, FHIR JSON, or pre-parsed JSON format.

        Args:
            patient_data: Raw patient document (CCDA XML, FHIR JSON, or pre-parsed JSON)

        Returns:
            Parsed patient data as dictionary

        Raises:
            CCDAParseError: If CCDA XML parsing fails
            FHIRParseError: If FHIR JSON parsing fails
            PreParsedJSONError: If pre-parsed JSON parsing fails
        """
        import json

        data = patient_data.strip()

        # Auto-detect format based on first character
        if data.startswith("<") or data.startswith("<?xml"):
            # CCDA XML format
            parsed_ccda = parse_ccda(data)
            return parsed_ccda.model_dump()
        elif data.startswith("{"):
            # JSON format - auto-detect FHIR Bundle vs pre-parsed vs generic
            parsed: dict[str, Any] = json.loads(data)

            if parsed.get("resourceType") == "Bundle":
                # FHIR Bundle format
                parsed_fhir = parse_fhir(data)
                return parsed_fhir.model_dump()

            if "demographics" in parsed:
                # Pre-parsed JSON (has demographics key - try schema validation)
                result = parse_preparsed_json(data)
                if hasattr(result, "model_dump"):
                    return result.model_dump()  # type: ignore[union-attr]
                return result  # type: ignore[return-value]

            # Generic JSON passthrough
            return parsed
        else:
            raise ValueError(
                "Unknown format. Expected CCDA XML (starts with '<') or JSON (starts with '{')"
            )
