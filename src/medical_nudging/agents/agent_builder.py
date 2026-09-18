"""Agent and tool configuration for the medical nudging system.

Builds Strands Agent instances with Bedrock model config, tool selection,
prompt caching, tracing callbacks, and observability hooks.
"""

import logging
import os
from typing import Any
from botocore.config import Config as BotocoreConfig

from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.types.content import SystemContentBlock

from medical_nudging.config import get_model_config, observability_enabled
from medical_nudging.search.backend import SearchBackend
from medical_nudging.tools import (
    create_calculator_tool,
    get_patient_data,
    invoke_subagent,
    list_guidelines,
    list_sources,
)
from medical_nudging.tracing.trace_capture import ExecutionTrace, TraceCapturingCallback
from medical_nudging.tracing.observability_events import emit_processing_started
from medical_nudging.tracing.strands_metrics import ObservabilityHookProvider

logger = logging.getLogger(__name__)

# Valid context conditions for experiments
CONTEXT_CONDITIONS = ("full", "summaries_only", "no_guidelines")

# Valid search modes for production use
SEARCH_MODES = ("auto", "knowledge_base", "summaries")


def resolve_effective_mode(search_mode: str | None, context_condition: str) -> str:
    """Map search_mode to effective mode string.

    ``search_mode`` (production request-level) takes precedence over
    ``context_condition`` (experiment-level default set at init).

    Args:
        search_mode: Request-level search mode (``"knowledge_base"``,
            ``"summaries"``, ``"auto"``, or ``None``)
        context_condition: Experiment-level default (``"full"``,
            ``"summaries_only"``, ``"no_guidelines"``)

    Returns:
        One of ``"full"``, ``"summaries_only"``, or ``"no_guidelines"``
    """
    if search_mode in ("knowledge_base", "auto"):
        return "full"
    if search_mode == "summaries":
        return "summaries_only"
    return context_condition


def build_tool_list(
    effective_mode: str,
    search_mode: str | None = None,
    fhir_api: bool = False,
    tool_limits: dict[str, Any] | None = None,
) -> list[Any]:
    """Build tool list for the given effective mode.

    Args:
        effective_mode: One of ``"full"``, ``"summaries_only"``, ``"no_guidelines"``
        search_mode: Original search_mode value; ``"auto"`` triggers OpenSearch probe
        fhir_api: When ``True``, include FHIR query tool and exclude
            ``get_patient_data`` (no document to parse in FHIR API mode)
        tool_limits: Request-scoped search and FHIR budget configuration

    Returns:
        List of tools configured for the mode
    """
    # Base tools — exclude get_patient_data in FHIR API mode (no document exists)
    # Specialty instructions are pre-loaded in the user prompt (not a tool).
    if fhir_api:
        tools: list[Any] = []
    else:
        tools = [get_patient_data]

    if effective_mode == "full":
        if search_mode == "auto":
            tools.extend(_auto_search_tools(tool_limits))
        else:
            from medical_nudging.search import get_search_backend

            tools.extend(_knowledge_base_tools(get_search_backend("opensearch"), tool_limits))
    elif effective_mode == "summaries_only":
        logger.info("Using summaries search mode (local ripgrep)")
        tools.extend(_summaries_tools(tool_limits))
    else:
        # no_guidelines: minimal tools, model relies on trained knowledge
        tools.append(invoke_subagent)
        logger.info("Running in no_guidelines mode - model will use trained knowledge only")

    # Add FHIR query tool when requested
    if fhir_api:
        from medical_nudging.tools.fhir_query import create_fhir_query_tool_from_config

        fhir_tool = create_fhir_query_tool_from_config(tool_limits)
        if fhir_tool:
            tools.append(fhir_tool)
        else:
            logger.warning("FHIR API requested but tool creation failed; check fhir_api config")

    tools.append(create_calculator_tool())

    return tools


def _search_limits(tool_limits: dict[str, Any] | None) -> dict[str, Any]:
    limits = (tool_limits or {}).get("guideline_search")
    return dict(limits) if isinstance(limits, dict) else {}


def _knowledge_base_tools(backend: SearchBackend, tool_limits: dict[str, Any] | None) -> list[Any]:
    """Guideline tools over a full-text backend: search, catalog, sources, subagent."""
    from medical_nudging.tools.guideline_search import create_search_guidelines_tool

    limits = _search_limits(tool_limits)
    search_tool = create_search_guidelines_tool(
        backend,
        max_calls=limits.get("max_calls"),
        max_results_per_call=limits.get("max_results_per_call"),
    )
    return [search_tool, list_guidelines, list_sources, invoke_subagent]


def _summaries_tools(tool_limits: dict[str, Any] | None) -> list[Any]:
    """Guideline tools over the local summaries directory (no source listing)."""
    from medical_nudging.tools.guideline_search import create_summaries_search_tool

    limits = _search_limits(tool_limits)
    summaries_search = create_summaries_search_tool(
        max_calls=limits.get("max_calls"),
        max_results_per_call=limits.get("max_results_per_call"),
    )
    return [summaries_search, list_guidelines, invoke_subagent]


def _auto_search_tools(tool_limits: dict[str, Any] | None = None) -> list[Any]:
    """Probe OpenSearch availability and return appropriate search tools.

    Returns full OpenSearch tools if available, otherwise falls back to
    ripgrep-based summaries search.
    """
    from medical_nudging.search import get_search_backend

    try:
        backend = get_search_backend("opensearch")
        if backend.is_available():
            logger.info("Auto search mode: using knowledge_base (OpenSearch available)")
            return _knowledge_base_tools(backend, tool_limits)
    except (ImportError, RuntimeError, OSError) as e:
        logger.info(f"Auto search mode: falling back to summaries ({e})")

    # OpenSearch unavailable or probe failed -- fall back to summaries
    return _summaries_tools(tool_limits)


def build_agent(
    model_id: str | None,
    extra_headers: dict[str, Any] | None,
    system_prompt: str,
    tools: list[Any],
    enable_trace: bool,
    trace: ExecutionTrace | None,
    request_id: str,
    plugins: list[Any] | None = None,
) -> Agent:
    """Construct a Strands Agent with model config, caching, callbacks, and hooks.

    Args:
        model_id: Override model ID (``None`` uses config default)
        extra_headers: Additional Bedrock ``additionalModelRequestFields``
        system_prompt: The system prompt text
        tools: Tools to provide to the agent
        enable_trace: Whether trace capture is enabled
        trace: ExecutionTrace instance (may be ``None``)
        request_id: Unique request ID for observability correlation
        plugins: Optional Strands plugins, e.g. the contract-enforcing steering handler
            used for steered source-nudge generation

    Returns:
        Configured Strands Agent ready for invocation
    """
    model_config = get_model_config()
    effective_model_id = model_id or model_config["model_id"]
    thinking_fields = _thinking_request_fields(model_config, effective_model_id)

    bedrock_model = _bedrock_model(model_config, effective_model_id, thinking_fields, extra_headers)

    emit_processing_started(
        request_id=request_id,
        model_id=effective_model_id,
        thinking_enabled=bool(thinking_fields),
    )

    agent_kwargs: dict[str, Any] = {
        "model": bedrock_model,
        "system_prompt": _system_content(system_prompt, model_config, effective_model_id),
        "tools": tools,
    }
    callback_handler, hooks = _agent_hooks(request_id, enable_trace, trace)
    if callback_handler is not None:
        agent_kwargs["callback_handler"] = callback_handler
    if hooks:
        agent_kwargs["hooks"] = hooks
    if plugins:
        agent_kwargs["plugins"] = list(plugins)

    return Agent(**agent_kwargs)


def _thinking_request_fields(model_config: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Bedrock request fields for Anthropic thinking; empty when disabled or not Anthropic."""
    thinking_type = model_config.get("thinking_type", "adaptive")
    if thinking_type == "disabled" or "anthropic" not in model_id:
        return {}
    if thinking_type == "enabled":
        budget_tokens = model_config.get("budget_tokens", 10000)
        return {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
    # adaptive (default)
    fields: dict[str, Any] = {"thinking": {"type": "adaptive"}}
    effort = model_config.get("effort", "high")
    if effort:
        fields["output_config"] = {"effort": effort}
    return fields


def _bedrock_model(
    model_config: dict[str, Any],
    model_id: str,
    thinking_fields: dict[str, Any],
    extra_headers: dict[str, Any] | None,
) -> BedrockModel:
    """BedrockModel with thinking, extra request fields, timeouts, and sampling settings."""
    additional_fields = dict(thinking_fields)
    # Merge extra headers (e.g., anthropic-beta for 1M context)
    extra = extra_headers or model_config.get("extra_headers", {})
    if extra:
        additional_fields.update(extra)

    bedrock_kwargs: dict[str, Any] = {
        "model_id": model_id,
        "boto_client_config": BotocoreConfig(
            read_timeout=model_config.get("read_timeout", 600),
            retries={"max_attempts": 2},
        ),
    }
    if additional_fields:
        bedrock_kwargs["additional_request_fields"] = additional_fields
    temperature = model_config.get("temperature")
    if temperature is not None:
        bedrock_kwargs["temperature"] = temperature
    # Without an explicit maxTokens the Bedrock service default applies, which is
    # small enough that thinking plus a large structured-output payload gets
    # truncated mid tool call.
    max_tokens = model_config.get("max_tokens")
    if max_tokens is not None:
        bedrock_kwargs["max_tokens"] = max_tokens
    return BedrockModel(**bedrock_kwargs)


def _system_content(
    system_prompt: str, model_config: dict[str, Any], model_id: str
) -> str | list[SystemContentBlock]:
    """System prompt, with a cache point appended when the model supports prompt caching."""
    supports_caching = "anthropic" in model_id or "amazon.nova" in model_id
    if model_config["cache_system_prompt"] and supports_caching:
        return [
            SystemContentBlock(text=system_prompt),
            SystemContentBlock(cachePoint={"type": "default"}),
        ]
    return system_prompt


def _agent_hooks(
    request_id: str, enable_trace: bool, trace: ExecutionTrace | None
) -> tuple[TraceCapturingCallback | None, list[Any]]:
    """Trace-capturing callback (when tracing or observability is on) and the hook list.

    The callback doubles as a hook; the observability hook joins it when observability
    is on.
    """
    hooks: list[Any] = []
    callback_handler: TraceCapturingCallback | None = None
    observability = observability_enabled()
    if enable_trace or observability:
        callback_handler = TraceCapturingCallback(request_id=request_id)
        if enable_trace and trace:
            callback_handler.trace = trace
        hooks.append(callback_handler)
    if observability:
        hooks.append(
            ObservabilityHookProvider(
                request_id=request_id,
                session_id=None,
                bucket_name=os.environ.get("OBSERVABILITY_BUCKET_NAME"),
            )
        )
    return callback_handler, hooks
