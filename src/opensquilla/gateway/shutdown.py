"""One monotonic budget shared by desktop shutdown participants."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class ShutdownRequest:
    mode: str
    deadline: float
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    termination_deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.termination_deadline = self.deadline + (5.0 if self.mode == "quit" else 0.0)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def update(self, mode: str, remaining_ms: int) -> None:
        """Accept escalation and a shorter deadline, never grant more time."""
        now = time.monotonic()
        requested_deadline = now + remaining_ms / 1000
        if mode == "quit":
            if self.mode != "quit":
                # The old drain deadline was its entire process budget. Reserve
                # its last five seconds for the parent's owned-tree termination.
                self.deadline = min(self.deadline, max(now, self.termination_deadline - 5.0))
            self.mode = "quit"
        self.deadline = min(self.deadline, requested_deadline)
        self.termination_deadline = min(
            self.termination_deadline, self.deadline + (5.0 if self.mode == "quit" else 0.0)
        )
        previous = self.changed
        self.changed = asyncio.Event()
        previous.set()

    def acknowledgement(self) -> dict[str, str | int]:
        now = time.monotonic()
        return {
            "accepted_mode": self.mode,
            "remaining_ms": int(max(0.0, self.deadline - now) * 1000),
            "total_remaining_ms": int(max(0.0, self.termination_deadline - now) * 1000),
        }
