"""RouterDecisionWriter — persistence facade for per-turn router decisions.

One row per routed user message into the yoyo-owned ``router_decisions``
table (V017). Mirrors the ``MetaRunWriter`` contract:

Connection contract:
    * ``check_same_thread=False`` — allows cross-thread access.
    * ``threading.Lock`` around every SQL call — serializes at Python level.
    * PRAGMAs set once at construction: ``foreign_keys=ON``,
      ``journal_mode=WAL``, ``synchronous=NORMAL``, ``busy_timeout=5000``.

Fail-open: persistence is observability; all writes are try/except →
log.warning so a writer failure can never fail a turn.

Privacy contract (test-enforced): no free text is ever stored. ``probs``
holds numbers only; ``flags`` holds enum tokens only; ``trail`` holds stage
entries whose string values are enum tokens and whose other values are
booleans/numbers. :func:`sanitize_flags` / :func:`sanitize_trail` /
:func:`sanitize_probs` are applied on every insert.

Retention: every ``prune_every`` (default 64) inserts the writer runs one
opportunistic, bounded delete of at most ``prune_batch`` (default 1000) rows
older than ``retention_days`` so the table stays bounded without a background
job or a long foreground write lock.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_PRUNE_EVERY = 64
_DEFAULT_PRUNE_BATCH = 1_000
_DAY_MS = 24 * 60 * 60 * 1000

# Enum-like token: identifiers such as tier names ("c2"), route classes
# ("R1"), stage names, model/provider ids ("openrouter", "deepseek/v4"),
# version tags. No whitespace, bounded length — free text cannot match.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@\-]{0,127}$")

_MAX_FLAGS = 32
_MAX_TRAIL_ENTRIES = 16
_MAX_TRAIL_KEYS = 12
_MAX_PROBS = 16

_COLUMNS = (
    "decision_id",
    "session_key",
    "turn_index",
    "ts_ms",
    "classifier",
    "proposed_tier",
    "confidence",
    "probs",
    "flags",
    "final_tier",
    "requested_provider",
    "requested_model",
    "provider",
    "model",
    "executed_provider",
    "executed_model",
    "fallback_reason",
    "thinking_level",
    "source",
    "trail",
    "baseline_model",
    "savings_pct",
    "executed_kind",
    "ensemble_profile",
    "fallback_hops",
)

_TEXT_TOKEN_COLUMNS = (
    "classifier",
    "proposed_tier",
    "final_tier",
    "requested_provider",
    "requested_model",
    "provider",
    "model",
    "executed_provider",
    "executed_model",
    "fallback_reason",
    "thinking_level",
    "source",
    "baseline_model",
    "ensemble_profile",
)

_EXECUTED_KINDS = frozenset({"single", "ensemble"})


def sanitize_token(value: object) -> str | None:
    """Return ``value`` when it is an enum-like token, else ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or not _TOKEN_RE.match(candidate):
        return None
    return candidate


def sanitize_flags(raw: object) -> list[str]:
    """Keep only enum-like token strings; drop anything that could be text."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        token = sanitize_token(item)
        if token is not None:
            out.append(token)
        if len(out) >= _MAX_FLAGS:
            break
    return out


def sanitize_probs(raw: object) -> list[float]:
    """Keep only finite numbers."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        value = float(item)
        if value != value or value in (float("inf"), float("-inf")):
            continue
        out.append(value)
        if len(out) >= _MAX_PROBS:
            break
    return out


def _sanitize_trail_value(value: object) -> object | None:
    """Trail values: booleans, finite numbers, or enum tokens. Nothing else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        as_float = float(value)
        if as_float != as_float or as_float in (float("inf"), float("-inf")):
            return None
        return value
    token = sanitize_token(value)
    if token is not None:
        return token
    return None


def sanitize_trail(raw: object) -> list[dict[str, Any]]:
    """Keep only trail entries made of enum tokens, booleans, and numbers."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        cleaned: dict[str, Any] = {}
        for key, value in entry.items():
            key_token = sanitize_token(key)
            if key_token is None:
                continue
            cleaned_value = _sanitize_trail_value(value)
            if cleaned_value is None:
                continue
            cleaned[key_token] = cleaned_value
            if len(cleaned) >= _MAX_TRAIL_KEYS:
                break
        if cleaned:
            out.append(cleaned)
        if len(out) >= _MAX_TRAIL_ENTRIES:
            break
    return out


def _load_json_list(raw: object) -> list[Any]:
    """Parse a stored JSON-array column; degrade to ``[]`` on any mismatch."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return None
    return as_float


def _optional_int(value: object) -> int | None:
    number = _optional_number(value)
    return None if number is None else int(number)


class RouterDecisionWriter:
    """Long-lived sync writer over a single sqlite3 connection.

    Caller responsibilities:
    * Construct via :func:`open_router_decision_writer` (sets PRAGMAs).
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
        self._schema_columns: tuple[str, ...] | None = None

    def _compatible_columns_unlocked(self) -> tuple[str, ...]:
        """Return columns present in both the runtime and persisted schema.

        Some standalone CLI paths open a pre-V021 decisions database without
        running gateway migrations first.  The additive deployment telemetry
        must not make those older databases unreadable or unwritable; missing
        fields simply remain ``None`` until normal migration runs.
        """
        if self._schema_columns is not None:
            return self._schema_columns
        rows = self._conn.execute("PRAGMA table_info(router_decisions)").fetchall()
        present = {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}
        columns = tuple(column for column in _COLUMNS if column in present)
        if columns:
            self._schema_columns = columns
        return columns

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("router_decision_writer.close_failed: %s", exc)

    # ------------- write path -------------

    def record_decision(self, record: Mapping[str, Any]) -> bool:
        """Insert one decision row. Best-effort — returns False on failure."""
        try:
            row = self._normalize_record(record)
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.normalize_failed: %s", exc)
            return False
        if row is None:
            return False
        try:
            with self._lock:
                columns = self._compatible_columns_unlocked()
                placeholders = ", ".join("?" for _ in columns)
                sql = (
                    f"INSERT OR REPLACE INTO router_decisions ({', '.join(columns)}) "
                    f"VALUES ({placeholders})"
                )
                self._conn.execute(sql, tuple(row[column] for column in columns))
                self._conn.commit()
                self._insert_count += 1
                should_prune = self._insert_count % self._prune_every == 0
            if should_prune:
                self._prune()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.record_failed: %s", exc)
            return False

    def _normalize_record(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        decision_id = sanitize_token(record.get("decision_id"))
        session_key = record.get("session_key")
        if decision_id is None or not isinstance(session_key, str) or not session_key:
            return None
        ts_ms = _optional_int(record.get("ts_ms"))
        if ts_ms is None:
            ts_ms = self._clock()
        executed_kind = record.get("executed_kind")
        if executed_kind not in _EXECUTED_KINDS:
            executed_kind = "single"
        row: dict[str, Any] = {
            "decision_id": decision_id,
            "session_key": session_key,
            "turn_index": _optional_int(record.get("turn_index")),
            "ts_ms": ts_ms,
            "confidence": _optional_number(record.get("confidence")),
            "probs": json.dumps(sanitize_probs(record.get("probs"))),
            "flags": json.dumps(sanitize_flags(record.get("flags"))),
            "trail": json.dumps(sanitize_trail(record.get("trail"))),
            "savings_pct": _optional_number(record.get("savings_pct")),
            "executed_kind": executed_kind,
            "fallback_hops": _optional_int(record.get("fallback_hops")) or 0,
        }
        for column in _TEXT_TOKEN_COLUMNS:
            row[column] = sanitize_token(record.get(column))
        return row

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention_days * _DAY_MS
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    DELETE FROM router_decisions
                    WHERE rowid IN (
                        SELECT rowid
                        FROM router_decisions
                        WHERE ts_ms < ?
                        ORDER BY ts_ms, decision_id
                        LIMIT ?
                    )
                    """,
                    (cutoff, self._prune_batch),
                )
                self._conn.commit()
            if cur.rowcount:
                log.info(
                    "router_decision_writer.pruned rows=%s retention_days=%s",
                    cur.rowcount,
                    self._retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.prune_failed: %s", exc)

    # ------------- cleanup -------------

    def purge_for_session(self, session_key: str) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM router_decisions WHERE session_key=?",
                    (session_key,),
                )
                self._conn.commit()
                return cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.purge_failed: %s", exc)
            return 0

    # ------------- read path -------------

    def load_recent_history(
        self,
        *,
        window_seconds: int = 1800,
        per_session: int = 5,
        now_ms: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the last ``per_session`` decisions per recently-active session.

        One bounded query — used at gateway boot to rehydrate the in-process
        ``RoutingHistoryStore`` so sticky/anti-downgrade survives a restart.
        Rows are plain dicts ordered oldest→newest per session.
        """
        now = now_ms if now_ms is not None else self._clock()
        cutoff = now - max(0, int(window_seconds)) * 1000
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT session_key, turn_index, ts_ms, proposed_tier, "
                    "       final_tier, confidence "
                    "  FROM router_decisions "
                    " WHERE ts_ms >= ? "
                    " ORDER BY session_key ASC, ts_ms ASC, decision_id ASC",
                    (cutoff,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.load_recent_failed: %s", exc)
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            entry = {
                "session_key": row["session_key"],
                "turn_index": row["turn_index"],
                "ts_ms": int(row["ts_ms"] or 0),
                "proposed_tier": row["proposed_tier"],
                "final_tier": row["final_tier"],
                "confidence": row["confidence"],
            }
            grouped.setdefault(str(row["session_key"]), []).append(entry)
        bound = max(1, int(per_session))
        return {key: entries[-bound:] for key, entries in grouped.items()}

    def list_decisions(
        self,
        *,
        session_key: str | None = None,
        limit: int = 50,
        before_ts_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return sanitized decision rows, newest first.

        Read surface for the ``router.decisions.list`` RPC. Only the
        whitelisted ``_COLUMNS`` are selected; the table stores enum tokens
        and numbers only (V017 privacy contract), so every returned value is
        already safe for operator display. ``before_ts_ms`` pages backwards:
        pass the oldest ``ts_ms`` of the previous page to fetch older rows.
        Best-effort like every method here — any failure returns ``[]``.

        JSON columns (``probs``/``flags``/``trail``) are parsed back into
        structures; a corrupt cell degrades to an empty list rather than
        failing the listing.
        """
        bound = max(1, min(int(limit), 1000))
        clauses: list[str] = []
        args: list[Any] = []
        if session_key:
            clauses.append("session_key = ?")
            args.append(session_key)
        if before_ts_ms is not None:
            clauses.append("ts_ms < ?")
            args.append(int(before_ts_ms))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(bound)
        try:
            with self._lock:
                columns = self._compatible_columns_unlocked()
                sql = (
                    f"SELECT {', '.join(columns)} FROM router_decisions"
                    f"{where} ORDER BY ts_ms DESC, decision_id DESC LIMIT ?"
                )
                rows = self._conn.execute(sql, tuple(args)).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.list_failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                column: row[column] if column in columns else None
                for column in _COLUMNS
            }
            for json_column in ("probs", "flags", "trail"):
                record[json_column] = _load_json_list(record.get(json_column))
            out.append(record)
        return out

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return one sanitized decision row by id, or ``None``.

        Reverse-lookup surface for feedback attribution
        (``router.feedback.submit`` resolves ``decisionId`` to the
        ``(session_key, turn_index, executed_kind)`` the sidecar needs).
        Same privacy posture and best-effort error handling as
        :meth:`list_decisions`.
        """
        token = sanitize_token(decision_id)
        if token is None:
            return None
        try:
            with self._lock:
                columns = self._compatible_columns_unlocked()
                sql = (
                    f"SELECT {', '.join(columns)} FROM router_decisions "
                    "WHERE decision_id = ?"
                )
                row = self._conn.execute(sql, (token,)).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("router_decision_writer.get_failed: %s", exc)
            return None
        if row is None:
            return None
        record: dict[str, Any] = {
            column: row[column] if column in columns else None
            for column in _COLUMNS
        }
        for json_column in ("probs", "flags", "trail"):
            record[json_column] = _load_json_list(record.get(json_column))
        return record


# ---------------------------------------------------------------------------
# Constructor with full PRAGMA contract
# ---------------------------------------------------------------------------


def open_router_decision_writer(
    db_path: str,
    *,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> RouterDecisionWriter:
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
    return RouterDecisionWriter(conn, retention_days=retention_days)
