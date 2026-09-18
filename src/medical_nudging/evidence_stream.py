"""Stream an evidence response as server-sent events so a long generation keeps bytes flowing.

The candidate gate asks the deployed runtime for its evidence record
(``config.return_evidence``). A steered generation takes minutes; a synchronous
response that sends nothing for that long is lost between the AgentCore front end and
the client even though the runtime logs a 200 (observed on every run past roughly five
minutes, independent of payload size). The runtime therefore answers a request that
accepts ``text/event-stream`` with:

- ``keepalive`` events while the generation runs in a worker thread;
- the JSON payload as a sequence of ``chunk`` events, each a JSON string literal
  holding at most :data:`CHUNK_BYTES` of the encoded payload (AgentCore caps a
  streaming chunk at 10 MB);
- one ``end`` event carrying the chunk count, byte count, and SHA-256 of the payload;
- or one ``error`` event (``status``, ``detail``) when the generation fails after the
  stream has started.

Nothing is written to disk or to S3 on either side: the record stays in memory in the
runtime until it is sent and in the gate until it is scored, which is what the raw
patient evidence it carries requires.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

EVENT_STREAM_MEDIA_TYPE = "text/event-stream"
CHUNK_BYTES = 1024 * 1024
KEEPALIVE_SECONDS = 15.0


def accepts_event_stream(accept_header: str | None) -> bool:
    """True when an ``Accept`` header asks for server-sent events."""
    if not accept_header:
        return False
    return any(
        part.split(";")[0].strip() == EVENT_STREAM_MEDIA_TYPE for part in accept_header.split(",")
    )


def format_event(event: str, data: Any) -> str:
    """One SSE event: ``event:`` name plus a single ``data:`` line of JSON."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def keepalive_event(elapsed_seconds: float) -> str:
    return format_event("keepalive", {"elapsed_s": round(elapsed_seconds, 1)})


def error_event(status: int, detail: str) -> str:
    return format_event("error", {"status": status, "detail": detail})


def payload_events(payload: Any, chunk_bytes: int | None = None) -> Iterator[str]:
    """Encode ``payload`` as ``chunk`` events followed by one ``end`` event."""
    chunk_bytes = chunk_bytes or CHUNK_BYTES
    encoded = json.dumps(payload).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    count = 0
    for start in range(0, len(encoded), chunk_bytes):
        # Slice the UTF-8 bytes, decode leniently so a split multibyte sequence never
        # raises; the receiver joins the byte slices, not the strings.
        piece = encoded[start : start + chunk_bytes]
        yield format_event("chunk", piece.decode("latin-1"))
        count += 1
    yield format_event("end", {"chunks": count, "bytes": len(encoded), "sha256": digest})


class EvidenceStreamError(RuntimeError):
    """The stream ended without a complete, verified payload."""


@dataclass
class EvidenceStreamAssembler:
    """Parse SSE lines from the runtime and rebuild the JSON payload.

    Feed decoded text lines (without the trailing newline) to :meth:`feed_line`; the
    assembler dispatches each blank-line-terminated event. :attr:`result` holds the
    decoded payload once the ``end`` event verified the SHA-256.
    """

    _event: str | None = None
    _data: list[str] = field(default_factory=list)
    _chunks: list[bytes] = field(default_factory=list)
    keepalives: int = 0
    result: Any = None
    done: bool = False

    def feed_line(self, line: str) -> None:
        if self.done:
            return
        if line == "":
            self._dispatch()
            return
        if line.startswith(":"):
            return  # SSE comment
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)

    def feed(self, lines: Iterable[str]) -> Any:
        """Feed every line; return the payload or raise :class:`EvidenceStreamError`."""
        for line in lines:
            self.feed_line(line)
            if self.done:
                return self.result
        raise EvidenceStreamError(
            f"Evidence stream ended before its end event ({len(self._chunks)} chunk(s), "
            f"{self.keepalives} keepalive(s) received)."
        )

    def _dispatch(self) -> None:
        event, data = self._event, "\n".join(self._data)
        self._event, self._data = None, []
        if event is None and not data:
            return
        try:
            value = json.loads(data) if data else None
        except json.JSONDecodeError as exc:
            raise EvidenceStreamError(f"Malformed {event or 'data'} event: {data[:120]!r}") from exc
        if event == "keepalive":
            self.keepalives += 1
        elif event == "chunk":
            if not isinstance(value, str):
                raise EvidenceStreamError("chunk event did not carry a string")
            self._chunks.append(value.encode("latin-1"))
        elif event == "end":
            self._finish(value if isinstance(value, dict) else {})
        elif event == "error":
            detail = value.get("detail") if isinstance(value, dict) else str(value)
            status = value.get("status") if isinstance(value, dict) else None
            raise EvidenceStreamError(f"Runtime reported an error (HTTP {status}): {detail}")
        else:
            raise EvidenceStreamError(f"Unexpected event {event!r} in evidence stream")

    def _finish(self, summary: dict[str, Any]) -> None:
        encoded = b"".join(self._chunks)
        digest = hashlib.sha256(encoded).hexdigest()
        if summary.get("chunks") != len(self._chunks) or summary.get("bytes") != len(encoded):
            raise EvidenceStreamError(
                f"Evidence stream incomplete: got {len(self._chunks)} chunk(s) / {len(encoded)} "
                f"bytes, end event announced {summary.get('chunks')} / {summary.get('bytes')}."
            )
        if summary.get("sha256") != digest:
            raise EvidenceStreamError("Evidence stream payload failed its SHA-256 check.")
        self.result = json.loads(encoded.decode("utf-8"))
        self.done = True
