"""One monotonic budget shared by desktop shutdown participants."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class ShutdownBudget:
    """Bound each phase without cancelling an owner's durable finalizers."""

    started_at: float
    fallback_deadline: float
    request: ShutdownRequest | None = None
    cleanup_reserve_limit: float = 12.05
    now: Callable[[], float] = field(default=time.monotonic, repr=False)

    @property
    def deadline(self) -> float:
        if self.request is None:
            return self.fallback_deadline
        return min(self.fallback_deadline, self.request.deadline)

    @property
    def work_deadline(self) -> float:
        deadline = self.deadline
        reserve = min(
            self.cleanup_reserve_limit, max(0.0, deadline - self.started_at) * 0.2,
        )
        return deadline - reserve

    def remaining(self, limit: float | None = None, *, work: bool = False) -> float:
        deadline = self.work_deadline if work else self.deadline
        remaining = max(0.0, deadline - self.now())
        return remaining if limit is None else min(limit, remaining)

    async def wait(
        self,
        task: asyncio.Future[Any],
        *,
        limit: float | None = None,
        work: bool = False,
    ) -> bool:
        """Observe tighter requests in flight; the caller retains task ownership.

        A phase cap is fixed once, not renewed by each request change. Timeout
        and cancellation of this waiter never cancel the phase task itself.
        """
        phase_deadline = None if limit is None else self.now() + max(0.0, limit)
        while not task.done():
            changed = (
                asyncio.create_task(self.request.changed.wait())
                if self.request is not None else None
            )
            deadline = self.work_deadline if work else self.deadline
            if phase_deadline is not None:
                deadline = min(deadline, phase_deadline)
            try:
                done, _ = await asyncio.wait(
                    {task, changed} if changed is not None else {task},
                    timeout=max(0.0, deadline - self.now()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if changed is not None:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
            if task.done():
                return True
            if not done or self.now() >= deadline:
                return False
        return True
