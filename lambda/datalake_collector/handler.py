"""Lambda handler for Datalake Collector.

This Lambda function consumes observability events from SQS and writes them
to S3 with partitioned keys for efficient querying.

Environment Variables:
    BUCKET_NAME: S3 bucket for observability data lake
    STACK_NAME: Stack name for metadata
    ENVIRONMENT: Environment name (dev, staging, prod)
    LOG_LEVEL: Logging level (default: INFO)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=log_level)
log = logging.getLogger(__name__)

# Cached S3 client
_s3_client: Any = None


def get_s3_client() -> Any:
    """Get cached S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def parse_event_timestamp(event_data: dict) -> datetime:
    """Parse timestamp from event data.

    Args:
        event_data: Event data dictionary

    Returns:
        Parsed datetime, defaults to now if parsing fails
    """
    try:
        timestamp_str = event_data.get("timestamp")
        if timestamp_str:
            # Handle ISO format with/without microseconds
            if "." in timestamp_str:
                return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            else:
                return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, TypeError) as e:
        log.warning("Failed to parse timestamp: %s", e)

    return datetime.now(timezone.utc)


def build_s3_key(event_data: dict) -> str:
    """Build partitioned S3 key for event storage.

    Key format: events/year=YYYY/month=MM/day=DD/hour=HH/{request_id}_{event_id}.json

    Args:
        event_data: Event data dictionary

    Returns:
        S3 object key
    """
    timestamp = parse_event_timestamp(event_data)

    # Extract IDs for filename
    event_id = event_data.get("event_id", "unknown")
    correlation = event_data.get("correlation", {})
    request_id = correlation.get("request_id", "no-request")

    # Build partitioned path
    key = (
        f"events/"
        f"year={timestamp.year:04d}/"
        f"month={timestamp.month:02d}/"
        f"day={timestamp.day:02d}/"
        f"hour={timestamp.hour:02d}/"
        f"{request_id}_{event_id}.json"
    )

    return key


def write_event_to_s3(bucket: str, key: str, event_data: dict) -> None:
    """Write event data to S3.

    Args:
        bucket: S3 bucket name
        key: S3 object key
        event_data: Event data to write
    """
    client = get_s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(event_data, default=str),
        ContentType="application/json",
    )
    log.debug("Wrote event to s3://%s/%s", bucket, key)


def process_sqs_record(record: dict, bucket: str) -> bool:
    """Process a single SQS record.

    Args:
        record: SQS record from Lambda event
        bucket: S3 bucket name

    Returns:
        True if processing succeeded, False otherwise
    """
    try:
        # Parse the message body
        body = record.get("body", "{}")
        event_data = json.loads(body)

        # Validate event has required fields
        if not event_data.get("event_id"):
            log.warning("Event missing event_id, skipping: %s", body[:200])
            return False

        # Build S3 key and write
        key = build_s3_key(event_data)
        write_event_to_s3(bucket, key, event_data)

        return True

    except json.JSONDecodeError as e:
        log.error("Failed to parse SQS message body: %s", e)
        return False
    except Exception as e:
        log.exception("Failed to process record: %s", e)
        return False


def handler(event: dict, context: Any) -> dict:  # noqa: ARG001
    """Lambda handler for datalake collector.

    Processes SQS batch and writes events to S3 with partitioned keys.

    Args:
        event: Lambda SQS event
        context: Lambda context

    Returns:
        Batch item failure response for partial batch failure handling
    """
    bucket = os.environ.get("BUCKET_NAME")
    if not bucket:
        log.error("BUCKET_NAME environment variable not set")
        raise RuntimeError("BUCKET_NAME environment variable not set")

    records = event.get("Records", [])
    log.info("Processing %d SQS records", len(records))

    # Track failed message IDs for partial batch failure
    failed_message_ids: list[str] = []
    success_count = 0

    for record in records:
        message_id = record.get("messageId", "unknown")

        if process_sqs_record(record, bucket):
            success_count += 1
        else:
            failed_message_ids.append(message_id)

    log.info(
        "Processed %d/%d events successfully, %d failed",
        success_count,
        len(records),
        len(failed_message_ids),
    )

    # Return batch item failures for SQS to retry
    if failed_message_ids:
        return {"batchItemFailures": [{"itemIdentifier": msg_id} for msg_id in failed_message_ids]}

    return {"batchItemFailures": []}
