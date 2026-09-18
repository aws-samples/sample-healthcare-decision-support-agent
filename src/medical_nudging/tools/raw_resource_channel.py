"""Raw-resource side channel for the evidence contract tool ledger.

The FHIR query tool returns *formatted prose* to the agent, because that is what the
model reads best.  The deterministic verifier needs the opposite: the exact raw FHIR
resources, one evidence span each, so a claim can name the resource it rests on.

This module is the side channel that carries the raw resources past the formatted
return value without changing it.  The tool publishes every resource it retrieved to
the active channel; the tool ledger reads the channel when it builds patient evidence
spans.  Nothing here alters the tool's user-facing output.

The channel is process-local and explicit: a caller that wants raw capture opens a
:func:`capture_raw_resources` scope. Outside a scope, publications are discarded.
When no raw resources were published, the tool ledger falls back to parsing the
formatted output and marks its own fidelity accordingly — it never silently pretends to
have raw evidence.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RawResourceRecord:
    """Raw resources published by one tool call."""

    tool_name: str
    query: str
    resources: tuple[dict[str, Any], ...]
    retrieved_at: str | None = None


@dataclass
class RawResourceChannel:
    """Ordered record of every raw resource published during a generation attempt."""

    records: list[RawResourceRecord] = field(default_factory=list)

    def publish(
        self,
        *,
        tool_name: str,
        query: str,
        resources: list[dict[str, Any]],
        retrieved_at: str | None = None,
    ) -> None:
        """Record the raw resources one tool call retrieved."""
        kept = tuple(item for item in resources if isinstance(item, dict) and item)
        if not kept:
            return
        self.records.append(
            RawResourceRecord(
                tool_name=tool_name,
                query=query,
                resources=kept,
                retrieved_at=retrieved_at,
            )
        )

    def resources(self) -> list[dict[str, Any]]:
        """Every published raw resource, in publication order."""
        return [resource for record in self.records for resource in record.resources]

    @property
    def is_empty(self) -> bool:
        """True when no tool published any raw resource."""
        return not self.records

    def clear(self) -> None:
        """Drop every recorded raw resource."""
        self.records = []


_ACTIVE_CHANNEL: ContextVar[RawResourceChannel | None] = ContextVar(
    "raw_resource_channel", default=None
)


def default_channel() -> RawResourceChannel:
    """Return the active capture, or an ephemeral channel when capture is disabled."""
    channel = _ACTIVE_CHANNEL.get()
    return channel if channel is not None else RawResourceChannel()


def reset_default_channel() -> RawResourceChannel:
    """Clear and return the default channel, so records stay per-run."""
    channel = default_channel()
    channel.clear()
    return channel


@contextmanager
def capture_raw_resources() -> Iterator[RawResourceChannel]:
    """Scope raw-resource capture to one generation attempt."""
    channel = RawResourceChannel()
    token = _ACTIVE_CHANNEL.set(channel)
    try:
        yield channel
    finally:
        _ACTIVE_CHANNEL.reset(token)
