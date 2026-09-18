"""CloudWatch custom metrics for Medical Nudging observability.

Provides functions for emitting custom metrics to CloudWatch for monitoring
request volume, latency, errors, tool calls, and nudge generation.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# CloudWatch metric namespace
METRIC_NAMESPACE = "MedicalNudging"

# Environment variable to control metric emission
METRICS_ENABLED_ENV = "METRICS_ENABLED"


def _get_default_environment() -> str:
    """Get default environment from environment variable.

    Returns:
        Environment name (dev, staging, prod)
    """
    return os.environ.get("ENVIRONMENT", "dev")


def _get_aws_region() -> str:
    """Get AWS region from environment or default to us-east-1.

    AgentCore containers may not have AWS_REGION set, so we need
    to check multiple environment variables or use a sensible default.

    Returns:
        AWS region string
    """
    region = os.environ.get(
        "AWS_REGION",
        os.environ.get("AWS_DEFAULT_REGION", ""),
    )
    if not region:
        # Fallback: try to get from boto3 session
        try:
            session = boto3.Session()
            region = session.region_name or "us-east-1"
        except Exception:
            region = "us-east-1"
    return region


# Module-level client cache
_cloudwatch_client: Any = None


def _get_cloudwatch_client() -> Any:
    """Get boto3 CloudWatch client (lazy initialization with caching).

    Returns:
        boto3 CloudWatch client
    """
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = _get_aws_region()
        logger.info(f"Creating CloudWatch client with region: {region}")
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region)
    return _cloudwatch_client


def _is_metrics_enabled() -> bool:
    """Check if metrics emission is enabled.

    Metrics are enabled by default but can be disabled via environment variable.

    Returns:
        True if metrics should be emitted
    """
    return os.environ.get(METRICS_ENABLED_ENV, "true").lower() == "true"


def put_metric(
    name: str,
    value: float,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
    namespace: str = METRIC_NAMESPACE,
) -> bool:
    """Emit a single metric to CloudWatch.

    Args:
        name: Metric name (e.g., "RequestCount", "RequestLatency")
        value: Metric value
        unit: CloudWatch unit (Count, Milliseconds, etc.)
        dimensions: Metric dimensions (e.g., {"Environment": "prod", "Specialty": "cardiology"})
        namespace: CloudWatch namespace (defaults to MedicalNudging)

    Returns:
        True if metric was emitted successfully, False otherwise
    """
    if not _is_metrics_enabled():
        logger.debug(f"Metrics disabled, skipping: {name}={value}")
        return False

    # Build dimension list with defaults
    dim_list: list[dict[str, str]] = []
    effective_dimensions = dimensions or {}

    # Always include Environment dimension
    if "Environment" not in effective_dimensions:
        effective_dimensions["Environment"] = _get_default_environment()

    for dim_name, dim_value in effective_dimensions.items():
        if dim_value:  # Skip None/empty values
            dim_list.append({"Name": dim_name, "Value": str(dim_value)})

    try:
        client = _get_cloudwatch_client()
        client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": dim_list,
                }
            ],
        )
        logger.debug(
            f"Emitted metric: {namespace}/{name}={value} {unit} dims={effective_dimensions}"
        )
        return True
    except ClientError as e:
        logger.warning(f"Failed to emit metric {name}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Unexpected error emitting metric {name}: {e}")
        return False


def increment_counter(
    name: str,
    dimensions: dict[str, str] | None = None,
    count: int = 1,
) -> bool:
    """Increment a counter metric.

    Convenience function for count-based metrics.

    Args:
        name: Counter name (e.g., "RequestCount", "ErrorCount")
        dimensions: Metric dimensions
        count: Amount to increment (default 1)

    Returns:
        True if metric was emitted successfully
    """
    return put_metric(name, float(count), unit="Count", dimensions=dimensions)


# Convenience functions for common metrics


def emit_request_count(
    specialty: str | None = None,
    visit_type: str | None = None,
) -> bool:
    """Emit RequestCount metric.

    Args:
        specialty: Medical specialty (e.g., "general", "cardiology")
        visit_type: Visit type (e.g., "ambulatory", "inpatient")

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {}
    if specialty:
        dimensions["Specialty"] = specialty
    if visit_type:
        dimensions["VisitType"] = visit_type

    return increment_counter("RequestCount", dimensions)


def emit_success_count(
    specialty: str | None = None,
    visit_type: str | None = None,
) -> bool:
    """Emit SuccessCount metric.

    Args:
        specialty: Medical specialty
        visit_type: Visit type

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {}
    if specialty:
        dimensions["Specialty"] = specialty
    if visit_type:
        dimensions["VisitType"] = visit_type

    return increment_counter("SuccessCount", dimensions)


def emit_error_count(
    specialty: str | None = None,
    visit_type: str | None = None,
    error_type: str | None = None,
) -> bool:
    """Emit ErrorCount metric.

    Args:
        specialty: Medical specialty
        visit_type: Visit type
        error_type: Type of error (e.g., "CCDAParseError", "TimeoutError")

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {}
    if specialty:
        dimensions["Specialty"] = specialty
    if visit_type:
        dimensions["VisitType"] = visit_type
    if error_type:
        dimensions["ErrorType"] = error_type

    return increment_counter("ErrorCount", dimensions)


def emit_request_latency(
    duration_ms: int,
    specialty: str | None = None,
    visit_type: str | None = None,
) -> bool:
    """Emit RequestLatency metric.

    Args:
        duration_ms: Request duration in milliseconds
        specialty: Medical specialty
        visit_type: Visit type

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {}
    if specialty:
        dimensions["Specialty"] = specialty
    if visit_type:
        dimensions["VisitType"] = visit_type

    return put_metric(
        "RequestLatency", float(duration_ms), unit="Milliseconds", dimensions=dimensions
    )


def emit_inference_latency(
    duration_ms: int,
    mode: str = "agentcore",
    status: str = "success",
) -> bool:
    """Emit InferenceLatency metric with Mode/Status dimensions.

    Args:
        duration_ms: Inference duration in milliseconds
        mode: Execution mode (agentcore, local)
        status: Inference status (success, error)

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {"Mode": mode, "Status": status}
    return put_metric(
        "InferenceLatency", float(duration_ms), unit="Milliseconds", dimensions=dimensions
    )


def emit_tool_call_count(
    tool_name: str,
    specialty: str | None = None,
) -> bool:
    """Emit ToolCallCount metric.

    Args:
        tool_name: Name of the tool (e.g., "search_guidelines", "get_patient_data")
        specialty: Medical specialty

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {"ToolName": tool_name}
    if specialty:
        dimensions["Specialty"] = specialty

    return increment_counter("ToolCallCount", dimensions)


def emit_nudge_count(
    count: int,
    specialty: str | None = None,
    visit_type: str | None = None,
) -> bool:
    """Emit NudgeCount metric.

    Args:
        count: Number of nudges generated
        specialty: Medical specialty
        visit_type: Visit type

    Returns:
        True if metric was emitted successfully
    """
    dimensions = {}
    if specialty:
        dimensions["Specialty"] = specialty
    if visit_type:
        dimensions["VisitType"] = visit_type

    return put_metric("NudgeCount", float(count), unit="Count", dimensions=dimensions)
