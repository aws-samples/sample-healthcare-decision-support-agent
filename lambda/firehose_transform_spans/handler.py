"""Firehose transformation Lambda for OTEL spans.

Extracts OTEL span JSON from CloudWatch Logs subscription filter envelope.
Input: CloudWatch Logs format (gzip compressed, base64 encoded by Firehose)
Output: Flat JSON lines (one span per line)
"""

import base64
import gzip
import json


def handler(event, context):
    """Transform CloudWatch Logs records to flat OTEL span JSON."""
    output = []

    for record in event["records"]:
        record_id = record["recordId"]
        
        try:
            # Decode base64 and decompress gzip
            compressed = base64.b64decode(record["data"])
            decompressed = gzip.decompress(compressed).decode("utf-8")
            cw_data = json.loads(decompressed)

            # Skip control messages
            if cw_data.get("messageType") == "CONTROL_MESSAGE":
                output.append({"recordId": record_id, "result": "Dropped", "data": ""})
                continue

            # Extract log events and flatten
            log_events = cw_data.get("logEvents", [])
            if not log_events:
                output.append({"recordId": record_id, "result": "Dropped", "data": ""})
                continue

            # Concatenate all span messages as newline-delimited JSON
            spans = []
            for log_event in log_events:
                message = log_event.get("message", "")
                if message:
                    spans.append(message)

            if spans:
                # Join with newlines, add trailing newline, encode
                flat_data = "\n".join(spans) + "\n"
                encoded = base64.b64encode(flat_data.encode("utf-8")).decode("utf-8")
                output.append({"recordId": record_id, "result": "Ok", "data": encoded})
            else:
                output.append({"recordId": record_id, "result": "Dropped", "data": ""})

        except Exception as e:
            # On error, pass through original data
            print(f"Error processing record {record_id}: {e}")
            output.append({"recordId": record_id, "result": "ProcessingFailed", "data": record["data"]})

    return {"records": output}
