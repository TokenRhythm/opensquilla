"""Durable channel ingress journal and outbound delivery intent store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.channels.contract import (
    ChannelSendResult,
    channel_capability_profile,
    classify_channel_send_error,
    normalize_channel_send_result,
)
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.paths import state_dir

log = structlog.get_logger(__name__)

_ERROR_SECRET = re.compile(
    r"(?i)\b(authorization|access[_ -]?token|app[_ -]?secret|bot[_ -]?token|"
    r"client[_ -]?secret|password|signing[_ -]?secret)\b"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_TELEGRAM_URL_TOKEN = re.compile(r"(?i)(/bot)[^/\s]+")

# Pending pairing requests an operator never acted on are pruned after this
# long; approved and revoked rows never expire — approval is a durable access
# grant, and revocation only stays enforced while its row survives.
PENDING_PAIRING_TTL_S = 7 * 24 * 3600.0
# New pending rows a channel will hold at once. Every denied DM from a new
# sender is a durable row, so without a cap one flood makes the operator's
# approval queue unreadable and the disk grow without bound.
MAX_PENDING_PAIRINGS_PER_CHANNEL = 25
# Repeat requests inside this window skip the durable write when nothing
# would change — a sender re-sending in a tight loop otherwise costs one
# synchronous commit per message before admission even runs.
PAIRING_REFRESH_WINDOW_S = 30.0
_PAIRING_PRUNE_EVERY = 64
# Match the bounded event caches used by channel adapters. Pending degraded
# events remain tracked until claimed; only claimed pass-through events are
# eligible for LRU eviction.
_DEGRADED_INGRESS_DEDUPE_SIZE = 10_000


@dataclass(frozen=True, slots=True)
class IngressClaim:
    event_key: str
    claim_token: str


@dataclass(frozen=True, slots=True)
class TransportLease:
    channel_name: str
    account_id: str
    owner_id: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class ChannelPairing:
    """Durable access decision for one authenticated channel principal."""

    pairing_id: str
    channel_name: str
    provider: str
    account_id: str
    sender_id: str
    sender_name: str | None
    status: str
    created_at: float
    last_seen_at: float
    approved_at: float | None
    revoked_at: float | None
    request_count: int
    # Address the request arrived on, so an approval can reach the sender who
    # has no session yet. Never holds message content — only the route.
    reply_to: str | None = None


def _safe_message_json(message: IncomingMessage | OutgoingMessage) -> str:
    try:
        return message.model_dump_json()
    except (TypeError, ValueError, UnicodeError):
        payload = message.model_dump(mode="python")
        attachments = payload.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict) and attachment.get("data") is not None:
                    attachment["data"] = None
                    metadata = attachment.setdefault("metadata", {})
                    if isinstance(metadata, dict):
                        metadata["durable_payload_omitted"] = True
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _safe_error_text(error: BaseException) -> str:
    text = str(error)[:2000]
    text = _ERROR_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return _TELEGRAM_URL_TOKEN.sub(r"\1[REDACTED]", text)


def inbound_event_key(channel_name: str, message: IncomingMessage) -> str | None:
    provenance = message.provenance
    native_event_id = provenance.event_id
    if not native_event_id:
        metadata = message.metadata or {}
        native_event_id = str(
            metadata.get("update_id")
            or metadata.get("event_id")
            or metadata.get("native_message_id")
            or metadata.get("message_id")
            or metadata.get("msg_id")
            or ""
        )
    if not native_event_id:
        return None
    provider = provenance.provider or channel_name
    account_id = provenance.account_id or channel_name
    return f"{provider}:{account_id}:{native_event_id}"


class ChannelDeliveryStore:
    """SQLite-backed at-least-once ingress and explicit outbound receipts."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        pending_pairing_ttl_s: float = PENDING_PAIRING_TTL_S,
        max_pending_pairings_per_channel: int = MAX_PENDING_PAIRINGS_PER_CHANNEL,
        pairing_refresh_window_s: float = PAIRING_REFRESH_WINDOW_S,
    ) -> None:
        self.path = Path(db_path) if db_path is not None else state_dir("channel_delivery.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_pairing_ttl_s = pending_pairing_ttl_s
        self._max_pending_pairings_per_channel = max_pending_pairings_per_channel
        self._pairing_refresh_window_s = pairing_refresh_window_s
        self._pairing_writes = 0
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            os.fspath(self.path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # ``recover_inbound`` returns already-persisted rows which the manager
        # feeds back through each adapter's normal ``enqueue`` method.  Keep a
        # one-shot, process-local permit for those rows so recovery can make
        # them visible in memory without treating an ordinary provider replay
        # of an unclaimed ``accepted`` row as a new delivery.
        self._recovered_pending_visibility: set[str] = set()
        # Events accepted memory-only because the journal write itself failed
        # (database locked past the busy timeout, disk full, I/O error).
        # ``claim_inbound`` honors these with a pass-through claim so a
        # degraded message is still processed instead of being dropped as a
        # duplicate for lacking an ``accepted`` row.
        self._unjournaled_events: set[str] = set()
        # A pass-through claim is issued at most once while the journal remains
        # unavailable. Claimed markers move into this bounded LRU so a long
        # outage cannot retain every event for the process lifetime.
        self._claimed_unjournaled_events: OrderedDict[str, None] = OrderedDict()
        self._max_claimed_unjournaled_events = _DEGRADED_INGRESS_DEDUPE_SIZE
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._init_schema()
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channel_ingress (
                    event_key       TEXT PRIMARY KEY,
                    channel_name    TEXT NOT NULL,
                    account_id      TEXT NOT NULL,
                    lane_key        TEXT NOT NULL,
                    message_json    TEXT NOT NULL,
                    state           TEXT NOT NULL,
                    disposition     TEXT NOT NULL DEFAULT '',
                    reason          TEXT NOT NULL DEFAULT '',
                    claim_token     TEXT,
                    claim_started_at REAL,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    last_error      TEXT NOT NULL DEFAULT '',
                    accepted_at     REAL NOT NULL,
                    updated_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_channel_ingress_pending
                ON channel_ingress(channel_name, state, accepted_at);

                CREATE TABLE IF NOT EXISTS channel_outbox (
                    send_id             TEXT PRIMARY KEY,
                    channel_name        TEXT NOT NULL,
                    target_id           TEXT NOT NULL,
                    message_json        TEXT NOT NULL,
                    content_sha256      TEXT NOT NULL,
                    state               TEXT NOT NULL,
                    capability          TEXT NOT NULL DEFAULT '',
                    provider_message_id TEXT NOT NULL DEFAULT '',
                    provider_file_id    TEXT NOT NULL DEFAULT '',
                    retryable           INTEGER NOT NULL DEFAULT 0,
                    error_class         TEXT NOT NULL DEFAULT '',
                    error_message       TEXT NOT NULL DEFAULT '',
                    created_at          REAL NOT NULL,
                    updated_at          REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_channel_outbox_state
                ON channel_outbox(channel_name, state, created_at);

                CREATE TABLE IF NOT EXISTS channel_transport_leases (
                    channel_name  TEXT NOT NULL,
                    account_id    TEXT NOT NULL,
                    owner_id      TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL,
                    PRIMARY KEY (channel_name, account_id)
                );

                CREATE TABLE IF NOT EXISTS channel_pairings (
                    pairing_id  TEXT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    provider     TEXT NOT NULL,
                    account_id   TEXT NOT NULL,
                    sender_id    TEXT NOT NULL,
                    sender_name  TEXT,
                    status       TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    approved_at  REAL,
                    revoked_at   REAL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    reply_to     TEXT,
                    UNIQUE (channel_name, account_id, sender_id)
                );
                CREATE INDEX IF NOT EXISTS idx_channel_pairings_status
                ON channel_pairings(channel_name, status, created_at);
                """
            )
            self._migrate_pairing_columns()
            self._migrate_ingress_columns()
            self._conn.commit()

    def _migrate_pairing_columns(self) -> None:
        """Add pairing columns introduced after the table shipped.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing database, so
        stores created before a column existed need it added explicitly or
        every read raises "no such column".
        """
        existing = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(channel_pairings)")
        }
        if "reply_to" not in existing:
            self._add_column("channel_pairings", "reply_to TEXT")

    def _migrate_ingress_columns(self) -> None:
        """Add ingress columns introduced after the table shipped."""
        existing = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(channel_ingress)")
        }
        if "reason" not in existing:
            self._add_column("channel_ingress", "reason TEXT NOT NULL DEFAULT ''")

    def _add_column(self, table: str, column_ddl: str) -> None:
        """ALTER-in a column, tolerating a concurrent opener winning the race.

        The PRAGMA check and the ALTER are separate autocommit statements, so
        two processes first-opening an un-migrated database can both see the
        column missing; the loser's ALTER must read as "already migrated", not
        crash its startup.
        """
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    @staticmethod
    def _pairing_from_row(row: sqlite3.Row) -> ChannelPairing:
        return ChannelPairing(
            pairing_id=str(row["pairing_id"]),
            channel_name=str(row["channel_name"]),
            provider=str(row["provider"]),
            account_id=str(row["account_id"]),
            sender_id=str(row["sender_id"]),
            sender_name=(str(row["sender_name"]) if row["sender_name"] else None),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            last_seen_at=float(row["last_seen_at"]),
            approved_at=(
                float(row["approved_at"]) if row["approved_at"] is not None else None
            ),
            reply_to=(str(row["reply_to"]) if row["reply_to"] else None),
            revoked_at=(
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
            request_count=int(row["request_count"]),
        )

    def request_pairing(
        self,
        *,
        channel_name: str,
        provider: str,
        account_id: str,
        sender_id: str,
        sender_name: str | None = None,
        reply_to: str | None = None,
    ) -> ChannelPairing | None:
        """Create or refresh a pending request without storing message content.

        ``reply_to`` is the address the request arrived on — kept so an
        approval can reach a sender who has no session yet. It is a route,
        never content.

        Returns ``None`` only when the store refuses to create a NEW row
        because the channel's pending queue is full. A sender with an existing
        row — pending, approved, or revoked — always gets that row back, so a
        full queue can never lock out an already-decided sender. Refusal is a
        return value, never an exception: this runs on the admission path,
        where an exception kills the channel's dispatch loop.
        """
        if not channel_name.strip() or not sender_id.strip():
            raise ValueError("channel_name and sender_id are required")
        now = time.time()
        pairing_id = uuid.uuid4().hex
        safe_name = sender_name.strip()[:256] if isinstance(sender_name, str) else None
        safe_name = safe_name or None
        account = account_id.strip() or channel_name.strip()
        clean_reply_to = (reply_to.strip() or None) if isinstance(reply_to, str) else None
        with self._lock:
            self._pairing_writes += 1
            if self._pairing_writes % _PAIRING_PRUNE_EVERY == 0:
                self._prune_stale_pending_pairings(now)
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT * FROM channel_pairings WHERE channel_name = ? "
                "AND account_id = ? AND sender_id = ?",
                (channel_name.strip(), account, sender_id.strip()),
            ).fetchone()
            if existing is not None:
                # Flood guard: a repeat request that would change nothing
                # skips the durable write entirely.
                fresh = now - float(existing["last_seen_at"]) < self._pairing_refresh_window_s
                same_route = clean_reply_to is None or clean_reply_to == existing["reply_to"]
                same_name = safe_name is None or safe_name == existing["sender_name"]
                if fresh and same_route and same_name:
                    self._conn.rollback()
                    return self._pairing_from_row(existing)
            else:
                pending = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM channel_pairings "
                    "WHERE channel_name = ? AND status = 'pending'",
                    (channel_name.strip(),),
                ).fetchone()
                if int(pending["count"]) >= self._max_pending_pairings_per_channel:
                    self._conn.rollback()
                    log.warning(
                        "channel.pairing_queue_full",
                        channel=channel_name.strip(),
                        pending=int(pending["count"]),
                        cap=self._max_pending_pairings_per_channel,
                    )
                    return None
            self._conn.execute(
                "INSERT INTO channel_pairings "
                "(pairing_id, channel_name, provider, account_id, sender_id, "
                "sender_name, status, created_at, last_seen_at, reply_to) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?) "
                "ON CONFLICT(channel_name, account_id, sender_id) DO UPDATE SET "
                "provider = excluded.provider, "
                "sender_name = COALESCE(excluded.sender_name, channel_pairings.sender_name), "
                "last_seen_at = excluded.last_seen_at, "
                "reply_to = COALESCE(excluded.reply_to, channel_pairings.reply_to), "
                "request_count = channel_pairings.request_count + 1",
                (
                    pairing_id,
                    channel_name.strip(),
                    provider.strip(),
                    account,
                    sender_id.strip(),
                    safe_name,
                    now,
                    now,
                    clean_reply_to,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM channel_pairings WHERE channel_name = ? "
                "AND account_id = ? AND sender_id = ?",
                (channel_name.strip(), account, sender_id.strip()),
            ).fetchone()
            self._conn.commit()
        if row is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("pairing request was not persisted")
        return self._pairing_from_row(row)

    def _prune_stale_pending_pairings(self, now: float) -> None:
        """Best-effort expiry of pending rows the operator never acted on.

        Only ``pending`` rows expire. Approved rows are durable access grants,
        and revoked rows must survive because revocation is enforced by the
        row's status — deleting one would let the sender's next message
        recreate a fresh pending request.
        """
        cutoff = now - self._pending_pairing_ttl_s
        try:
            cursor = self._conn.execute(
                "DELETE FROM channel_pairings WHERE status = 'pending' AND last_seen_at < ?",
                (cutoff,),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.warning("channel.pairing_prune_failed", exc_info=True)
            return
        if cursor.rowcount:
            log.info(
                "channel.pairing_pruned",
                rows=cursor.rowcount,
                ttl_days=round(self._pending_pairing_ttl_s / 86400.0, 1),
            )

    def list_pairings(
        self,
        *,
        channel_name: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ChannelPairing]:
        """List pairing records newest-first for operator review.

        ``limit``/``offset`` are opt-in: the default remains the full list, so
        callers that summarize by status (badges, tab counts) stay complete.
        """
        if status is not None and status not in {"pending", "approved", "revoked"}:
            raise ValueError("invalid pairing status")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        clauses: list[str] = []
        values: list[Any] = []
        if channel_name:
            clauses.append("channel_name = ?")
            values.append(channel_name)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        page = ""
        if limit is not None:
            page = " LIMIT ? OFFSET ?"
            values.extend((limit, offset))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM channel_pairings{where} "  # noqa: S608
                f"ORDER BY last_seen_at DESC, pairing_id ASC{page}",
                values,
            ).fetchall()
        return [self._pairing_from_row(row) for row in rows]

    def set_pairing_status(
        self,
        *,
        channel_name: str,
        pairing_id: str,
        status: str,
    ) -> ChannelPairing:
        """Approve or revoke a pairing with a channel-bound identifier."""
        if status not in {"approved", "revoked"}:
            raise ValueError("pairing status must be approved or revoked")
        now = time.time()
        approved_at = now if status == "approved" else None
        revoked_at = now if status == "revoked" else None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE channel_pairings SET status = ?, approved_at = ?, revoked_at = ? "
                "WHERE channel_name = ? AND pairing_id = ?",
                (status, approved_at, revoked_at, channel_name, pairing_id),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise KeyError(f"Pairing not found: {pairing_id}")
            row = self._conn.execute(
                "SELECT * FROM channel_pairings WHERE channel_name = ? AND pairing_id = ?",
                (channel_name, pairing_id),
            ).fetchone()
            self._conn.commit()
        if row is None:  # pragma: no cover - guarded by the update above
            raise RuntimeError("pairing status was not persisted")
        return self._pairing_from_row(row)

    def accept_inbound(self, channel_name: str, message: IncomingMessage) -> bool:
        """Commit an inbound event before a provider-facing ACK is returned.

        A storage fault degrades to memory-only acceptance instead of raising:
        this runs inside adapter receive loops (Telegram polling, the Discord
        gateway dispatch loop, SDK message hooks), where one transient SQLite
        error would otherwise kill the channel's receive path outright. The
        degraded event loses at-least-once crash durability for that single
        message — the pre-journal baseline — and is recorded so
        :meth:`claim_inbound` still lets dispatch process it.
        """
        event_key = inbound_event_key(channel_name, message)
        if event_key is None:
            return True
        now = time.time()
        lane_key = f"{message.channel_id}:{message.sender_id}"
        with self._lock:
            if event_key in self._claimed_unjournaled_events:
                self._claimed_unjournaled_events.move_to_end(event_key)
                return False
            if event_key in self._unjournaled_events:
                # A prior delivery of this event was already accepted
                # memory-only after a journal fault, and its pass-through claim
                # is still pending. If storage has since recovered, taking the
                # durable INSERT path now would commit an ``accepted`` row while
                # the marker still exists, so both the marker and the row would
                # hand out a claim and the message would dispatch twice. Treat
                # the redelivery as a duplicate (mirroring the durable-duplicate
                # return below) so it is not re-enqueued. ``claim_inbound``
                # either promotes the marker to a durable processing row or
                # retains it after issuing one pass-through claim.
                return False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        "INSERT INTO channel_ingress "
                        "(event_key, channel_name, account_id, lane_key, message_json, "
                        "state, accepted_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)",
                        (
                            event_key,
                            channel_name,
                            message.provenance.account_id or channel_name,
                            lane_key,
                            _safe_message_json(message),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    row = self._conn.execute(
                        "SELECT state FROM channel_ingress WHERE event_key = ?",
                        (event_key,),
                    ).fetchone()
                    if (
                        row is not None
                        and str(row["state"]) == "accepted"
                        and event_key in self._recovered_pending_visibility
                    ):
                        self._recovered_pending_visibility.discard(event_key)
                        return True
                    return False
                self._conn.commit()
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
                self._unjournaled_events.add(event_key)
                log.warning(
                    "channel.ingress_journal_degraded",
                    channel=channel_name,
                    error_type=type(exc).__name__,
                    error=_safe_error_text(exc),
                )
        return True

    def claim_inbound(
        self,
        channel_name: str,
        message: IncomingMessage,
    ) -> IngressClaim | None:
        event_key = inbound_event_key(channel_name, message)
        if event_key is None:
            return IngressClaim("", "")
        with self._lock:
            if event_key in self._claimed_unjournaled_events:
                self._claimed_unjournaled_events.move_to_end(event_key)
                return None
            if event_key in self._unjournaled_events:
                # Accepted memory-only after a journal write failure: there is
                # no ``accepted`` row to update. If storage has recovered,
                # restore the ordinary durable claim lifecycle so completion,
                # failure, payload scrubbing, and restart recovery all work.
                token = uuid.uuid4().hex
                now = time.time()
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.execute(
                        "INSERT INTO channel_ingress "
                        "(event_key, channel_name, account_id, lane_key, message_json, "
                        "state, claim_token, claim_started_at, attempts, accepted_at, "
                        "updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'processing', ?, ?, 1, ?, ?)",
                        (
                            event_key,
                            channel_name,
                            message.provenance.account_id or channel_name,
                            f"{message.channel_id}:{message.sender_id}",
                            _safe_message_json(message),
                            token,
                            now,
                            now,
                            now,
                        ),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.rollback()
                    # Another writer already made this event durable. Its row
                    # is now the dedup authority; do not dispatch this copy.
                    self._unjournaled_events.discard(event_key)
                    return None
                except sqlite3.Error:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.rollback()
                    # Preserve availability for the original delivery, while
                    # retaining enough process-local state to reject both a
                    # provider redelivery and a repeated direct claim.
                    self._unjournaled_events.discard(event_key)
                    self._claimed_unjournaled_events[event_key] = None
                    if (
                        len(self._claimed_unjournaled_events)
                        > self._max_claimed_unjournaled_events
                    ):
                        self._claimed_unjournaled_events.popitem(last=False)
                    return IngressClaim("", "")
                self._unjournaled_events.discard(event_key)
                return IngressClaim(event_key, token)
        token = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE channel_ingress SET state = 'processing', claim_token = ?, "
                "claim_started_at = ?, attempts = attempts + 1, updated_at = ? "
                "WHERE event_key = ? AND channel_name = ? AND state = 'accepted'",
                (token, now, now, event_key, channel_name),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return None
            self._conn.commit()
        return IngressClaim(event_key, token)

    def complete_inbound(
        self,
        claim: IngressClaim | None,
        disposition: str,
        *,
        reason: str = "",
        scrub_payload: bool = False,
    ) -> None:
        """Finalize a claimed inbound event.

        ``reason`` carries the admission reason code so operators can later ask
        *why* a message was denied (or on what basis it was admitted) — a
        reason code only, never a sender identity.
        """
        if claim is None or not claim.event_key:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE channel_ingress SET state = 'completed', disposition = ?, reason = ?, "
                "message_json = CASE WHEN ? THEN '{}' ELSE message_json END, "
                "claim_token = NULL, claim_started_at = NULL, updated_at = ? "
                "WHERE event_key = ? AND claim_token = ?",
                (
                    disposition,
                    reason,
                    1 if scrub_payload else 0,
                    time.time(),
                    claim.event_key,
                    claim.claim_token,
                ),
            )
            self._conn.commit()

    def fail_inbound(self, claim: IngressClaim | None, error: BaseException) -> None:
        if claim is None or not claim.event_key:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE channel_ingress SET state = 'accepted', last_error = ?, "
                "claim_token = NULL, claim_started_at = NULL, updated_at = ? "
                "WHERE event_key = ? AND claim_token = ?",
                (
                    f"{type(error).__name__}: {_safe_error_text(error)}"[:2000],
                    time.time(),
                    claim.event_key,
                    claim.claim_token,
                ),
            )
            self._conn.commit()

    def recover_inbound(self, channel_name: str) -> list[IncomingMessage]:
        """Release interrupted claims and return accepted events in lane order."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "UPDATE channel_ingress SET state = 'accepted', claim_token = NULL, "
                "claim_started_at = NULL, updated_at = ? "
                "WHERE channel_name = ? AND state = 'processing'",
                (time.time(), channel_name),
            )
            rows = self._conn.execute(
                "SELECT event_key, message_json FROM channel_ingress "
                "WHERE channel_name = ? AND state = 'accepted' "
                "ORDER BY lane_key, accepted_at",
                (channel_name,),
            ).fetchall()
            self._recovered_pending_visibility.update(
                str(row["event_key"]) for row in rows
            )
            self._conn.commit()
        recovered: list[IncomingMessage] = []
        for row in rows:
            try:
                recovered.append(IncomingMessage.model_validate_json(row["message_json"]))
            except (ValueError, TypeError):
                continue
        return recovered

    def begin_send(
        self,
        channel_name: str,
        message: OutgoingMessage,
        *,
        capability: str = "message",
    ) -> str:
        send_id = str(message.metadata.get("delivery_id") or uuid.uuid4().hex)
        now = time.time()
        target_id = str(message.reply_to or message.metadata.get("channel") or "")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO channel_outbox "
                "(send_id, channel_name, target_id, message_json, content_sha256, "
                "state, capability, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    send_id,
                    channel_name,
                    target_id,
                    _safe_message_json(message),
                    hashlib.sha256(message.content.encode()).hexdigest(),
                    capability,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return send_id

    def complete_send(
        self,
        send_id: str,
        result: Any,
        *,
        capability: str = "message",
        target_id: str = "",
    ) -> ChannelSendResult:
        normalized = normalize_channel_send_result(
            result,
            capability=capability,
            target_id=target_id,
        )
        state = (
            "sent" if result is not None and normalized.is_delivered() else normalized.status.value
        )
        if result is None:
            state = "sent_unconfirmed"
        with self._lock:
            self._conn.execute(
                "UPDATE channel_outbox SET state = ?, capability = ?, "
                "provider_message_id = ?, provider_file_id = ?, retryable = ?, "
                "error_message = ?, updated_at = ? WHERE send_id = ?",
                (
                    state,
                    normalized.capability,
                    normalized.provider_message_id,
                    normalized.provider_file_id,
                    1 if normalized.retryable else 0,
                    normalized.reason,
                    time.time(),
                    send_id,
                ),
            )
            self._conn.commit()
        return normalized

    def fail_send(self, send_id: str, error: BaseException) -> None:
        # error_class carries the taxonomy value every consumer branches on
        # (doctor's auth_invalid alert, the console's operator cause lines);
        # the concrete exception type stays in error_message so nothing is
        # lost for debugging.
        with self._lock:
            self._conn.execute(
                "UPDATE channel_outbox SET state = 'unknown', error_class = ?, "
                "error_message = ?, updated_at = ? WHERE send_id = ?",
                (
                    classify_channel_send_error(error),
                    f"{type(error).__name__}: {_safe_error_text(error)}"[:2000],
                    time.time(),
                    send_id,
                ),
            )
            self._conn.commit()

    def diagnostics(self, channel_name: str) -> dict[str, Any]:
        with self._lock:
            ingress = self._conn.execute(
                "SELECT state, COUNT(*) AS count, MIN(accepted_at) AS oldest "
                "FROM channel_ingress WHERE channel_name = ? GROUP BY state",
                (channel_name,),
            ).fetchall()
            outbox = self._conn.execute(
                "SELECT state, COUNT(*) AS count, MIN(created_at) AS oldest "
                "FROM channel_outbox WHERE channel_name = ? GROUP BY state",
                (channel_name,),
            ).fetchall()
            leases = self._conn.execute(
                "SELECT account_id, owner_id, fencing_token, expires_at "
                "FROM channel_transport_leases WHERE channel_name = ?",
                (channel_name,),
            ).fetchall()
        return {
            "ingress": {
                str(row["state"]): {
                    "count": int(row["count"]),
                    "oldest_at": row["oldest"],
                }
                for row in ingress
            },
            "outbox": {
                str(row["state"]): {
                    "count": int(row["count"]),
                    "oldest_at": row["oldest"],
                }
                for row in outbox
            },
            "leases": [
                {
                    "account_id": str(row["account_id"]),
                    "owner_id": str(row["owner_id"]),
                    "fencing_token": int(row["fencing_token"]),
                    "expires_at": float(row["expires_at"]),
                    "expired": float(row["expires_at"]) <= time.time(),
                }
                for row in leases
            ],
        }

    def admission_reason_counts(self, channel_name: str) -> dict[str, dict[str, Any]]:
        """Per-reason admission tallies: ``{reason: {count, first_at, last_at}}``.

        Tallies span the store's whole lifetime; ``first_at`` lets consumers
        label that horizon so old denials are not mistaken for a live condition
        under the current policy. Aggregate counts and timestamps only — reason
        codes carry no sender identity, so this is safe to surface in operator
        diagnostics verbatim.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, COUNT(*) AS count, "
                "MIN(updated_at) AS first_at, MAX(updated_at) AS last_at "
                "FROM channel_ingress WHERE channel_name = ? AND reason != '' "
                "GROUP BY reason",
                (channel_name,),
            ).fetchall()
        return {
            str(row["reason"]): {
                "count": int(row["count"]),
                "first_at": float(row["first_at"]),
                "last_at": float(row["last_at"]),
            }
            for row in rows
        }

    def acquire_transport_lease(
        self,
        channel_name: str,
        account_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 120.0,
    ) -> TransportLease | None:
        now = time.time()
        expires_at = now + max(5.0, ttl_seconds)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT owner_id, fencing_token, expires_at "
                "FROM channel_transport_leases "
                "WHERE channel_name = ? AND account_id = ?",
                (channel_name, account_id),
            ).fetchone()
            if (
                row is not None
                and str(row["owner_id"]) != owner_id
                and float(row["expires_at"]) > now
            ):
                self._conn.rollback()
                return None
            fencing_token = int(row["fencing_token"]) + 1 if row is not None else 1
            self._conn.execute(
                "INSERT INTO channel_transport_leases "
                "(channel_name, account_id, owner_id, fencing_token, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_name, account_id) DO UPDATE SET "
                "owner_id = excluded.owner_id, fencing_token = excluded.fencing_token, "
                "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (
                    channel_name,
                    account_id,
                    owner_id,
                    fencing_token,
                    expires_at,
                    now,
                ),
            )
            self._conn.commit()
        return TransportLease(
            channel_name,
            account_id,
            owner_id,
            fencing_token,
            expires_at,
        )

    def renew_transport_lease(
        self,
        lease: TransportLease,
        *,
        ttl_seconds: float = 120.0,
    ) -> TransportLease | None:
        now = time.time()
        expires_at = now + max(5.0, ttl_seconds)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE channel_transport_leases SET expires_at = ?, updated_at = ? "
                "WHERE channel_name = ? AND account_id = ? AND owner_id = ? "
                "AND fencing_token = ?",
                (
                    expires_at,
                    now,
                    lease.channel_name,
                    lease.account_id,
                    lease.owner_id,
                    lease.fencing_token,
                ),
            )
            self._conn.commit()
        if cursor.rowcount != 1:
            return None
        return TransportLease(
            lease.channel_name,
            lease.account_id,
            lease.owner_id,
            lease.fencing_token,
            expires_at,
        )

    def release_transport_lease(self, lease: TransportLease) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE channel_transport_leases SET expires_at = 0, updated_at = ? "
                "WHERE channel_name = ? AND account_id = ? AND owner_id = ? "
                "AND fencing_token = ?",
                (
                    time.time(),
                    lease.channel_name,
                    lease.account_id,
                    lease.owner_id,
                    lease.fencing_token,
                ),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def delivery_store_for_config(config: Any) -> ChannelDeliveryStore:
    configured = str(getattr(config, "state_dir", "") or "").strip()
    root = Path(configured).expanduser() if configured else state_dir()
    return ChannelDeliveryStore(root / "channel_delivery.sqlite")


def durable_enqueue(channel: Any, message: IncomingMessage, queue: Any) -> bool:
    """Commit then enqueue using a store injected by :class:`ChannelManager`."""
    store = getattr(channel, "_delivery_store", None)
    channel_name = str(getattr(channel, "_delivery_channel_name", "") or "")
    if isinstance(store, ChannelDeliveryStore) and channel_name:
        if not store.accept_inbound(channel_name, message):
            return False
    queue.put_nowait(message)
    return True


async def deliver_with_outbox(channel: Any, message: OutgoingMessage) -> Any:
    """Persist a send intent and explicit terminal/unknown receipt."""
    store = getattr(channel, "_delivery_store", None)
    channel_name = str(getattr(channel, "_delivery_channel_name", "") or "")
    raw_send = getattr(channel, "_delivery_raw_send", None)
    if not callable(raw_send):
        raw_send = channel.send
    if not isinstance(store, ChannelDeliveryStore) or not channel_name:
        return await raw_send(message)
    delivery_id = str(message.metadata.get("delivery_id") or uuid.uuid4().hex)
    metadata = dict(message.metadata or {})
    metadata["delivery_id"] = delivery_id
    durable_message = message.model_copy(update={"metadata": metadata})
    send_id = store.begin_send(channel_name, durable_message)
    try:
        result = await raw_send(durable_message)
    except BaseException as exc:
        store.fail_send(send_id, exc)
        raise
    store.complete_send(
        send_id,
        result,
        target_id=str(durable_message.reply_to or ""),
    )
    return result


def _operation_message(
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> OutgoingMessage:
    """Build a secret-free durable intent for a non-``send`` mutation."""
    target = ""
    for key in ("target_id", "channel_id", "chat_id", "room_id"):
        value = kwargs.get(key)
        if value is not None and str(value).strip():
            target = str(value).strip()
            break
    if not target and operation != "send_streaming" and args:
        target = str(args[0])

    content = ""
    if operation == "edit":
        value = kwargs.get("content", args[1] if len(args) > 1 else "")
        content = str(value or "")
    elif operation == "set_reaction":
        value = kwargs.get("emoji", args[2] if len(args) > 2 else "")
        content = str(value or "")
    elif operation == "send_file":
        value = kwargs.get("content", args[2] if len(args) > 2 else "")
        content = str(value or "")

    return OutgoingMessage(
        content=content,
        reply_to=target or None,
        metadata={
            "delivery_id": uuid.uuid4().hex,
            "outbox_operation": operation,
        },
    )


async def deliver_operation_with_outbox(
    channel: Any,
    operation: str,
    raw_operation: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Persist one declared mutating operation and its terminal outcome."""
    store = getattr(channel, "_delivery_store", None)
    channel_name = str(getattr(channel, "_delivery_channel_name", "") or "")
    if not isinstance(store, ChannelDeliveryStore) or not channel_name:
        return await raw_operation(*args, **kwargs)

    intent = _operation_message(operation, args, kwargs)
    send_id = store.begin_send(channel_name, intent, capability=operation)
    try:
        result = await raw_operation(*args, **kwargs)
    except BaseException as exc:
        store.fail_send(send_id, exc)
        raise
    store.complete_send(
        send_id,
        result,
        capability=operation,
        target_id=str(intent.reply_to or ""),
    )
    return result


def install_outbox(channel: Any) -> None:
    """Wrap one adapter's declared public mutation surfaces exactly once."""
    if callable(getattr(channel, "_delivery_raw_send", None)):
        return
    raw_send = getattr(channel, "send", None)
    if not callable(raw_send):
        return
    setattr(channel, "_delivery_raw_send", raw_send)

    async def _durable_send(message: OutgoingMessage) -> Any:
        return await deliver_with_outbox(channel, message)

    setattr(channel, "send", _durable_send)

    profile = channel_capability_profile(channel)
    supported_operations = {
        "send_file": bool(
            profile
            and (
                profile.native_file_upload
                or profile.media
                or profile.artifact_delivery
            )
        ),
        "edit": bool(profile and profile.edit),
        "delete": bool(profile and profile.delete),
        "set_reaction": bool(profile and profile.reactions),
        # ``send_streaming`` is an active runtime surface even for adapters
        # which implement it by accumulating chunks into one final send.
        "send_streaming": True,
    }
    for operation, supported in supported_operations.items():
        raw_operation = getattr(channel, operation, None)
        if not supported or not callable(raw_operation):
            continue
        raw_name = f"_delivery_raw_{operation}"
        if callable(getattr(channel, raw_name, None)):
            continue
        setattr(channel, raw_name, raw_operation)

        def _wrapper(raw: Any, name: str) -> Any:
            async def _durable_operation(*args: Any, **kwargs: Any) -> Any:
                return await deliver_operation_with_outbox(
                    channel,
                    name,
                    raw,
                    args,
                    kwargs,
                )

            return _durable_operation

        setattr(channel, operation, _wrapper(raw_operation, operation))


__all__ = [
    "ChannelDeliveryStore",
    "ChannelPairing",
    "IngressClaim",
    "TransportLease",
    "deliver_operation_with_outbox",
    "deliver_with_outbox",
    "delivery_store_for_config",
    "durable_enqueue",
    "install_outbox",
    "inbound_event_key",
]
