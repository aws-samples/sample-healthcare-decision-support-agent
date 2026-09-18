"""Request-scoped external tool budgets."""

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class CallBudget:
    """Thread-safe call counter shared by a request's parent and subagents."""

    tool_name: str
    max_calls: int | None
    calls_used: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if self.max_calls is not None and self.max_calls < 0:
            raise ValueError("max_calls must be non-negative or None")

    def consume(self) -> tuple[bool, int | None]:
        """Consume one allowed external call and return remaining capacity."""
        with self._lock:
            if self.max_calls is not None and self.calls_used >= self.max_calls:
                return False, 0
            self.calls_used += 1
            if self.max_calls is None:
                return True, None
            return True, self.max_calls - self.calls_used

    def exhausted_message(self) -> str:
        """Return a stable message instructing the agent to stop exploring."""
        return (
            f"BUDGET_EXHAUSTED: {self.tool_name} allows at most "
            f"{self.max_calls} external calls per patient. "
            "Use the evidence already retrieved and finalize the response."
        )
