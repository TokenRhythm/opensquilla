"""Shared, bounded accounting for one Gateway's encoded transport buffers.

The counters include snapshot transfers, queued wire data and unacknowledged
deliveries. They do not purport to measure the process's complete RSS. All
reservation mutations run synchronously on the Gateway event loop.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

CONNECTION_BUFFER_BYTES = 50 * 1024 * 1024
GLOBAL_BUFFER_BYTES = 256 * 1024 * 1024
FLOW_WINDOW_BYTES = 4 * 1024 * 1024
FLOW_WINDOW_FRAMES = 128
CONTROL_BUFFER_BYTES = 1024 * 1024
CONTROL_BUFFER_FRAMES = 32
RECOVERY_WINDOW_BYTES = 1024 * 1024
RECOVERY_WINDOW_FRAMES = 2
RECOVERY_CREDIT_SECONDS = 30.0
FLOW_CAPABILITY = "transport.flow.v1"
PROBE_CAPABILITY = "transport.probe.v1"
MAX_WIRE_BYTES = 25 * 1024 * 1024
type BudgetKind = Literal["bulk", "recovery", "control"] | None


@dataclass
class Delivery:
    size: int
    recovery: bool = False
    sent: bool = False
    sending: bool = False
    acknowledged: bool = False
    key: str | None = None
    owner: Any = None
    segment_index: int | None = None
    deadline: float | None = None
    publication: Literal["reserved", "original", "tombstone"] = "reserved"


class FlowWindow:
    """Credit covers admitted and sent frames, including browser backlog.

    Only sizes and IDs are retained after send. Dirty streams stop producing
    per-token allocations until their owner installs an authoritative base.
    """

    def __init__(
        self,
        reserve: Callable[[int], bool],
        release: Callable[[int], None],
        *,
        recovery_limit: int = 1,
        reserve_recovery: Callable[[int], bool] | None = None,
        release_recovery: Callable[[int], None] | None = None,
        on_stage: Callable[[Delivery], None] | None = None,
    ) -> None:
        self.epoch = uuid4().hex
        self._reserve = reserve
        self._release = release
        self._reserve_recovery = reserve_recovery or reserve
        self._release_recovery = release_recovery or release
        self._recovery_limit = min(RECOVERY_WINDOW_FRAMES, max(1, recovery_limit))
        self._on_stage = on_stage
        self._closed = False
        self.next_id = 1
        self.ack_id = 0
        self.staged_id = 0
        self.deliveries: OrderedDict[int, Delivery] = OrderedDict()
        self.dirty: dict[str, tuple[str | None, int]] = {}
        self.global_dirty = False
        self.global_revision = 0
        self._dirty_tokens: dict[str, object] = {}
        self._global_dirty_token: object | None = None

    def admit(
        self,
        size: int,
        *,
        recovery: bool = False,
        wire_size: int | None = None,
        key: str | None = None,
        owner: Any = None,
        segment_index: int | None = None,
        deadline: float | None = None,
    ) -> int | None:
        # An encoded event reserves a little extra for its writer-assigned
        # sequence. Accounting overhead is not part of the public wire limit.
        wire_size = size if wire_size is None else wire_size
        if self._closed:
            return None
        if not 0 < wire_size <= MAX_WIRE_BYTES or not wire_size <= size <= MAX_WIRE_BYTES + 32:
            return None
        ordinary = [entry for entry in self.deliveries.values() if not entry.recovery]
        if recovery:
            pieces = [entry for entry in self.deliveries.values() if entry.recovery]
            if (
                len(pieces) >= self._recovery_limit
                or sum(entry.size for entry in pieces) + size > RECOVERY_WINDOW_BYTES
                or (key is not None and any(entry.key == key for entry in pieces))
            ):
                return None
        elif len(ordinary) >= FLOW_WINDOW_FRAMES or (
            ordinary and sum(entry.size for entry in ordinary) + size > FLOW_WINDOW_BYTES
        ):
            return None
        if not (self._reserve_recovery if recovery else self._reserve)(size):
            return None
        delivery_id = self.next_id
        self.next_id += 1
        self.deliveries[delivery_id] = Delivery(
            size, recovery=recovery, key=key, owner=owner,
            segment_index=segment_index, deadline=deadline,
        )
        return delivery_id

    def claim(self, delivery_id: int, publication: Literal["original", "tombstone"]) -> bool:
        """Exactly one frame may publish a reserved recovery receipt."""
        delivery = self.deliveries.get(delivery_id)
        if (
            delivery is None or not delivery.recovery
            or delivery.publication != "reserved" or delivery.sending or delivery.sent
        ):
            return False
        delivery.publication = publication
        return True

    def _release_delivery(self, delivery_id: int) -> None:
        delivery = self.deliveries.pop(delivery_id)
        (self._release_recovery if delivery.recovery else self._release)(delivery.size)

    def _confirm_delivery(self, delivery: Delivery) -> None:
        if delivery.acknowledged:
            return
        delivery.acknowledged = True
        if delivery.recovery and delivery.publication == "original" and self._on_stage:
            self._on_stage(delivery)

    def mark_sending(self, delivery_id: int) -> None:
        self.deliveries[delivery_id].sending = True

    def mark_sent(self, delivery_id: int) -> None:
        delivery = self.deliveries[delivery_id]
        delivery.sent = True
        delivery.sending = False
        if delivery.acknowledged:
            self._release_delivery(delivery_id)

    def mark_send_finished(self, delivery_id: int) -> None:
        """Release a retired write only after its physical send has unwound."""
        delivery = self.deliveries.get(delivery_id)
        if delivery is None:
            return
        delivery.sending = False
        if self._closed or delivery.acknowledged:
            self._release_delivery(delivery_id)

    def validate_stage(self, epoch: str, delivery_id: int) -> None:
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        if type(delivery_id) is not int or delivery_id <= 0:
            raise ValueError("Invalid staged delivery acknowledgement")
        delivery = self.deliveries.get(delivery_id)
        if delivery is None and delivery_id <= max(self.staged_id, self.ack_id):
            return
        if delivery is None or not delivery.recovery:
            raise ValueError("Acknowledgement is not a recovery delivery")
        if not delivery.sent and not delivery.sending:
            raise ValueError("Acknowledgement includes an unsent delivery")

    def stage(self, epoch: str, delivery_id: int) -> None:
        """Release one staged recovery segment without crossing an ordinary hole.

        Removed recovery IDs need no per-segment tombstone: the ordinary ledger
        retains every actual hole, while a scalar makes staging retries harmless.
        The sole writer preserves send order and queued IDs are still rejected.
        """
        self.validate_stage(epoch, delivery_id)
        delivery = self.deliveries.get(delivery_id)
        if delivery is None:
            return
        self.staged_id = max(self.staged_id, delivery_id)
        self._confirm_delivery(delivery)
        if delivery.sent:
            self._release_delivery(delivery_id)

    def validate_acknowledgement(self, epoch: str, delivery_id: int) -> None:
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        if not isinstance(delivery_id, int) or isinstance(delivery_id, bool) or delivery_id < 0:
            raise ValueError("Invalid delivery acknowledgement")
        if delivery_id <= self.ack_id:
            return
        if delivery_id >= self.next_id:
            raise ValueError("Acknowledgement is ahead of delivery")
        if any(
            not entry.sent and not entry.sending
            for key, entry in self.deliveries.items()
            if key <= delivery_id
        ):
            raise ValueError("Acknowledgement includes an unsent delivery")

    def acknowledge(self, epoch: str, delivery_id: int) -> None:
        self.validate_acknowledgement(epoch, delivery_id)
        if delivery_id <= self.ack_id:
            return
        for key in tuple(self.deliveries):
            if key > delivery_id:
                break
            entry = self.deliveries[key]
            self._confirm_delivery(entry)
            # The browser can consume bytes before send_text's drain returns.
            # Accept that ACK but retain the active write's reservation until
            # completion, so an early/forged ACK cannot oversubscribe memory.
            if entry.sent:
                self._release_delivery(key)
        self.ack_id = delivery_id

    def mark_dirty(self, key: str | None, generation: str | None = None, sequence: int = 0) -> bool:
        if not key or len(key) > 4096:
            return self.mark_global_dirty()
        if key not in self.dirty and len(self.dirty) >= 128:
            return self.mark_global_dirty()
        prior = self.dirty.get(key)
        self.dirty[key] = (generation, max(sequence, prior[1] if prior else 0))
        # A clean/dirty/clean/dirty transition must never reuse an old barrier.
        self._dirty_tokens = {
            name: token for name, token in self._dirty_tokens.items() if name in self.dirty
        }
        self._dirty_tokens[key] = object()
        return prior is None

    def dirty_revision(self, key: str) -> tuple[object | None, object | None]:
        """Per-session invalidation identity, independent of notice delivery."""
        return (
            self._dirty_tokens.get(key) if key in self.dirty else None,
            self._global_dirty_token if self.global_dirty else None,
        )

    def mark_global_dirty(self, *, renew: bool = False) -> bool:
        changed = not self.global_dirty or renew
        if changed:
            self.global_revision += 1
        self.global_dirty = True
        self._global_dirty_token = object()
        return changed

    def status(self) -> dict[str, Any]:
        return {
            "delivery_epoch": self.epoch,
            "ack_delivery_id": self.ack_id,
            "dirty_keys": sorted(self.dirty),
            "global_dirty": self.global_dirty,
        }

    def dirty_notice(self) -> dict[str, Any]:
        return {key: value for key, value in self.status().items() if key != "ack_delivery_id"}

    def close(self) -> None:
        self._closed = True
        for delivery_id in tuple(self.deliveries):
            delivery = self.deliveries[delivery_id]
            if delivery.sending and not delivery.sent:
                delivery.acknowledged = True
            else:
                self._release_delivery(delivery_id)
        self.dirty.clear()
        self._dirty_tokens.clear()


class TransportBudget:
    def __init__(self, limit: int = GLOBAL_BUFFER_BYTES) -> None:
        self.limit = limit
        self.used = 0
        self._by_kind: dict[BudgetKind, int] = dict.fromkeys(
            (None, "bulk", "recovery", "control"), 0
        )

    def reserve(self, size: int, *, kind: BudgetKind = None) -> bool:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("transport reservation must be a nonnegative integer")
        headroom = 0
        if kind in {"bulk", "recovery"}:
            headroom += max(0, CONTROL_BUFFER_BYTES - self._by_kind["control"])
        if kind == "bulk":
            headroom += max(0, RECOVERY_WINDOW_BYTES - self._by_kind["recovery"])
        if self.used + size > self.limit - headroom:
            return False
        self.used += size
        self._by_kind[kind] += size
        return True

    def release(self, size: int, *, kind: BudgetKind = None) -> None:
        if (
            not isinstance(size, int) or isinstance(size, bool)
            or not 0 <= size <= self._by_kind[kind]
        ):
            raise ValueError("transport release exceeds its reservation")
        self.used -= size
        self._by_kind[kind] -= size


_global_budget = TransportBudget()


def get_transport_budget() -> TransportBudget:
    return _global_budget
