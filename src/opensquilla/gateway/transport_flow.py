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
# Additive v2 lane budget.  A lane is one session stream on a multiplexed
# connection.  The global v1 window remains unchanged unless the client
# explicitly negotiates transport.flow.v2; v2 keeps each lane bounded while
# allowing a stalled lane to stop consuming the whole connection window.
FLOW_LANE_WINDOW_BYTES = 256 * 1024
FLOW_LANE_WINDOW_FRAMES = 8
FLOW_V2_MAX_LANES = 4
# Retired epochs remain until their retire receipt is confirmed. Bound that
# authoritative history so an unresponsive client cannot turn epoch churn into
# an unbounded ledger; admission backpressures once this cap is reached.
FLOW_V2_MAX_LANE_EPOCHS = 16
CONTROL_BUFFER_BYTES = 1024 * 1024
CONTROL_BUFFER_FRAMES = 32
RECOVERY_WINDOW_BYTES = 1024 * 1024
RECOVERY_WINDOW_FRAMES = 16
RECOVERY_CREDIT_SECONDS = 30.0
FLOW_CAPABILITY = "transport.flow.v1"
FLOW_CAPABILITY_V2 = "transport.flow.v2"
# Frozen W6 wire capability.  ``FLOW_CAPABILITY_V2`` remains accepted as an
# additive early-adopter alias for clients that shipped the admission spike.
SESSION_FLOW_V2_CAPABILITY = "transport.session-flow.v2"
PROBE_CAPABILITY = "transport.probe.v1"
MAX_WIRE_BYTES = 25 * 1024 * 1024
type BudgetKind = Literal["bulk", "recovery", "control"] | None


@dataclass
class Delivery:
    size: int
    recovery: bool = False
    # ``lane`` is deliberately optional for v1.  The v1 wire contract keeps
    # one cumulative connection watermark; v2 can attach a stable session or
    # recovery lane without changing the accounting object again.
    lane: str | None = None
    lane_epoch: str | None = None
    lane_sequence: int | None = None
    sent: bool = False
    sending: bool = False
    acknowledged: bool = False
    retired_unsent: bool = False
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
        lane_mode: bool = False,
        lane_limit: int = FLOW_V2_MAX_LANES,
    ) -> None:
        self.epoch = uuid4().hex
        self._reserve = reserve
        self._release = release
        self._reserve_recovery = reserve_recovery or reserve
        self._release_recovery = release_recovery or release
        self._recovery_limit = min(RECOVERY_WINDOW_FRAMES, max(1, recovery_limit))
        self._on_stage = on_stage
        self._lane_mode = bool(lane_mode)
        self._lane_limit = max(1, int(lane_limit))
        self._lane_seen: set[str] = set()
        self._lane_states: dict[str, Literal["ACTIVE", "RETIRED", "CLOSED"]] = {}
        self._lane_epochs: dict[str, str | None] = {}
        self._lane_epoch_states: dict[
            tuple[str, str | None], Literal["ACTIVE", "RETIRED", "CLOSED"]
        ] = {}
        self._lane_retire_tokens: dict[str, str] = {}
        self._lane_epoch_retire_tokens: dict[tuple[str, str | None], str] = {}
        self._lane_final_ids: dict[str, int] = {}
        self._lane_epoch_final_ids: dict[tuple[str, str | None], int] = {}
        self._closed = False
        self.next_id = 1
        self.ack_id = 0
        # Per-lane watermarks are inert until a v2 caller supplies a lane.
        # Keeping them beside the v1 scalar lets the old contract and all
        # existing accounting paths retain their exact semantics.
        self.lane_ack_ids: dict[str, int] = {}
        # Physical delivery-id watermarks used by session-flow v2 are kept
        # separate from legacy lane-local sequence ACKs.  Sharing the map
        # lets a v1 lane sequence accidentally suppress a v2 ACK (or vice
        # versa) when the values happen to overlap.
        self.lane_delivery_ack_ids: dict[str, int] = {}
        self._lane_epoch_delivery_ack_ids: dict[tuple[str, str | None], int] = {}
        self._lane_next_ids: dict[str, int] = {}
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
        lane: str | None = None,
        lane_epoch: str | None = None,
    ) -> int | None:
        # An encoded event reserves a little extra for its writer-assigned
        # sequence. Accounting overhead is not part of the public wire limit.
        wire_size = size if wire_size is None else wire_size
        if self._closed:
            return None
        if lane is not None and self._lane_mode:
            state = self._lane_states.get(lane)
            previous_epoch = self._lane_epochs.get(lane)
            if (
                (lane, lane_epoch) not in self._lane_epoch_states
                and len(self._lane_epoch_states) >= FLOW_V2_MAX_LANE_EPOCHS
            ):
                return None
            if state in {"RETIRED", "CLOSED"}:
                # A replacement subscription receives a fresh epoch and may
                # reuse its session key.  Old-epoch ACKs remain fenced by the
                # epoch check below; only the replacement lane reactivates it.
                if lane_epoch is None or lane_epoch == previous_epoch:
                    return None
                self._lane_states[lane] = "ACTIVE"
                self.lane_ack_ids.pop(lane, None)
                self.lane_delivery_ack_ids.pop(lane, None)
                self._lane_next_ids[lane] = 1
            if lane not in self._lane_seen and len(self._lane_seen) >= self._lane_limit:
                return None
        if not 0 < wire_size <= MAX_WIRE_BYTES or not wire_size <= size <= MAX_WIRE_BYTES + 32:
            return None
        ordinary = [entry for entry in self.deliveries.values() if not entry.recovery]
        if self._lane_mode and lane is not None:
            lane_entries = [
                entry
                for entry in ordinary
                if entry.lane == lane and entry.lane_epoch == lane_epoch
            ]
            active_lanes = {entry.lane for entry in ordinary if entry.lane is not None}
            if lane not in active_lanes and len(active_lanes) >= self._lane_limit:
                return None
            if (
                len(lane_entries) >= FLOW_LANE_WINDOW_FRAMES
                or (
                    lane_entries
                    and sum(entry.size for entry in lane_entries) + size
                    > FLOW_LANE_WINDOW_BYTES
                )
            ):
                return None
            # Keep the aggregate v2 budget bounded as well.  It is deliberately
            # the sum of the small per-lane windows, so one stalled lane cannot
            # starve another while a connection still has a hard upper bound.
            if (
                len(ordinary) >= FLOW_LANE_WINDOW_FRAMES * self._lane_limit
                or sum(entry.size for entry in ordinary) + size
                > FLOW_LANE_WINDOW_BYTES * self._lane_limit
            ):
                return None
        if recovery:
            pieces = [entry for entry in self.deliveries.values() if entry.recovery]
            if (
                len(pieces) >= self._recovery_limit
                or sum(entry.size for entry in pieces) + size > RECOVERY_WINDOW_BYTES
                or (key is not None and any(entry.key == key for entry in pieces))
            ):
                return None
        elif self._lane_mode:
            # Unkeyed events cannot participate in lane fairness, but they
            # still share the bounded aggregate v2 budget.  Keep the legacy
            # single-window cap for that fallback subset as well.
            if lane is None and (
                len(ordinary) >= FLOW_WINDOW_FRAMES
                or (
                    ordinary
                    and sum(entry.size for entry in ordinary) + size > FLOW_WINDOW_BYTES
                )
            ):
                return None
            if (
                len(ordinary) >= FLOW_LANE_WINDOW_FRAMES * self._lane_limit
                or sum(entry.size for entry in ordinary) + size
                > FLOW_LANE_WINDOW_BYTES * self._lane_limit
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
        lane_sequence: int | None = None
        if lane is not None:
            lane_sequence = self._lane_next_ids.get(lane, 1)
            self._lane_next_ids[lane] = lane_sequence + 1
            self._lane_seen.add(lane)
            self._lane_states.setdefault(lane, "ACTIVE")
            self._lane_epochs[lane] = lane_epoch
            self._lane_epoch_states.setdefault((lane, lane_epoch), "ACTIVE")
            self._lane_epoch_delivery_ack_ids.setdefault((lane, lane_epoch), 0)
        self.deliveries[delivery_id] = Delivery(
            size, recovery=recovery, lane=lane, lane_epoch=lane_epoch,
            lane_sequence=lane_sequence,
            key=key, owner=owner,
            segment_index=segment_index, deadline=deadline,
        )
        return delivery_id

    def register_lane_epoch(self, lane: str, lane_epoch: str | None) -> None:
        """Fence a subscription epoch before its first physical delivery.

        A replacement subscription must invalidate an old ACK even when no
        event has been emitted on the replacement yet.  Delivery-time
        admission alone cannot establish that fence because an idle lane has
        no frame to admit.
        """
        if not self._lane_mode:
            return
        if not isinstance(lane, str) or not lane or len(lane) > 4096:
            raise ValueError("Invalid delivery lane")
        if lane_epoch is not None and (
            not isinstance(lane_epoch, str) or len(lane_epoch) > 4096
        ):
            raise ValueError("Invalid lane epoch")
        current = self._lane_epochs.get(lane)
        if current == lane_epoch:
            return
        if current is not None:
            state = self._lane_epoch_states.get((lane, current))
            # The old discard receipt may already have completed before a
            # user reopens this session. Both states fence old publication;
            # keep the old epoch ledger while registering the replacement.
            if state not in {"RETIRED", "CLOSED"} and not (
                state is None and self._lane_states.get(lane) in {"RETIRED", "CLOSED"}
            ):
                raise ValueError("Lane epoch replacement is not retired")
        epoch_key = (lane, lane_epoch)
        if (
            epoch_key not in self._lane_epoch_states
            and len(self._lane_epoch_states) >= FLOW_V2_MAX_LANE_EPOCHS
        ):
            raise ValueError("Lane epoch history is full")
        self._lane_seen.add(lane)
        self._lane_epochs[lane] = lane_epoch
        self._lane_states[lane] = "ACTIVE"
        self._lane_epoch_states.setdefault(epoch_key, "ACTIVE")
        self._lane_epoch_delivery_ack_ids.setdefault(epoch_key, 0)
        self._lane_next_ids.setdefault(lane, 1)
        self.lane_ack_ids.pop(lane, None)
        self.lane_delivery_ack_ids.pop(lane, None)

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

    def discard_retired_queued(self, delivery_id: int) -> bool:
        """Drop an explicitly retired queue entry, retaining credit until dequeue."""
        delivery = self.deliveries[delivery_id]
        if not delivery.retired_unsent:
            return False
        if delivery.sent or delivery.sending:
            raise ValueError("Retired queued delivery has already started sending")
        self._release_delivery(delivery_id)
        return True

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

    def validate_lane_acknowledgement(
        self, epoch: str, lane: str, lane_sequence: int,
    ) -> None:
        """Validate a v2 per-lane cumulative acknowledgement.

        This method is intentionally not used by the v1 connection path.  A
        Lane watermarks use lane-local sequence numbers.  They never depend on
        the interleaving of another lane's global delivery IDs.
        """
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        if not isinstance(lane, str) or not lane or len(lane) > 4096:
            raise ValueError("Invalid delivery lane")
        if (
            not isinstance(lane_sequence, int)
            or isinstance(lane_sequence, bool)
            or lane_sequence < 0
        ):
            raise ValueError("Invalid lane delivery acknowledgement")
        active_epoch = self._lane_epochs.get(lane)
        if any(
            entry.lane == lane and entry.lane_epoch != active_epoch
            for entry in self.deliveries.values()
        ):
            raise ValueError("Lane epoch is not current")
        previous = self.lane_ack_ids.get(lane, 0)
        if lane_sequence <= previous:
            return
        next_lane_sequence = self._lane_next_ids.get(lane, 1)
        if lane_sequence >= next_lane_sequence:
            raise ValueError("Acknowledgement is ahead of delivery")
        if any(
            entry.lane == lane and entry.lane_sequence is not None
            and entry.lane_sequence <= lane_sequence
            and not entry.sent and not entry.sending
            for entry in self.deliveries.values()
        ):
            raise ValueError("Acknowledgement includes an unsent delivery")
        if not any(
            entry.lane == lane and entry.lane_sequence is not None
            and entry.lane_sequence <= lane_sequence
            for entry in self.deliveries.values()
        ) and lane_sequence > previous:
            # A lane cannot jump over a sequence it has never owned.  This
            # catches forged lane names and stale client watermarks while
            # allowing an empty lane's initial zero ACK to remain idempotent.
            raise ValueError("Acknowledgement does not belong to lane")

    def acknowledge_lane(self, epoch: str, lane: str, lane_sequence: int) -> None:
        """Release only deliveries owned by ``lane`` through its watermark.

        The connection-wide ``ack_id`` remains unchanged when another lane has
        a hole.  This is the key isolation property absent from v1: a stalled
        lane keeps its own reservations while an active lane can continue to
        make progress and release its bytes.
        """
        self.validate_lane_acknowledgement(epoch, lane, lane_sequence)
        previous = self.lane_ack_ids.get(lane, 0)
        if lane_sequence <= previous:
            return
        for key in tuple(self.deliveries):
            entry = self.deliveries[key]
            if (
                entry.lane != lane or entry.lane_sequence is None
                or entry.lane_sequence > lane_sequence
            ):
                continue
            self._confirm_delivery(entry)
            # As with the v1 ACK, a physical send that is still unwinding must
            # retain its reservation until ``mark_send_finished``.
            if entry.sent:
                self._release_delivery(key)
        self.lane_ack_ids[lane] = lane_sequence

    def validate_lane_delivery_acknowledgement(
        self, epoch: str, lane: str, through_delivery_id: int,
        lane_epoch: str | None = None,
    ) -> None:
        """Validate a v2 cumulative ACK in physical delivery-id space."""
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        if not isinstance(lane, str) or not lane or len(lane) > 4096:
            raise ValueError("Invalid delivery lane")
        if (
            not isinstance(through_delivery_id, int)
            or isinstance(through_delivery_id, bool)
            or through_delivery_id < 0
        ):
            raise ValueError("Invalid lane delivery acknowledgement")
        current_lane_epoch = self._lane_epochs.get(lane)
        lane_epoch = current_lane_epoch if lane_epoch is None else lane_epoch
        epoch_state = self._lane_epoch_states.get((lane, lane_epoch))
        # A retire receipt may be acknowledged in the same control RPC that
        # retires the lane, while the lane's current epoch is still the old
        # one.  Once a replacement epoch has been admitted, however, an ACK
        # from the old epoch must stay fenced; the retire token is the only
        # authority allowed to release that old delivery set.
        if lane_epoch != current_lane_epoch:
            raise ValueError("Lane epoch is not current")
        if epoch_state not in {"ACTIVE", "RETIRED"}:
            raise ValueError("Lane epoch is not current")
        previous = self._lane_epoch_delivery_ack_ids.get((lane, lane_epoch), 0)
        if through_delivery_id <= previous:
            return
        if through_delivery_id >= self.next_id:
            raise ValueError("Acknowledgement is ahead of delivery")
        owned = [
            entry for delivery_id, entry in self.deliveries.items()
            if delivery_id <= through_delivery_id
            and entry.lane == lane and entry.lane_epoch == lane_epoch
        ]
        if not owned:
            raise ValueError("Acknowledgement does not belong to lane")
        if any(not entry.sent and not entry.sending for entry in owned):
            raise ValueError("Acknowledgement includes an unsent delivery")

    def acknowledge_lane_delivery(
        self, epoch: str, lane: str, through_delivery_id: int,
        lane_epoch: str | None = None,
    ) -> None:
        self.validate_lane_delivery_acknowledgement(
            epoch, lane, through_delivery_id, lane_epoch,
        )
        lane_epoch = self._lane_epochs.get(lane) if lane_epoch is None else lane_epoch
        previous = self._lane_epoch_delivery_ack_ids.get((lane, lane_epoch), 0)
        if through_delivery_id <= previous:
            return
        for delivery_id in tuple(self.deliveries):
            if delivery_id > through_delivery_id:
                break
            entry = self.deliveries[delivery_id]
            if entry.lane != lane or entry.lane_epoch != lane_epoch:
                continue
            self._confirm_delivery(entry)
            if entry.sent:
                self._release_delivery(delivery_id)
        self._lane_epoch_delivery_ack_ids[(lane, lane_epoch)] = through_delivery_id
        if self._lane_epochs.get(lane) == lane_epoch:
            self.lane_delivery_ack_ids[lane] = through_delivery_id

    def retire_lane(
        self, epoch: str, lane: str, *, retire_token: str | None = None,
        final_published_id: int | None = None, lane_epoch: str | None = None,
    ) -> str:
        """Fence publication for a lane and return its bounded retire token."""
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        if not isinstance(lane, str) or not lane or len(lane) > 4096:
            raise ValueError("Invalid delivery lane")
        if lane not in self._lane_seen:
            raise ValueError("Lane is not current")
        lane_epoch = self._lane_epochs.get(lane) if lane_epoch is None else lane_epoch
        epoch_key = (lane, lane_epoch)
        state = self._lane_epoch_states.get(epoch_key)
        if state is None:
            # Failed subscription admission can still run unsubscribe cleanup.
            # Cleanup must not allocate the epoch that registration rejected.
            raise ValueError("Lane epoch is not current")
        if state in {"RETIRED", "CLOSED"}:
            token = self._lane_epoch_retire_tokens.get(epoch_key)
            if retire_token is not None and retire_token != token:
                raise ValueError("Lane retire token is not current")
            expected_final = self._lane_epoch_final_ids.get(epoch_key, 0)
            if final_published_id is not None and final_published_id != expected_final:
                raise ValueError("Lane final delivery fence is not current")
            return token or uuid4().hex
        token = retire_token or self._lane_epoch_retire_tokens.get(epoch_key) or uuid4().hex
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError("Invalid lane retire token")
        self._lane_states[lane] = "RETIRED"
        self._lane_retire_tokens[lane] = token
        self._lane_epoch_states[epoch_key] = "RETIRED"
        self._lane_epoch_retire_tokens[epoch_key] = token
        if final_published_id is None:
            final_published_id = max(
                (delivery_id for delivery_id, delivery in self.deliveries.items()
                if delivery.lane == lane and delivery.lane_epoch == lane_epoch), default=0,
            )
        if not isinstance(final_published_id, int) or isinstance(final_published_id, bool):
            raise ValueError("Invalid lane final delivery id")
        self._lane_final_ids[lane] = max(0, final_published_id)
        self._lane_epoch_final_ids[epoch_key] = max(0, final_published_id)
        # Queued payloads still occupy the outbox. Keep their ledger and
        # credit until the writer dequeues and discards them; otherwise a
        # task switch leaves an uncharged frame with a missing delivery ID.
        for delivery_id, delivery in tuple(self.deliveries.items()):
            if (delivery.lane == lane and delivery.lane_epoch == lane_epoch
                    and not delivery.sent and not delivery.sending):
                delivery.retired_unsent = True
        return token

    def validate_lane_retire_confirmation(
        self, epoch: str, lane: str, retire_token: str, final_published_id: int,
        lane_epoch: str | None = None,
    ) -> None:
        if epoch != self.epoch:
            raise ValueError("Delivery epoch is not current")
        lane_epoch = self._lane_epochs.get(lane) if lane_epoch is None else lane_epoch
        epoch_key = (lane, lane_epoch)
        if self._lane_epoch_states.get(epoch_key) != "RETIRED":
            raise ValueError("Lane is not retired")
        if self._lane_epoch_retire_tokens.get(epoch_key) != retire_token:
            raise ValueError("Lane retire token is not current")
        if self._lane_epoch_final_ids.get(epoch_key) != final_published_id:
            raise ValueError("Lane final delivery fence is not current")

    def confirm_lane_retire(
        self, epoch: str, lane: str, retire_token: str, final_published_id: int,
        lane_epoch: str | None = None,
    ) -> None:
        self.validate_lane_retire_confirmation(
            epoch, lane, retire_token, final_published_id, lane_epoch,
        )
        lane_epoch = self._lane_epochs.get(lane) if lane_epoch is None else lane_epoch
        epoch_key = (lane, lane_epoch)
        for delivery_id, delivery in tuple(self.deliveries.items()):
            if delivery.lane != lane or delivery.lane_epoch != lane_epoch:
                continue
            if delivery.retired_unsent:
                continue
            self._confirm_delivery(delivery)
            if delivery.sent:
                self._release_delivery(delivery_id)
        self._lane_epoch_states[epoch_key] = "CLOSED"
        if self._lane_epochs.get(lane) == lane_epoch:
            self._lane_states[lane] = "CLOSED"
        # Once the client has confirmed the retire fence, the old epoch no
        # longer needs to remain in the authoritative ledger. Dropping it
        # releases one bounded epoch slot; a late ACK now fails as an unknown
        # epoch instead of being matched to a reused entry.
        self._lane_epoch_states.pop(epoch_key, None)
        self._lane_epoch_retire_tokens.pop(epoch_key, None)
        self._lane_epoch_final_ids.pop(epoch_key, None)
        self._lane_epoch_delivery_ack_ids.pop(epoch_key, None)

    def prune_settled_lanes(self) -> None:
        """Forget closed keys only after all epoch and physical-write owners leave.

        The connection calls this after fencing publication by its subscription
        authority. Standalone legacy users retain the lazy-admission API.
        """
        retained = {lane for lane, _ in self._lane_epoch_states}
        retained.update(
            delivery.lane for delivery in self.deliveries.values() if delivery.lane is not None
        )
        for lane, state in tuple(self._lane_states.items()):
            if state != "CLOSED" or lane in retained:
                continue
            self._lane_seen.discard(lane)
            for mapping in (
                self._lane_states, self._lane_epochs, self._lane_retire_tokens,
                self._lane_final_ids, self._lane_next_ids, self.lane_ack_ids,
                self.lane_delivery_ack_ids,
            ):
                mapping.pop(lane, None)

    def lane_for_epoch(self, lane_epoch: str) -> str | None:
        """Resolve a retired subscription epoch from the authoritative ledger."""
        for lane, epoch in self._lane_epoch_states:
            if epoch == lane_epoch:
                return lane
        return None

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
            "lane_states": dict(self._lane_states),
            "lane_ack_ids": dict(self.lane_ack_ids),
            "lane_delivery_ack_ids": dict(self.lane_delivery_ack_ids),
            "lane_epochs": dict(self._lane_epochs),
            "lane_epoch_states": {
                f"{lane}:{lane_epoch}": state
                for (lane, lane_epoch), state in self._lane_epoch_states.items()
            },
            "lane_retire_tokens": dict(self._lane_retire_tokens),
        }

    def dirty_notice(self) -> dict[str, Any]:
        return {
            "delivery_epoch": self.epoch,
            "dirty_keys": sorted(self.dirty),
            "global_dirty": self.global_dirty,
        }

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
        self.lane_ack_ids.clear()
        self.lane_delivery_ack_ids.clear()
        self._lane_epoch_delivery_ack_ids.clear()
        self._lane_next_ids.clear()
        self._lane_seen.clear()
        self._lane_states.clear()
        self._lane_epochs.clear()
        self._lane_epoch_states.clear()
        self._lane_retire_tokens.clear()
        self._lane_epoch_retire_tokens.clear()
        self._lane_final_ids.clear()
        self._lane_epoch_final_ids.clear()


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
