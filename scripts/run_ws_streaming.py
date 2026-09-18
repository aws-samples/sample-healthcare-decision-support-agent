# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=14.0", "rich>=13.0", "httpx>=0.27"]
# ///
"""End-to-end WebSocket streaming test client.

Tests the full flow:
  1. POST /ws-url to get a pre-signed WebSocket URL (if --api-url provided)
  2. Connect to WebSocket (direct URL or pre-signed)
  3. Send patient data
  4. Receive and display streaming events

Usage:
    # Direct local WebSocket
    uv run scripts/run_ws_streaming.py --url ws://localhost:8080/ws --patient-file data/sample-ccda/file.xml

    # Via pre-signed URL (deployed)
    uv run scripts/run_ws_streaming.py --api-url https://API_ID.execute-api.REGION.amazonaws.com/v1/ws-url --api-key KEY --patient-file data/sample-ccda/file.xml

    # Pre-parsed JSON
    uv run scripts/run_ws_streaming.py --url ws://localhost:8080/ws --patient-file data/preparsed/preparsed_01.json --format preparsed
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx
import websockets.sync.client as ws_client
from rich.logging import RichHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("ws_streaming")

MAX_FRAME_SIZE = 28_000  # Stay under AgentCore's 32KB WebSocket frame limit


def get_presigned_url(api_url: str, api_key: str) -> dict:
    """Get pre-signed WebSocket URL from the REST endpoint."""
    log.info(f"[bold]Step 1:[/bold] Getting pre-signed URL from {api_url}")
    resp = httpx.post(
        api_url, headers={"x-api-key": api_key, "Content-Type": "application/json"}, timeout=60.0
    )
    resp.raise_for_status()
    data = resp.json()
    log.info(f"  Session ID: {data['session_id']}")
    log.info(f"  Expires in: {data['expires_in']}s")
    return data


def load_patient_data(path: Path, fmt: str) -> dict:
    """Load patient data and build the request payload."""
    content = path.read_text()

    if fmt == "preparsed":
        patient_data = json.loads(content)
        return {"patient_data": patient_data, "visit_context": {"specialty": "general"}}
    elif fmt == "fhir":
        return {"fhir_json": content, "visit_context": {"specialty": "general"}}
    else:
        # Default: CCDA XML
        return {"ccda_xml": content, "visit_context": {"specialty": "general"}}


def send_payload(conn, payload: dict) -> None:
    """Send payload, chunking if it exceeds the WebSocket frame size limit.

    AgentCore enforces a 32KB WebSocket frame size limit. For payloads that
    exceed this limit (e.g., raw CCDA XML), we use an application-level
    chunking protocol:
      1. Send: {"_chunked": true, "total_chunks": N, "meta": {visit_context, config, format}}
      2. Send N data chunks: {"_chunk_index": i, "_chunk_data": "..."}
      3. Server reassembles and processes as normal
    """
    encoded = json.dumps(payload)
    size = len(encoded.encode("utf-8"))

    if size <= MAX_FRAME_SIZE:
        log.info(f"  Sending as single message ({size} bytes)")
        conn.send(encoded)
        return

    # Determine which field contains the large data
    data_field = None
    data_value = None
    for field in ("ccda_xml", "fhir_json", "patient_data"):
        if field in payload and payload[field]:
            val = payload[field] if isinstance(payload[field], str) else json.dumps(payload[field])
            if len(val.encode("utf-8")) > MAX_FRAME_SIZE:
                data_field = field
                data_value = val
                break

    if not data_field:
        # No single field is too large, send as-is (shouldn't happen often)
        log.info(f"  Sending as single message ({size} bytes, no large field)")
        conn.send(encoded)
        return

    # Split the large field into chunks
    chunk_size = MAX_FRAME_SIZE - 200  # Leave room for chunk envelope JSON
    chunks = [data_value[i : i + chunk_size] for i in range(0, len(data_value), chunk_size)]

    # Build metadata (everything except the large data field)
    meta = {k: v for k, v in payload.items() if k != data_field}
    meta["_data_field"] = data_field

    # Send init message
    init_msg = {"_chunked": True, "total_chunks": len(chunks), "meta": meta}
    log.info(
        f"  Chunking {data_field}: {len(data_value)} bytes → {len(chunks)} chunks of ~{chunk_size} bytes"
    )
    conn.send(json.dumps(init_msg))

    # Send data chunks
    for i, chunk in enumerate(chunks):
        chunk_msg = json.dumps({"_chunk_index": i, "_chunk_data": chunk})
        conn.send(chunk_msg)

    log.info(f"  All {len(chunks)} chunks sent")


URGENCY_COLORS = {"urgent": "red", "warning": "yellow", "informational": "blue"}


class StreamSession:
    """Counts and text collected while one WebSocket stream is consumed."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.event_counts: dict[str, int] = {}
        self.text_chunks: list[str] = []
        self.start_time = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def handle(self, event: dict) -> None:
        event_type = event.get("event", "unknown")
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        handler = EVENT_HANDLERS.get(event_type, _log_other_event)
        handler(self, event)

    def log_summary(self) -> None:
        events_str = ", ".join(f"{k}={v}" for k, v in sorted(self.event_counts.items()))
        char_count = sum(len(c) for c in self.text_chunks)
        log.info("─" * 60)
        log.info(
            f"[bold]Summary:[/bold] events={events_str}, text_chunks={len(self.text_chunks)}, "
            f"characters={char_count}, wall_time={self.elapsed:.1f}s"
        )


def _handle_text(session: StreamSession, event: dict) -> None:
    chunk = event.get("data", "")
    session.text_chunks.append(chunk)
    # Stream text live to stdout (bypasses Rich logger for inline output)
    print(chunk, end="", flush=True)


def _log_tool_start(session: StreamSession, event: dict) -> None:
    tool_name = event.get("data", {}).get("tool", "unknown")
    log.info(f"[{session.elapsed:6.1f}s] [yellow]tool_start:[/yellow] {tool_name}")


def _log_tool_end(session: StreamSession, event: dict) -> None:
    tool_name = event.get("data", {}).get("tool", "unknown")
    duration = event.get("data", {}).get("duration_ms", 0)
    log.info(f"[{session.elapsed:6.1f}s] [green]tool_end:[/green] {tool_name} ({duration}ms)")


def _log_complete_event(session: StreamSession, event: dict) -> None:
    # Newline after streamed text before structured output
    if session.text_chunks:
        print("\n", flush=True)

    data = event.get("data", {})
    status = data.get("status", "unknown")
    nudges = data.get("nudges", [])
    usage = data.get("usage", {})
    proc_time = data.get("processing_time_ms", 0)
    log.info(
        f"[{session.elapsed:6.1f}s] [bold green]complete:[/bold green] "
        f"status={status}, nudges={len(nudges)}, tokens={usage}, time={proc_time}ms"
    )

    patient_summary = data.get("patient_summary")
    if patient_summary:
        log.info("")
        log.info("[bold cyan]═══ Patient Summary ═══[/bold cyan]")
        log.info(patient_summary)

    if nudges:
        log.info("")
        log.info(f"[bold cyan]═══ Nudges ({len(nudges)}) ═══[/bold cyan]")
    for i, nudge in enumerate(nudges, 1):
        _log_nudge(i, nudge)


def _log_nudge(index: int, nudge: dict) -> None:
    """Full detail of one nudge from the complete event."""
    urgency = nudge.get("urgency", "?")
    urgency_color = URGENCY_COLORS.get(urgency, "white")
    log.info("")
    log.info(
        f"[bold]{index}. {nudge.get('title', 'Untitled')}[/bold] "
        f"[{urgency_color}]\\[{urgency}][/{urgency_color}]"
    )
    if nudge.get("category"):
        log.info(f"   Category: {nudge['category']}")
    if nudge.get("action_type"):
        actions = nudge["action_type"]
        if isinstance(actions, list):
            actions = ", ".join(actions)
        log.info(f"   Action type: {actions}")
    for label, key in (
        ("Description", "description"),
        ("Rationale", "rationale"),
        ("Grounding", "grounding"),
    ):
        if nudge.get(key):
            log.info(f"   {label}: {nudge[key]}")
    citation = nudge.get("guideline_citation")
    if isinstance(citation, dict):
        src = citation.get("source", "")
        sec = citation.get("section", "")
        log.info(f"   Citation: {src} — {sec}")
    if nudge.get("icd_codes"):
        log.info(f"   ICD codes: {', '.join(nudge['icd_codes'])}")


def _log_error_event(session: StreamSession, event: dict) -> None:
    msg = event.get("data", {}).get("message", "")
    log.error(f"[{session.elapsed:6.1f}s] {msg}")


def _log_fatal_event(session: StreamSession, event: dict) -> None:
    msg = event.get("data", {}).get("message", "")
    code = event.get("data", {}).get("code", "")
    log.error(f"[{session.elapsed:6.1f}s] [bold red]fatal ({code}):[/bold red] {msg}")


def _log_other_event(session: StreamSession, event: dict) -> None:
    event_type = event.get("event", "unknown")
    log.info(f"[{session.elapsed:6.1f}s] {event_type}: {json.dumps(event.get('data', ''))}")


EVENT_HANDLERS = {
    "text": _handle_text,
    "tool_start": _log_tool_start,
    "tool_end": _log_tool_end,
    "complete": _log_complete_event,
    "error": _log_error_event,
    "fatal": _log_fatal_event,
}


def _log_connection_closed(session: StreamSession, error: Exception) -> None:
    close_code = getattr(error, "code", None) or getattr(error, "rcvd", None)
    close_reason = getattr(error, "reason", None)
    log.info(f"Connection closed: {error} (code={close_code}, reason={close_reason})")
    if not session.event_counts:
        payload_size = len(json.dumps(session.payload))
        log.warning(
            f"No events received before disconnect. "
            f"Payload was {payload_size} bytes - check AgentCore WebSocket message size limits."
        )


def _consume_events(conn, session: StreamSession) -> None:
    for raw_msg in conn:
        if isinstance(raw_msg, bytes):
            raw_msg = raw_msg.decode("utf-8")
        try:
            event = json.loads(raw_msg)
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON: {raw_msg}")
            continue
        session.handle(event)


def run_streaming_test(ws_url: str, payload: dict) -> None:
    """Connect to WebSocket and stream events."""
    log.info("[bold]Step 2:[/bold] Connecting to WebSocket...")
    log.info(f"  URL: {ws_url}...")

    session = StreamSession(payload)

    try:
        with ws_client.connect(ws_url, close_timeout=5, open_timeout=30) as conn:
            log.info("[green]Connected![/green]")

            # Send patient data (with chunking if needed)
            payload_size = len(json.dumps(payload))
            log.info(f"[bold]Step 3:[/bold] Sending patient data ({payload_size} bytes)...")
            send_payload(conn, payload)

            log.info("[bold]Step 4:[/bold] Receiving streaming events...")
            _consume_events(conn, session)

    except Exception as e:
        if "ConnectionClosed" in type(e).__name__:
            _log_connection_closed(session, e)
        else:
            log.exception("WebSocket error")

    session.log_summary()


def main() -> None:
    parser = argparse.ArgumentParser(description="WebSocket streaming test client")
    parser.add_argument("--url", help="Direct WebSocket URL (ws:// or wss://)")
    parser.add_argument("--api-url", help="REST endpoint for pre-signed URL generation")
    parser.add_argument("--api-key", help="API key for the REST endpoint")
    parser.add_argument("--patient-file", required=True, help="Path to patient data file")
    parser.add_argument(
        "--format",
        choices=["ccda", "fhir", "preparsed"],
        default="ccda",
        help="Patient data format (default: ccda)",
    )
    parser.add_argument("--visit-context", help="JSON string of visit context overrides")
    args = parser.parse_args()

    if not args.url and not args.api_url:
        parser.error("Either --url or --api-url is required")

    if args.api_url and not args.api_key:
        parser.error("--api-key is required when using --api-url")

    # Load patient data
    patient_path = Path(args.patient_file)
    if not patient_path.exists():
        log.error(f"File not found: {patient_path}")
        sys.exit(1)

    payload = load_patient_data(patient_path, args.format)

    if args.visit_context:
        payload["visit_context"].update(json.loads(args.visit_context))

    # Get WebSocket URL
    if args.api_url:
        presigned = get_presigned_url(args.api_url, args.api_key)
        ws_url = presigned["ws_url"]
    else:
        ws_url = args.url

    # Run the test
    run_streaming_test(ws_url, payload)


if __name__ == "__main__":
    main()
