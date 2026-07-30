"""Runtime-owned state for interactive terminal UI loops."""

from __future__ import annotations

import collections
from dataclasses import dataclass, field


@dataclass
class TuiRuntimeState:
    """Explicit state for pending input and the active turn."""

    _pending: collections.deque[str] = field(default_factory=collections.deque)
    _pending_client_message_ids: collections.deque[str | None] = field(
        default_factory=collections.deque
    )
    active_input: str | None = None

    @property
    def pending_size(self) -> int:
        return len(self._pending)

    @property
    def pending_items(self) -> tuple[str, ...]:
        return tuple(self._pending)

    @property
    def has_active_turn(self) -> bool:
        return self.active_input is not None

    def enqueue(self, user_input: str, *, client_message_id: str | None = None) -> None:
        self._pending.append(user_input)
        self._pending_client_message_ids.append(client_message_id)

    def enqueue_front(self, user_input: str, *, client_message_id: str | None = None) -> None:
        """Return an unresolved input to the head of the local queue."""

        self._pending.appendleft(user_input)
        self._pending_client_message_ids.appendleft(client_message_id)

    def promote_next(self) -> str | None:
        promoted = self.promote_next_with_identity()
        return promoted[0] if promoted is not None else None

    def promote_next_with_identity(self) -> tuple[str, str | None] | None:
        if not self._pending:
            return None
        user_input = self._pending.popleft()
        client_message_id = (
            self._pending_client_message_ids.popleft()
            if self._pending_client_message_ids
            else None
        )
        return user_input, client_message_id

    def clear_pending(self) -> tuple[str, ...]:
        dropped = tuple(self._pending)
        self._pending.clear()
        self._pending_client_message_ids.clear()
        return dropped

    def drain_pending(self) -> list[str]:
        """Return and clear queued inputs for in-turn agent injection."""

        return list(self.clear_pending())

    def peek_pending(self) -> list[str]:
        """Return queued inputs without changing their FIFO ownership."""

        return list(self._pending)

    def mark_applied(self, *, iteration: int, model_call_id: str) -> None:
        """Satisfy the injection contract for this legacy in-memory queue.

        ``drain_pending`` removes these non-durable items immediately, so
        there is no persisted claim whose applied metadata needs updating.
        """

    def mark_turn_started(self, user_input: str) -> None:
        self.active_input = user_input

    def mark_turn_finished(self) -> None:
        self.active_input = None
