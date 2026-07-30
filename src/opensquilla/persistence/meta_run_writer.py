"""MetaRunWriter — persistence facade for meta-skill execution traces.

G4 traceable and auditable ledger. Thread-safe sync writer over a long-lived
SQLite connection; the orchestrator wraps calls in ``asyncio.to_thread()``.

Connection contract:
    * ``check_same_thread=False`` — allows cross-thread access.
    * ``threading.Lock`` around every SQL call — serializes at Python level.
    * PRAGMAs set once at construction: ``foreign_keys=ON``,
      ``journal_mode=WAL``, ``synchronous=NORMAL``, ``busy_timeout=5000``.

Fail-open: persistence is observability; all writes are try/except → log.warning
so a writer failure cannot fail a meta-skill turn.

Retention: every ``prune_every`` (default 64) ``begin_run_sync`` calls the
writer runs one opportunistic, bounded delete of at most ``prune_batch``
(default 1000) terminal rows older than ``retention_days`` so the table stays
bounded without a background job or a long foreground write lock.
Live (``running``) and parked (``awaiting_user``) runs are never pruned
regardless of age; step rows follow their run via FK ``ON DELETE CASCADE``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

log = logging.getLogger(__name__)

# 64 KiB matches tool_truncation.py
_DEFAULT_MAX_FIELD_BYTES = 64 * 1024
# 4 KiB per-string clip for redactor; small enough to discourage secrets
_REDACTOR_PER_STRING_BYTES = 4 * 1024
# Retention defaults — mirror the RouterDecisionWriter contract: an
# opportunistic write-time prune, no background job, no config keys.
_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_PRUNE_EVERY = 64
_DEFAULT_PRUNE_BATCH = 1_000
_DAY_MS = 24 * 60 * 60 * 1000

_SECRET_KEY_RE = re.compile(
    r"(?i)(api_?key|access_?key|secret|token|password|passwd|auth(?:_?header)?|bearer)"
)
_SECRET_PREFIX_RE = re.compile(
    r"^(sk-|pk-|ghp_|gho_|ghu_|ghs_|ghr_|xoxb-|xoxp-|Bearer )"
)
# Crockford Base32 — no I, L, O, U
_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Monotonic ULID state — when two _gen_ulid() calls land in the same ms, the
# random component is incremented by 1 instead of re-rolled so the result
# is strictly greater than the previous one (ULID spec §"Monotonicity").
_ULID_LOCK = threading.Lock()
_ULID_LAST_TS_MS: int = -1
_ULID_LAST_RAND: int = 0
_ULID_RAND_MAX = (1 << 80) - 1


# ---------------------------------------------------------------------------
# Dataclasses (public API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepRecord:
    run_id: str
    step_id: str
    step_kind: str
    declared_skill: str
    effective_skill: str
    status: str
    started_at_ms: int
    ended_at_ms: int | None
    rendered_inputs_json: str
    output_text: str | None
    error: str | None
    substitute_step_id: str | None
    truncated_fields: tuple[str, ...]
    usage_json: str = "{}"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    meta_skill_name: str
    meta_skill_digest: str
    plan_snapshot_json: str
    triggered_by: str
    session_key: str | None
    turn_id: str | None
    owner_pid: int | None
    status: str
    started_at_ms: int
    ended_at_ms: int | None
    inputs_json: str
    final_text: str | None
    failed_step_id: str | None
    error: str | None
    truncated_fields: tuple[str, ...]
    steps: tuple[StepRecord, ...] = ()


@dataclass
class AwaitingPeek:
    """Read-only view of a single `awaiting_user` row.

    Returned by `MetaRunWriter.peek_awaiting`. The schema field is the
    deserialized `ClarifyStepConfig`; the `_json` strings are exposed
    raw so callers that just want to forward them (e.g. the surface
    renderers) don't pay a JSON parse cost.
    """

    run_id: str
    step_id: str
    awaiting_since: float
    awaiting_session_id: str
    awaiting_schema_json: str
    awaiting_filled_json: str
    step_outputs_json: str
    inputs_json: str
    parse_failure_count: int


@dataclass
class ResumePayload:
    """Full payload required to call MetaOrchestrator.resume.

    Returned by `MetaRunWriter.try_claim_resume` only on rowcount==1
    (the caller has won the CAS for this run).
    """

    run_id: str
    plan_snapshot_json: str
    inputs_json: str
    step_outputs_json: str
    awaiting_step_id: str
    awaiting_schema_json: str
    awaiting_filled_json: str


def summarize_step_record(step: StepRecord) -> dict[str, Any]:
    """Return a stable read-side summary for one persisted step."""
    duration_ms = (
        max(0, step.ended_at_ms - step.started_at_ms)
        if step.ended_at_ms is not None
        else None
    )
    output_chars = len(step.output_text or "")
    return {
        "step_id": step.step_id,
        "kind": step.step_kind,
        "declared_skill": step.declared_skill,
        "effective_skill": step.effective_skill,
        "status": step.status,
        "started_at_ms": step.started_at_ms,
        "ended_at_ms": step.ended_at_ms,
        "duration_ms": duration_ms,
        "output_chars": output_chars,
        "error_present": bool(step.error),
        "substitute_step_id": step.substitute_step_id,
        "truncated_fields": list(step.truncated_fields),
        "usage": _usage_summary_from_json(step.usage_json),
    }


def summarize_run_record(record: RunRecord) -> dict[str, Any]:
    """Return a stable P1 run detail summary for history UIs and CLI JSON.

    Historical meta-run tables do not persist usage rows yet. The summary
    therefore reports deterministic execution facts and explicitly marks
    usage/cost unavailable instead of manufacturing zeros that look billed.
    """
    duration_ms = (
        max(0, record.ended_at_ms - record.started_at_ms)
        if record.ended_at_ms is not None
        else None
    )
    step_summaries = [summarize_step_record(step) for step in record.steps]
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "status": record.status,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "duration_ms": duration_ms,
        "step_count": len(record.steps),
        "completed_step_count": sum(
            1 for step in record.steps if step.status in {"ok", "substituted"}
        ),
        "failed_step_count": sum(1 for step in record.steps if step.status == "failed"),
        "running_step_count": sum(1 for step in record.steps if step.status == "running"),
        "final_text_chars": len(record.final_text or ""),
        "step_output_chars": sum(len(step.output_text or "") for step in record.steps),
        "failed_step_id": record.failed_step_id,
        "error_present": bool(record.error),
        "truncated_fields": list(record.truncated_fields),
        "usage": _aggregate_usage_summary(
            [step["usage"] for step in step_summaries],
        ),
        "steps": step_summaries,
    }


def _unavailable_usage_summary() -> dict[str, Any]:
    return {
        "available": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "unavailable",
        "reason": "meta run persistence does not store historical usage yet",
    }


def _number_from_usage(raw: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 0.0


def _int_from_usage(raw: Mapping[str, Any], *keys: str) -> int:
    return int(_number_from_usage(raw, *keys))


def _usage_summary_from_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    input_tokens = _int_from_usage(raw, "input_tokens")
    output_tokens = _int_from_usage(raw, "output_tokens")
    cache_read_tokens = _int_from_usage(raw, "cache_read_tokens", "cache_read_input_tokens")
    cache_write_tokens = _int_from_usage(
        raw,
        "cache_write_tokens",
        "cache_creation_input_tokens",
    )
    total_tokens = _int_from_usage(raw, "total_tokens")
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    if not (input_tokens or output_tokens or cache_read_tokens or cache_write_tokens):
        return _unavailable_usage_summary()
    cost_usd = _number_from_usage(raw, "cost_usd", "total_cost_usd")
    billed_cost_usd = _number_from_usage(raw, "billed_cost_usd", "billed_cost")
    estimated_cost_usd = _number_from_usage(raw, "estimated_cost_usd", "cost")
    cost_source = str(raw.get("cost_source") or "").strip()
    if not cost_source:
        cost_source = "provider_billed" if billed_cost_usd else "opensquilla_estimate"
    return {
        "available": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": round(cost_usd, 6),
        "billed_cost_usd": round(billed_cost_usd, 6),
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "cost_source": cost_source,
        "model": str(raw.get("model") or raw.get("model_id") or "").strip(),
        "is_provider_billed": cost_source == "provider_billed",
    }


def _usage_summary_from_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return _unavailable_usage_summary()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _unavailable_usage_summary()
    if not isinstance(payload, Mapping):
        return _unavailable_usage_summary()
    return _usage_summary_from_mapping(payload)


def _aggregate_usage_summary(usages: list[dict[str, Any]]) -> dict[str, Any]:
    available = [usage for usage in usages if usage.get("available") is True]
    if not available:
        return _unavailable_usage_summary()
    cost_sources = {
        str(usage.get("cost_source") or "").strip()
        for usage in available
        if str(usage.get("cost_source") or "").strip()
    }
    cost_source = next(iter(cost_sources)) if len(cost_sources) == 1 else "mixed"
    return {
        "available": True,
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in available),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in available),
        "total_tokens": sum(int(usage.get("total_tokens") or 0) for usage in available),
        "cache_read_tokens": sum(
            int(usage.get("cache_read_tokens") or 0) for usage in available
        ),
        "cache_write_tokens": sum(
            int(usage.get("cache_write_tokens") or 0) for usage in available
        ),
        "cost_usd": round(
            sum(float(usage.get("cost_usd") or 0.0) for usage in available),
            6,
        ),
        "billed_cost_usd": round(
            sum(float(usage.get("billed_cost_usd") or 0.0) for usage in available),
            6,
        ),
        "estimated_cost_usd": round(
            sum(float(usage.get("estimated_cost_usd") or 0.0) for usage in available),
            6,
        ),
        "cost_source": cost_source,
        "step_count": len(available),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _truncate(
    value: str | None, field_name: str, *, max_bytes: int
) -> tuple[str | None, bool]:
    """UTF-8 byte-bounded truncate. Returns (value, was_truncated)."""
    if value is None:
        return None, False
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped, True


def _gen_ulid() -> str:
    """26-char monotonic ULID.

    48-bit ms timestamp + 80-bit randomness, Crockford-base32. When two calls
    fall in the same millisecond, the random component is incremented from
    the previous one rather than re-rolled — guarantees lexicographic order
    matches insertion order even at sub-ms cadence (ULID spec §monotonic).
    """
    global _ULID_LAST_TS_MS, _ULID_LAST_RAND
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    with _ULID_LOCK:
        if ts_ms == _ULID_LAST_TS_MS:
            # Same ms — increment last random. If it overflows the 80-bit
            # space, bump the ms by 1 and re-roll (extremely rare).
            rand_int = _ULID_LAST_RAND + 1
            if rand_int > _ULID_RAND_MAX:
                ts_ms = (ts_ms + 1) & ((1 << 48) - 1)
                rand_int = int.from_bytes(secrets.token_bytes(10), "big")
        else:
            rand_int = int.from_bytes(secrets.token_bytes(10), "big")
        _ULID_LAST_TS_MS = ts_ms
        _ULID_LAST_RAND = rand_int
    full = (ts_ms << 80) | rand_int
    out_chars: list[str] = []
    for shift in range(125, -1, -5):  # 26 * 5 = 130 bits; top two are zero
        out_chars.append(_BASE32[(full >> shift) & 0x1F])
    return "".join(out_chars[:26])


def _redact_inputs_json(raw: Mapping[str, Any], *, max_bytes: int) -> str:
    """Recursive redactor for arbitrary inputs mapping.

    Rules:
    * Key match against secret regex → ``[REDACTED]``
    * Value prefix match against secret prefix regex → ``[REDACTED]``
    * Per-string clip to ``_REDACTOR_PER_STRING_BYTES``
    * Total JSON ≤ ``max_bytes``; drops fields in reverse-key order on overflow
    """

    def _walk(node: Any, key_hint: str | None) -> Any:
        if isinstance(node, Mapping):
            return {k: _walk(v, key_hint=str(k)) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(item, key_hint=key_hint) for item in node]
        if isinstance(node, str):
            if key_hint and _SECRET_KEY_RE.search(key_hint):
                return "[REDACTED]"
            if _SECRET_PREFIX_RE.match(node):
                return "[REDACTED]"
            encoded = node.encode("utf-8")
            if len(encoded) > _REDACTOR_PER_STRING_BYTES:
                clipped = encoded[:_REDACTOR_PER_STRING_BYTES].decode(
                    "utf-8", errors="ignore"
                )
                # Keep suffix tiny (1 char) so total stays within
                # ``_REDACTOR_PER_STRING_BYTES + 4`` chars; callers asserting
                # the budget would fail with a verbose suffix.
                return f"{clipped}…"
            return node
        return node

    redacted = _walk(dict(raw), key_hint=None)
    text = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    # Overflow — drop fields in reverse-key order
    keys = sorted(redacted.keys(), reverse=True)
    while keys:
        dropped = keys.pop(0)
        redacted.pop(dropped, None)
        redacted["_redaction_overflow"] = True
        text = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
        if len(text.encode("utf-8")) <= max_bytes:
            return text
    return json.dumps({"_redaction_overflow": True}, sort_keys=True)


def _serialize_plan(plan: MetaPlan) -> tuple[str, str]:
    """Returns (plan_snapshot_json, meta_skill_digest).

    Thin wrapper over opensquilla.skills.meta.plan_serde; this preserves the
    versioned user-input plan snapshot contract.
    The snapshot JSON is the *envelope* (with ``"v": 1``); the digest is
    over the same canonical JSON. Existing rows' digests will change on
    next write — this is a one-time churn at the V013 cut-over.
    """
    from opensquilla.skills.meta.plan_serde import plan_digest, to_jsonable

    snapshot = to_jsonable(plan)
    snapshot_json = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    digest = plan_digest(plan)
    return snapshot_json, digest


def _serialize_usage_json(usage: Mapping[str, Any] | None) -> str:
    if not usage:
        return "{}"
    summary = _usage_summary_from_mapping(usage)
    if summary.get("available") is not True:
        return "{}"
    return json.dumps(summary, sort_keys=True, ensure_ascii=False)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:  # noqa: BLE001
        return False
    return any(row[1] == column for row in rows)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class MetaRunWriter:
    """Long-lived sync writer over a single sqlite3 connection.

    Caller responsibilities:
    * Construct via :func:`open_meta_run_writer` (sets PRAGMAs).
    * Call ``close()`` at shutdown.
    * Wrap calls in ``asyncio.to_thread()`` / ``loop.run_in_executor()``
      if used from async code.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        max_field_bytes: int = _DEFAULT_MAX_FIELD_BYTES,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        prune_every: int = _DEFAULT_PRUNE_EVERY,
        prune_batch: int = _DEFAULT_PRUNE_BATCH,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        id_gen: Callable[[], str] = _gen_ulid,
        pid_fn: Callable[[], int] = os.getpid,
    ) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        self._max_field_bytes = max_field_bytes
        self._retention_days = max(1, int(retention_days))
        self._prune_every = max(1, int(prune_every))
        self._prune_batch = max(1, int(prune_batch))
        self._begin_run_count = 0
        self._clock = clock
        self._id_gen = id_gen
        self._pid_fn = pid_fn
        self._has_step_usage_column = _has_column(
            connection,
            "meta_skill_run_steps",
            "usage_json",
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("meta_run_writer.close_failed: %s", exc)

    # ------------- write path -------------

    def begin_run_sync(
        self,
        *,
        meta_skill_name: str,
        meta_plan: MetaPlan,
        triggered_by: Literal[
            "hard_takeover",
            "soft_meta_invoke",
            "auto_cron",
            "auto_dream",
            "manual_command",
        ],
        inputs: Mapping[str, Any],
        session_key: str | None,
        turn_id: str | None,
    ) -> str | None:
        run_id = self._id_gen()
        snapshot_json, digest = _serialize_plan(meta_plan)
        inputs_json = _redact_inputs_json(inputs, max_bytes=self._max_field_bytes)
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO meta_skill_runs (
                        run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json,
                        triggered_by, session_key, turn_id, owner_pid, status,
                        started_at_ms, inputs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        run_id, meta_skill_name, digest, snapshot_json,
                        triggered_by, session_key, turn_id, self._pid_fn(),
                        self._clock(), inputs_json,
                    ),
                )
                self._conn.commit()
                self._begin_run_count += 1
                should_prune = self._begin_run_count % self._prune_every == 0
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.begin_run_failed: %s", exc)
            return None
        if should_prune:
            self._prune()
        return run_id

    def _prune(self) -> None:
        """Opportunistic retention prune (fail-open, write-time only).

        Deletes terminal runs older than ``retention_days``; ``running`` and
        ``awaiting_user`` rows are never deleted regardless of age. Step rows
        cascade via the FK on ``meta_skill_run_steps.run_id``.
        """
        cutoff = self._clock() - self._retention_days * _DAY_MS
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    DELETE FROM meta_skill_runs
                    WHERE run_id IN (
                        SELECT run_id
                        FROM meta_skill_runs
                        WHERE started_at_ms < ?
                        AND status NOT IN ('running', 'awaiting_user')
                        ORDER BY started_at_ms, run_id
                        LIMIT ?
                    )
                    """,
                    (cutoff, self._prune_batch),
                )
                self._conn.commit()
            if cur.rowcount:
                log.info(
                    "meta_run_writer.pruned rows=%s retention_days=%s",
                    cur.rowcount,
                    self._retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.prune_failed: %s", exc)

    def begin_step_sync(
        self,
        *,
        run_id: str,
        step: MetaStep,
        effective_skill: str,
        rendered_inputs: Mapping[str, Any],
    ) -> None:
        rendered_json = _redact_inputs_json(rendered_inputs, max_bytes=self._max_field_bytes)
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO meta_skill_run_steps (
                        run_id, step_id, step_kind, declared_skill, effective_skill,
                        status, started_at_ms, rendered_inputs_json
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        run_id, step.id, step.kind, step.skill, effective_skill,
                        self._clock(), rendered_json,
                    ),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.begin_step_failed: %s", exc)

    def finish_step_sync(
        self,
        *,
        run_id: str,
        step_id: str,
        status: Literal["ok", "failed", "substituted"],
        output_text: str | None,
        error: str | None = None,
        substitute_step_id: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        truncated: list[str] = []
        out, was_t = _truncate(output_text, "output_text", max_bytes=self._max_field_bytes)
        if was_t:
            truncated.append("output_text")
        usage_json = _serialize_usage_json(usage)
        try:
            with self._lock:
                if self._has_step_usage_column:
                    self._conn.execute(
                        """
                        UPDATE meta_skill_run_steps
                           SET status=?, ended_at_ms=?, output_text=?, error=?,
                               substitute_step_id=?, truncated_fields=?, usage_json=?
                         WHERE run_id=? AND step_id=?
                        """,
                        (
                            status, self._clock(), out, error,
                            substitute_step_id, ",".join(truncated), usage_json,
                            run_id, step_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE meta_skill_run_steps
                           SET status=?, ended_at_ms=?, output_text=?, error=?,
                               substitute_step_id=?, truncated_fields=?
                         WHERE run_id=? AND step_id=?
                        """,
                        (
                            status, self._clock(), out, error,
                            substitute_step_id, ",".join(truncated), run_id, step_id,
                        ),
                    )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.finish_step_failed: %s", exc)

    def on_step_failover_sync(
        self,
        *,
        run_id: str,
        failed_step_id: str,
        substitute_step_id: str,
        error: str,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """C3: mark original step as substituted with substitute pointer."""
        self.finish_step_sync(
            run_id=run_id,
            step_id=failed_step_id,
            status="substituted",
            output_text=None,
            error=error,
            substitute_step_id=substitute_step_id,
            usage=usage,
        )

    def finish_run_sync(
        self,
        *,
        run_id: str,
        status: Literal["ok", "failed", "cancelled"],
        result: MetaResult | None,
    ) -> None:
        """Finalize a run row with a terminal status.

        Status-guarded like the sibling transitions: only ``running`` rows
        (plus ``cancelled`` preflight gates, whose confirmed re-run reuses
        the row — see ``_is_confirmable_preflight_run``) may be finalized.
        A late finalize can therefore never clobber a committed
        ``awaiting_user`` row; lost races are logged at debug level.
        Step rows still ``running`` are swept to ``failed`` in the same
        explicit transaction (``BEGIN IMMEDIATE`` — the connection is
        otherwise autocommit) so the finalize and the sweep land atomically.
        A step INSERT abandoned by a cancelled turn can still commit after
        the sweep; :meth:`mark_orphans_failed` repairs those at next boot.
        """
        truncated: list[str] = []
        final_text: str | None = None
        failed_step_id: str | None = None
        error: str | None = None
        if result is not None:
            final_text_raw = result.final_text or None
            final_text, was_t = _truncate(
                final_text_raw, "final_text", max_bytes=self._max_field_bytes,
            )
            if was_t:
                truncated.append("final_text")
            failed_step_id = result.failed_step_id
            error = result.error
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = self._conn.execute(
                        """
                        UPDATE meta_skill_runs
                           SET status=?, ended_at_ms=?, final_text=?,
                               failed_step_id=?, error=?, truncated_fields=?
                         WHERE run_id=?
                           AND (status='running'
                                OR (status='cancelled' AND error='preflight_required'))
                        """,
                        (
                            status, self._clock(), final_text,
                            failed_step_id, error, ",".join(truncated),
                            run_id,
                        ),
                    )
                    if cur.rowcount == 0:
                        self._conn.rollback()
                        log.debug(
                            "meta_run_writer.finish_run_skipped run=%s status=%s "
                            "(row not in a finalizable status)",
                            run_id,
                            status,
                        )
                        return
                    self._conn.execute(
                        """
                        UPDATE meta_skill_run_steps
                           SET status='failed', ended_at_ms=?,
                               error='run finalized before step completed'
                         WHERE run_id=? AND status='running'
                        """,
                        (self._clock(), run_id),
                    )
                    self._conn.commit()
                except Exception:
                    # Never leave a transaction open on the shared connection.
                    self._conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.finish_run_failed: %s", exc)

    # ------------- read path -------------

    def get_run(self, run_id: str) -> RunRecord | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM meta_skill_runs WHERE run_id=?", (run_id,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.get_run_failed: %s", exc)
            return None
        if row is None:
            return None
        return self._row_to_run(row, steps=tuple(self.get_steps(run_id)))

    def get_steps(self, run_id: str) -> list[StepRecord]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM meta_skill_run_steps WHERE run_id=? "
                    "ORDER BY started_at_ms ASC, step_id ASC",
                    (run_id,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.get_steps_failed: %s", exc)
            return []
        return [self._row_to_step(r) for r in rows]

    def get_steps_for_runs(self, run_ids: list[str]) -> dict[str, tuple[StepRecord, ...]]:
        """Bulk read steps for a set of runs, grouped by run_id."""
        ids = [run_id for run_id in run_ids if run_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM meta_skill_run_steps "
                    f"WHERE run_id IN ({placeholders}) "
                    "ORDER BY run_id ASC, started_at_ms ASC, step_id ASC",
                    ids,
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.get_steps_for_runs_failed: %s", exc)
            return {}
        grouped: dict[str, list[StepRecord]] = {}
        for row in rows:
            step = self._row_to_step(row)
            grouped.setdefault(step.run_id, []).append(step)
        return {run_id: tuple(steps) for run_id, steps in grouped.items()}

    def hydrate_runs(self, rows: list[RunRecord]) -> list[RunRecord]:
        """Attach steps to shallow list_runs rows using one bulk step query."""
        missing = [row.run_id for row in rows if not row.steps]
        if not missing:
            return rows
        by_run = self.get_steps_for_runs(missing)
        return [
            row if row.steps else replace(row, steps=by_run.get(row.run_id, ()))
            for row in rows
        ]

    def list_runs(
        self,
        *,
        name: str | None = None,
        status: str | None = None,
        session_key: str | None = None,
        since_ms: int | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if name is not None:
            clauses.append("meta_skill_name = ?")
            params.append(name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(session_key)
        if since_ms is not None:
            clauses.append("started_at_ms >= ?")
            params.append(since_ms)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM meta_skill_runs{where} "
            "ORDER BY started_at_ms DESC, run_id DESC LIMIT ?"
        )
        params.append(limit)
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.list_runs_failed: %s", exc)
            return []
        return [self._row_to_run(r, steps=()) for r in rows]

    def list_failures(
        self,
        *,
        name: str | None = None,
        since_ms: int | None = None,
        session_key: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        return self.list_runs(
            name=name,
            status="failed",
            session_key=session_key,
            since_ms=since_ms,
            limit=limit,
        )

    def peek_awaiting(self, *, session_id: str) -> AwaitingPeek | None:
        """Read the single awaiting_user record for this session, if any.

        Read-only: does NOT transition state. The partial unique index on
        session_key WHERE status='awaiting_user' guarantees at most one row.
        """
        if not session_id:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM meta_skill_runs "
                    "WHERE session_key=? AND status='awaiting_user' LIMIT 1",
                    (session_id,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.peek_awaiting_failed: %s", exc)
            return None
        if row is None:
            return None
        return AwaitingPeek(
            run_id=row["run_id"],
            step_id=row["awaiting_step_id"] or "",
            awaiting_since=float(row["awaiting_since"] or 0.0),
            awaiting_session_id=row["session_key"] or "",
            awaiting_schema_json=row["awaiting_schema_json"] or "{}",
            awaiting_filled_json=row["awaiting_filled_json"] or "{}",
            step_outputs_json=row["step_outputs_json"] or "{}",
            inputs_json=row["inputs_json"] or "{}",
            parse_failure_count=int(row["parse_failure_count"] or 0),
        )

    def try_claim_awaiting(
        self,
        *,
        run_id: str,
        step_id: str,
        schema_json: str,
        session_id: str,
        inputs_json: str,
        step_outputs_json: str,
        awaiting_since: float,
        awaiting_filled_json: str = "{}",
    ) -> bool:
        """Atomically transition status running → awaiting_user.

        Returns True on success (rowcount == 1).
        Returns False if either:
          (a) the run is no longer 'running' (lost a race to finalize); or
          (b) another run in the same session is already awaiting_user
              (partial unique index rejects the write with IntegrityError).

        Callers MUST NOT raise MetaPaused on False — the user_input
        executor treats False as a normal step failure (design §10).

        ``awaiting_filled_json`` defaults to ``"{}"`` for callers that
        haven't run prefill. The user_input executor's prefill pass
        passes the JSON-encoded ``{field: value, __prefill_audit__:...}``
        here so the surface form renders pre-filled values on first
        paint (rather than always starting blank).
        """
        try:
            with self._lock:
                try:
                    cur = self._conn.execute(
                        """
                        UPDATE meta_skill_runs
                           SET status='awaiting_user',
                               awaiting_step_id=?,
                               awaiting_schema_json=?,
                               awaiting_since=?,
                               session_key=?,
                               inputs_json=?,
                               step_outputs_json=?,
                               awaiting_filled_json=?,
                               parse_failure_count=0
                         WHERE run_id=? AND status='running'
                        """,
                        (
                            step_id, schema_json, awaiting_since,
                            session_id, inputs_json, step_outputs_json,
                            awaiting_filled_json, run_id,
                        ),
                    )
                    if cur.rowcount == 0:
                        self._conn.rollback()
                        return False
                    self._conn.commit()
                    return True
                except sqlite3.IntegrityError as exc:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                    log.info(
                        "meta_run_writer.try_claim_awaiting.race_lost run=%s session=%s err=%s",
                        run_id, session_id, exc,
                    )
                    return False
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.try_claim_awaiting_failed: %s", exc)
            return False

    def try_claim_resume(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> ResumePayload | None:
        """Atomically transition awaiting_user → running, scoped by session.

        On rowcount==1, returns the full payload for MetaOrchestrator.resume.
        On rowcount==0, returns None (race lost, or cancelled/expired).
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT plan_snapshot_json, inputs_json, step_outputs_json, "
                    "       awaiting_step_id, awaiting_schema_json, "
                    "       awaiting_filled_json "
                    "  FROM meta_skill_runs "
                    " WHERE run_id=? AND session_key=? AND status='awaiting_user' "
                    "LIMIT 1",
                    (run_id, session_id),
                ).fetchone()
                if row is None:
                    return None
                cur = self._conn.execute(
                    """
                    UPDATE meta_skill_runs
                       SET status='running',
                           awaiting_step_id=NULL,
                           awaiting_schema_json=NULL,
                           awaiting_since=NULL
                     WHERE run_id=? AND session_key=? AND status='awaiting_user'
                    """,
                    (run_id, session_id),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    return None
                self._conn.commit()
                return ResumePayload(
                    run_id=run_id,
                    plan_snapshot_json=row["plan_snapshot_json"] or "{}",
                    inputs_json=row["inputs_json"] or "{}",
                    step_outputs_json=row["step_outputs_json"] or "{}",
                    awaiting_step_id=row["awaiting_step_id"] or "",
                    awaiting_schema_json=row["awaiting_schema_json"] or "{}",
                    awaiting_filled_json=row["awaiting_filled_json"] or "{}",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.try_claim_resume_failed: %s", exc)
            return None

    def mark_expired(self, *, run_id: str) -> None:
        """Transition an awaiting_user run to 'expired'.

        Idempotent: a row already in 'expired' is left untouched.
        """
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE meta_skill_runs
                       SET status='expired', ended_at_ms=?
                     WHERE run_id=? AND status='awaiting_user'
                    """,
                    (self._clock(), run_id),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.mark_expired_failed: %s", exc)

    def mark_cancelled(self, *, run_id: str, reason: str) -> None:
        """Transition an awaiting_user run to 'cancelled' with a recorded reason.

        Reason is stored in the existing `error` column (with `cancelled:`
        prefix); callers must check `status` before interpreting `error`.
        """
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE meta_skill_runs
                       SET status='cancelled', ended_at_ms=?, error=?
                     WHERE run_id=? AND status='awaiting_user'
                    """,
                    (self._clock(), f"cancelled:{reason}", run_id),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.mark_cancelled_failed: %s", exc)

    def increment_parse_failures(self, *, run_id: str) -> int:
        """Increment parse_failure_count for an awaiting run; return new value.

        Returns 0 if the row is not in 'awaiting_user' (no-op sentinel).

        Atomicity comes from this writer's single-connection contract: the
        ``threading.Lock`` serializes the status-guarded UPDATE and the
        follow-up SELECT so no other call on this writer can interleave.
        (An earlier ``UPDATE ... RETURNING`` form required SQLite >= 3.35;
        on older system builds the syntax error was swallowed by the
        fail-open handler and the parse-failure limit never triggered.)
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    UPDATE meta_skill_runs
                       SET parse_failure_count = parse_failure_count + 1
                     WHERE run_id=? AND status='awaiting_user'
                    """,
                    (run_id,),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    return 0
                row = self._conn.execute(
                    "SELECT parse_failure_count FROM meta_skill_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                self._conn.commit()
                if row is None:
                    return 0
                if isinstance(row, sqlite3.Row):
                    return int(row["parse_failure_count"])
                return int(row[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.increment_parse_failures_failed: %s", exc)
            return 0

    def update_awaiting_partial(
        self, *, run_id: str, filled_json: str, awaiting_since: float,
    ) -> bool:
        """Persist a partial fill for chat-mode awaiting (design §5.4).

        Resets awaiting_since so the user gets a fresh timeout window.
        Returns True only if the row was still in awaiting_user.
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    UPDATE meta_skill_runs
                       SET awaiting_filled_json=?, awaiting_since=?
                     WHERE run_id=? AND status='awaiting_user'
                    """,
                    (filled_json, awaiting_since, run_id),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    return False
                self._conn.commit()
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.update_awaiting_partial_failed: %s", exc)
            return False

    # ------------- cleanup -------------

    def purge_for_session(self, session_key: str) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM meta_skill_runs WHERE session_key=?", (session_key,),
                )
                self._conn.commit()
                return cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.purge_failed: %s", exc)
            return 0

    def mark_orphans_failed(self, *, age_ms: int = 3_600_000) -> int:
        """W6: boot cleanup. Only marks rows owned by other-or-null pid AND aged.

        Also repairs step rows left ``running`` under a terminal run — a step
        INSERT abandoned by a cancelled turn can land after the run's
        finalize sweep, and expired/cancelled awaiting runs park their
        ``user_input`` step as ``running``; neither is repairable at the time
        it happens, so the next boot sweeps them here. Returns the number of
        run rows (not step rows) marked failed, matching the historical
        contract.
        """
        current_pid = self._pid_fn()
        cutoff = self._clock() - age_ms
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = self._conn.execute(
                        """
                        UPDATE meta_skill_runs
                           SET status='failed', ended_at_ms=?, error='gateway restart'
                         WHERE status='running'
                           AND (owner_pid IS NULL OR owner_pid != ?)
                           AND started_at_ms < ?
                        """,
                        (self._clock(), current_pid, cutoff),
                    )
                    self._conn.execute(
                        """
                        UPDATE meta_skill_run_steps
                           SET status='failed', ended_at_ms=?,
                               error='run finalized before step completed'
                         WHERE status='running'
                           AND run_id IN (
                               SELECT run_id FROM meta_skill_runs
                                WHERE status NOT IN ('running', 'awaiting_user')
                           )
                        """,
                        (self._clock(),),
                    )
                    self._conn.commit()
                    return cur.rowcount or 0
                except Exception:
                    self._conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            log.warning("meta_run_writer.mark_orphans_failed: %s", exc)
            return 0

    # ------------- row mappers -------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row, *, steps: tuple[StepRecord, ...]) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            meta_skill_name=row["meta_skill_name"],
            meta_skill_digest=row["meta_skill_digest"],
            plan_snapshot_json=row["plan_snapshot_json"],
            triggered_by=row["triggered_by"],
            session_key=row["session_key"],
            turn_id=row["turn_id"],
            owner_pid=row["owner_pid"],
            status=row["status"],
            started_at_ms=row["started_at_ms"],
            ended_at_ms=row["ended_at_ms"],
            inputs_json=row["inputs_json"],
            final_text=row["final_text"],
            failed_step_id=row["failed_step_id"],
            error=row["error"],
            truncated_fields=tuple(
                f for f in (row["truncated_fields"] or "").split(",") if f
            ),
            steps=steps,
        )

    @staticmethod
    def _row_to_step(row: sqlite3.Row) -> StepRecord:
        usage_json = "{}"
        if "usage_json" in row.keys():
            usage_json = row["usage_json"] or "{}"
        return StepRecord(
            run_id=row["run_id"],
            step_id=row["step_id"],
            step_kind=row["step_kind"],
            declared_skill=row["declared_skill"],
            effective_skill=row["effective_skill"],
            status=row["status"],
            started_at_ms=row["started_at_ms"],
            ended_at_ms=row["ended_at_ms"],
            rendered_inputs_json=row["rendered_inputs_json"],
            output_text=row["output_text"],
            error=row["error"],
            substitute_step_id=row["substitute_step_id"],
            truncated_fields=tuple(
                f for f in (row["truncated_fields"] or "").split(",") if f
            ),
            usage_json=usage_json,
        )


# ---------------------------------------------------------------------------
# Constructor with full PRAGMA contract
# ---------------------------------------------------------------------------


def open_meta_run_writer(
    db_path: str,
    *,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    prune_every: int = _DEFAULT_PRUNE_EVERY,
) -> MetaRunWriter:
    """Open writer with PRAGMA contract (C2 + W1 v2).

    ``retention_days`` / ``prune_every`` tune the opportunistic write-time
    retention prune (see module docstring); defaults keep 90 days and prune
    on every 64th ``begin_run_sync``.
    """
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,  # W1 — orchestrator runs us in executor threads
        isolation_level=None,     # autocommit; we still call .commit() explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return MetaRunWriter(
        conn,
        retention_days=retention_days,
        prune_every=prune_every,
    )
