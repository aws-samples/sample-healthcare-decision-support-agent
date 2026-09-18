"""CloudWatch Metrics publisher for agent observability."""

import logging
from typing import Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CloudWatchMetricsPublisher:
    """Publish metrics to CloudWatch for agent monitoring."""

    NAMESPACE = "MedicalNudging"

    def __init__(self, region: str | None = None, profile: str | None = None):
        """Initialize CloudWatch client.

        Args:
            region: AWS region (uses default if not specified)
            profile: AWS profile name (uses default if not specified)
        """
        session = boto3.Session(profile_name=profile, region_name=region)
        self.client = session.client("cloudwatch")

    def publish_inference_metrics(
        self,
        processing_time_ms: int,
        tool_duration_ms: int,
        model_duration_ms: int,
        nudge_count: int,
        status: str,
        mode: Literal["local", "agentcore"],
    ) -> bool:
        """Publish inference metrics to CloudWatch.

        Args:
            processing_time_ms: Total inference time in milliseconds
            tool_duration_ms: Time spent in tool calls
            model_duration_ms: Time spent in model inference
            nudge_count: Number of nudges generated
            status: Inference status (success, partial, error)
            mode: Execution mode (local or agentcore)

        Returns:
            True if metrics published successfully, False otherwise
        """
        dimensions = [
            {"Name": "Mode", "Value": mode},
            {"Name": "Status", "Value": status},
        ]

        metric_data = [
            {
                "MetricName": "InferenceLatency",
                "Value": processing_time_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "ToolDuration",
                "Value": tool_duration_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "ModelDuration",
                "Value": model_duration_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "NudgeCount",
                "Value": nudge_count,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "InferenceCount",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
        ]

        try:
            self.client.put_metric_data(Namespace=self.NAMESPACE, MetricData=metric_data)
            logger.debug(
                "Published inference metrics: latency=%dms, nudges=%d",
                processing_time_ms,
                nudge_count,
            )
            return True
        except ClientError as e:
            logger.error("Failed to publish inference metrics: %s", e)
            return False

    def publish_tool_metrics(
        self,
        tool_name: str,
        duration_ms: int,
        success: bool,
    ) -> bool:
        """Publish per-tool metrics to CloudWatch.

        Args:
            tool_name: Name of the tool
            duration_ms: Tool execution time in milliseconds
            success: Whether the tool call succeeded

        Returns:
            True if metrics published successfully, False otherwise
        """
        dimensions = [{"Name": "ToolName", "Value": tool_name}]

        metric_data = [
            {
                "MetricName": "ToolCallDuration",
                "Value": duration_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "ToolCallSuccess" if success else "ToolCallFailure",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
        ]

        try:
            self.client.put_metric_data(Namespace=self.NAMESPACE, MetricData=metric_data)
            return True
        except ClientError as e:
            logger.error("Failed to publish tool metrics for %s: %s", tool_name, e)
            return False

    def publish_batch_summary(
        self,
        total_samples: int,
        success_count: int,
        error_count: int,
        avg_latency_ms: float,
        mode: Literal["local", "agentcore"],
    ) -> bool:
        """Publish batch inference summary metrics.

        Args:
            total_samples: Total number of samples processed
            success_count: Number of successful inferences
            error_count: Number of failed inferences
            avg_latency_ms: Average latency across all samples
            mode: Execution mode (local or agentcore)

        Returns:
            True if metrics published successfully, False otherwise
        """
        dimensions = [{"Name": "Mode", "Value": mode}]

        metric_data = [
            {
                "MetricName": "BatchSampleCount",
                "Value": total_samples,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "BatchSuccessCount",
                "Value": success_count,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "BatchErrorCount",
                "Value": error_count,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "BatchAvgLatency",
                "Value": avg_latency_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            },
        ]

        try:
            self.client.put_metric_data(Namespace=self.NAMESPACE, MetricData=metric_data)
            logger.info(
                "Published batch summary: %d samples, %d success, %d errors, %.0fms avg",
                total_samples,
                success_count,
                error_count,
                avg_latency_ms,
            )
            return True
        except ClientError as e:
            logger.error("Failed to publish batch summary metrics: %s", e)
            return False
