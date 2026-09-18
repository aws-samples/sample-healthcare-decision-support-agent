"""Strands SDK metrics extraction and S3 trace writing.

Provides hook-based comprehensive metrics capture via AfterInvocationEvent.
Writes full traces to S3 (untruncated, gzipped) for evaluation support.
Emits summary metrics via SNS for observability queries.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import boto3
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent

if TYPE_CHECKING:
    from strands.agent import AgentResult

logger = logging.getLogger(__name__)

# Lazy S3 client
_s3_client = None


def _get_s3_client() -> Any:
    """Get or create S3 client (lazy initialization)."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def extract_summary_metrics(result: AgentResult) -> dict[str, Any] | None:
    """Extract summary metrics from Strands AgentResult for SNS emission.

    Args:
        result: AgentResult from agent invocation

    Returns:
        Dictionary with token_usage, performance, tool_summary, cycles.
        None if metrics not available.
    """
    if not hasattr(result, "metrics") or not result.metrics:
        return None

    m = result.metrics
    usage = m.accumulated_usage or {}
    perf = m.accumulated_metrics or {}

    return {
        "token_usage": {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "total_tokens": usage.get("totalTokens", 0),
            "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
            "cache_write_tokens": usage.get("cacheWriteInputTokens", 0),
        },
        "performance": {
            "model_latency_ms": perf.get("latencyMs", 0),
            "time_to_first_byte_ms": perf.get("timeToFirstByteMs", 0),
        },
        "tool_summary": {
            name: {
                "calls": tm.call_count,
                "successes": tm.success_count,
                "errors": tm.error_count,
                "total_time_s": round(tm.total_time, 3),
            }
            for name, tm in (m.tool_metrics or {}).items()
        },
        "cycles": {
            "count": m.cycle_count,
            "total_duration_s": round(sum(m.cycle_durations or []), 3),
        },
    }


def write_trace_to_s3(
    request_id: str,
    result: AgentResult,
    bucket_name: str,
) -> str | None:
    """Write full trace to S3 (untruncated, gzipped).

    Writes the complete Strands trace hierarchy including tool outputs
    for evaluation support.

    Args:
        request_id: Unique request identifier
        result: AgentResult from agent invocation
        bucket_name: S3 bucket name

    Returns:
        S3 key if successful, None otherwise
    """
    if not hasattr(result, "metrics") or not result.metrics:
        return None

    traces = result.metrics.traces
    if not traces:
        return None

    # Serialize traces using to_dict() method
    trace_data = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "traces": [t.to_dict() for t in traces],
        "tool_metrics": {
            name: {
                "call_count": tm.call_count,
                "success_count": tm.success_count,
                "error_count": tm.error_count,
                "total_time": tm.total_time,
            }
            for name, tm in (result.metrics.tool_metrics or {}).items()
        },
        "accumulated_usage": result.metrics.accumulated_usage,
        "accumulated_metrics": result.metrics.accumulated_metrics,
        "cycle_count": result.metrics.cycle_count,
        "cycle_durations": result.metrics.cycle_durations,
    }

    # Compress with gzip
    json_bytes = json.dumps(trace_data, default=str).encode("utf-8")
    compressed = gzip.compress(json_bytes)

    # Generate S3 key with date partitioning
    now = datetime.now(timezone.utc)
    s3_key = f"traces/year={now.year}/month={now.month:02d}/day={now.day:02d}/{request_id}.json.gz"

    try:
        _get_s3_client().put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=compressed,
            ContentType="application/gzip",
        )
        logger.debug(f"Wrote trace to s3://{bucket_name}/{s3_key}")
        return s3_key
    except Exception as e:
        logger.warning(f"Failed to write trace to S3: {e}")
        return None


class ObservabilityHookProvider(HookProvider):
    """Hook provider for emitting comprehensive metrics after agent invocation.

    Registers an AfterInvocationEvent callback that:
    1. Writes full traces to S3 (untruncated) for evaluation
    2. Emits summary metrics via SNS for observability

    Usage:
        hook = ObservabilityHookProvider(
            request_id=request_id,
            session_id=session_id,
            bucket_name=os.environ.get("OBSERVABILITY_BUCKET_NAME"),
        )
        agent = Agent(..., hooks=[hook])
    """

    def __init__(
        self,
        request_id: str,
        session_id: str | None = None,
        bucket_name: str | None = None,
    ) -> None:
        """Initialize the hook provider.

        Args:
            request_id: Unique request identifier for correlation
            session_id: AgentCore session ID (optional)
            bucket_name: S3 bucket for traces (optional, skips S3 write if not provided)
        """
        self.request_id = request_id
        self.session_id = session_id
        self.bucket_name = bucket_name

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register callbacks with the hook registry.

        Args:
            registry: HookRegistry to register callbacks with
            **kwargs: Additional arguments (ignored)
        """
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)

    def _on_after_invocation(self, event: AfterInvocationEvent) -> None:
        """Handle AfterInvocationEvent to extract and emit metrics.

        This callback fires after the agent completes invocation.
        Errors are caught and logged but not raised (fire-and-forget pattern).

        Args:
            event: AfterInvocationEvent containing the AgentResult
        """
        if not event.result:
            return

        try:
            # 1. Write full trace to S3 (untruncated)
            trace_s3_key = None
            if self.bucket_name:
                trace_s3_key = write_trace_to_s3(self.request_id, event.result, self.bucket_name)

            # 2. Emit summary metrics via SNS
            summary = extract_summary_metrics(event.result)
            if summary:
                # Import here to avoid circular dependency
                from medical_nudging.tracing.observability_events import (
                    emit_strands_metrics,
                )

                emit_strands_metrics(
                    request_id=self.request_id,
                    session_id=self.session_id,
                    strands_metrics=summary,
                    trace_s3_key=trace_s3_key,
                )
        except Exception as e:
            # Fire-and-forget: log but don't break main flow
            logger.warning(f"Failed to emit metrics from hook: {e}")
