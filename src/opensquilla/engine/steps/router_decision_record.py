"""Router decision-record hook: stage at routing time, flush at turn end.

The squilla-router step stages one sanitized decision record per routed user
message (``stage_router_decision``); the turn runner flushes it to the
registered :class:`~opensquilla.persistence.router_decision_writer.RouterDecisionWriter`
when the per-turn decision log is emitted (``schedule_router_decision_flush``,
which hands the SQLite insert to a worker thread so the event loop never
blocks on a contended WAL commit). The
flush is deliberately late so the persisted ``executed_kind`` /
``ensemble_profile`` / ``fallback_hops`` describe what actually executed —
a record must never name a model that did not run.

The writer is registered process-wide via :func:`set_decision_writer`
(mirroring ``provider.model_catalog.set_shared_catalog``); gateway boot
installs it only when the session DB is real (not ``:memory:``). With no
writer registered every function here is a no-op, so the router step's
behavior is unchanged on CLI/standalone paths.

Privacy: records carry tier/route-class/model tokens, numbers, and booleans
only. The trail is rebuilt here from ``routing_extra`` through an explicit
whitelist — prompt text, complaint terms, and error strings never enter the
record — and the writer re-sanitizes every JSON column on insert.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.asyncio_utils import create_background_task

if TYPE_CHECKING:
    from opensquilla.persistence.router_decision_writer import RouterDecisionWriter

log = structlog.get_logger(__name__)

# Private turn-metadata keys (same convention as the deferred-history keys in
# engine/steps/squilla_router.py).
PENDING_RECORD_KEY = "_pending_router_decision_record"
DECISION_ID_METADATA_KEY = "router_decision_id"
FALLBACK_HOPS_METADATA_KEY = "router_fallback_hops"

_decision_writer: RouterDecisionWriter | None = None

# In-flight fire-and-forget flush tasks. Tracked so shutdown can drain them
# before the writer's connection closes — otherwise a turn finishing near
# shutdown loses its record (task cancelled) or writes on a closed connection.
_pending_flush_tasks: set[asyncio.Task[None]] = set()


def set_decision_writer(writer: RouterDecisionWriter | None) -> None:
    """Install (or, with ``None``, clear) the process-wide decision writer."""
    global _decision_writer
    _decision_writer = writer


def get_decision_writer() -> RouterDecisionWriter | None:
    return _decision_writer


async def drain_pending_flushes(timeout: float = 2.0) -> None:
    """Wait briefly for in-flight decision flushes, cancelling stragglers.

    Called at gateway shutdown before the writer's connection is closed.
    Best-effort like the flushes themselves: a straggler past *timeout* is
    cancelled (its record is lost, which the fail-open contract permits)
    rather than left to race the connection close.
    """
    pending = {task for task in _pending_flush_tasks if not task.done()}
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        log.warning(
            "router_decision_record.drain_timeout",
            cancelled=len(still_pending),
            flushed=len(done),
        )


# ---------------------------------------------------------------------------
# Trail construction (enum tokens + numbers + booleans only)
# ---------------------------------------------------------------------------


def _token(value: object) -> str | None:
    from opensquilla.persistence.router_decision_writer import sanitize_token

    return sanitize_token(value)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return None
    return as_float


def _trail_entry(stage: str, **fields: object) -> dict[str, Any]:
    entry: dict[str, Any] = {"stage": stage}
    for key, value in fields.items():
        if value is None:
            continue
        entry[key] = value
    return entry


def build_trail(extra: Mapping[str, Any], *, final_tier: str | None) -> list[dict[str, Any]]:
    """Rebuild the policy-stage trail from ``routing_extra`` as safe entries.

    Only whitelisted fields are read; every string is token-sanitized and
    every other value is a boolean or a number. Free-text fields present in
    ``routing_extra`` (complaint terms, classifier errors, prompt hints) are
    intentionally never copied.
    """
    trail: list[dict[str, Any]] = []
    base_tier = _token(extra.get("base_tier"))
    if base_tier is not None:
        trail.append(
            _trail_entry(
                "classify",
                tier=base_tier,
                route_class=_token(extra.get("route_class")),
            )
        )
    if "confidence_gate_applied" in extra:
        trail.append(
            _trail_entry(
                "confidence_gate",
                applied=bool(extra.get("confidence_gate_applied")),
                threshold=_number(extra.get("confidence_threshold")),
                default_tier=_token(extra.get("confidence_default_tier")),
            )
        )
    if "complaint_upgrade_applied" in extra:
        # terms_count only — the matched terms echo user wording.
        terms = extra.get("complaint_terms")
        trail.append(
            _trail_entry(
                "complaint_upgrade",
                applied=bool(extra.get("complaint_upgrade_applied")),
                terms_count=len(terms) if isinstance(terms, (list, tuple)) else 0,
            )
        )
    if "anti_downgrade_applied" in extra:
        trail.append(
            _trail_entry(
                "anti_downgrade",
                applied=bool(extra.get("anti_downgrade_applied")),
                previous_tier=_token(extra.get("previous_tier")),
                window_seconds=_number(extra.get("kv_cache_window_seconds")),
            )
        )
    if extra.get("large_context_floor_applied"):
        trail.append(
            _trail_entry(
                "large_context_floor",
                applied=True,
                from_tier=_token(extra.get("large_context_floor_from_tier")),
                min_tier=_token(extra.get("large_context_floor_min_tier")),
                material_tokens=_number(extra.get("large_context_material_tokens")),
            )
        )
    final_token = _token(extra.get("final_tier")) or _token(final_tier)
    if final_token is not None:
        trail.append(
            _trail_entry(
                "final",
                tier=final_token,
                route_class=_token(extra.get("final_route_class")),
            )
        )
    return trail


# ---------------------------------------------------------------------------
# Stage (router step) / flush (turn finalize)
# ---------------------------------------------------------------------------


def stage_router_decision(
    ctx: Any,
    *,
    decision: Any,
    routing_extra: Mapping[str, Any] | None = None,
) -> None:
    """Stage one decision record on the turn if a writer is registered.

    Called by ``apply_squilla_router`` after the decision is finalized. Never
    raises; with no writer registered it is a no-op. The record is completed
    and handed to the writer by :func:`flush_router_decision` at turn end.
    """
    writer = _decision_writer
    if writer is None:
        return
    try:
        extra: Mapping[str, Any] = routing_extra if isinstance(routing_extra, Mapping) else {}
        metadata: dict[str, Any] = ctx.metadata
        decision_id = uuid.uuid4().hex
        turn_index: int | None = None
        history = metadata.get("routing_history")
        if isinstance(history, list) and history:
            last = history[-1]
            if isinstance(last, dict) and isinstance(last.get("turn_index"), int):
                turn_index = last["turn_index"]
        record: dict[str, Any] = {
            "decision_id": decision_id,
            "session_key": str(ctx.session_key),
            "turn_index": turn_index,
            "ts_ms": int(time.time() * 1000),
            "classifier": _token(extra.get("model_version")),
            "proposed_tier": _token(extra.get("base_tier")) or _token(decision.tier),
            "confidence": _number(getattr(decision, "confidence", None)),
            "probs": extra.get("probabilities"),
            "flags": extra.get("flags"),
            "final_tier": _token(extra.get("final_tier")) or _token(decision.tier),
            "requested_provider": _token(metadata.get("routed_provider")),
            "requested_model": _token(getattr(decision, "model", None)),
            "provider": _token(metadata.get("routed_provider")),
            "model": _token(getattr(decision, "model", None)),
            "thinking_level": _token(metadata.get("thinking_level")),
            "source": _token(getattr(decision, "source", None)),
            "trail": build_trail(extra, final_tier=getattr(decision, "tier", None)),
            "baseline_model": _token(metadata.get("baseline_model")),
            # C2: today's display value, verbatim — savings math untouched.
            "savings_pct": _number(metadata.get("savings_pct")),
        }
        metadata[DECISION_ID_METADATA_KEY] = decision_id
        metadata[PENDING_RECORD_KEY] = record
    except Exception:  # noqa: BLE001 — decision records must never fail a turn
        log.warning("router_decision_record.stage_failed", exc_info=True)


def _complete_pending_record(
    metadata: dict[str, Any],
    *,
    ensemble_trace: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], RouterDecisionWriter] | None:
    """Pop the staged record and stamp executed facts (cheap dict work only).

    Returns ``(record, writer)`` ready for the SQLite insert, or ``None``
    when nothing was staged or no writer is registered. Never raises; the
    pop-once guard makes repeated calls no-ops.
    """
    record: Any = None
    try:
        record = metadata.pop(PENDING_RECORD_KEY, None)
    except Exception:  # noqa: BLE001 — tolerate read-only mappings
        return None
    writer = _decision_writer
    if not isinstance(record, dict) or writer is None:
        return None
    try:
        ensemble_enabled = bool(metadata.get("ensemble_enabled"))
        record["executed_kind"] = "ensemble" if ensemble_enabled else "single"
        profile = None
        if ensemble_enabled and isinstance(ensemble_trace, Mapping):
            profile = _token(ensemble_trace.get("profile"))
        record["ensemble_profile"] = profile
        record["fallback_hops"] = int(metadata.get(FALLBACK_HOPS_METADATA_KEY) or 0)
        executed_provider = _token(metadata.get("executed_provider"))
        executed_model = _token(metadata.get("executed_model"))
        if ensemble_enabled and isinstance(ensemble_trace, Mapping):
            final_request = ensemble_trace.get("final_request")
            execution = (
                final_request.get("execution")
                if isinstance(final_request, Mapping)
                else None
            )
            if isinstance(execution, Mapping):
                executed_provider = _token(execution.get("provider")) or executed_provider
                executed_model = _token(execution.get("model")) or executed_model
        if executed_provider is None:
            executed_provider = (
                _token(metadata.get("routed_provider_applied"))
                or _token(metadata.get("routed_provider_fallback_provider"))
                or _token(record.get("requested_provider"))
                or _token(record.get("provider"))
            )
        if executed_model is None:
            executed_model = (
                _token(metadata.get("routed_provider_fallback_model"))
                or _token(metadata.get("routed_model"))
                or _token(record.get("requested_model"))
                or _token(record.get("model"))
            )
        record["executed_provider"] = executed_provider
        record["executed_model"] = executed_model
        fallback_reason = _token(metadata.get("routed_provider_fallback_reason"))
        if fallback_reason is None and record["fallback_hops"]:
            fallback_reason = _token(metadata.get("router_fallback_reason")) or "selector_fallback"
        if (
            fallback_reason is None
            and ensemble_enabled
            and isinstance(ensemble_trace, Mapping)
            and bool(ensemble_trace.get("fallback_used"))
        ):
            fallback_reason = (
                _token(ensemble_trace.get("fallback_code"))
                or _token(ensemble_trace.get("fallback_reason"))
                or "ensemble_fallback_single"
            )
        record["fallback_reason"] = fallback_reason
        if not ensemble_enabled and executed_model is not None:
            # Preserve the legacy model column's existing executed-model
            # semantics while the additive requested/executed fields expose
            # both sides explicitly.
            record["model"] = executed_model
    except Exception:  # noqa: BLE001 — decision records must never fail a turn
        log.warning("router_decision_record.flush_failed", exc_info=True)
        return None
    return record, writer


def _write_record(writer: RouterDecisionWriter, record: dict[str, Any]) -> None:
    try:
        writer.record_decision(record)
    except Exception:  # noqa: BLE001 — decision records must never fail a turn
        log.warning("router_decision_record.flush_failed", exc_info=True)


async def _write_record_off_loop(writer: RouterDecisionWriter, record: dict[str, Any]) -> None:
    try:
        await asyncio.to_thread(writer.record_decision, record)
    except Exception:  # noqa: BLE001 — decision records must never fail a turn
        log.warning("router_decision_record.flush_failed", exc_info=True)


def flush_router_decision(
    metadata: dict[str, Any],
    *,
    ensemble_trace: Mapping[str, Any] | None = None,
) -> None:
    """Complete the staged record with executed facts and hand it to the writer.

    ``executed_kind`` is ``"ensemble"`` only when the runtime actually wrapped
    the turn (``metadata["ensemble_enabled"]``, stamped in
    ``engine/runtime.py`` next to ``routed_model_before_ensemble``);
    ``ensemble_profile`` comes from the ensemble trace of the final
    ``DoneEvent``. ``fallback_hops`` counts selector fallbacks actually taken.
    Never raises; a pop-once guard makes repeated calls no-ops.

    The SQLite insert runs inline on the calling thread. Event-loop callers
    must use :func:`schedule_router_decision_flush` instead — the writer
    commits under ``busy_timeout=5000`` and a contended WAL write would
    otherwise stall the whole loop.
    """
    completed = _complete_pending_record(metadata, ensemble_trace=ensemble_trace)
    if completed is None:
        return
    record, writer = completed
    _write_record(writer, record)


def schedule_router_decision_flush(
    metadata: dict[str, Any],
    *,
    ensemble_trace: Mapping[str, Any] | None = None,
) -> asyncio.Task[None] | None:
    """Flush like :func:`flush_router_decision`, but keep SQLite off the loop.

    The staged record is completed synchronously (pop-once semantics are
    identical), then the insert — a WAL commit under ``busy_timeout=5000``
    plus the periodic retention DELETE — is handed to a worker thread as a
    fire-and-forget background task, so a contended write can never freeze
    the turn-finalize path. Persistence stays best-effort: failures are
    logged, never raised. Returns the scheduled task so tests can await it;
    returns ``None`` when nothing was staged or, with no running loop, after
    falling back to the inline write.
    """
    completed = _complete_pending_record(metadata, ensemble_trace=ensemble_trace)
    if completed is None:
        return None
    record, writer = completed
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No event loop to protect (sync CLI/standalone callers): write inline.
        _write_record(writer, record)
        return None
    task = create_background_task(_write_record_off_loop(writer, record))
    if isinstance(task, asyncio.Task):
        _pending_flush_tasks.add(task)
        task.add_done_callback(_pending_flush_tasks.discard)
        return task
    return None


# ---------------------------------------------------------------------------
# Boot-time rehydration of the sticky/anti-downgrade history
# ---------------------------------------------------------------------------


def rehydrate_history_from_writer(
    writer: RouterDecisionWriter,
    *,
    window_seconds: int = 1800,
    per_session: int = 5,
) -> int:
    """Seed ``RoutingHistoryStore`` from persisted decisions (one query).

    Best-effort: returns the number of sessions seeded, 0 on any failure.
    Persisted ``ts_ms`` wall-clock ages are mapped onto the current monotonic
    clock because the history window filter compares ``_ts`` against
    ``time.monotonic()``.
    """
    try:
        from opensquilla.engine.routing.policy import route_class_for_tier
        from opensquilla.engine.steps.squilla_router import seed_routing_history
        from opensquilla.router_tiers import normalize_text_tier

        grouped = writer.load_recent_history(
            window_seconds=window_seconds,
            per_session=per_session,
        )
        if not grouped:
            return 0
        now_ms = int(time.time() * 1000)
        now_mono = time.monotonic()
        entries_by_session: dict[str, list[dict[str, Any]]] = {}
        for session_key, rows in grouped.items():
            entries: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                final_tier = normalize_text_tier(row.get("final_tier"))
                proposed_tier = normalize_text_tier(row.get("proposed_tier"))
                if final_tier is None and proposed_tier is None:
                    continue
                age_seconds = max(0.0, (now_ms - int(row.get("ts_ms") or 0)) / 1000.0)
                turn_index = row.get("turn_index")
                entry: dict[str, Any] = {
                    "turn_index": turn_index if isinstance(turn_index, int) else index,
                    "_ts": max(0.0, now_mono - age_seconds),
                    "base_tier": proposed_tier or final_tier,
                    "final_tier": final_tier or proposed_tier,
                    "route_class": route_class_for_tier(proposed_tier or final_tier or ""),
                    "final_route_class": route_class_for_tier(final_tier or proposed_tier or ""),
                    "rehydrated": True,
                }
                entries.append(entry)
            if entries:
                entries_by_session[session_key] = entries
        return seed_routing_history(entries_by_session)
    except Exception:  # noqa: BLE001 — rehydration must never block boot
        log.warning("router_decision_record.rehydrate_failed", exc_info=True)
        return 0
