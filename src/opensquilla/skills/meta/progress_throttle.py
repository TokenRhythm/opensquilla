"""Per-run/per-step throttle for status_text bursts + state-transition
de-duplication for meta-skill step events.

The orchestrator may receive status_text updates very frequently when an
`agent` step's sub-turn fires multiple tool calls per second. This helper
caps emission to one status_text per (run_id, step_id) per
``min_interval_ms`` (default 500ms) so the WebUI ribbon does not flood.

It also tracks the last emitted state per (run_id, step_id) so identical
state transitions (e.g. running → running) are suppressed; only first
occurrence of each state is allowed through.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class ProgressThrottle:
    """Per-(run_id, step_id) throttle + state dedupe."""

    def __init__(
        self,
        *,
        min_interval_ms: int = 500,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._min_interval = min_interval_ms / 1000.0
        self._clock = clock or time.monotonic
        self._last_status_text_at: dict[tuple[str, str], float] = {}
        self._last_state: dict[tuple[str, str], str] = {}

    def allow_status_text(self, run_id: str, step_id: str) -> bool:
        """Return True if a new status_text emission is permitted."""
        key = (run_id, step_id)
        now = self._clock()
        last = self._last_status_text_at.get(key)
        if last is not None and (now - last) < self._min_interval:
            return False
        self._last_status_text_at[key] = now
        return True

    def allow_state(self, run_id: str, step_id: str, state: str) -> bool:
        """Return True if this state transition has not been emitted yet."""
        key = (run_id, step_id)
        if self._last_state.get(key) == state:
            return False
        self._last_state[key] = state
        return True
