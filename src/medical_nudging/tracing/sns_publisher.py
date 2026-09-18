"""SNS Event Publisher for observability data lake.

Fire-and-forget publishing of observability events to SNS topic.
Failures are logged but not raised to avoid impacting inference performance.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from medical_nudging.config import observability_enabled

logger = logging.getLogger(__name__)

# Event schema version
EVENT_SCHEMA_VERSION = "1.0"

# Cached SNS client
_sns_client: Any = None


def _get_sns_client() -> Any | None:
    """Get cached SNS client.

    Returns:
        boto3 SNS client or None if boto3 not available
    """
    global _sns_client
    if _sns_client is None:
        try:
            import boto3

            _sns_client = boto3.client("sns")
        except ImportError:
            logger.warning("boto3 not available - SNS publishing disabled")
            return None
    return _sns_client


def _is_observability_enabled() -> bool:
    """Whether SNS publishing is on (the shared ``OBSERVABILITY_ENABLED`` switch)."""
    return observability_enabled()


def _get_topic_arn() -> str | None:
    """Get SNS topic ARN from environment variable.

    Returns:
        Topic ARN or None if not configured
    """
    return os.environ.get("OBSERVABILITY_SNS_TOPIC_ARN")


class SNSEventPublisher:
    """Publisher for observability events to SNS.

    Fire-and-forget pattern - failures are logged but not raised.
    """

    def __init__(
        self,
        topic_arn: str | None = None,
        stack_name: str | None = None,
        environment: str | None = None,
        region: str | None = None,
    ):
        """Initialize SNS event publisher.

        Args:
            topic_arn: SNS topic ARN (defaults to OBSERVABILITY_SNS_TOPIC_ARN env var)
            stack_name: Stack name for metadata (defaults to STACK_NAME env var)
            environment: Environment name (defaults to ENVIRONMENT env var)
            region: AWS region (defaults to AWS_REGION env var)
        """
        self.topic_arn = topic_arn or _get_topic_arn()
        self.stack_name = stack_name or os.environ.get("STACK_NAME", "medical-nudging")
        self.environment = environment or os.environ.get("ENVIRONMENT", "dev")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._enabled = _is_observability_enabled() and self.topic_arn is not None

    def is_enabled(self) -> bool:
        """Check if publishing is enabled.

        Returns:
            True if observability is enabled and topic ARN is configured
        """
        return self._enabled

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation: dict[str, str | None] | None = None,
        source_component: str = "orchestrator",
    ) -> str | None:
        """Publish an event to SNS (fire-and-forget).

        Args:
            event_type: Type of event (e.g., 'request_received', 'tool_started')
            payload: Event-specific payload data
            correlation: Correlation IDs (request_id, session_id, trace_id)
            source_component: Component emitting the event

        Returns:
            Event ID if published successfully, None otherwise
        """
        if not self._enabled:
            logger.debug("Observability disabled - skipping event: %s", event_type)
            return None

        event_id = str(uuid.uuid4())
        event = self._build_event(event_id, event_type, payload, correlation, source_component)

        try:
            client = _get_sns_client()
            if client is None:
                return None

            client.publish(
                TopicArn=self.topic_arn,
                Message=json.dumps(event, default=str),
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": event_type},
                    "request_id": {
                        "DataType": "String",
                        "StringValue": (correlation or {}).get("request_id") or "unknown",
                    },
                },
            )
            logger.debug("Published event %s: %s", event_id, event_type)
            return event_id

        except Exception as e:
            # Fire-and-forget: log but don't raise
            logger.warning("Failed to publish event %s: %s", event_type, e)
            return None

    def _build_event(
        self,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        correlation: dict[str, str | None] | None,
        source_component: str,
    ) -> dict[str, Any]:
        """Build standardized event structure.

        Args:
            event_id: Unique event identifier
            event_type: Type of event
            payload: Event-specific payload
            correlation: Correlation IDs
            source_component: Source component name

        Returns:
            Complete event dictionary
        """
        # Determine mode based on runtime environment
        mode = "agentcore" if os.environ.get("AWS_EXECUTION_ENV") else "local"

        return {
            "version": EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation": {
                "request_id": (correlation or {}).get("request_id"),
                "session_id": (correlation or {}).get("session_id"),
                "trace_id": (correlation or {}).get("trace_id"),
            },
            "source": {
                "component": source_component,
                "mode": mode,
                "environment": self.environment,
            },
            "payload": payload,
            "metadata": {
                "stack_name": self.stack_name,
                "region": self.region,
                "image_tag": os.environ.get("IMAGE_TAG"),
                "ecr_repository": os.environ.get("ECR_REPOSITORY"),
            },
        }


# Global publisher instance (lazy initialization)
_global_publisher: SNSEventPublisher | None = None


def get_publisher() -> SNSEventPublisher:
    """Get global SNS event publisher instance.

    Returns:
        Singleton SNSEventPublisher instance
    """
    global _global_publisher
    if _global_publisher is None:
        _global_publisher = SNSEventPublisher()
    return _global_publisher
