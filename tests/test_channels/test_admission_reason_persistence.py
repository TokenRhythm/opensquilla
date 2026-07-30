"""Persisting the admission reason so operators can ask "why was this denied?".

The admission decision is computed on every inbound message and was retained
nowhere: a denied sender saw silence and the operator had no record of the
reason. The ingress row now keeps the reason code — never a sender identity —
and the store can aggregate per-reason tallies for diagnostics.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.types import (
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
)


def _message(event_id: str) -> IncomingMessage:
    return IncomingMessage(
        sender_id="user-1",
        channel_id="chat-1",
        content="hello",
        metadata={"is_group": False, "native_message_id": event_id},
        provenance=IngressProvenance(
            provider="slack",
            account_id="acct-1",
            event_id=event_id,
            verification=IngressVerification.WEBHOOK_SIGNATURE,
        ),
    )


def _complete(store: ChannelDeliveryStore, event_id: str, disposition: str, reason: str) -> None:
    message = _message(event_id)
    assert store.accept_inbound("slack-main", message) is True
    claim = store.claim_inbound("slack-main", message)
    assert claim is not None
    store.complete_inbound(claim, disposition, reason=reason)


def test_complete_inbound_persists_the_reason_code(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    _complete(store, "event-1", "admission_denied", "not_in_allowlist")
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT disposition, reason FROM channel_ingress").fetchone()
    assert row == ("admission_denied", "not_in_allowlist")
    store.close()


def test_reason_defaults_to_empty_for_callers_that_do_not_pass_one(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    _complete(store, "event-1", "turn_completed", "")
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT reason FROM channel_ingress").fetchone()
    assert row == ("",)
    assert store.admission_reason_counts("slack-main") == {}
    store.close()


def test_admission_reason_counts_aggregates_per_reason(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    _complete(store, "event-1", "admission_denied", "pairing_required")
    _complete(store, "event-2", "admission_denied", "pairing_required")
    _complete(store, "event-3", "admission_denied", "not_in_allowlist")
    _complete(store, "event-4", "turn_dispatched", "dm_admitted")

    counts = store.admission_reason_counts("slack-main")

    assert counts["pairing_required"]["count"] == 2
    assert counts["not_in_allowlist"]["count"] == 1
    assert counts["dm_admitted"]["count"] == 1
    for entry in counts.values():
        assert isinstance(entry["last_at"], float)
        # first_at labels the tally horizon for consumers.
        assert isinstance(entry["first_at"], float)
        assert entry["first_at"] <= entry["last_at"]
    # Scoped per channel: another channel sees nothing.
    assert store.admission_reason_counts("other-channel") == {}
    store.close()


def test_reason_column_is_added_to_a_store_created_before_it_existed(tmp_path: Path) -> None:
    # CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so a
    # database from an older release lacks the column until the guarded
    # ALTER runs. Recreate that world with the pre-reason DDL.
    path = tmp_path / "delivery.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE channel_ingress (
                event_key       TEXT PRIMARY KEY,
                channel_name    TEXT NOT NULL,
                account_id      TEXT NOT NULL,
                lane_key        TEXT NOT NULL,
                message_json    TEXT NOT NULL,
                state           TEXT NOT NULL,
                disposition     TEXT NOT NULL DEFAULT '',
                claim_token     TEXT,
                claim_started_at REAL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT NOT NULL DEFAULT '',
                accepted_at     REAL NOT NULL,
                updated_at      REAL NOT NULL
            );
            INSERT INTO channel_ingress (
                event_key, channel_name, account_id, lane_key, message_json,
                state, disposition, accepted_at, updated_at
            ) VALUES (
                'slack:acct-1:legacy-1', 'slack-main', 'acct-1', 'chat-1:user-1',
                '{}', 'completed', 'turn_completed', 1.0, 1.0
            );
            """
        )

    store = ChannelDeliveryStore(path)
    # Legacy rows read back with the default; new writes carry a reason.
    with sqlite3.connect(store.path) as connection:
        legacy = connection.execute(
            "SELECT reason FROM channel_ingress WHERE event_key = 'slack:acct-1:legacy-1'"
        ).fetchone()
    assert legacy == ("",)
    _complete(store, "event-9", "admission_denied", "dm_denied")
    counts = store.admission_reason_counts("slack-main")
    assert list(counts) == ["dm_denied"]
    assert counts["dm_denied"]["count"] == 1
    store.close()


def test_migration_tolerates_losing_the_alter_race(tmp_path: Path) -> None:
    # Two processes first-opening an un-migrated DB can both observe the
    # column missing before either ALTER commits; the loser's "duplicate
    # column" error must read as already-migrated, not crash startup.
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    # Replay the ALTERs as the losing opener would: the columns already exist.
    store._add_column("channel_ingress", "reason TEXT NOT NULL DEFAULT ''")
    store._add_column("channel_pairings", "reply_to TEXT")
    # Any other operational error still surfaces.
    with pytest.raises(sqlite3.OperationalError):
        store._add_column("channel_ingress", "reason2 SYNTAX ERROR (")
    store.close()
