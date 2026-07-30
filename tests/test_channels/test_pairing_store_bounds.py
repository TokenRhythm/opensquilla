"""Bounds on the pairing store: pending cap, TTL prune, and write suppression.

Every denied DM used to be an unconditional durable write with no deletion
path — one flood and the operator's approval queue is unreadable forever.
The bounds must never touch decided rows: approval is a durable grant, and
revocation only stays enforced while its row survives.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from opensquilla.channels.delivery_store import (
    ChannelDeliveryStore,
)


def _request(store: ChannelDeliveryStore, sender: str, **kwargs):
    return store.request_pairing(
        channel_name="telegram-main",
        provider="telegram",
        account_id="bot-1",
        sender_id=sender,
        **kwargs,
    )


def test_pending_cap_refuses_new_senders_without_locking_out_decided_ones(
    tmp_path: Path,
) -> None:
    store = ChannelDeliveryStore(
        tmp_path / "delivery.sqlite", max_pending_pairings_per_channel=3
    )
    approved = _request(store, "veteran")
    assert approved is not None
    store.set_pairing_status(
        channel_name="telegram-main", pairing_id=approved.pairing_id, status="approved"
    )
    for i in range(3):
        assert _request(store, f"stranger-{i}") is not None

    # Queue full: a brand-new sender is refused with a value, not an exception.
    assert _request(store, "stranger-overflow") is None
    # An approved sender still gets their row back at cap.
    veteran = _request(store, "veteran")
    assert veteran is not None and veteran.status == "approved"
    # An already-pending sender also still gets their row back.
    repeat = _request(store, "stranger-1")
    assert repeat is not None and repeat.status == "pending"
    # Other channels are not affected by this channel's full queue.
    other = store.request_pairing(
        channel_name="slack-main", provider="slack", account_id="bot-2", sender_id="new"
    )
    assert other is not None
    store.close()


def test_prune_expires_only_stale_pending_rows(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite", pending_pairing_ttl_s=3600.0)
    stale_pending = _request(store, "ghost")
    approved = _request(store, "grantee")
    revoked = _request(store, "banned")
    assert stale_pending and approved and revoked
    store.set_pairing_status(
        channel_name="telegram-main", pairing_id=approved.pairing_id, status="approved"
    )
    store.set_pairing_status(
        channel_name="telegram-main", pairing_id=revoked.pairing_id, status="revoked"
    )
    # Backdate everything past the TTL: only the pending row may expire —
    # approval is a durable grant, and deleting a revoked row would let the
    # sender's next message recreate a fresh pending request.
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE channel_pairings SET last_seen_at = last_seen_at - 90000")

    store._prune_stale_pending_pairings(time.time())

    statuses = {p.sender_id: p.status for p in store.list_pairings(channel_name="telegram-main")}
    assert statuses == {"grantee": "approved", "banned": "revoked"}
    store.close()


def test_repeat_request_in_window_skips_the_durable_write(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite", pairing_refresh_window_s=3600.0)
    first = _request(store, "user-1", reply_to="dm-1")
    assert first is not None and first.request_count == 1

    # Same sender, same route, inside the window: no write happens.
    repeat = _request(store, "user-1", reply_to="dm-1")
    assert repeat is not None
    assert repeat.request_count == 1
    assert repeat.last_seen_at == first.last_seen_at

    # A changed reply address must write through — the approval notice depends
    # on the freshest route.
    moved = _request(store, "user-1", reply_to="dm-2")
    assert moved is not None
    assert moved.reply_to == "dm-2"
    assert moved.request_count == 2
    store.close()


def test_list_pairings_pagination_is_opt_in(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    for i in range(5):
        assert _request(store, f"sender-{i}") is not None

    everything = store.list_pairings(channel_name="telegram-main")
    assert len(everything) == 5

    page = store.list_pairings(channel_name="telegram-main", limit=2, offset=2)
    assert len(page) == 2
    assert [p.sender_id for p in page] == [p.sender_id for p in everything[2:4]]
    store.close()
