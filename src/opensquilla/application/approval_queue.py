"""Approval queue with single-process persistent state."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast

from opensquilla.paths import state_dir

VALID_APPROVAL_MODES = frozenset({"auto-approve", "auto-deny", "prompt"})
VALID_ELEVATED_MODES = frozenset({"on", "bypass", "full"})
VALID_RUN_MODES = frozenset({"standard", "trusted", "full"})


@dataclass
class ApprovalSettings:
    mode: str = "prompt"
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


def _command_matches(command: str, pattern: str) -> bool:
    """Match shell-style globs or plain substrings, case-sensitively."""
    pattern = pattern.strip()
    if not pattern:
        return False
    return fnmatchcase(command, pattern) or pattern in command


def classify_command(
    command: str,
    allow_patterns: list[str],
    deny_patterns: list[str],
) -> str | None:
    """Classify a command against allow/deny patterns; deny wins."""
    if not command:
        return None
    for pattern in deny_patterns:
        if _command_matches(command, pattern):
            return "deny"
    for pattern in allow_patterns:
        if _command_matches(command, pattern):
            return "allow"
    return None


RESOLUTION_APPROVED = "approved"
RESOLUTION_DENIED = "denied"
RESOLUTION_EXPIRED = "expired"


@dataclass
class PendingApproval:
    approval_id: str
    namespace: str  # "exec" or "plugin"
    params: dict
    created_at: float = field(default_factory=time.time)
    resolved: bool = False
    approved: bool = False
    consumed: bool = False
    deadline: float = 0.0
    resolution: str = ""
    claim_token: str | None = None
    claim_started_at: float | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event)


_DEFAULT_APPROVAL_QUEUE_PATH = state_dir("approval_queue.sqlite")

# Listener signature: (event, info) where event is "requested" or "resolved"
# and info mirrors ``ApprovalQueue.status()`` for the affected approval.
ApprovalEventListener = Callable[[str, dict], None]


class ApprovalQueue:
    def __init__(
        self,
        default_timeout: float = 300.0,
        *,
        db_path: str | None = None,
        poll_interval: float = 0.25,
        claim_ttl_seconds: float = 60.0,
    ):
        self._pending: dict[str, PendingApproval] = {}
        self._timeout = default_timeout
        self._poll_interval = max(0.01, float(poll_interval))
        self._claim_ttl_seconds = max(0.0, float(claim_ttl_seconds))
        self._global_settings = ApprovalSettings()
        self._node_settings: dict[str, ApprovalSettings] = {}
        self._session_run_modes: dict[str, str] = {}
        self._event_listeners: list[ApprovalEventListener] = []

        self._db_path = Path(db_path or os.fspath(_DEFAULT_APPROVAL_QUEUE_PATH))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()
        self._load_pending()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approval_queue (
                approval_id   TEXT PRIMARY KEY,
                namespace     TEXT NOT NULL,
                params        TEXT NOT NULL,
                created_at    REAL NOT NULL,
                resolved      INTEGER NOT NULL DEFAULT 0,
                approved      INTEGER NOT NULL DEFAULT 0,
                consumed      INTEGER NOT NULL DEFAULT 0,
                deadline      REAL NOT NULL DEFAULT 0,
                resolution    TEXT NOT NULL DEFAULT '',
                claim_token   TEXT,
                claim_started_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_approval_namespace_status
            ON approval_queue(namespace, resolved);

            CREATE TABLE IF NOT EXISTS channel_approval_codes (
                code                TEXT PRIMARY KEY,
                approval_id         TEXT NOT NULL UNIQUE,
                namespace           TEXT NOT NULL,
                session_key         TEXT NOT NULL,
                owner_sender_id     TEXT NOT NULL,
                origin_channel_name TEXT NOT NULL DEFAULT '',
                origin_channel_id   TEXT NOT NULL DEFAULT '',
                origin_thread_id    TEXT NOT NULL DEFAULT '',
                approver_policy     TEXT NOT NULL DEFAULT 'requester_only',
                created_at          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_channel_approval_code_approval
            ON channel_approval_codes(approval_id);
            """
        )
        existing = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(approval_queue)")
        }
        if "deadline" not in existing:
            self._conn.execute(
                "ALTER TABLE approval_queue ADD COLUMN deadline REAL NOT NULL DEFAULT 0"
            )
        if "resolution" not in existing:
            self._conn.execute(
                "ALTER TABLE approval_queue ADD COLUMN resolution TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute(
                "UPDATE approval_queue SET resolution = ? "
                "WHERE resolved = 1 AND resolution = '' AND approved = 1",
                (RESOLUTION_APPROVED,),
            )
            self._conn.execute(
                "UPDATE approval_queue SET resolution = ? "
                "WHERE resolved = 1 AND resolution = '' AND approved = 0",
                (RESOLUTION_DENIED,),
            )
        if "claim_token" not in existing:
            self._conn.execute("ALTER TABLE approval_queue ADD COLUMN claim_token TEXT")
        if "claim_started_at" not in existing:
            self._conn.execute("ALTER TABLE approval_queue ADD COLUMN claim_started_at REAL")
        self._conn.commit()

    def channel_code_for_approval(self, approval_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT code FROM channel_approval_codes WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return str(row["code"]) if row is not None else None

    def bind_channel_code(
        self,
        code: str,
        *,
        approval_id: str,
        namespace: str,
        session_key: str,
        owner_sender_id: str,
        origin_channel_name: str = "",
        origin_channel_id: str = "",
        origin_thread_id: str = "",
        approver_policy: str = "requester_only",
    ) -> bool:
        """Persist one short-code binding; return False on a code collision."""
        existing = self.channel_code_for_approval(approval_id)
        if existing is not None:
            return existing == code
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO channel_approval_codes "
                "(code, approval_id, namespace, session_key, owner_sender_id, "
                "origin_channel_name, origin_channel_id, origin_thread_id, "
                "approver_policy, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    approval_id,
                    namespace,
                    session_key,
                    owner_sender_id,
                    origin_channel_name,
                    origin_channel_id,
                    origin_thread_id,
                    approver_policy,
                    time.time(),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False
        return True

    def resolve_channel_code(self, code: str) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT code, approval_id, namespace, session_key, owner_sender_id, "
            "origin_channel_name, origin_channel_id, origin_thread_id, "
            "approver_policy FROM channel_approval_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return {key: str(row[key] or "") for key in row.keys()}

    def release_channel_code(self, approval_id: str) -> None:
        self._conn.execute(
            "DELETE FROM channel_approval_codes WHERE approval_id = ?",
            (approval_id,),
        )
        self._conn.commit()

    def clear_channel_codes(self) -> None:
        self._conn.execute("DELETE FROM channel_approval_codes")
        self._conn.commit()

    def _release_stale_claims(self) -> None:
        threshold = time.time() - self._claim_ttl_seconds
        self._conn.execute("BEGIN IMMEDIATE")
        self._conn.execute(
            "UPDATE approval_queue "
            "SET claim_token = NULL, claim_started_at = NULL "
            "WHERE resolved = 0 "
            "AND claim_token IS NOT NULL "
            "AND (claim_started_at IS NULL OR claim_started_at <= ?)",
            (threshold,),
        )
        self._conn.commit()

    def _serialize_params(self, params: dict | None) -> str:
        return json.dumps(params or {}, ensure_ascii=False, sort_keys=True)

    def _deserialize_params(self, raw: str | bytes | bytearray) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _row_to_entry(self, row: sqlite3.Row) -> PendingApproval:
        aid = str(row["approval_id"])
        existing = self._pending.get(aid)
        return PendingApproval(
            approval_id=aid,
            namespace=str(row["namespace"]),
            params=self._deserialize_params(row["params"]),
            created_at=float(row["created_at"]),
            resolved=bool(row["resolved"]),
            approved=bool(row["approved"]),
            consumed=bool(row["consumed"]),
            deadline=float(row["deadline"] or 0.0),
            resolution=str(row["resolution"] or ""),
            claim_token=str(row["claim_token"] or "") or None,
            claim_started_at=(
                float(row["claim_started_at"]) if row["claim_started_at"] is not None else None
            ),
            _event=existing._event if existing is not None else asyncio.Event(),
        )

    def _load_pending(self) -> None:
        self._pending = {}
        self._release_stale_claims()
        for row in self._conn.execute(
            "SELECT approval_id, namespace, params, created_at, resolved, approved, "
            "consumed, deadline, resolution, claim_token, claim_started_at "
            "FROM approval_queue WHERE resolved = 0"
        ):
            entry = self._row_to_entry(row)
            self._pending[entry.approval_id] = entry

    def _get_row(self, approval_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._conn.execute(
                "SELECT approval_id, namespace, params, created_at, resolved, approved, "
                "consumed, deadline, resolution, claim_token, claim_started_at "
                "FROM approval_queue WHERE approval_id = ?",
                (approval_id,),
            ).fetchone(),
        )

    def add_event_listener(self, listener: ApprovalEventListener) -> Callable[[], None]:
        """Register a lifecycle listener; returns a remove callable.

        Listeners fire synchronously on ``requested`` (an approval was
        created — the moment a run blocks) and ``resolved`` (a decision
        landed, including deny-on-timeout). Listener errors are swallowed:
        notification is best-effort and must never break queue state.
        """
        self._event_listeners.append(listener)

        def _remove() -> None:
            try:
                self._event_listeners.remove(listener)
            except ValueError:
                pass

        return _remove

    def _notify_event(self, event: str, entry: PendingApproval) -> None:
        if not self._event_listeners:
            return
        info = {
            "id": entry.approval_id,
            "namespace": entry.namespace,
            "params": dict(entry.params),
            "created_at": entry.created_at,
            "deadline": entry.deadline,
            "resolved": entry.resolved,
            "approved": entry.approved,
            "resolution": entry.resolution,
        }
        for listener in list(self._event_listeners):
            try:
                listener(event, info)
            except Exception:  # pragma: no cover — listeners are best-effort
                continue

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        payload = self._serialize_params(params or {})
        while True:
            approval_id = uuid.uuid4().hex[:12]
            now = time.time()
            deadline = now + self._timeout
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO approval_queue "
                    "(approval_id, namespace, params, created_at, resolved, approved, "
                    "consumed, deadline, resolution, claim_token, claim_started_at) "
                    "VALUES (?, ?, ?, ?, 0, 0, 0, ?, '', NULL, NULL)",
                    (approval_id, namespace, payload, now, deadline),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                continue
            break

        entry = PendingApproval(
            approval_id=approval_id,
            namespace=namespace,
            params=params or {},
            created_at=now,
            deadline=deadline,
        )
        self._pending[approval_id] = entry
        self._notify_event("requested", entry)
        return approval_id

    def get(self, approval_id: str) -> PendingApproval:
        self._release_stale_claims()
        row = self._get_row(approval_id)
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        self._pending[approval_id] = entry
        return entry

    def update_params(self, approval_id: str, params: dict) -> None:
        """Replace review metadata while an approval is still unresolved."""

        payload = self._serialize_params(params)
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved or entry.claim_token:
            self._conn.rollback()
            raise ValueError(f"Approval is not pending: {approval_id}")
        became_human_actionable = (
            entry.params.get("humanActionable") is False
            and params.get("humanActionable") is True
        )
        cursor = self._conn.execute(
            "UPDATE approval_queue SET params = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
            (payload, approval_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise ValueError(f"Approval params could not be updated: {approval_id}")
        self._conn.commit()
        updated_entry = self.get(approval_id)
        self._pending[approval_id] = updated_entry
        if became_human_actionable:
            self._notify_event("requested", updated_entry)

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        entry = self.get(approval_id)
        if entry.resolved and entry.claim_token is None:
            return entry.approved
        if timeout is not None:
            self._rearm_deadline(approval_id, time.time() + timeout)
        while True:
            entry = self.get(approval_id)
            if entry.resolved and entry.claim_token is None:
                return entry.approved
            remaining = entry.deadline - time.time()
            if remaining <= 0:
                outcome = self._expire_if_unresolved(approval_id)
                if outcome is not None:
                    return outcome
                continue
            try:
                await asyncio.wait_for(
                    entry._event.wait(),
                    timeout=min(self._poll_interval, remaining),
                )
            except TimeoutError:
                pass
            entry = self.get(approval_id)
            if entry.resolved and entry.claim_token is None:
                return entry.approved

    def _rearm_deadline(self, approval_id: str, deadline: float) -> None:
        """Set a pending request's wall-clock deadline; no-op once resolved."""
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE approval_queue SET deadline = ? "
                "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
                (deadline, approval_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        cached = self._pending.get(approval_id)
        if cached is not None and not cached.resolved and cached.claim_token is None:
            cached.deadline = deadline

    def extend(self, approval_id: str, seconds: float) -> float:
        """Push a pending request's deadline out by ``seconds`` and return it."""
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution in progress: {approval_id}")
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            return entry.deadline
        new_deadline = float(entry.deadline or time.time()) + float(seconds)
        self._conn.execute(
            "UPDATE approval_queue SET deadline = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
            (new_deadline, approval_id),
        )
        self._conn.commit()
        entry = self.get(approval_id)
        return entry.deadline

    def _expire_if_unresolved(self, approval_id: str) -> bool | None:
        """Resolve a lapsed request as expired, unless it was extended."""
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution in progress: {approval_id}")
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            return entry.approved
        if entry.deadline > time.time():
            self._conn.rollback()
            self._pending[approval_id] = entry
            return None
        self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 1, approved = 0, resolution = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
            (RESOLUTION_EXPIRED, approval_id),
        )
        self._conn.commit()
        entry = self.get(approval_id)
        entry._event.set()
        self._pending[approval_id] = entry
        self._notify_event("resolved", entry)
        return entry.approved

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        *,
        elevated_mode: str | None = None,
        allow_idempotent: bool = True,
    ) -> None:
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution in progress: {approval_id}")
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            if allow_idempotent and entry.approved == approved:
                return
            raise ValueError(f"Approval already resolved: {approval_id}")

        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 1, approved = ?, resolution = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
            (
                1 if approved else 0,
                RESOLUTION_APPROVED if approved else RESOLUTION_DENIED,
                approval_id,
            ),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            entry = self.get(approval_id)
            if entry.resolved:
                entry._event.set()
                if allow_idempotent and entry.approved == approved:
                    return
                raise ValueError(f"Approval already resolved: {approval_id}")
            if entry.claim_token:
                raise ValueError(f"Approval resolution in progress: {approval_id}")
            raise ValueError(f"Approval could not be resolved: {approval_id}")
        self._conn.commit()

        entry = self.get(approval_id)
        entry.approved = bool(approved)
        entry.resolved = True
        entry._event.set()
        self._pending[approval_id] = entry
        self._notify_event("resolved", entry)

        del elevated_mode

    def claim_resolution(self, approval_id: str) -> str:
        self._release_stale_claims()
        token = uuid.uuid4().hex
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution in progress: {approval_id}")
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            raise ValueError(f"Approval already resolved: {approval_id}")
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET claim_token = ?, claim_started_at = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token IS NULL",
            (token, now, approval_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            entry = self.get(approval_id)
            if entry.resolved:
                raise ValueError(f"Approval already resolved: {approval_id}")
            if entry.claim_token:
                raise ValueError(f"Approval resolution in progress: {approval_id}")
            raise ValueError(f"Approval could not be claimed: {approval_id}")
        self._conn.commit()
        self._pending[approval_id] = self.get(approval_id)
        return token

    def finalize_claimed_resolution(
        self,
        approval_id: str,
        claim_token: str,
        approved: bool,
        *,
        elevated_mode: str | None = None,
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            raise ValueError(f"Approval already resolved: {approval_id}")
        if entry.claim_token != claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution claim mismatch: {approval_id}")
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 1, approved = ?, resolution = ? "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token = ?",
            (
                1 if approved else 0,
                RESOLUTION_APPROVED if approved else RESOLUTION_DENIED,
                approval_id,
                claim_token,
            ),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise ValueError(f"Approval could not be finalized: {approval_id}")
        self._conn.commit()

        row = self._get_row(approval_id)
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        entry.approved = bool(approved)
        entry.resolved = True
        self._pending[approval_id] = entry

        del elevated_mode

    def complete_claimed_resolution(
        self,
        approval_id: str,
        claim_token: str,
        *,
        elevated_mode: str | None = None,
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved and entry.approved and entry.claim_token is None:
            self._conn.rollback()
            entry._event.set()
            self._pending[approval_id] = entry
            return
        if not entry.resolved or not entry.approved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval is not approved: {approval_id}")
        if entry.claim_token != claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution claim mismatch: {approval_id}")
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET claim_token = NULL, claim_started_at = NULL "
            "WHERE approval_id = ? AND resolved = 1 AND approved = 1 "
            "AND claim_token = ?",
            (approval_id, claim_token),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise ValueError(f"Approval could not be completed: {approval_id}")
        self._conn.commit()

        entry = self.get(approval_id)
        entry._event.set()
        self._pending[approval_id] = entry
        self._notify_event("resolved", entry)

        del elevated_mode

    def release_resolution_claim(self, approval_id: str, claim_token: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            return
        if entry.claim_token != claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            return
        self._conn.execute(
            "UPDATE approval_queue "
            "SET claim_token = NULL, claim_started_at = NULL "
            "WHERE approval_id = ? AND resolved = 0 AND claim_token = ?",
            (approval_id, claim_token),
        )
        self._conn.commit()
        self._pending[approval_id] = self.get(approval_id)

    def reopen_resolved_approval(
        self,
        approval_id: str,
        *,
        expected_approved: bool = True,
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if not entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            return
        if entry.approved != expected_approved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolved state mismatch: {approval_id}")
        self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 0, approved = 0, consumed = 0, "
            "resolution = '', claim_token = NULL, claim_started_at = NULL "
            "WHERE approval_id = ? AND resolved = 1 AND approved = ?",
            (approval_id, 1 if expected_approved else 0),
        )
        self._conn.commit()
        reopened = self.get(approval_id)
        reopened._event.clear()
        self._pending[approval_id] = reopened

    def consume(self, approval_id: str) -> None:
        self._release_stale_claims()
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.claim_token:
            self._conn.rollback()
            self._pending[approval_id] = entry
            raise ValueError(f"Approval resolution in progress: {approval_id}")
        if not entry.resolved or not entry.approved:
            self._conn.rollback()
            raise ValueError(f"Approval is not approved: {approval_id}")
        if entry.consumed:
            self._conn.rollback()
            raise ValueError(f"Approval already consumed: {approval_id}")
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET consumed = 1 "
            "WHERE approval_id = ? AND resolved = 1 AND approved = 1 "
            "AND consumed = 0 AND claim_token IS NULL",
            (approval_id,),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            entry = self.get(approval_id)
            if entry.consumed:
                raise ValueError(f"Approval already consumed: {approval_id}")
            raise ValueError(f"Approval is not approved: {approval_id}")
        self._conn.commit()
        entry = self.get(approval_id)
        self._pending.pop(approval_id, None)

    def status(self, approval_id: str) -> dict:
        entry = self.get(approval_id)
        ready = entry.resolved and entry.claim_token is None
        return {
            "id": entry.approval_id,
            "namespace": entry.namespace,
            "params": entry.params,
            "created_at": entry.created_at,
            "deadline": entry.deadline,
            "resolved": ready,
            "approved": entry.approved if ready else False,
            "resolution": entry.resolution if ready else "",
            "consumed": entry.consumed if ready else False,
        }

    def list_pending(self, namespace: str | None = None) -> list[dict]:
        self._release_stale_claims()
        if namespace:
            rows = self._conn.execute(
                "SELECT approval_id, namespace, params, created_at "
                ", deadline, resolution "
                "FROM approval_queue "
                "WHERE resolved = 0 AND claim_token IS NULL AND namespace = ?",
                (namespace,),
            )
        else:
            rows = self._conn.execute(
                "SELECT approval_id, namespace, params, created_at "
                ", deadline, resolution "
                "FROM approval_queue "
                "WHERE resolved = 0 AND claim_token IS NULL",
            )
        return [
            {
                "id": str(row["approval_id"]),
                "namespace": str(row["namespace"]),
                "params": self._deserialize_params(row["params"]),
                "created_at": float(row["created_at"]),
                "deadline": float(row["deadline"] or 0.0),
                "resolution": str(row["resolution"] or ""),
            }
            for row in rows
        ]

    def set_elevated_mode(self, session_key: str, mode: str | None) -> None:
        """Legacy compatibility wrapper for session run mode."""
        key = session_key.strip()
        if not key:
            raise ValueError("session_key is required")
        if mode in (None, "", "off"):
            self._session_run_modes.pop(key, None)
            return
        if mode not in VALID_ELEVATED_MODES:
            raise ValueError("mode must be one of: on, bypass, full, off")
        self.set_run_mode(key, "full" if mode == "full" else "trusted")

    def get_elevated_mode(self, session_key: str | None) -> str | None:
        """Legacy compatibility wrapper returning only full host access."""
        mode = self.get_run_mode(session_key)
        return "full" if mode == "full" else None

    def set_run_mode(self, session_key: str, mode: str | None) -> None:
        key = session_key.strip()
        if not key:
            raise ValueError("session_key is required")
        if mode in (None, "", "off"):
            self._session_run_modes.pop(key, None)
            return
        if mode not in VALID_RUN_MODES:
            raise ValueError("mode must be one of: full, standard, trusted, off")
        self._session_run_modes[key] = mode

    def get_run_mode(self, session_key: str | None) -> str | None:
        key = (session_key or "").strip()
        if not key:
            return None
        return self._session_run_modes.get(key)

    def resolve_pending_for_session(
        self,
        session_key: str,
        *,
        approved: bool,
        elevated_mode: str | None = None,
    ) -> int:
        key = session_key.strip()
        if not key:
            return 0
        self._release_stale_claims()
        count = 0
        for row in self._conn.execute(
            "SELECT approval_id, namespace, params, created_at, resolved, approved, "
            "consumed, deadline, resolution, claim_token, claim_started_at "
            "FROM approval_queue "
            "WHERE resolved = 0 AND claim_token IS NULL AND namespace = 'exec'",
        ).fetchall():
            entry = self._row_to_entry(row)
            if str(entry.params.get("sessionKey") or "").strip() != key:
                continue
            self.resolve(
                entry.approval_id,
                approved,
                elevated_mode=elevated_mode,
            )
            count += 1
        return count

    def get_settings(self, node_id: str | None = None) -> ApprovalSettings:
        settings = self._node_settings.get(node_id) if node_id else self._global_settings
        if settings is None:
            settings = self._global_settings
        return ApprovalSettings(
            mode=settings.mode,
            allow_patterns=list(settings.allow_patterns),
            deny_patterns=list(settings.deny_patterns),
        )

    def has_node_settings(self, node_id: str) -> bool:
        return node_id in self._node_settings

    def set_settings(
        self,
        mode: str,
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
        node_id: str | None = None,
    ) -> ApprovalSettings:
        if mode not in VALID_APPROVAL_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_APPROVAL_MODES))}")
        settings = ApprovalSettings(
            mode=mode,
            allow_patterns=list(allow_patterns or []),
            deny_patterns=list(deny_patterns or []),
        )
        if node_id is None:
            self._global_settings = settings
        else:
            self._node_settings[node_id] = settings
        return settings

    def close(self) -> None:
        self._conn.close()


_queue: ApprovalQueue | None = None


def get_approval_queue() -> ApprovalQueue:
    global _queue
    if _queue is None:
        _queue = ApprovalQueue()
    return _queue


def reset_approval_queue() -> None:
    global _queue
    if _queue is not None:
        path = _queue._db_path
        _queue.close()
        _queue = None
    else:
        path = _DEFAULT_APPROVAL_QUEUE_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
