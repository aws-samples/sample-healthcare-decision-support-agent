"""Encoder/decoder round trip for the streamed evidence response."""

import hashlib
import json

import pytest

from medical_nudging.evidence_stream import (
    EvidenceStreamAssembler,
    EvidenceStreamError,
    accepts_event_stream,
    error_event,
    format_event,
    keepalive_event,
    payload_events,
)


def _lines(events: str) -> list[str]:
    return events.split("\n")


def test_accepts_event_stream_matches_media_type_only():
    assert accepts_event_stream("text/event-stream")
    assert accepts_event_stream("application/json, text/event-stream;q=0.9")
    assert not accepts_event_stream("application/json")
    assert not accepts_event_stream(None)
    assert not accepts_event_stream("")


def test_payload_round_trips_across_chunk_boundaries_with_multibyte_text():
    payload = {"evidence": {"note": "µg/dL — ≥ 2.5 " * 500, "n": list(range(200))}}
    events = "".join(payload_events(payload, chunk_bytes=64))
    assembler = EvidenceStreamAssembler()
    assert assembler.feed(_lines(events)) == payload
    assert assembler.done
    encoded = json.dumps(payload).encode("utf-8")
    assert events.count("event: chunk") == -(-len(encoded) // 64)


def test_keepalives_are_counted_and_ignored_in_payload():
    payload = {"output": {"status": "success"}}
    events = keepalive_event(15.0) + keepalive_event(30.2) + "".join(payload_events(payload))
    assembler = EvidenceStreamAssembler()
    assert assembler.feed(_lines(events)) == payload
    assert assembler.keepalives == 2


def test_error_event_raises_with_status_and_detail():
    events = keepalive_event(15.0) + error_event(409, "frozen on corpus X")
    with pytest.raises(EvidenceStreamError, match=r"HTTP 409.*frozen on corpus X"):
        EvidenceStreamAssembler().feed(_lines(events))


def test_stream_without_end_event_is_an_error():
    events = "".join(list(payload_events({"a": 1}))[:-1])
    with pytest.raises(EvidenceStreamError, match="ended before its end event"):
        EvidenceStreamAssembler().feed(_lines(events))


def test_tampered_chunk_fails_the_hash_check():
    payload = {"a": "x" * 100}
    events = "".join(payload_events(payload, chunk_bytes=50)).replace('"x', '"y', 1)
    with pytest.raises(EvidenceStreamError, match="SHA-256"):
        EvidenceStreamAssembler().feed(_lines(events))


def test_end_event_announces_matching_counts_and_digest():
    payload = {"k": "v"}
    encoded = json.dumps(payload).encode()
    end = list(payload_events(payload))[-1]
    assert end == format_event(
        "end", {"chunks": 1, "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    )


def test_unknown_event_is_rejected():
    with pytest.raises(EvidenceStreamError, match="Unexpected event"):
        EvidenceStreamAssembler().feed(_lines(format_event("surprise", {})))
