"""AgentCore Observability integration for retrieving rich traces.

This module uses the bedrock_agentcore_starter_toolkit's Observability class
to retrieve proper OTEL traces from AgentCore, including:
- Agent invocation spans
- Model call spans with token usage
- Tool execution spans with timing
- Complete message history

This replaces the basic CloudWatch Logs approach that only captured HTTP logs.
"""

import logging
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class AgentCoreTraceRetriever:
    """Retrieve rich traces from AgentCore using the Observability API.

    Uses the bedrock_agentcore_starter_toolkit to query spans and traces
    from CloudWatch, providing full visibility into agent execution.
    """

    def __init__(
        self,
        agent_id: str,
        region: str | None = None,
        profile: str | None = None,
    ):
        """Initialize the trace retriever.

        Args:
            agent_id: AgentCore runtime ID (extracted from ARN)
            region: AWS region
            profile: AWS profile name
        """
        self.agent_id = agent_id
        self.region = region
        self.profile = profile
        self._client = None

    def _get_client(self) -> Any:
        """Lazy-load the observability client."""
        if self._client is None:
            try:
                from bedrock_agentcore_starter_toolkit.operations.observability import (
                    ObservabilityClient,
                )

                self._client = ObservabilityClient(
                    region_name=self.region or "us-east-1",
                )
            except ImportError:
                logger.error(
                    "bedrock_agentcore_starter_toolkit not available. "
                    "Install with: pip install bedrock-agentcore-starter-toolkit"
                )
                raise
        return self._client

    def get_traces_for_session(
        self,
        session_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> dict | None:
        """Get traces for a specific session.

        Args:
            session_id: The session ID to retrieve traces for
            start_time: Start of time window
            end_time: End of time window

        Returns:
            Trace data dict with spans, runtime logs, and timing, or None if not found
        """
        try:
            from bedrock_agentcore_starter_toolkit.operations.observability import TraceData
            from bedrock_agentcore_starter_toolkit.operations.observability.trace_processor import (
                TraceProcessor,
            )

            client = self._get_client()

            # Convert to milliseconds for the API
            start_ms = int(start_time.timestamp() * 1000)
            end_ms = int(end_time.timestamp() * 1000)

            # Query spans by session
            spans = client.query_spans_by_session(
                session_id=session_id,
                start_time_ms=start_ms,
                end_time_ms=end_ms,
                agent_id=self.agent_id,
            )

            if not spans:
                logger.debug("No spans found for session %s", session_id)
                return None

            # Build trace data structure
            trace_data = TraceData(
                session_id=session_id,
                spans=spans,
                agent_id=self.agent_id,
            )
            TraceProcessor.group_spans_by_trace(trace_data)

            # Extract unique trace IDs from spans
            trace_ids = list(set(s.trace_id for s in spans if s.trace_id))

            # Query runtime logs for events (contains tool inputs/outputs, messages)
            runtime_logs = []
            if trace_ids:
                try:
                    runtime_logs = client.query_runtime_logs_by_traces(
                        trace_ids=trace_ids,
                        start_time_ms=start_ms,
                        end_time_ms=end_ms,
                        agent_id=self.agent_id,
                        endpoint_name="DEFAULT",
                    )
                    logger.debug(
                        "Retrieved %d runtime logs for session %s",
                        len(runtime_logs),
                        session_id,
                    )
                except Exception as e:
                    # Log but don't fail - runtime logs are optional enhancement
                    logger.debug(
                        "Could not retrieve runtime logs for session %s: %s",
                        session_id,
                        e,
                    )

            # Add runtime logs to trace data
            trace_data.runtime_logs = runtime_logs

            # Convert to dict format compatible with our trace output
            result = TraceProcessor.to_dict(trace_data)

            # Add runtime logs to result (TraceProcessor.to_dict may not include them)
            if runtime_logs:
                result["runtime_logs"] = [
                    {
                        "timestamp": log.timestamp,
                        "message": log.message,
                        "span_id": log.span_id,
                        "trace_id": log.trace_id,
                        "raw_message": log.raw_message,
                    }
                    for log in runtime_logs
                ]

            return result

        except Exception as e:
            logger.warning("Failed to retrieve traces for session %s: %s", session_id, e)
            return None

    def get_traces_in_window(
        self,
        start_time: datetime,
        end_time: datetime,
        limit: int = 100,
    ) -> list[dict]:
        """Get all traces in a time window.

        Args:
            start_time: Start of time window
            end_time: End of time window
            limit: Maximum number of traces to return

        Returns:
            List of trace data dicts
        """
        try:
            from bedrock_agentcore_starter_toolkit.operations.observability import TraceData
            from bedrock_agentcore_starter_toolkit.operations.observability.trace_processor import (
                TraceProcessor,
            )

            client = self._get_client()

            # Convert to milliseconds for the API
            start_ms = int(start_time.timestamp() * 1000)
            end_ms = int(end_time.timestamp() * 1000)

            # Get ALL sessions in the time window (not just latest)
            session_ids = self._get_all_session_ids(
                client=client,
                start_time_ms=start_ms,
                end_time_ms=end_ms,
                limit=limit,
            )

            if not session_ids:
                logger.debug("No sessions found in time window")
                return []

            logger.debug("Found %d sessions in time window", len(session_ids))

            # Query spans for ALL sessions
            all_traces = []
            for session_id in session_ids:
                spans = client.query_spans_by_session(
                    session_id=session_id,
                    start_time_ms=start_ms,
                    end_time_ms=end_ms,
                    agent_id=self.agent_id,
                )

                if not spans:
                    continue

                # Build and process trace data
                trace_data = TraceData(
                    session_id=session_id,
                    spans=spans,
                    agent_id=self.agent_id,
                )
                TraceProcessor.group_spans_by_trace(trace_data)

                # Add individual traces from this session
                for trace_id, trace_spans in trace_data.traces.items():
                    trace_dict = {
                        "trace_id": trace_id,
                        "session_id": session_id,
                        "span_count": len(trace_spans),
                        "total_duration_ms": TraceProcessor.calculate_trace_duration(trace_spans),
                        "error_count": TraceProcessor.count_error_spans(trace_spans),
                        "spans": [self._span_to_dict(s) for s in trace_spans],
                    }
                    all_traces.append(trace_dict)

                if len(all_traces) >= limit:
                    break

            return all_traces[:limit]

        except Exception as e:
            logger.warning("Failed to retrieve traces in window: %s", e)
            return []

    def _get_all_session_ids(
        self,
        client: Any,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 100,
    ) -> list[str]:
        """Get all unique session IDs in a time window.

        Args:
            client: ObservabilityClient instance
            start_time_ms: Start time in milliseconds
            end_time_ms: End time in milliseconds
            limit: Maximum sessions to return

        Returns:
            List of session IDs
        """
        # Use the query builder to get sessions with higher limit
        query_string = client.query_builder.build_latest_session_query(self.agent_id, limit=limit)

        results = client._execute_cloudwatch_query(
            query_string=query_string,
            log_group_name=client.SPANS_LOG_GROUP,
            start_time=start_time_ms,
            end_time=end_time_ms,
        )

        session_ids = []
        for result in results:
            for field in result:
                if field.get("field") == "attributes.session.id":
                    session_id = field.get("value")
                    if session_id and session_id not in session_ids:
                        session_ids.append(session_id)
                    break

        return session_ids

    def _span_to_dict(self, span: Any) -> dict:
        """Convert a Span object to a dictionary."""
        return {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "span_name": span.span_name,
            "duration_ms": span.duration_ms,
            "status_code": span.status_code,
            "status_message": span.status_message,
            "parent_span_id": span.parent_span_id,
            "attributes": span.attributes,
            "events": span.events,
        }


def extract_agent_id_from_arn(agent_arn: str) -> str | None:
    """Extract the agent ID from an AgentCore ARN.

    Args:
        agent_arn: Full ARN (e.g., arn:aws:bedrock-agentcore:...:runtime/my-agent)

    Returns:
        Agent ID or None if cannot be parsed
    """
    try:
        if ":runtime/" in agent_arn:
            return agent_arn.split(":runtime/")[-1]
    except Exception:
        pass
    return None


def fetch_agentcore_traces_otel(
    results: list[Any],
    agent_arn: str,
    region: str,
    profile: str | None,
) -> list[tuple[str, dict | None]]:
    """Fetch rich traces from AgentCore using OTEL/Observability API.

    Uses session_id from InferenceResult for direct trace lookup when available.
    Results without a session_id get no trace.

    Args:
        results: List of InferenceResults with timing info and session_id
        agent_arn: AgentCore runtime ARN
        region: AWS region
        profile: AWS profile name

    Returns:
        List of (sample_id, trace_dict) tuples
    """
    agent_id = extract_agent_id_from_arn(agent_arn)
    if not agent_id:
        logger.warning("Could not extract agent ID from ARN: %s", agent_arn)
        return _empty_traces(results)

    window = _trace_window(results)
    if window is None:
        logger.warning("No invocation timing data available for trace fetch")
        return _empty_traces(results)
    window_start, window_end = window

    try:
        retriever = AgentCoreTraceRetriever(agent_id=agent_id, region=region, profile=profile)
        logger.info(
            "Fetching OTEL traces from AgentCore (%s to %s)",
            window_start.strftime("%H:%M:%S"),
            window_end.strftime("%H:%M:%S"),
        )
        results_with_session = sum(1 for r in results if getattr(r, "session_id", None))
        logger.info(
            "Found %d/%d results with session_id for direct lookup",
            results_with_session,
            len(results),
        )
        traces = [
            (result.sample_id, _fetch_trace_for_result(retriever, result, window_start, window_end))
            for result in results
        ]
        logger.info(
            "Retrieved OTEL traces for %d/%d invocations",
            sum(1 for _, t in traces if t),
            len(results),
        )
        return traces
    except ImportError:
        logger.warning(
            "bedrock_agentcore_starter_toolkit not available. "
            "Install with: pip install bedrock-agentcore-starter-toolkit"
        )
        return _empty_traces(results)
    except Exception as e:
        logger.warning("Failed to fetch OTEL traces: %s", e)
        return _empty_traces(results)


def _empty_traces(results: list[Any]) -> list[tuple[str, dict | None]]:
    """One (sample_id, None) entry per result: nothing could be fetched."""
    return [(r.sample_id, None) for r in results]


def _trace_window(results: list[Any]) -> tuple[datetime, datetime] | None:
    """Overall query window across all results, padded for OTEL processing delay.

    The trailing 90 s buffer follows the AWS samples' recommendation.
    """
    start_times = [r.invocation_start for r in results if r.invocation_start]
    end_times = [r.invocation_end for r in results if r.invocation_end]
    if not start_times or not end_times:
        return None
    return min(start_times) - timedelta(seconds=10), max(end_times) + timedelta(seconds=90)


def _fetch_trace_for_result(
    retriever: AgentCoreTraceRetriever,
    result: Any,
    window_start: datetime,
    window_end: datetime,
) -> dict | None:
    """Look up one result's trace by session_id; None when there is nothing to fetch."""
    session_id = getattr(result, "session_id", None)
    if not session_id:
        logger.warning("No session_id for sample %s - cannot retrieve trace", result.sample_id)
        return None
    trace_data = retriever.get_traces_for_session(
        session_id=session_id, start_time=window_start, end_time=window_end
    )
    if not trace_data:
        logger.warning("No trace found for session %s (sample: %s)", session_id, result.sample_id)
        return None
    return _trace_dict_for_session(result, session_id, trace_data)


def _trace_dict_for_session(result: Any, session_id: str, trace_data: dict) -> dict:
    """Flatten one session's OTEL traces and runtime logs into the saved trace shape."""
    # TraceProcessor.to_dict returns: {"traces": {trace_id: {"root_spans": [...]}}}
    all_spans: list[dict] = []
    first_trace_id = None
    total_error_count = 0
    for trace_id, trace_info in trace_data.get("traces", {}).items():
        if first_trace_id is None:
            first_trace_id = trace_id
        # Flatten root_spans (they may have nested children)
        all_spans.extend(_flatten_spans(trace_info.get("root_spans", [])))
        total_error_count += trace_info.get("error_count", 0)

    # Messages (tool inputs/outputs, prompts) come from the runtime logs
    runtime_logs = trace_data.get("runtime_logs", [])
    extracted = _extract_messages_from_logs(runtime_logs)
    token_usage = _extract_token_usage(all_spans)
    tool_time = _calculate_tool_time(all_spans)
    logger.debug(
        "Retrieved trace for session %s (%d spans, %d runtime logs, %d tokens)",
        session_id,
        len(all_spans),
        len(runtime_logs),
        token_usage.get("total_tokens", 0),
    )
    return {
        "sample_id": result.sample_id,
        "source": "agentcore_otel",
        "trace_id": first_trace_id,
        "session_id": session_id,
        "timing": {
            "total_ms": result.latency_ms,
            "tool_ms": tool_time,
            "model_ms": result.latency_ms - tool_time,
        },
        "spans": all_spans,
        "span_count": trace_data.get("total_span_count", len(all_spans)),
        "error_count": total_error_count,
        "system_prompt": extracted.get("system_prompt"),
        "user_prompt": extracted.get("user_prompt"),
        "messages": extracted.get("messages", []),
        "tool_inputs": extracted.get("tool_inputs", {}),
        "tool_outputs": extracted.get("tool_outputs", {}),
        "token_usage": token_usage,
    }


def _flatten_spans(spans: list[dict]) -> list[dict]:
    """Recursively flatten hierarchical spans into a flat list.

    Args:
        spans: List of span dicts, potentially with "children" nested spans

    Returns:
        Flat list of all spans
    """
    flat = []
    for span in spans:
        # Add the span itself (without children to avoid duplication)
        span_copy = {k: v for k, v in span.items() if k != "children"}
        flat.append(span_copy)
        # Recursively add children
        children = span.get("children", [])
        if children:
            flat.extend(_flatten_spans(children))
    return flat


def _calculate_tool_time(spans: list[dict]) -> int:
    """Calculate total time spent in tool calls from spans.

    Args:
        spans: List of span dicts

    Returns:
        Total tool time in milliseconds
    """
    tool_time = 0
    for span in spans:
        span_name = span.get("span_name", "")
        if "tool" in span_name.lower() or "execute_tool" in span_name:
            tool_time += span.get("duration_ms", 0) or 0
    return tool_time


def _extract_text_content(content: Any) -> str:
    """Extract text from message content (handles various formats).

    Args:
        content: Message content (string, list of blocks, or dict)

    Returns:
        Extracted text string
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Handle Anthropic content blocks format
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
                elif "text" in block:
                    texts.append(str(block["text"]))
        return "\n".join(texts)
    if isinstance(content, dict):
        text_val = content.get("text")
        return str(text_val) if text_val is not None else str(content)
    return str(content) if content else ""


@dataclass
class _ConversationExtract:
    """Accumulator for what the runtime logs reveal about one conversation."""

    messages: list[dict] = dataclasses.field(default_factory=list)
    system_prompt: str | None = None
    user_prompt: str | None = None
    tool_inputs: dict[str, Any] = dataclasses.field(default_factory=dict)
    tool_outputs: dict[str, Any] = dataclasses.field(default_factory=dict)
    seen_messages: set[str] = dataclasses.field(default_factory=set)

    def add_message(self, msg: dict) -> None:
        """Keep the first occurrence of each role/content prefix."""
        key = f"{msg.get('role', '')}:{str(msg.get('content', ''))[:100]}"
        if key not in self.seen_messages:
            self.seen_messages.add(key)
            self.messages.append(msg)

    def as_dict(self) -> dict:
        return {
            "messages": self.messages,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "tool_inputs": self.tool_inputs,
            "tool_outputs": self.tool_outputs,
        }


def _system_prompt_text(system_content: Any) -> str:
    """Join a list of system blocks, else fall back to generic text extraction."""
    if isinstance(system_content, list):
        texts = [
            block["text"] for block in system_content if isinstance(block, dict) and "text" in block
        ]
        return "\n".join(texts)
    return _extract_text_content(system_content)


def _collect_input(extract: _ConversationExtract, input_data: dict, span_id: Any) -> None:
    """Record the system prompt, input messages, and first user prompt from one event."""
    if "system" in input_data and not extract.system_prompt:
        extract.system_prompt = _system_prompt_text(input_data["system"])
    for msg in input_data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        extract.add_message(msg)
        if msg.get("role") == "user" and not extract.user_prompt:
            extract.user_prompt = _extract_text_content(msg.get("content", ""))
    if span_id:
        extract.tool_inputs[span_id] = input_data


def _collect_output(extract: _ConversationExtract, output_data: dict, span_id: Any) -> None:
    """Record output messages from one event."""
    for msg in output_data.get("messages", []):
        if isinstance(msg, dict):
            extract.add_message(msg)
    if span_id:
        extract.tool_outputs[span_id] = output_data


def _extract_messages_from_logs(runtime_logs: list[dict]) -> dict:
    """Extract tool inputs/outputs and messages from runtime logs.

    Runtime logs contain event bodies with input/output messages that capture
    the full conversation flow including tool calls.

    Args:
        runtime_logs: List of runtime log dicts with raw_message field

    Returns:
        Dict with messages, system_prompt, user_prompt, tool_inputs, tool_outputs
    """
    extract = _ConversationExtract()
    for log in runtime_logs:
        raw_message = log.get("raw_message")
        if not raw_message or not isinstance(raw_message, dict):
            continue
        body = raw_message.get("body", {})
        # Skip non-dict bodies (some logs have string bodies like "Processing...")
        if not isinstance(body, dict):
            continue
        span_id = log.get("span_id")
        if "input" in body:
            _collect_input(extract, body["input"], span_id)
        if "output" in body:
            _collect_output(extract, body["output"], span_id)
    return extract.as_dict()


def _extract_token_usage(spans: list[dict]) -> dict[str, Any]:
    """Extract token usage from chat spans.

    Chat spans have gen_ai.usage.* attributes with token counts per model call.

    Args:
        spans: List of span dicts with attributes

    Returns:
        Dict with total_input_tokens, total_output_tokens, total_tokens, per_span breakdown
    """
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    per_span: list[dict[str, Any]] = []

    for span in spans:
        attrs = span.get("attributes", {})
        if not attrs:
            continue

        # Look for token usage attributes (different naming conventions)
        input_tokens = attrs.get("gen_ai.usage.input_tokens") or attrs.get(
            "gen_ai.usage.prompt_tokens"
        )
        output_tokens = attrs.get("gen_ai.usage.output_tokens") or attrs.get(
            "gen_ai.usage.completion_tokens"
        )

        # Only include spans with token usage data
        if input_tokens is not None or output_tokens is not None:
            input_val = int(input_tokens) if input_tokens is not None else 0
            output_val = int(output_tokens) if output_tokens is not None else 0

            total_input_tokens += input_val
            total_output_tokens += output_val
            total_tokens += input_val + output_val

            per_span.append(
                {
                    "span_id": span.get("span_id"),
                    "span_name": span.get("span_name"),
                    "input_tokens": input_val if input_tokens is not None else None,
                    "output_tokens": output_val if output_tokens is not None else None,
                    "model": attrs.get("gen_ai.request.model"),
                    "time_to_first_token": attrs.get("gen_ai.server.time_to_first_token"),
                    "request_duration": attrs.get("gen_ai.server.request.duration"),
                }
            )

    return {
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,
        "per_span": per_span,
    }
