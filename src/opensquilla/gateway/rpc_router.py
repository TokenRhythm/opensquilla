"""Router decision-record RPC handlers.

Read/observe surface over the ``router_decisions`` table (V017), which the
engine populates best-effort via
``opensquilla.engine.steps.router_decision_record``. The handlers resolve the
process-wide writer through that hook module's ``get_decision_writer()``
(mirroring how the shared model catalog is consumed) rather than reaching
into boot internals; when no writer is registered (``:memory:`` session DB,
CLI/standalone paths) the list surface degrades to an empty envelope instead
of erroring.

Privacy: the table stores enum tokens and numbers only — no prompt text
(V017 contract, test-enforced) — so every value surfaced here is already
operator-safe and, unlike ``meta.runs.list``, read-only principals need no
per-session gating. These handlers observe routing; they never change it.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from opensquilla.engine.steps.router_decision_record import get_decision_writer
from opensquilla.gateway.protocol import ERROR_INVALID_REQUEST
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.persistence.router_decision_writer import sanitize_token

log = structlog.get_logger(__name__)

_d = get_dispatcher()

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

_FEEDBACK_RATINGS = frozenset({"up", "down", "neutral"})

# snake_case DB column -> camelCase wire key, in canonical V017 column order.
_WIRE_KEYS: tuple[tuple[str, str], ...] = (
    ("decision_id", "decisionId"),
    ("session_key", "sessionKey"),
    ("turn_index", "turnIndex"),
    ("ts_ms", "tsMs"),
    ("classifier", "classifier"),
    ("proposed_tier", "proposedTier"),
    ("confidence", "confidence"),
    ("probs", "probs"),
    ("flags", "flags"),
    ("final_tier", "finalTier"),
    ("requested_provider", "requestedProvider"),
    ("requested_model", "requestedModel"),
    ("provider", "provider"),
    ("model", "model"),
    ("executed_provider", "executedProvider"),
    ("executed_model", "executedModel"),
    ("fallback_reason", "fallbackReason"),
    ("thinking_level", "thinkingLevel"),
    ("source", "source"),
    ("trail", "trail"),
    ("baseline_model", "baselineModel"),
    # C2: stored display value passes through verbatim — never recomputed.
    ("savings_pct", "savingsPct"),
    ("executed_kind", "executedKind"),
    ("ensemble_profile", "ensembleProfile"),
    ("fallback_hops", "fallbackHops"),
)


def _bounded_limit(value: Any, *, default: int = _DEFAULT_LIMIT, maximum: int = _MAX_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < 1:
        return default
    return min(parsed, maximum)


def _optional_int_param(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wire_decision(record: Mapping[str, Any]) -> dict[str, Any]:
    return {wire: record.get(column) for column, wire in _WIRE_KEYS}


@_d.method("router.decisions.list", scope="operator.read")
async def _handle_router_decisions_list(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """List persisted router decision records, newest first.

    Params (all optional): ``sessionKey`` filters to one session; ``limit``
    (default 50, clamped to 200) bounds the page; ``beforeTs`` (epoch ms,
    exclusive) pages backwards — pass the oldest ``tsMs`` of the previous
    page. With no writer registered the envelope is ``{"decisions": []}``;
    this is a pure local DB read (no network, no routing side effects).
    """
    writer = get_decision_writer()
    if writer is None:
        return {"decisions": []}
    p = params if isinstance(params, dict) else {}
    session_key = p.get("sessionKey") or p.get("session_key")
    rows = await asyncio.to_thread(
        writer.list_decisions,
        session_key=str(session_key) if session_key else None,
        limit=_bounded_limit(p.get("limit")),
        before_ts_ms=_optional_int_param(p.get("beforeTs") or p.get("before_ts_ms")),
    )
    return {"decisions": [_wire_decision(row) for row in rows]}


@_d.method("router.feedback.submit", scope="operator.write")
async def _handle_router_feedback_submit(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Record a user rating (up/down/neutral) for one routing decision.

    The F7 feedback intake, live: ``decisionId`` is resolved through the
    decision writer (V017) to the ``(session_key, turn_index)`` the
    self-learning trainer joins on, then appended to the per-agent feedback
    sidecar. Revisions are last-write-wins; ``neutral`` revokes. A decision
    that is unknown (retention pruned, ``:memory:`` writer, or never staged)
    returns ``accepted: false`` rather than an error — clients surface it as
    "this message's routing record expired".

    The rating never mutates the ``router_decisions`` table or routing state;
    consumption happens offline at dataset-build time.
    """
    p = params if isinstance(params, dict) else {}
    decision_id = sanitize_token(p.get("decisionId") or p.get("decision_id"))
    if decision_id is None:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "decisionId must be an id token",
        )
    rating = p.get("rating")
    if not isinstance(rating, str) or rating not in _FEEDBACK_RATINGS:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "rating must be one of: up, down, neutral",
        )

    import anyio

    writer = get_decision_writer()
    if writer is not None:
        # SQLite read via a threading.Lock'd connection — keep it (and any
        # writer-lock contention) off the gateway event loop.
        record = await anyio.to_thread.run_sync(writer.get_decision, decision_id)
    else:
        record = None
    if record is None:
        log.info(
            "router_feedback.decision_not_found",
            decision_id=decision_id,
            rating=rating,
        )
        return {"accepted": False, "reason": "decision_not_found"}

    session_key = str(record.get("session_key") or "")
    turn_index = int(record.get("turn_index") or 0)
    executed_kind = str(record.get("executed_kind") or "single")
    ts_ms = record.get("ts_ms")
    decision_ts = (
        datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ts_ms
        else None
    )

    from opensquilla.session.keys import parse_agent_id
    from opensquilla.squilla_router.self_learning.feedback import write_feedback

    agent_id = parse_agent_id(session_key)
    router_cfg = getattr(ctx.config, "squilla_router", None)
    sl_cfg = getattr(router_cfg, "self_learning", None)
    try:
        retention_days = int(getattr(sl_cfg, "retention_days", 30))
    except (TypeError, ValueError):
        retention_days = 30

    def _write() -> None:
        write_feedback(
            agent_id,
            decision_id=decision_id,
            session_key=session_key,
            turn_index=turn_index,
            rating=rating,
            executed_kind=executed_kind,
            decision_ts=decision_ts,
            retention_days=retention_days,
        )

    try:
        await anyio.to_thread.run_sync(_write)
    except Exception as exc:  # noqa: BLE001 — a lost rating must not error the client
        log.warning(
            "router_feedback.write_failed", decision_id=decision_id, error=str(exc)
        )
        return {"accepted": False, "reason": "write_failed"}

    log.info(
        "router_feedback.submitted",
        decision_id=decision_id,
        rating=rating,
        executed_kind=executed_kind,
        agent_id=agent_id,
    )
    return {"accepted": True, "recorded": rating}


@_d.method("router.selflearning.status", scope="operator.read")
async def _handle_selflearning_status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Read-only status of the router self-learning loop for one agent.

    Everything here is derived from on-disk state the loop already writes
    (event store scan, train-state JSON, active pointer, receipts) plus a live
    gate evaluation — no side effects, no model loads, no training. This is
    the single source the Web UI status card and CLI doctor consume, so gate
    reason codes are surfaced verbatim for the client to localize.

    Params (optional): ``agentId`` (defaults to ``main``).
    """

    p = params if isinstance(params, dict) else {}
    agent_raw = p.get("agentId") or p.get("agent_id") or "main"
    agent_id = sanitize_token(agent_raw)
    if agent_id is None:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "agentId must be an id token")

    router_cfg = getattr(ctx.config, "squilla_router", None)
    sl_cfg = getattr(router_cfg, "self_learning", None)
    memory_cfg = getattr(ctx.config, "memory", None)
    dream_cfg = getattr(memory_cfg, "dream", None)

    enabled = bool(getattr(sl_cfg, "enabled", False))
    dream_enabled = bool(getattr(dream_cfg, "enabled", False))
    dream_scheduled = bool(getattr(dream_cfg, "auto_schedule", False))
    dream_killed = os.getenv("OPENSQUILLA_MEMORY_DREAM_DISABLED") == "1"

    payload: dict[str, Any] = {
        "agentId": agent_id,
        "enabled": enabled,
        "captureEnabled": enabled and bool(getattr(sl_cfg, "capture_enabled", True)),
        # The training trigger rides the dream cadence; false while
        # self-learning is on means samples accumulate but training never
        # runs. Mirrors the boot warning's condition, kill switch included.
        "trainingReachable": enabled and dream_enabled and dream_scheduled and not dream_killed,
        "dream": {
            "enabled": dream_enabled,
            "autoSchedule": dream_scheduled,
            "killSwitchActive": dream_killed,
        },
        "activeModel": {"kind": "baseline", "version": None, "promotedAt": None},
        "samples": None,
        "gate": None,
        "lastReceipt": None,
    }
    if not enabled:
        return payload

    def _collect_disk_state() -> dict[str, Any]:
        # Blocking file IO (event-store scan can be many JSONL files); runs in
        # a worker thread so the gateway event loop never stalls on it.
        from opensquilla.squilla_router.self_learning.gates import evaluate_training_gates
        from opensquilla.squilla_router.self_learning.promotion import read_active
        from opensquilla.squilla_router.self_learning.state import (
            load_train_state,
            scan_event_store,
        )
        from opensquilla.squilla_router.self_learning.store import (
            router_data_root,
            self_learning_disabled_by_env,
        )

        state = load_train_state(agent_id)
        stats = scan_event_store(agent_id)
        # Same feedback merge the orchestrator applies before ITS gate —
        # otherwise wouldTrain/reason here could contradict the actual run.
        from opensquilla.squilla_router.self_learning.gates import (
            merge_feedback_into_stats,
        )

        stats = merge_feedback_into_stats(stats, agent_id)
        gate = evaluate_training_gates(config=sl_cfg, state=state, stats=stats)

        out: dict[str, Any] = {}
        active = read_active()
        if active.startswith("learned/") and state.active_version:
            out["activeModel"] = {
                "kind": "learned",
                "version": state.active_version,
                "promotedAt": state.promoted_at,
            }
        from opensquilla.squilla_router.self_learning.feedback import scan_feedback_stats

        fb = scan_feedback_stats(agent_id)
        out["samples"] = {
            "total": stats.total,
            "highValue": stats.high_value,
            "requiredHighValue": gate.effective_min_samples,
            "distinctClasses": stats.distinct_classes,
            "complaintRate": round(stats.complaint_rate, 4),
            "lastCapturedAt": stats.last_ts,
            # Explicit thumbs feedback (F7). downSingle is the slice the
            # rollback monitor actually uses (ensemble ratings excluded).
            "feedback": {"up": fb.up, "down": fb.down, "downSingle": fb.down_single},
        }
        out["gate"] = {
            "wouldTrain": gate.should_train,
            "reason": gate.reason,
            "consecutiveFailures": state.consecutive_failures,
            "lastAttemptAt": state.last_attempt_ts,
            "lastTrainedAt": state.last_train_ts,
            "killSwitchActive": self_learning_disabled_by_env(),
        }

        receipts_dir = router_data_root() / ".receipts"
        if receipts_dir.is_dir():
            # Receipt names are "<agent_id>-<20-digit stamp>-<kind>.json"; the
            # stamp-digit anchor keeps agent "main" from matching another
            # agent's "main-backup-..." receipts on the shared prefix.
            name_re = re.compile(rf"^{re.escape(agent_id)}-\d{{14,20}}-[a-z_]+\.json$")
            candidates = [
                f for f in receipts_dir.glob(f"{agent_id}-*.json") if name_re.match(f.name)
            ]
            latest = max(candidates, key=lambda f: f.name, default=None)
            if latest is not None:
                import json as _json

                try:
                    receipt = _json.loads(latest.read_text(encoding="utf-8"))
                    out["lastReceipt"] = {
                        "kind": receipt.get("kind"),
                        "version": receipt.get("version"),
                        "reason": receipt.get("reason"),
                        "file": latest.name,
                    }
                except (OSError, ValueError):
                    out["lastReceipt"] = {"kind": "unreadable", "file": latest.name}
        return out

    try:
        import anyio

        payload.update(await anyio.to_thread.run_sync(_collect_disk_state))
    except Exception as exc:  # noqa: BLE001 — status must degrade, not error
        log.warning("router_selflearning.status_failed", agent_id=agent_id, error=str(exc))
        payload["error"] = "status_partial"

    return payload
