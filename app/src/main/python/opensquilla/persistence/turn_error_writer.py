"""Durable per-turn error records over the yoyo-owned ``turn_errors`` table.

Same shape as ``router_decision_writer``: a long-lived synchronous sqlite3
connection guarded by a ``threading.Lock``, best-effort fail-open methods (a
persistence failure must never fail or mask the turn error being recorded),
write-time retention pruning, and an ``open_*`` factory applying the shared
persistence PRAGMA contract.

``message``/``traceback`` are free text by design (V019 docstring records the
deliberate divergence from V017's no-free-text bar); ``traceback`` passes
``observability.redact.scrub_text`` before insert so secret-shaped values and
home paths never persist.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from opensquilla.observability.redact import scrub_text

log = structlog.get_logger(__name__)

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_PRUNE_EVERY = 64
_DEFAULT_PRUNE_BATCH = 1_000
_DAY_MS = 24 * 60 * 60 * 1000
_MAX_MESSAGE_CHARS = 4_000
_MAX_TRACEBACK_CHARS = 20_000

_COLUMNS = (
    "error_id",
    "turn_id",
    "session_key",
    "session_id",
    "ts_ms",
    "surface",
    "error_class",
    "message",
    "traceback",
    "provider",
    "model",
    "fallback_hops",
)
_TEXT_COLUMNS = (
    "turn_id",
    "session_id",
    "surface",
    "error_class",
    "provider",
    "model",
)


def new_error_id() -> str:
    """Short user-visible reference id.

    Lowercase hex only: the terminal-reply classifiers substring-match on
    words like ``timeout``, which a hex id can never contain.
    """
    return uuid.uuid4().hex[:8]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TurnErrorWriter:
    """Long-lived sync writer over a single sqlite3 connection.

    Caller responsibilities:
    * Construct via :func:`open_turn_error_writer` (sets PRAGMAs).
    * Call ``close()`` at shutdown.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        prune_every: int = _DEFAULT_PRUNE_EVERY,
        prune_batch: int = _DEFAULT_PRUNE_BATCH,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        self._retention_days = max(1, int(retention_days))
        self._prune_every = max(1, int(prune_every))
        self._prune_batch = max(1, int(prune_batch))
        self._clock = clock
        self._insert_count = 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("turn_error_writer.close_failed", error=str(exc))

    def record_error(self, record: Mapping[str, Any]) -> bool:
        """Insert one error row. Best-effort — returns False on failure."""
        try:
            row = self._normalize_record(record)
        except Exception as exc:  # noqa: BLE001
            log.warning("turn_error_writer.normalize_failed", error=str(exc))
            return False
        if row is None:
            return False
        placeholders = ", ".join("?" for _ in _COLUMNS)
        sql = (
            f"INSERT OR REPLACE INTO turn_errors ({', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._lock:
                self._conn.execute(sql, tuple(row[column] for column in _COLUMNS))
                self._conn.commit()
                self._insert_count += 1
                should_prune = self._insert_count % self._prune_every == 0
            if should_prune:
                self._prune()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("turn_error_writer.record_failed", error=str(exc))
            return False

    def _normalize_record(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        error_id = record.get("error_id")
        session_key = record.get("session_key")
        if not isinstance(error_id, str) or not error_id:
            return None
        if not isinstance(session_key, str) or not session_key:
            return None
        ts_ms = _optional_int(record.get("ts_ms"))
        if ts_ms is None:
            ts_ms = self._clock()
        message = record.get("message")
        traceback_text = record.get("traceback")
        row: dict[str, Any] = {
            "error_id": error_id,
            "session_key": session_key,
            "ts_ms": ts_ms,
            "message": (
                scrub_text(str(message))[:_MAX_MESSAGE_CHARS] if message else None
            ),
            "traceback": (
                scrub_text(str(traceback_text))[-_MAX_TRACEBACK_CHARS:]
                if traceback_text
                else None
            ),
            "fallback_hops": _optional_int(record.get("fallback_hops")) or 0,
        }
        for column in _TEXT_COLUMNS:
            value = record.get(column)
            row[column] = str(value) if value else None
        return row

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention_days * _DAY_MS
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    DELETE FROM turn_errors
                    WHERE rowid IN (
                        SELECT rowid
                        FROM turn_errors
                        WHERE ts_ms < ?
                        ORDER BY ts_ms, error_id
                        LIMIT ?
                    )
                    """,
                    (cutoff, self._prune_batch),
                )
                self._conn.commit()
            if cur.rowcount:
                log.info(
                    "turn_error_writer.pruned",
                    rows=cur.rowcount,
                    retention_days=self._retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("turn_error_writer.prune_failed", error=str(exc))

    def purge_for_session(self, session_key: str) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM turn_errors WHERE session_key=?",
                    (session_key,),
                )
                self._conn.commit()
                return cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            log.warning("turn_error_writer.purge_failed", error=str(exc))
            return 0

    def list_errors(
        self,
        *,
        session_key: str | None = None,
        limit: int = 50,
        before_ts_ms: int | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return error rows, newest first. Best-effort — [] on failure."""
        bound = max(1, min(int(limit), 1000))
        clauses: list[str] = []
        args: list[Any] = []
        if session_key:
            clauses.append("session_key = ?")
            args.append(session_key)
        if before_ts_ms is not None:
            clauses.append("ts_ms < ?")
            args.append(int(before_ts_ms))
        if days is not None:
            clauses.append("ts_ms >= ?")
            args.append(self._clock() - max(1, int(days)) * _DAY_MS)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM turn_errors"
            f"{where} ORDER BY ts_ms DESC, error_id DESC LIMIT ?"
        )
        args.append(bound)
        try:
            with self._lock:
                rows = self._conn.execute(sql, tuple(args)).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("turn_error_writer.list_failed", error=str(exc))
            return []
        return [{column: row[column] for column in _COLUMNS} for row in rows]


def open_turn_error_writer(
    db_path: str,
    *,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> TurnErrorWriter:
    """Open writer with the shared persistence PRAGMA contract."""
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit; we still call .commit() explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return TurnErrorWriter(conn, retention_days=retention_days)
