"""Cron job handlers — registered at gateway boot time."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

import structlog

from opensquilla.engine.stream_wrappers import wrap_stream
from opensquilla.engine.types import done_text_snapshot
from opensquilla.scheduler.delivery import (
    DeliveryChain,
    build_reply_rendezvous_envelope,
    strip_reply_directives,
)
from opensquilla.scheduler.heartbeat_loop import DEFAULT_HEARTBEAT_PROMPT
from opensquilla.scheduler.payloads import payload_agent_id, payload_text
from opensquilla.scheduler.types import (
    CronJob,
    CronWakeMode,
    DeliveryMode,
    HandlerResult,
    SessionTarget,
)
from opensquilla.session.keys import build_main_key
from opensquilla.session.terminal_reply import (
    build_terminal_reply,
    is_context_payload_too_large,
    sanitize_agent_error,
)
from opensquilla.tools.types import ToolContext

log = structlog.get_logger(__name__)

WorkspaceResolver = Callable[[str], tuple[str | None, bool]]
DefaultElevatedResolver = Callable[[], str | None]


def _resolve_default_elevated(
    default_elevated: str | DefaultElevatedResolver | None,
) -> str | None:
    return default_elevated() if callable(default_elevated) else default_elevated


def _resolve_session_key(job: CronJob) -> str:
    """Compute the session key based on the job's session_target."""
    target = (
        job.session_target
        if isinstance(job.session_target, SessionTarget)
        else SessionTarget(job.session_target)
    )
    match target:
        case SessionTarget.ISOLATED:
            run_id = uuid.uuid4().hex[:8]
            return f"cron:{job.id}:run:{run_id}"
        case SessionTarget.SESSION:
            return job.session_key or f"cron:{job.id}"
        case SessionTarget.MAIN:
            raise NotImplementedError(
                "MAIN target requires heartbeat mechanism, not yet implemented"
            )
        case SessionTarget.CURRENT:
            if job.session_key:
                return job.session_key
            if job.origin_session_key:
                return job.origin_session_key
            raise ValueError("CURRENT target requires a bound session key")
        case _:
            return f"cron:{job.id}"


def _required_delivery_error(job: CronJob, report: Any) -> str | None:
    """Return an error when required primary delivery failed."""
    if job.delivery.best_effort:
        return None
    mode = (
        job.delivery.mode
        if isinstance(job.delivery.mode, DeliveryMode)
        else DeliveryMode(job.delivery.mode)
    )
    channel_status = getattr(report, "channel_status", "skipped")
    session_status = getattr(report, "session_status", "skipped")
    if channel_status == "delivery_failed":
        return f"Cron job '{job.name}' delivery failed"
    if (
        mode in {DeliveryMode.CHANNEL, DeliveryMode.ORIGIN, DeliveryMode.WEBHOOK}
        and channel_status == "skipped"
    ):
        return f"Cron job '{job.name}' delivery was skipped"
    if session_status == "forward_failed":
        return f"Cron job '{job.name}' session delivery failed"
    return None


def _required_heartbeat_delivery_error(
    job: CronJob,
    delivery_override: dict[str, str] | None,
    hb_result: Any,
) -> str | None:
    """Return an error when pinned heartbeat delivery was required but failed."""
    if job.delivery.best_effort or delivery_override is None:
        return None
    hb_status = getattr(hb_result, "status", "")
    delivery_status = getattr(hb_result, "delivery_status", "")
    reason = getattr(hb_result, "reason", "")
    if delivery_status in {"delivery_failed", "forward_failed"}:
        return str(reason or delivery_status)
    if hb_status == "skipped":
        return str(reason or delivery_status or "delivery skipped")
    return None


def _build_cron_tool_context(
    agent_id: str,
    job: CronJob,
    *,
    session_key: str | None = None,
    workspace_resolver: WorkspaceResolver | None = None,
    default_elevated: str | DefaultElevatedResolver | None = None,
) -> ToolContext:
    from opensquilla.scheduler.routing import build_cron_route_envelope, tool_context_from_envelope

    resolved_session_key = session_key
    if resolved_session_key is None:
        target = (
            job.session_target
            if isinstance(job.session_target, SessionTarget)
            else SessionTarget(job.session_target)
        )
        resolved_session_key = (
            build_main_key(agent_id) if target == SessionTarget.MAIN else f"cron:{job.id}"
        )
    envelope = build_cron_route_envelope(
        job,
        session_key=resolved_session_key,
        agent_id=agent_id,
    )
    workspace_dir = None
    workspace_strict = False
    if workspace_resolver is not None:
        workspace_dir, workspace_strict = workspace_resolver(agent_id)
    return tool_context_from_envelope(
        envelope,
        is_owner=bool(getattr(job, "creator_is_owner", False)),
        workspace_dir=workspace_dir,
        workspace_strict=workspace_strict,
        default_elevated=_resolve_default_elevated(default_elevated),
    )


def _delivery_override_from_snapshot(job: CronJob) -> dict[str, str] | None:
    snapshot = job.delivery.originating_reply_target
    if snapshot is None or not snapshot.channel_name:
        return None
    return {
        "channel_name": snapshot.channel_name,
        "channel_id": snapshot.to,
        "account_id": snapshot.account_id,
        "thread_id": snapshot.thread_id,
    }


def _delivery_override_from_fields(job: CronJob) -> dict[str, str] | None:
    delivery = job.delivery
    if not delivery.channel_name:
        return None
    return {
        "channel_name": delivery.channel_name,
        "channel_id": delivery.channel_id,
        "account_id": delivery.account_id,
        "thread_id": delivery.thread_id,
    }


def _resolve_system_event_heartbeat_delivery_override(job: CronJob) -> dict[str, str] | None:
    """Resolve main-system-event delivery from persisted job fields only."""
    delivery = job.delivery
    if delivery.mode == DeliveryMode.CHANNEL:
        return _delivery_override_from_fields(job)
    snapshot_override = _delivery_override_from_snapshot(job)
    if snapshot_override is not None:
        return snapshot_override
    if delivery.mode == DeliveryMode.ORIGIN:
        return _delivery_override_from_fields(job)
    return None


def _event_payload(event: Any) -> dict[str, Any]:
    if is_dataclass(event):
        payload = asdict(event)  # type: ignore[arg-type]
    else:
        payload = {
            key: value
            for key, value in getattr(event, "__dict__", {}).items()
            if not key.startswith("_")
        }
    payload.pop("kind", None)
    return payload


def make_agent_run_handler(
    delivery_chain: DeliveryChain,
    turn_runner_ref: Callable[[], Any] | None = None,
    session_manager_ref: Callable[[], Any] | None = None,
    task_runtime_ref: Callable[[], Any] | None = None,
    workspace_resolver: WorkspaceResolver | None = None,
    default_elevated: str | DefaultElevatedResolver | None = None,
) -> Callable:
    """Factory: creates an agent_run_handler with explicit DI.

    Replaces the old pattern that used monkey-patched _scheduler._turn_runner.
    """

    async def agent_run_handler(job: CronJob) -> HandlerResult:
        session_key = _resolve_session_key(job)

        turn_runner = turn_runner_ref() if turn_runner_ref else None
        task_runtime = task_runtime_ref() if task_runtime_ref else None
        if turn_runner is None and task_runtime is None:
            log.error("agent_run_handler.no_turn_runner", job_id=job.id)
            raise RuntimeError("turn_runner not available")

        sm = session_manager_ref() if session_manager_ref else None
        task = payload_text(job.payload, job.session_target)
        agent_id = payload_agent_id(job.payload)

        if not task:
            log.warning("agent_run_handler.empty_task", job_id=job.id)
            return HandlerResult()

        # Session setup
        if sm is not None:
            try:
                await sm.get_or_create(
                    session_key=session_key,
                    agent_id=agent_id,
                    display_name=f"Cron: {job.name[:50]}",
                )
                _persisted = await sm.append_message(session_key, role="user", content=task)
                if _persisted is not None and isinstance(_persisted.content, str):
                    task = _persisted.content
            except Exception:
                log.warning(
                    "agent_run_handler.session_setup_failed",
                    job_id=job.id,
                    session_key=session_key,
                    exc_info=True,
                )

        # Emit cron.run.start (pre-execution notification, best-effort)
        await delivery_chain.notify_start(job, task)

        log.info(
            "agent_run_handler.start",
            job_id=job.id,
            task=task[:80],
            agent_id=agent_id,
            session_target=str(job.session_target),
            session_key=session_key,
        )

        # Agent execution
        collected_text: list[str] = []
        done_text_present = False
        done_text = ""
        success = True
        error_message: str | None = None
        result_text = ""
        summary: str | None = None
        try:
            if task_runtime is not None:
                from opensquilla.scheduler.routing import build_cron_route_envelope
                from opensquilla.session.models import AgentTaskStatus

                transcript_watermark = await _transcript_watermark(sm, session_key)
                route_envelope = build_cron_route_envelope(
                    job,
                    session_key=session_key,
                    agent_id=agent_id,
                )
                handle = await task_runtime.enqueue(
                    route_envelope,
                    task,
                    mode="followup",
                    run_kind="cron_turn",
                )
                try:
                    record = await task_runtime.wait(handle.task_id, timeout=job.timeout_seconds)
                except TimeoutError:
                    await _cancel_runtime_task(task_runtime, handle.task_id)
                    success = False
                    timeout_value = (
                        f"{job.timeout_seconds:g}"
                        if isinstance(job.timeout_seconds, (int, float))
                        else str(job.timeout_seconds)
                    )
                    error_message = f"Cron job '{job.name}' timed out after {timeout_value}s"
                    result_text = error_message
                    summary = result_text[:500]
                else:
                    success = getattr(record, "status", None) == AgentTaskStatus.SUCCEEDED
                    if success:
                        result_text = await _latest_assistant_text_after(
                            sm,
                            session_key,
                            transcript_watermark,
                        )
                    else:
                        error_message = (
                            getattr(record, "error_message", None)
                            or getattr(record, "terminal_reason", None)
                            or "Agent error"
                        )
                        if is_context_payload_too_large(record):
                            error_message = build_terminal_reply(record)
                        result_text = error_message or ""
                    summary = result_text[:500] if result_text else error_message
            else:
                assert turn_runner is not None
                tool_context = _build_cron_tool_context(
                    agent_id,
                    job,
                    session_key=session_key,
                    workspace_resolver=workspace_resolver,
                    default_elevated=default_elevated,
                )
                async for event in wrap_stream(
                    turn_runner.run(
                        message=task,
                        session_key=session_key,
                        tool_context=tool_context,
                        agent_id=agent_id,
                        timeout=job.timeout_seconds,
                        run_kind="cron_turn",
                        input_provenance={"kind": "cron_job", "job_id": job.id},
                    ),
                    idle_timeout=None,
                ):
                    event_kind = getattr(event, "kind", "")
                    if event_kind == "error":
                        success = False
                        error_message = getattr(event, "message", "Agent error") or "Agent error"
                        if is_context_payload_too_large(
                            {
                                "terminal_reason": getattr(event, "code", ""),
                                "error_class": getattr(event, "code", ""),
                                "error_message": error_message,
                            }
                        ):
                            error_message = build_terminal_reply(
                                {
                                    "terminal_reason": getattr(event, "code", ""),
                                    "error_class": getattr(event, "code", ""),
                                    "error_message": error_message,
                                }
                            )
                    elif event_kind == "done":
                        snapshot_present, snapshot_text = done_text_snapshot(event)
                        if snapshot_present:
                            done_text_present = True
                            done_text = snapshot_text
                    elif (
                        event_kind not in {"done", "state_change", "tool_use_start", "tool_result"}
                        and hasattr(event, "text")
                        and event.text
                    ):
                        collected_text.append(event.text)
                result_text = done_text if done_text_present else "".join(collected_text)
                if not success and not result_text:
                    result_text = error_message or ""
                summary = result_text[:500] if result_text else error_message
        except Exception as exc:
            _, error_message = sanitize_agent_error(
                {
                    "status": "failed",
                    "terminal_reason": "error",
                    "error_class": getattr(exc, "code", None) or type(exc).__name__,
                    "error_message": str(exc),
                },
                fallback_error_message=str(exc) or "Agent error",
            )
            result_text = f"Cron job '{job.name}' failed: {error_message}"
            summary = result_text[:500]
            success = False

        result_text = strip_reply_directives(result_text) or ""
        summary = strip_reply_directives(summary)

        # Delivery chain — Channel + WS + session forward in parallel
        report = await delivery_chain.deliver(
            job,
            result_text=result_text,
            success=success,
            summary=summary,
            session_key=session_key,
            route_envelope=build_reply_rendezvous_envelope(job, session_key),
        )

        # Signal failure to scheduler pipeline
        if not success:
            raise RuntimeError(error_message or result_text or f"Cron job '{job.name}' failed")
        delivery_error = _required_delivery_error(job, report)
        if delivery_error:
            raise RuntimeError(delivery_error)

        return HandlerResult(
            summary=summary,
            session_key=session_key,
            delivery_status=(
                f"{report.channel_status}|ws:{report.ws_status}|fwd:{report.session_status}"
            ),
        )

    return agent_run_handler


def make_static_message_handler(delivery_chain: DeliveryChain) -> Callable:
    """Factory for reminder cron jobs that only deliver static text."""

    async def static_message_handler(job: CronJob) -> HandlerResult:
        session_key = _resolve_session_key(job)
        text = payload_text(job.payload, job.session_target)
        if not text.strip():
            log.warning("static_message_handler.empty_text", job_id=job.id)
            return HandlerResult(session_key=session_key)

        await delivery_chain.notify_start(job, text)
        log.info(
            "static_message_handler.start",
            job_id=job.id,
            session_target=str(job.session_target),
            session_key=session_key,
        )
        report = await delivery_chain.deliver(
            job,
            result_text=text,
            success=True,
            summary=text[:500],
            session_key=session_key,
            route_envelope=build_reply_rendezvous_envelope(job, session_key),
        )
        delivery_error = _required_delivery_error(job, report)
        if delivery_error:
            raise RuntimeError(delivery_error)
        return HandlerResult(
            summary=text[:500],
            session_key=session_key,
            delivery_status=(
                f"{report.channel_status}|ws:{report.ws_status}|fwd:{report.session_status}"
            ),
        )

    return static_message_handler


async def _read_transcript_rows(sm: Any, session_key: str) -> list[Any]:
    if sm is None:
        return []
    read_transcript = getattr(sm, "read_transcript", None)
    if not callable(read_transcript):
        return []
    try:
        rows = await read_transcript(session_key)
    except Exception:
        log.warning("agent_run_handler.read_transcript_failed", session_key=session_key)
        return []
    return list(rows or [])


async def _transcript_watermark(sm: Any, session_key: str) -> int:
    return len(await _read_transcript_rows(sm, session_key))


async def _latest_assistant_text_after(sm: Any, session_key: str, start_index: int) -> str:
    rows = await _read_transcript_rows(sm, session_key)
    for row in reversed(rows[start_index:]):
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        content = row.get("content") if isinstance(row, dict) else getattr(row, "content", None)
        if role == "assistant" and isinstance(content, str):
            return content
    return ""


async def _latest_assistant_text(sm: Any, session_key: str) -> str:
    return await _latest_assistant_text_after(sm, session_key, 0)


async def _cancel_runtime_task(task_runtime: Any, task_id: str) -> None:
    cancel = getattr(task_runtime, "cancel", None)
    if not callable(cancel):
        return
    try:
        await cancel(task_id=task_id)
    except Exception:
        log.warning("agent_run_handler.runtime_cancel_failed", task_id=task_id, exc_info=True)


def make_system_event_handler(
    delivery_chain: DeliveryChain,
    turn_runner_ref: Callable[[], Any] | None = None,
    session_manager_ref: Callable[[], Any] | None = None,
    session_event_emitter: Callable[[str, str, dict[str, Any]], Any] | None = None,
    heartbeat_service_ref: Callable[[], Any] | None = None,
    heartbeat_loop_ref: Callable[[], Any] | None = None,
    workspace_resolver: WorkspaceResolver | None = None,
    default_elevated: str | DefaultElevatedResolver | None = None,
    wake_now_busy_max_wait_seconds: float = 120.0,
    wake_now_busy_retry_delay_seconds: float = 0.25,
) -> Callable:
    """Factory for main-session system event cron jobs."""

    async def system_event_handler(job: CronJob) -> HandlerResult:
        sm = session_manager_ref() if session_manager_ref else None
        text = payload_text(job.payload, job.session_target)
        agent_id = payload_agent_id(job.payload)
        session_key = job.session_key or build_main_key(agent_id)

        if not text:
            log.warning("system_event_handler.empty_text", job_id=job.id)
            return HandlerResult(session_key=session_key)

        if sm is not None:
            await sm.get_or_create(
                session_key=session_key,
                agent_id=agent_id,
                display_name="Main Session",
            )
            await sm.append_message(
                session_key,
                role="system",
                content=text,
                provenance={
                    "kind": "cron",
                    "source_tool": f"cron:{job.id}",
                },
        )

        await delivery_chain.notify_start(job, text)
        heartbeat_loop = heartbeat_loop_ref() if heartbeat_loop_ref else None
        reason = f"cron:{job.id}"
        wake_mode = getattr(job.wake_mode, "value", str(job.wake_mode or CronWakeMode.NOW))
        delivery_override = _resolve_system_event_heartbeat_delivery_override(job)

        if wake_mode == CronWakeMode.NEXT_HEARTBEAT.value:
            await _request_heartbeat_now(
                heartbeat_loop,
                reason=reason,
                agent_id=agent_id,
                session_key=session_key,
            )
            if session_event_emitter is not None:
                await session_event_emitter(
                    session_key,
                    "sessions.changed",
                    {"key": session_key, "reason": "cron_system_event"},
                )
            return HandlerResult(
                summary=text,
                session_key=session_key,
                delivery_status="not-requested",
            )

        heartbeat_service = heartbeat_service_ref() if heartbeat_service_ref else None
        if heartbeat_service is None:
            raise RuntimeError("heartbeat_service not available")

        tool_context = _build_cron_tool_context(
            agent_id,
            job,
            session_key=session_key,
            workspace_resolver=workspace_resolver,
            default_elevated=default_elevated,
        )
        heartbeat_kwargs: dict[str, Any] = {
            "reason": reason,
            "agent_id": agent_id,
            "session_key": session_key,
            "prompt": DEFAULT_HEARTBEAT_PROMPT,
            "target": "last",
            "tool_context": tool_context,
            "timeout": job.timeout_seconds,
        }
        if delivery_override is not None:
            heartbeat_kwargs["delivery_override"] = delivery_override
        run_once_now = getattr(heartbeat_loop, "run_once_now", None)
        if callable(run_once_now):
            async def _run_once():
                run_once_kwargs: dict[str, Any] = {
                    "reason": reason,
                    "agent_id": agent_id,
                    "session_key": session_key,
                    "target": "last",
                    "tool_context": tool_context,
                    "timeout": job.timeout_seconds,
                }
                if delivery_override is not None:
                    run_once_kwargs["delivery_override"] = delivery_override
                return await run_once_now(**run_once_kwargs)

        else:
            async def _run_once():
                return await heartbeat_service.run_once(**heartbeat_kwargs)

        hb_result, busy_fallback = await _run_heartbeat_now_with_busy_retry(
            _run_once,
            heartbeat_loop=heartbeat_loop,
            reason=reason,
            agent_id=agent_id,
            session_key=session_key,
            max_wait_seconds=wake_now_busy_max_wait_seconds,
            retry_delay_seconds=wake_now_busy_retry_delay_seconds,
        )

        hb_status = getattr(hb_result, "status", "")
        hb_reason = getattr(hb_result, "reason", "")
        if hb_status == "failed":
            raise RuntimeError(hb_reason or "heartbeat failed")
        if not busy_fallback:
            delivery_error = _required_heartbeat_delivery_error(
                job,
                delivery_override,
                hb_result,
            )
            if delivery_error:
                raise RuntimeError(delivery_error)

        if session_event_emitter is not None:
            await session_event_emitter(
                session_key,
                "sessions.changed",
                {"key": session_key, "reason": "cron_system_event"},
            )

        return HandlerResult(
            summary=text,
            session_key=session_key,
            delivery_status=(
                "not-requested"
                if busy_fallback
                else getattr(hb_result, "delivery_status", "") or hb_status or "skipped"
            ),
        )

    return system_event_handler


async def _run_heartbeat_now_with_busy_retry(
    run_once: Callable[[], Any],
    *,
    heartbeat_loop: Any,
    reason: str,
    agent_id: str,
    session_key: str,
    max_wait_seconds: float,
    retry_delay_seconds: float,
) -> tuple[Any, bool]:
    started = time.monotonic()
    max_wait = max(0.0, max_wait_seconds)
    retry_delay = max(0.0, retry_delay_seconds)

    while True:
        result = await run_once()
        if (
            getattr(result, "status", "") != "skipped"
            or getattr(result, "reason", "") != "requests-in-flight"
        ):
            return result, False

        if time.monotonic() - started >= max_wait:
            await _request_heartbeat_now(
                heartbeat_loop,
                reason=reason,
                agent_id=agent_id,
                session_key=session_key,
            )
            return result, True

        await asyncio.sleep(retry_delay)


async def _request_heartbeat_now(
    heartbeat_loop: Any,
    *,
    reason: str,
    agent_id: str,
    session_key: str,
) -> None:
    if heartbeat_loop is None:
        return
    request_now = getattr(heartbeat_loop, "request_now", None)
    if callable(request_now):
        result = request_now(reason=reason, agent_id=agent_id, session_key=session_key)
        if inspect.isawaitable(result):
            await result
        return
    nudge = getattr(heartbeat_loop, "nudge", None)
    if callable(nudge):
        result = nudge()
        if inspect.isawaitable(result):
            await result
