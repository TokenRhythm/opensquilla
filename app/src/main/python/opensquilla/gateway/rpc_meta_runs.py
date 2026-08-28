"""Meta-skill run history RPC handlers."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from opensquilla.engine.steps.meta_command import (
    format_meta_replay_sentinel,
    pending_meta_launch_put,
    pending_meta_replay_put,
)
from opensquilla.gateway.protocol import (
    ERROR_INVALID_REQUEST,
    ERROR_NOT_FOUND,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
)
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, WRITE_SCOPE
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.persistence.meta_run_query import parse_since_ms
from opensquilla.persistence.meta_run_writer import (
    RunRecord,
    StepRecord,
    replay_inputs_are_modified,
    summarize_run_record,
)
from opensquilla.session.storage import (
    MetaControlIntentConflictError,
    MetaLaunchDraftCapacityError,
    MetaLaunchDraftConflictError,
    MetaLaunchDraftDiscardedError,
    MetaLaunchDraftUnavailableError,
    normalize_meta_launch_coordinates,
)
from opensquilla.skills.hub.deps import install_deps
from opensquilla.skills.meta.author_seed import draft_meta_skill_seed
from opensquilla.skills.meta.enabled import is_meta_skill_enabled
from opensquilla.skills.meta.readiness import (
    MetaSkillReadiness,
    assess_meta_skill_readiness,
    format_meta_setup_error,
    meta_readiness_context,
)
from opensquilla.skills.meta.replay_safety import (
    paid_fresh_run_block_reason,
    paid_live_replay_block_reason,
)
from opensquilla.skills.meta.run_reports import (
    build_cost_summary,
    build_eval_baseline,
    build_recovery_events,
    build_replay_request,
    build_run_diff,
    build_validation_availability,
    build_validation_summary,
    confirmation_message,
    deserialize_plan,
    filter_template_fields,
    json_object,
    missing_required_fields,
)

_d = get_dispatcher()
_META_DRAFT_LIST_TIMEOUT_SECONDS = 3.0


@dataclass
class _MetaSetupJob:
    """In-memory status for an explicitly confirmed dependency setup."""

    id: str
    name: str
    session_key: str
    action_ids: tuple[str, ...]
    status: str = "queued"
    phase: str = "queued"
    message: str = ""
    current_action: str = ""
    downloaded_bytes: int = 0
    download_total_bytes: int = 0
    completed_actions: list[str] = field(default_factory=list)
    error: str = ""
    started_at_ms: int = 0
    finished_at_ms: int = 0
    readiness: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "name": self.name,
            "sessionKey": self.session_key,
            "action_ids": list(self.action_ids),
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "current_action": self.current_action,
            "downloaded_bytes": self.downloaded_bytes,
            "download_total_bytes": self.download_total_bytes,
            "completed_actions": list(self.completed_actions),
            "error": self.error,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "readiness": self.readiness,
        }


_META_SETUP_JOBS: dict[str, _MetaSetupJob] = {}
_META_SETUP_LATEST: dict[tuple[str, str], str] = {}
_META_SETUP_TASKS: set[asyncio.Task[None]] = set()
_META_SETUP_JOB_TTL_MS = 60 * 60 * 1000
_META_SETUP_JOB_LIMIT = 64
_META_SETUP_ACTIVE_JOB_LIMIT = 4

_META_REPLAY_TICKET_TTL_SECONDS = 30.0
_META_REPLAY_TICKET_LIMIT = 128
_META_REPLAY_LIVE_MODES = frozenset({"failed-step", "partial-context"})
@dataclass(frozen=True)
class _MetaReplayTicket:
    """Short-lived capability used to commit one live replay launch."""

    token: str
    session_key: str
    run_id: str
    meta_skill_name: str
    mode: str
    expires_at: float


_META_REPLAY_TICKET_LOCK = threading.Lock()
_META_REPLAY_TICKETS: dict[str, _MetaReplayTicket] = {}


def _writer_from_context(ctx: RpcContext) -> Any:
    writer = getattr(ctx, "meta_run_writer", None)
    if writer is not None:
        return writer
    raise RpcUnavailableError("meta run writer is not configured")


def _serialize_record(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "meta_skill_digest": record.meta_skill_digest,
        "plan_snapshot_json": record.plan_snapshot_json,
        "triggered_by": record.triggered_by,
        "session_key": record.session_key,
        "turn_id": record.turn_id,
        "owner_pid": record.owner_pid,
        "status": record.status,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "inputs_json": record.inputs_json,
        "final_text": record.final_text,
        "failed_step_id": record.failed_step_id,
        "error": record.error,
        "truncated_fields": list(record.truncated_fields),
        "steps": [_serialize_step(step) for step in record.steps],
        "summary": summarize_run_record(record),
    }


def _serialize_record_summary(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "triggered_by": record.triggered_by,
        "session_key": record.session_key,
        "turn_id": record.turn_id,
        "status": record.status,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "failed_step_id": record.failed_step_id,
        "error_present": bool(record.error),
        "truncated_fields": list(record.truncated_fields),
        "summary": summarize_run_record(record),
        "validation": build_validation_availability(record),
    }


def _serialize_step(step: StepRecord) -> dict[str, Any]:
    return {
        "run_id": step.run_id,
        "step_id": step.step_id,
        "step_kind": step.step_kind,
        "declared_skill": step.declared_skill,
        "effective_skill": step.effective_skill,
        "status": step.status,
        "started_at_ms": step.started_at_ms,
        "ended_at_ms": step.ended_at_ms,
        "rendered_inputs_json": step.rendered_inputs_json,
        "output_text": step.output_text,
        "error": step.error,
        "substitute_step_id": step.substitute_step_id,
        "truncated_fields": list(step.truncated_fields),
    }


def _hydrate_records(writer: Any, rows: list[RunRecord]) -> list[RunRecord]:
    hydrate = getattr(writer, "hydrate_runs", None)
    if callable(hydrate):
        return list(hydrate(rows))
    return rows


def _bounded_limit(value: Any, *, default: int = 50, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < 1:
        return default
    return min(parsed, maximum)


def _parse_since_param(value: Any) -> int | None:
    if value is None:
        return None
    return parse_since_ms(str(value))


def _session_key_for_history(params: dict[str, Any], ctx: RpcContext) -> str | None:
    session_key = params.get("sessionKey") or params.get("session_key")
    if ADMIN_SCOPE in ctx.principal.scopes or ctx.principal.is_owner:
        if session_key:
            return str(session_key)
        return None
    if session_key:
        return str(session_key)
    raise RpcHandlerError(
        ERROR_UNAUTHORIZED,
        "meta run history requires a sessionKey for read-only access.",
    )


def _existing_specs(ctx: RpcContext) -> list[Any]:
    loader = getattr(ctx, "skill_loader", None)
    if loader is None:
        return []
    try:
        return list(loader.load_all())
    except Exception:  # noqa: BLE001 - draft conflict detection is advisory
        return []


async def _record_or_404(writer: Any, run_id: str) -> RunRecord:
    # The writer is sync sqlite (busy_timeout=5000); keep its reads off
    # the event loop so a contended commit cannot stall the gateway.
    record = await asyncio.to_thread(writer.get_run, run_id)
    if record is None:
        raise RpcHandlerError(ERROR_NOT_FOUND, f"meta run not found: {run_id}")
    return cast(RunRecord, record)


def _run_id_param(params: dict[str, Any]) -> str:
    run_id = str(params.get("runId") or params.get("run_id") or "")
    if not run_id:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "runId is required")
    return run_id


def _prune_meta_replay_tickets_locked(now: float) -> None:
    expired = [
        token for token, ticket in _META_REPLAY_TICKETS.items()
        if ticket.expires_at <= now
    ]
    for token in expired:
        _META_REPLAY_TICKETS.pop(token, None)
    while len(_META_REPLAY_TICKETS) >= _META_REPLAY_TICKET_LIMIT:
        oldest = next(iter(_META_REPLAY_TICKETS), None)
        if oldest is None:
            break
        _META_REPLAY_TICKETS.pop(oldest, None)


def _issue_meta_replay_ticket(
    *,
    session_key: str,
    record: RunRecord,
    mode: str,
) -> _MetaReplayTicket:
    now = time.monotonic()
    ticket = _MetaReplayTicket(
        token=uuid.uuid4().hex,
        session_key=session_key,
        run_id=record.run_id,
        meta_skill_name=record.meta_skill_name,
        mode=mode,
        expires_at=now + _META_REPLAY_TICKET_TTL_SECONDS,
    )
    with _META_REPLAY_TICKET_LOCK:
        _prune_meta_replay_tickets_locked(now)
        _META_REPLAY_TICKETS[ticket.token] = ticket
    return ticket


def _consume_meta_replay_ticket(
    token: str,
    *,
    session_key: str,
    run_id: str,
    mode: str,
) -> _MetaReplayTicket | None:
    """Consume only an unexpired ticket with the exact bound coordinates.

    A forged or cross-session attempt does not burn a legitimate owner's
    ticket.  A successful claim removes it atomically, making reuse fail.
    """

    now = time.monotonic()
    with _META_REPLAY_TICKET_LOCK:
        _prune_meta_replay_tickets_locked(now)
        ticket = _META_REPLAY_TICKETS.get(token)
        if ticket is None:
            return None
        if (
            ticket.session_key != session_key
            or ticket.run_id != run_id
            or ticket.mode != mode
        ):
            return None
        return _META_REPLAY_TICKETS.pop(token, None)


def _live_replay_session_key(params: dict[str, Any], record: RunRecord) -> str:
    session_key = str(params.get("sessionKey") or params.get("session_key") or "").strip()
    if not session_key:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "sessionKey is required for live replay",
        )
    if record.session_key and record.session_key != session_key:
        raise RpcHandlerError(
            ERROR_UNAUTHORIZED,
            "the replay run does not belong to this session",
        )
    return session_key


def _validate_live_replay_record(record: RunRecord, mode: str) -> None:
    if mode not in _META_REPLAY_LIVE_MODES:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "live replay mode must be failed-step or partial-context",
        )
    if record.status != "failed" or not record.failed_step_id:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "only a failed meta-skill run can resume from a failed step",
        )
    if replay_inputs_are_modified(record):
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "This run cannot safely retry only the failed step because its saved "
            "request was redacted or truncated. Start a new meta-skill run and "
            "provide the original request again.",
        )
    try:
        plan = deserialize_plan(record)
    except Exception as exc:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "This run's saved plan cannot be validated for a safe live replay.",
        ) from exc
    paid_block = paid_live_replay_block_reason(
        plan=plan,
        persisted_steps=record.steps,
        failed_step_id=str(record.failed_step_id or ""),
    )
    if paid_block:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, paid_block)


@_d.method("meta.runs.list", scope="operator.read")
async def _handle_meta_runs_list(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    session_key = _session_key_for_history(p, ctx)
    rows = await asyncio.to_thread(
        writer.list_runs,
        name=p.get("name"),
        status=p.get("status"),
        session_key=session_key,
        since_ms=_parse_since_param(p.get("since")),
        limit=_bounded_limit(p.get("limit")),
    )
    hydrated = await asyncio.to_thread(_hydrate_records, writer, rows)
    return {"runs": [_serialize_record_summary(row) for row in hydrated]}


@_d.method("meta.runs.show", scope="operator.admin")
async def _handle_meta_runs_show(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    run_id = str(p.get("runId") or p.get("run_id") or "")
    record = await asyncio.to_thread(writer.get_run, run_id)
    if record is None:
        return {"run": None}
    return {"run": _serialize_record(record)}


@_d.method("meta.runs.failures", scope="operator.read")
async def _handle_meta_runs_failures(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    session_key = _session_key_for_history(p, ctx)
    rows = await asyncio.to_thread(
        writer.list_failures,
        name=p.get("name"),
        session_key=session_key,
        since_ms=_parse_since_param(p.get("since")),
        limit=_bounded_limit(p.get("limit")),
    )
    hydrated = await asyncio.to_thread(_hydrate_records, writer, rows)
    return {"runs": [_serialize_record_summary(row) for row in hydrated]}


@_d.method("meta.runs.recovery", scope="operator.admin")
async def _handle_meta_runs_recovery(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Return the latest unresolved failed-run ribbon for one session.

    Live ``session.event.meta_*`` frames are not transcript content, so a UI
    restart cannot reconstruct its recovery card from ``chat.history`` alone.
    The persisted meta-run ledger is the source of truth for this reconnect
    read model. A later replay supersedes its source run via lineage metadata.
    """

    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    session_key = str(p.get("sessionKey") or p.get("session_key") or "").strip()
    if not session_key:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "sessionKey is required for meta run recovery",
        )

    rows = await asyncio.to_thread(
        writer.list_runs,
        session_key=session_key,
        limit=_bounded_limit(p.get("limit"), default=50, maximum=100),
    )
    hydrated = await asyncio.to_thread(_hydrate_records, writer, rows)
    rows_by_id = {row.run_id: row for row in hydrated}
    superseded_run_ids: set[str] = set()
    for descendant in hydrated:
        lineage = json_object(descendant.inputs_json)
        source_run_id = str(lineage.get("meta_replay_source_run_id") or "").strip()
        mode = str(lineage.get("meta_replay_mode") or "").strip()
        source = rows_by_id.get(source_run_id)
        if (
            source is None
            or source.run_id == descendant.run_id
            or source.session_key != session_key
            or descendant.session_key != session_key
            or source.meta_skill_name != descendant.meta_skill_name
            or (source.started_at_ms, source.run_id)
            >= (descendant.started_at_ms, descendant.run_id)
            or mode not in _META_REPLAY_LIVE_MODES
            or descendant.status in {"cancelled", "expired"}
            or (
                descendant.status == "failed"
                and build_recovery_events(descendant) is None
            )
        ):
            continue
        superseded_run_ids.add(source.run_id)
    for row in hydrated:
        if row.run_id in superseded_run_ids:
            continue
        recovery = build_recovery_events(row)
        if recovery is not None:
            return {"recovery": recovery}
    return {"recovery": None}


@_d.method("meta.runs.draft", scope="operator.admin")
async def _handle_meta_runs_draft(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    run_id = str(p.get("runId") or p.get("run_id") or "")
    record = await asyncio.to_thread(writer.get_run, run_id)
    if record is None:
        return {"draft": None}
    return {
        "draft": draft_meta_skill_seed(
            record,
            existing_specs=_existing_specs(ctx),
        ),
    }


@_d.method("meta.runs.confirm_preflight", scope="operator.admin")
async def _handle_meta_runs_confirm_preflight(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    record = await _record_or_404(writer, _run_id_param(p))
    plan = deserialize_plan(record)
    fields_raw = p.get("fields") or p.get("confirmedFields") or {}
    fields = (
        filter_template_fields(plan.request_template, dict(fields_raw))
        if isinstance(fields_raw, dict)
        else {}
    )
    missing = missing_required_fields(plan.request_template, fields)
    if missing:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            f"required preflight fields are missing: {', '.join(missing)}",
            details={"missing_fields": missing},
        )
    interpreted_request = str(p.get("interpretedRequest") or p.get("interpreted_request") or "")
    return {
        "confirmed": True,
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "fields": fields,
        "message": confirmation_message(
            record=record,
            interpreted_request=interpreted_request,
            fields=fields,
        ),
    }


@_d.method("meta.runs.diff", scope="operator.admin")
async def _handle_meta_runs_diff(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    left_id = str(p.get("leftRunId") or p.get("left_run_id") or "")
    right_id = str(p.get("rightRunId") or p.get("right_run_id") or "")
    if not left_id or not right_id:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "leftRunId and rightRunId are required")
    return {
        "diff": build_run_diff(
            await _record_or_404(writer, left_id),
            await _record_or_404(writer, right_id),
        )
    }


@_d.method("meta.runs.replay", scope="operator.admin")
async def _handle_meta_runs_replay(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    record = await _record_or_404(writer, _run_id_param(p))
    mode = str(p.get("mode") or "run")
    replay = build_replay_request(record, mode=mode)

    # Backward-compatible inspection path: callers that do not opt into a
    # live replay still receive an actionable, bounded replay description.
    # Its ``message`` is a canonical /meta command, never model prose.
    prepare_live = bool(p.get("prepareLive") or p.get("prepare_live"))
    replay_token = str(p.get("replayToken") or p.get("replay_token") or "").strip()
    if not prepare_live and not replay_token:
        if mode == "run":
            try:
                plan = deserialize_plan(record)
            except Exception:
                plan = None
            block_reason = (
                paid_fresh_run_block_reason(plan=plan, persisted_steps=record.steps)
                if plan is not None
                else "The saved plan cannot be validated for a safe fresh retry."
            )
            if block_reason:
                # Inspection remains available, but never hand a one-click
                # client a canonical /meta command that could repeat billing.
                replay["message"] = ""
                replay["replay_kind"] = "blocked-paid-fresh-run"
                replay["live_replay"] = {
                    "available": False,
                    "reason": block_reason,
                }
        return {"replay": replay}

    session_key = _live_replay_session_key(p, record)
    _validate_live_replay_record(record, mode)

    if prepare_live:
        issued_ticket = _issue_meta_replay_ticket(
            session_key=session_key,
            record=record,
            mode=mode,
        )
        replay["replay_kind"] = "live-prepared"
        replay["live_replay"] = {
            "available": True,
            "prepared": True,
            "replay_token": issued_ticket.token,
            "expires_in_ms": int(_META_REPLAY_TICKET_TTL_SECONDS * 1000),
        }
        return {"replay": replay}

    consumed_ticket = _consume_meta_replay_ticket(
        replay_token,
        session_key=session_key,
        run_id=record.run_id,
        mode=mode,
    )
    if (
        consumed_ticket is None
        or consumed_ticket.meta_skill_name != record.meta_skill_name
    ):
        raise RpcHandlerError(
            ERROR_UNAUTHORIZED,
            "the replay authorization expired, was already used, or does not match",
        )

    # The capability token ends here. Only a nonce-bound sentinel enters
    # chat.send, so the token can never be persisted in transcript/history or
    # exposed to the provider. Production gateways stage its exact binding in
    # the session database; the in-process store remains a compatibility path
    # for storage-less embeddings and test doubles.
    replay_nonce = uuid.uuid4().hex
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    stage_control = getattr(storage, "stage_meta_control_intent", None)
    if callable(stage_control):
        try:
            await stage_control(
                session_key=session_key,
                control_kind="replay",
                correlation_id=f"nonce:{replay_nonce}",
                meta_skill_name=record.meta_skill_name,
                replay_run_id=record.run_id,
                replay_mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed after token consumption
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "could not durably stage the replay turn; prepare it again",
            ) from exc
    else:
        replay_nonce = pending_meta_replay_put(
            session_key,
            run_id=record.run_id,
            name=record.meta_skill_name,
            mode=mode,
        )
    if not replay_nonce:  # Defensive: validated coordinates should make this unreachable.
        raise RpcHandlerError(ERROR_UNAVAILABLE, "could not stage the replay turn")
    replay["replay_kind"] = "live-committed"
    replay["launch_text"] = format_meta_replay_sentinel(replay_nonce)
    replay["display_text"] = (
        f"Retry failed step · {record.meta_skill_name}"
        if mode == "failed-step"
        else f"Retry with partial context · {record.meta_skill_name}"
    )
    replay["live_replay"] = {
        "available": True,
        "prepared": False,
        "committed": True,
    }
    return {"replay": replay}


@_d.method("meta.runs.cost", scope="operator.read")
async def _handle_meta_runs_cost(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    session_key = _session_key_for_history(p, ctx)
    rows = await asyncio.to_thread(
        writer.list_runs,
        name=p.get("name"),
        status=p.get("status"),
        session_key=session_key,
        since_ms=_parse_since_param(p.get("since")),
        limit=_bounded_limit(p.get("limit")),
    )
    return build_cost_summary(await asyncio.to_thread(_hydrate_records, writer, rows))


@_d.method("meta.runs.validate", scope="operator.admin")
async def _handle_meta_runs_validate(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    record = await _record_or_404(writer, _run_id_param(p))
    return {"validation": build_validation_summary(record)}


@_d.method("meta.runs.eval_baseline", scope="operator.admin")
async def _handle_meta_runs_eval_baseline(params: Any, ctx: RpcContext) -> dict[str, Any]:
    writer = _writer_from_context(ctx)
    p = params if isinstance(params, dict) else {}
    record = await _record_or_404(writer, _run_id_param(p))
    return {"baseline": build_eval_baseline(record)}


def _meta_setup_plan(name: str, ctx: RpcContext) -> tuple[MetaSkillReadiness, dict[str, Any]]:
    """Resolve one invokable meta-skill and its current recursive readiness."""

    specs = _existing_specs(ctx)
    skill_index = {spec.name: spec for spec in specs}
    spec = skill_index.get(name)
    if (
        spec is None
        or getattr(spec, "kind", "skill") != "meta"
        or getattr(spec, "disable_model_invocation", False)
    ):
        raise RpcHandlerError(ERROR_NOT_FOUND, f"meta-skill not found: {name}")
    return (
        assess_meta_skill_readiness(
            spec,
            skill_index=skill_index,
            ctx=meta_readiness_context(config=getattr(ctx, "config", None)),
            config=getattr(ctx, "config", None),
        ),
        skill_index,
    )


def _prune_meta_setup_jobs() -> None:
    now_ms = int(time.time() * 1000)
    removable = [
        job_id
        for job_id, job in _META_SETUP_JOBS.items()
        if job.finished_at_ms and now_ms - job.finished_at_ms > _META_SETUP_JOB_TTL_MS
    ]
    if len(_META_SETUP_JOBS) - len(removable) > _META_SETUP_JOB_LIMIT:
        finished = sorted(
            (
                job.finished_at_ms,
                job_id,
            )
            for job_id, job in _META_SETUP_JOBS.items()
            if job.finished_at_ms and job_id not in removable
        )
        extra = len(_META_SETUP_JOBS) - len(removable) - _META_SETUP_JOB_LIMIT
        removable.extend(job_id for _, job_id in finished[:extra])
    for job_id in removable:
        job = _META_SETUP_JOBS.pop(job_id, None)
        if job is None:
            continue
        key = (job.name, job.session_key)
        if _META_SETUP_LATEST.get(key) == job_id:
            _META_SETUP_LATEST.pop(key, None)


def _setup_job_for_request(params: dict[str, Any]) -> _MetaSetupJob:
    job_id = str(params.get("jobId") or params.get("job_id") or "")
    name = str(params.get("name") or "")
    session_key = str(params.get("sessionKey") or params.get("session_key") or "")
    if not session_key:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "sessionKey is required")
    if not job_id:
        if not name:
            raise RpcHandlerError(ERROR_INVALID_REQUEST, "jobId or name is required")
        job_id = _META_SETUP_LATEST.get((name, session_key), "")
    job = _META_SETUP_JOBS.get(job_id)
    if job is None or job.session_key != session_key:
        raise RpcHandlerError(ERROR_NOT_FOUND, "meta setup job not found")
    return job


def _active_meta_setup_job_count() -> int:
    return sum(
        job.status in {"queued", "running"} for job in _META_SETUP_JOBS.values()
    )


async def _run_meta_setup_job(
    job: _MetaSetupJob,
    *,
    ctx: RpcContext,
    actions_by_id: dict[str, Any],
) -> None:
    job.status = "running"
    job.phase = "installing"
    job.started_at_ms = int(time.time() * 1000)
    try:
        loader = getattr(ctx, "skill_loader", None)
        if loader is None:
            raise RuntimeError("Skill loader is unavailable")
        for action_id in job.action_ids:
            action = actions_by_id[action_id]
            job.current_action = action_id
            job.message = action.label
            job.downloaded_bytes = 0
            job.download_total_bytes = 0
            owner = loader.get_by_name(action.skill)
            if owner is None or owner.metadata is None:
                raise RuntimeError(f"Setup owner is unavailable: {action.skill}")
            install = next(
                (item for item in owner.metadata.install if item.id == action.install_id),
                None,
            )
            if install is None:
                raise RuntimeError(f"Setup action changed or disappeared: {action_id}")
            accepting_progress = True

            def record_progress(_spec: Any, current: int, total: int) -> None:
                # Toolchain downloads run in a worker thread. Integer assignment is
                # atomic under CPython, and status snapshots tolerate seeing either
                # side of two adjacent assignments.
                if (
                    not accepting_progress
                    or job.status != "running"
                    or job.current_action != action_id
                ):
                    return
                job.downloaded_bytes = max(0, int(current))
                job.download_total_bytes = max(0, int(total))

            try:
                results = await install_deps([install], progress_cb=record_progress)
            finally:
                # asyncio.to_thread cannot stop its worker after a timeout or
                # cancellation. Close this callback before terminalizing the job
                # so a late download update cannot rewrite the final snapshot.
                accepting_progress = False
            result = results[0]
            if not result.success:
                raise RuntimeError(result.message or f"Setup action failed: {action_id}")
            job.completed_actions.append(action_id)

        job.phase = "verifying"
        job.message = "Verifying installed capabilities"
        from opensquilla.engine.steps.skills_filter import invalidate_skill_eligibility_cache

        invalidate_skill_eligibility_cache()
        readiness, _ = await asyncio.to_thread(_meta_setup_plan, job.name, ctx)
        job.readiness = readiness.to_dict()
        if readiness.ready:
            job.status = "completed"
            job.phase = "completed"
            job.message = "Setup complete"
        else:
            job.status = "blocked"
            job.phase = "blocked"
            job.error = format_meta_setup_error(job.name, readiness)
            job.message = "Setup finished, but additional requirements remain"
    except asyncio.CancelledError:
        job.status = "failed"
        job.phase = "failed"
        job.error = "Setup was interrupted"
        raise
    except Exception as exc:  # noqa: BLE001 - status must retain installer failures
        job.status = "failed"
        job.phase = "failed"
        job.error = str(exc) or type(exc).__name__
        job.message = "Setup failed"
    finally:
        job.current_action = ""
        job.finished_at_ms = int(time.time() * 1000)


@_d.method("meta.setup.plan", scope="operator.read")
async def _handle_meta_setup_plan(params: Any, ctx: RpcContext) -> dict[str, Any]:
    p = params if isinstance(params, dict) else {}
    name = str(p.get("name") or "")
    if not name:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "name is required")
    if not is_meta_skill_enabled(ctx.config):
        return {"ok": False, "disabled": True, "error": "meta-skills are disabled"}
    # The setup surface must use the same capability probe as launch. Merely
    # checking that binary names exist creates a dead end when a system TeX or
    # FFmpeg installation lacks the required packages, filters, or fonts: the
    # launch blocks but the UI would otherwise offer no managed repair action.
    readiness, _ = await asyncio.to_thread(_meta_setup_plan, name, ctx)
    return {"ok": True, "name": name, "readiness": readiness.to_dict()}


@_d.method("meta.setup.install", scope="operator.admin")
async def _handle_meta_setup_install(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Start an explicitly confirmed setup in the background."""

    p = params if isinstance(params, dict) else {}
    name = str(p.get("name") or "")
    session_key = str(p.get("sessionKey") or p.get("session_key") or "")
    if not name:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "name is required")
    if not session_key:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "sessionKey is required")
    if p.get("confirmed") is not True:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "confirmed=true is required")
    if not is_meta_skill_enabled(ctx.config):
        return {"ok": False, "disabled": True, "error": "meta-skills are disabled"}

    _prune_meta_setup_jobs()
    existing_id = _META_SETUP_LATEST.get((name, session_key))
    existing = _META_SETUP_JOBS.get(existing_id or "")
    if existing is not None and existing.status in {"queued", "running"}:
        return {"ok": True, "job": existing.to_dict(), "reused": True}
    if _active_meta_setup_job_count() >= _META_SETUP_ACTIVE_JOB_LIMIT:
        raise RpcHandlerError(
            ERROR_UNAVAILABLE,
            "Meta setup capacity is full; wait for an active setup to finish and retry.",
        )

    readiness, _ = await asyncio.to_thread(_meta_setup_plan, name, ctx)
    # Readiness runs outside the event loop. A same-session request may have
    # created its job while this request was awaiting the worker thread. Reuse
    # that job before applying the global capacity check or creating another
    # installer for the same launch.
    existing_id = _META_SETUP_LATEST.get((name, session_key))
    existing = _META_SETUP_JOBS.get(existing_id or "")
    if existing is not None and existing.status in {"queued", "running"}:
        return {"ok": True, "job": existing.to_dict(), "reused": True}
    if readiness.ready:
        return {"ok": True, "already_ready": True, "readiness": readiness.to_dict()}
    actions_by_id = {action.id: action for action in readiness.setup_actions}
    requested_raw = p.get("action_ids")
    if requested_raw is None:
        requested = [action.id for action in readiness.setup_actions if action.available]
    elif isinstance(requested_raw, list):
        requested = [str(value) for value in requested_raw]
    else:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "action_ids must be a list")
    if not requested:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "No installable setup action is available")
    unknown = [action_id for action_id in requested if action_id not in actions_by_id]
    unavailable = [
        action_id
        for action_id in requested
        if action_id in actions_by_id and not actions_by_id[action_id].available
    ]
    if unknown:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            f"Unknown setup action: {', '.join(unknown)}",
        )
    if unavailable:
        detail = "; ".join(
            f"{action_id}: {actions_by_id[action_id].reason}" for action_id in unavailable
        )
        raise RpcHandlerError(ERROR_INVALID_REQUEST, f"Setup action unavailable: {detail}")
    # Planning runs in a worker thread. Recheck after that await so concurrent
    # requests cannot all observe the same free slot and overfill the cap.
    if _active_meta_setup_job_count() >= _META_SETUP_ACTIVE_JOB_LIMIT:
        raise RpcHandlerError(
            ERROR_UNAVAILABLE,
            "Meta setup capacity is full; wait for an active setup to finish and retry.",
        )

    job = _MetaSetupJob(
        id=uuid.uuid4().hex,
        name=name,
        session_key=session_key,
        action_ids=tuple(dict.fromkeys(requested)),
        readiness=readiness.to_dict(),
    )
    _META_SETUP_JOBS[job.id] = job
    _META_SETUP_LATEST[(name, session_key)] = job.id
    task = asyncio.create_task(_run_meta_setup_job(job, ctx=ctx, actions_by_id=actions_by_id))
    _META_SETUP_TASKS.add(task)
    task.add_done_callback(_META_SETUP_TASKS.discard)
    return {"ok": True, "job": job.to_dict(), "reused": False}


@_d.method("meta.setup.status", scope="operator.read")
async def _handle_meta_setup_status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    p = params if isinstance(params, dict) else {}
    _prune_meta_setup_jobs()
    return {"ok": True, "job": _setup_job_for_request(p).to_dict()}


@_d.method("meta.list", scope="operator.read")
async def _handle_meta_list(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Enumerate invokable meta-skills for manual-invocation surfaces.

    Gated by the master ``meta_skill.enabled`` flag: when disabled the surface
    receives an explicit empty list rather than a partial enumeration. Skills
    are filtered to launchable ``kind == "meta"`` entries and sorted by name
    for stable ordering across calls.
    """
    if not is_meta_skill_enabled(ctx.config):
        return {"skills": [], "disabled": True}
    def project_skills() -> list[dict[str, Any]]:
        specs = _existing_specs(ctx)
        skill_index = {spec.name: spec for spec in specs}
        skills = []
        for spec in specs:
            if getattr(spec, "kind", "skill") != "meta":
                continue
            if getattr(spec, "disable_model_invocation", False):
                continue
            readiness = assess_meta_skill_readiness(
                spec,
                skill_index=skill_index,
                ctx=meta_readiness_context(config=getattr(ctx, "config", None)),
                verify_capabilities=False,
                config=getattr(ctx, "config", None),
            )
            skills.append({
                "name": spec.name,
                "description": getattr(spec, "description", ""),
                **readiness.to_dict(),
            })
        skills.sort(key=lambda skill: skill["name"])
        return skills

    return {"skills": await asyncio.to_thread(project_skills)}


def _require_meta_draft_owner(ctx: RpcContext) -> None:
    if ctx.principal.is_owner or ADMIN_SCOPE in ctx.principal.scopes:
        return
    raise RpcHandlerError(
        ERROR_UNAUTHORIZED,
        "MetaSkill draft recovery requires the local owner or an administrator",
    )


@_d.method("meta.drafts.list", scope=WRITE_SCOPE)
async def _handle_meta_drafts_list(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Return live, unaccepted launch drafts for crash/app-restart recovery."""

    _require_meta_draft_owner(ctx)
    p = params if isinstance(params, dict) else {}
    session_key = str(p.get("sessionKey") or p.get("key") or "").strip()
    agent_id = str(p.get("agentId") or p.get("agent_id") or "").strip()
    if not session_key and not agent_id:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "sessionKey or agentId is required")
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    list_drafts = getattr(storage, "list_meta_launch_drafts", None)
    if not callable(list_drafts):
        return {"ok": True, "drafts": [], "durable": False}
    try:
        async with asyncio.timeout(_META_DRAFT_LIST_TIMEOUT_SECONDS):
            drafts = await list_drafts(
                session_key=session_key or None,
                agent_id=agent_id or None,
                # Agent-wide recovery exists only to find provisional draft chats that
                # have no sessions row or URL yet. Filtering in SQL prevents existing
                # chats from starving that bounded page and avoids disclosing their raw
                # prompts through a broad query.
                provisional_only=bool(agent_id and not session_key),
            )
            get_session = getattr(storage, "get_session", None)
            projected: list[dict[str, Any]] = []
            for draft in drafts:
                session_exists = (
                    bool(await get_session(draft.session_key))
                    if callable(get_session)
                    else True
                )
                projected.append({
                    "sessionKey": draft.session_key,
                    "clientRequestId": draft.client_request_id,
                    "name": draft.meta_skill_name,
                    "launchText": draft.launch_text,
                    "createdAt": draft.created_at,
                    "expiresAt": draft.expires_at,
                    "sessionExists": session_exists,
                })
    except TimeoutError as exc:
        raise RpcHandlerError(
            ERROR_UNAVAILABLE,
            "MetaSkill draft recovery timed out; retry after reconnecting",
            retryable=True,
            retry_after_ms=1_000,
        ) from exc
    return {
        "ok": True,
        "durable": True,
        "drafts": projected,
    }


@_d.method("meta.drafts.discard", scope=WRITE_SCOPE)
async def _handle_meta_drafts_discard(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Forget one launch only after an explicit user discard."""

    _require_meta_draft_owner(ctx)
    p = params if isinstance(params, dict) else {}
    raw_session_key = p.get("sessionKey") or p.get("key") or ""
    raw_client_request_id = (
        p.get("clientRequestId") or p.get("client_request_id") or ""
    )
    try:
        session_key, client_request_id = normalize_meta_launch_coordinates(
            raw_session_key,
            raw_client_request_id,
        )
    except ValueError as exc:
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "sessionKey and clientRequestId must be valid bounded identifiers",
        ) from exc
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    discard_draft = getattr(storage, "discard_meta_launch_draft", None)
    if callable(discard_draft):
        try:
            discarded = bool(
                await discard_draft(
                    session_key=session_key,
                    client_request_id=client_request_id,
                )
            )
        except MetaLaunchDraftCapacityError as exc:
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill cancellation could not be retained safely; retry later",
            ) from exc
        accepted = not discarded
    else:
        discarded = False
        accepted = False
    return {
        "ok": True,
        "discarded": discarded,
        # Valid coordinates that cannot be discarded have crossed the
        # acceptance boundary. Older clients remain fail-closed on the existing
        # ``discarded`` flag; newer clients can explain why no draft is restored.
        "accepted": accepted,
    }


@_d.method("meta.run", scope="operator.write")
async def _handle_meta_run(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Stamp a pending meta-skill launch for the ``/meta`` command surface.

    This RPC does NOT start a turn. It validates that ``name`` is an
    invokable meta-skill, records a one-shot pending launch keyed by the
    caller's session, and returns. The surface then sends a normal turn;
    the ``meta_command_launch`` pipeline step pops the pending launch and
    seeds ``ctx.metadata["meta_launch"]`` so the agent dispatches it.

    Gated by the master ``meta_skill.enabled`` flag: when disabled the
    surface receives an explicit refusal rather than a stamped launch.
    """
    p = params if isinstance(params, dict) else {}
    raw_name = str(p.get("name") or "").strip()
    # Command surfaces may pass their raw argument string. Only the first
    # token identifies the skill; the user request travels on the hidden turn
    # where it is bound to the one-shot launch marker.
    name = raw_name.split(None, 1)[0] if raw_name else ""
    session_key = str(p.get("sessionKey") or p.get("key") or "")
    raw_client_request_id = p.get(
        "clientRequestId",
        p.get("client_request_id"),
    )
    client_request_id: str | None
    if raw_client_request_id is None:
        client_request_id = None
    elif not isinstance(raw_client_request_id, str) or not raw_client_request_id.strip():
        raise RpcHandlerError(
            ERROR_INVALID_REQUEST,
            "clientRequestId must be a non-empty string",
        )
    else:
        client_request_id = raw_client_request_id.strip()
        if len(client_request_id) > 256:
            raise RpcHandlerError(
                ERROR_INVALID_REQUEST,
                "clientRequestId must not exceed 256 characters",
            )
    raw_launch_text = p.get("launchText", p.get("launch_text"))
    launch_text: str | None
    if raw_launch_text is None:
        launch_text = None
    elif not isinstance(raw_launch_text, str) or not raw_launch_text:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "launchText must be a non-empty string")
    else:
        launch_text = raw_launch_text
        if len(launch_text) > 128_000:
            raise RpcHandlerError(
                ERROR_INVALID_REQUEST,
                "launchText must not exceed 128000 characters",
            )
        if client_request_id is None:
            raise RpcHandlerError(
                ERROR_INVALID_REQUEST,
                "launchText requires clientRequestId",
            )
    if not name:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "name is required")
    if not session_key:
        raise RpcHandlerError(ERROR_INVALID_REQUEST, "sessionKey is required")
    if client_request_id is not None:
        try:
            session_key, client_request_id = normalize_meta_launch_coordinates(
                session_key,
                client_request_id,
            )
        except ValueError as exc:
            raise RpcHandlerError(
                ERROR_INVALID_REQUEST,
                "sessionKey and clientRequestId must be valid bounded identifiers",
            ) from exc

    if not is_meta_skill_enabled(ctx.config):
        return {"ok": False, "error": "meta-skills are disabled", "disabled": True}

    specs = await asyncio.to_thread(_existing_specs, ctx)
    skill_index = {spec.name: spec for spec in specs}
    invokable_spec = None
    for spec in specs:
        if getattr(spec, "name", None) != name:
            continue
        if getattr(spec, "kind", "skill") != "meta":
            continue
        if getattr(spec, "disable_model_invocation", False):
            continue
        invokable_spec = spec
        break
    if invokable_spec is None:
        return {"ok": False, "error": f"{name!r} is not an available meta-skill"}

    storage = get_session_storage(getattr(ctx, "session_manager", None))
    is_discarded = getattr(storage, "is_meta_launch_discarded", None)
    if client_request_id is not None and callable(is_discarded):
        try:
            request_was_discarded = bool(
                await is_discarded(
                    session_key=session_key,
                    client_request_id=client_request_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - cancellation checks fail closed
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill cancellation state could not be checked; retry shortly",
                retryable=True,
                accepted=False,
            ) from exc
        if request_was_discarded:
            raise RpcHandlerError(
                "META_DRAFT_DISCARDED",
                "This MetaSkill request was explicitly discarded and cannot be resumed",
                retryable=False,
                accepted=False,
            )
    stage_draft = getattr(storage, "stage_meta_launch_draft", None)
    draft_disposition: str | None = None
    if launch_text is not None and client_request_id is not None:
        if not callable(stage_draft):
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill request recovery is unavailable; retry after Gateway recovery",
                retryable=True,
                accepted=False,
            )
        try:
            _draft, draft_disposition = await stage_draft(
                session_key=session_key,
                client_request_id=client_request_id,
                meta_skill_name=name,
                launch_text=launch_text,
            )
        except MetaLaunchDraftDiscardedError as exc:
            raise RpcHandlerError(
                "META_DRAFT_DISCARDED",
                "This MetaSkill request was explicitly discarded and cannot be resumed",
                retryable=False,
                accepted=False,
            ) from exc
        except (MetaLaunchDraftConflictError, ValueError) as exc:
            raise RpcHandlerError(
                "IDEMPOTENCY_CONFLICT",
                "clientRequestId was already used for a different MetaSkill request",
                retryable=False,
                accepted=False,
            ) from exc
        except MetaLaunchDraftCapacityError as exc:
            raise RpcHandlerError(
                "META_DRAFT_OUTBOX_FULL",
                "Too many MetaSkill requests are awaiting completion; discard one or retry later",
                retryable=True,
                retry_after_ms=1000,
                accepted=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - request durability must fail closed
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill request could not be saved durably; retry shortly",
                retryable=True,
                accepted=False,
            ) from exc

    readiness = await asyncio.to_thread(
        assess_meta_skill_readiness,
        invokable_spec,
        skill_index=skill_index,
        ctx=meta_readiness_context(config=getattr(ctx, "config", None)),
        config=getattr(ctx, "config", None),
    )
    if not readiness.ready:
        return {
            "ok": False,
            "name": name,
            "sessionKey": session_key,
            "code": "META_SKILL_SETUP_REQUIRED",
            "setup_required": True,
            "drafted": draft_disposition is not None,
            "readiness": readiness.to_dict(),
            "error": format_meta_setup_error(name, readiness),
        }

    stage_control = getattr(storage, "stage_meta_control_intent", None)
    promote_draft = getattr(storage, "promote_meta_launch_draft", None)
    if launch_text is not None and client_request_id is not None:
        if not callable(promote_draft):
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill request promotion is unavailable; retry after Gateway recovery",
                retryable=True,
                accepted=False,
            )
        try:
            _intent, launch_disposition = await promote_draft(
                session_key=session_key,
                client_request_id=client_request_id,
                meta_skill_name=name,
                launch_text=launch_text,
            )
        except MetaLaunchDraftDiscardedError as exc:
            raise RpcHandlerError(
                "META_DRAFT_DISCARDED",
                "This MetaSkill request was explicitly discarded and cannot be resumed",
                retryable=False,
                accepted=False,
            ) from exc
        except MetaLaunchDraftUnavailableError as exc:
            raise RpcHandlerError(
                "META_DRAFT_UNAVAILABLE",
                "The saved MetaSkill request was discarded or expired before launch",
                retryable=False,
                accepted=False,
            ) from exc
        except MetaLaunchDraftConflictError as exc:
            raise RpcHandlerError(
                "IDEMPOTENCY_CONFLICT",
                "clientRequestId was already used for a different MetaSkill request",
                retryable=False,
                accepted=False,
            ) from exc
        except MetaControlIntentConflictError as exc:
            raise RpcHandlerError(
                "IDEMPOTENCY_CONFLICT",
                "clientRequestId was already used for a different meta-skill launch",
                retryable=False,
                accepted=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - authorization must be durable
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill launch could not be staged durably; retry shortly",
                retryable=True,
                accepted=False,
            ) from exc
    elif client_request_id is not None and callable(stage_control):
        try:
            _intent, launch_disposition = await stage_control(
                session_key=session_key,
                control_kind="manual",
                correlation_id=f"request:{client_request_id}",
                meta_skill_name=name,
            )
        except MetaLaunchDraftDiscardedError as exc:
            raise RpcHandlerError(
                "META_DRAFT_DISCARDED",
                "This MetaSkill request was explicitly discarded and cannot be resumed",
                retryable=False,
                accepted=False,
            ) from exc
        except MetaControlIntentConflictError as exc:
            raise RpcHandlerError(
                "IDEMPOTENCY_CONFLICT",
                "clientRequestId was already used for a different meta-skill launch",
                retryable=False,
                accepted=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - authorization must be durable
            raise RpcHandlerError(
                ERROR_UNAVAILABLE,
                "MetaSkill launch could not be staged durably; retry shortly",
                retryable=True,
                accepted=False,
            ) from exc
    else:
        launch_disposition = pending_meta_launch_put(
            session_key,
            name,
            client_request_id=client_request_id,
        )
    if launch_disposition == "conflict":
        raise RpcHandlerError(
            "IDEMPOTENCY_CONFLICT",
            "clientRequestId was already used for a different meta-skill launch",
            retryable=False,
            accepted=False,
        )
    if launch_disposition == "capacity":
        raise RpcHandlerError(
            "META_LAUNCH_BUSY",
            "Too many meta-skill launches are awaiting their turns; retry shortly",
            retryable=True,
            retry_after_ms=1000,
            accepted=False,
        )

    result: dict[str, Any] = {
        "ok": True,
        "name": name,
        "sessionKey": session_key,
    }
    if client_request_id is not None:
        result["clientRequestId"] = client_request_id
        result["replayed"] = launch_disposition == "replayed"
    if draft_disposition is not None:
        result["drafted"] = True
    return result
