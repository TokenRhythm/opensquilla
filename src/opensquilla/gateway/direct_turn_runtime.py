"""Execute an accepted direct turn against the existing runner and event stream."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.artifacts import enrich_artifact_event_dict
from opensquilla.engine.stream_wrappers import is_context_bound_owner, wrap_stream
from opensquilla.engine.types import AnswerGenerationResetEvent
from opensquilla.gateway.config import GatewayConfig, effective_agent_stream_idle_timeout_seconds
from opensquilla.gateway.project_workspace_runtime import (
    AcceptedRunModeOverride,
    apply_accepted_run_mode_override,
    apply_run_context_route_metadata,
    authoritative_project_run_context,
    map_project_workspace_error,
)
from opensquilla.gateway.protocol import serialize_public_event
from opensquilla.gateway.routing import RouteEnvelope, tool_context_from_envelope
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.permissions import configured_default_elevated
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.sandbox.guest_profile import GuestProfile
from opensquilla.sandbox.policy_store import pin_sandbox_policy
from opensquilla.session.models import SessionIntent, SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.terminal_reply import build_terminal_reply, sanitize_agent_error
from opensquilla.session.turn_context import turn_context_scope

if TYPE_CHECKING:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.session.manager import SessionManager

log = structlog.get_logger(__name__)
_STREAM_IDLE_TIMEOUT_CODE = "stream_idle_timeout"
_STREAM_IDLE_TIMEOUT_MESSAGE = "Session event stream idle before terminal event"


def _optional_positive_timeout(config: Any, attr: str, default: float) -> float | None:
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


async def run_direct_turn(
    *,
    runner: TurnRunner | None,
    sessions: SessionManager,
    storage: SessionStorage,
    config: GatewayConfig,
    principal_is_owner: bool,
    host_execute_allowed: bool,
    configured_workspace_dir: str | None,
    route_envelope: RouteEnvelope,
    guest_profile: GuestProfile | None,
    accepted_run_mode_override: AcceptedRunModeOverride | None,
    session_key: str,
    agent_id: str,
    turn_id: str,
    session_id: str,
    provider_message: str,
    semantic_message: str,
    attachments: list[dict[str, Any]],
    session_intent: SessionIntent,
    run_kind: str,
    no_memory_capture: bool,
    fresh_user_session: bool,
    user_message_id: str | None,
    turn_context: dict[str, Any],
    publish: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    normalize_terminal: Callable[[str, dict[str, Any]], dict[str, Any]],
    session_model: Callable[[SessionNode, str], str | None],
) -> None:
    """Stream one committed turn; acceptance and persistence belong to the caller."""

    terminal_emitted = False

    def current_task() -> asyncio.Task | None:
        task = asyncio.current_task()
        return task if isinstance(task, asyncio.Task) else None

    def event_payload(payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        enriched.setdefault("session_id", session_id)
        if not enriched.get("turn_id"):
            enriched["turn_id"] = turn_id
        enriched.setdefault("client_message_id", turn_context.get("client_message_id"))
        if user_message_id:
            enriched.setdefault("user_message_id", user_message_id)
        enriched.setdefault("surface_id", turn_context.get("surface_id"))
        return enriched

    async def emit_terminal_once(event_name: str, payload: dict[str, Any]) -> None:
        nonlocal terminal_emitted
        task = current_task()
        if terminal_emitted or (
            task is not None and getattr(task, "_opensquilla_terminal_emitted", False)
        ):
            return
        terminal_emitted = True
        if task is not None:
            setattr(task, "_opensquilla_terminal_emitted", True)
        await publish(
            session_key, event_name, event_payload(normalize_terminal(event_name, payload))
        )

    try:
        task = current_task()
        if task is not None:
            setattr(task, "_opensquilla_started", True)
        turn_scope = turn_context_scope({**turn_context, "disposition": "applied"})
        turn_scope.__enter__()
        if runner is None:
            log.error("sessions.send.no_turn_runner", session_key=session_key)
            await sessions.append_message(
                session_key, role="system", content="Error: No turn runner available"
            )
            await emit_terminal_once(
                "session.event.error",
                {"message": "No turn runner available", "code": "no_turn_runner"},
            )
            return

        execution_session = await storage.get_session(session_key)
        if execution_session is None:
            raise KeyError(f"Session not found: {session_key}")
        if guest_profile is not None:
            execution_run_context = guest_profile.run_context()
        else:
            execution_run_context, _ = await authoritative_project_run_context(
                storage=storage,
                session_manager=sessions,
                session=execution_session,
                config=config,
                default_workspace=configured_workspace_dir,
            )
        execution_run_context = apply_accepted_run_mode_override(
            execution_run_context,
            accepted_run_mode_override,
        )
        apply_run_context_route_metadata(
            route_envelope,
            execution_run_context,
            principal_is_owner=principal_is_owner,
        )
        execution_workspace_dir = execution_run_context.workspace or configured_workspace_dir
        workspace_strict = getattr(config, "workspace_strict", None)
        if not isinstance(workspace_strict, bool):
            workspace_strict = bool(execution_workspace_dir)
        tool_ctx = tool_context_from_envelope(
            route_envelope,
            is_owner=principal_is_owner,
            host_execute_allowed=host_execute_allowed,
            workspace_dir=execution_workspace_dir,
            workspace_strict=workspace_strict,
            default_elevated=configured_default_elevated(config),
        )
        pin_sandbox_policy(tool_ctx, config)
        raw_stream = runner.run(
            provider_message,
            session_key,
            tool_context=tool_ctx,
            agent_id=agent_id,
            model=session_model(execution_session, agent_id),
            attachments=attachments,
            session_intent=session_intent.value,
            input_provenance=route_envelope.input_provenance,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
            semantic_message=semantic_message,
            fresh_user_session=fresh_user_session,
            root_turn_id=turn_id,
        )
        raw_idle_timeout = effective_agent_stream_idle_timeout_seconds(config)
        idle_timeout = raw_idle_timeout if raw_idle_timeout > 0 else None
        heartbeat_interval = _optional_positive_timeout(
            config,
            "agent_stream_heartbeat_interval_seconds",
            15.0,
        )
        composed_stream = wrap_stream(
            raw_stream,
            idle_timeout=idle_timeout,
            heartbeat_interval=heartbeat_interval,
            heartbeat_message="Agent run is still active",
            context_bound=is_context_bound_owner(runner),
        )
        async for event in composed_stream:
            if isinstance(event, AnswerGenerationResetEvent):
                event_dict = serialize_public_event(event)
            else:
                event_dict = asdict(event)
            event_kind = event_dict.pop("kind", event.__class__.__name__)
            if event_kind == "thinking" and not event_dict.get("block_id"):
                event_dict.pop("block_id", None)
                event_dict.pop("block_index", None)
            if event_kind == "artifact":
                event_dict = enrich_artifact_event_dict(event_dict)
            if event_kind in ("done", "error") or (
                event_kind == "answer_generation_reset" and event_dict.get("terminal") is True
            ):
                await emit_terminal_once(f"session.event.{event_kind}", event_dict)
            else:
                await publish(session_key, f"session.event.{event_kind}", event_payload(event_dict))

        await publish(
            session_key,
            "sessions.changed",
            event_payload(build_sessions_changed_payload(session_key, "turn_complete")),
        )
    except asyncio.CancelledError:
        log.info("sessions.send.aborted", session_key=session_key)
        try:
            await emit_terminal_once("session.event.done", {"reason": "aborted"})
        except Exception:
            pass
    except TimeoutError:
        log.warning("sessions.send.stream_idle_timeout", session_key=session_key)
        timeout_message = build_terminal_reply(
            {
                "status": "timeout",
                "terminal_reason": "timeout",
                "error_class": _STREAM_IDLE_TIMEOUT_CODE,
                "error_message": _STREAM_IDLE_TIMEOUT_MESSAGE,
            }
        )
        await sessions.append_message(session_key, role="system", content=timeout_message)
        await emit_terminal_once(
            "session.event.error",
            {"message": _STREAM_IDLE_TIMEOUT_MESSAGE, "code": _STREAM_IDLE_TIMEOUT_CODE},
        )
    except ProjectWorkspaceStateError as exc:
        mapped = map_project_workspace_error(exc, owner=principal_is_owner)
        log.warning(
            "sessions.send.project_workspace_unavailable",
            session_key=session_key,
            reason=exc.reason,
        )
        await sessions.append_message(
            session_key, role="system", content=f"Error: {mapped.message}"
        )
        await emit_terminal_once(
            "session.event.error",
            {
                "message": mapped.message,
                "code": mapped.code,
                "details": mapped.details,
            },
        )
    except Exception as exc:
        error_code, error_message = sanitize_agent_error(
            {
                "status": "failed",
                "terminal_reason": "error",
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            },
            fallback_error_class="agent_error",
            fallback_error_message=str(exc) or "Agent error",
        )
        event_code = error_code if error_code == "provider_request_too_large" else "agent_error"
        log.error(
            "sessions.send.agent_failed", session_key=session_key, error=str(exc), exc_info=True
        )
        await sessions.append_message(session_key, role="system", content=f"Error: {error_message}")
        await emit_terminal_once(
            "session.event.error",
            {"message": error_message, "code": event_code},
        )
    finally:
        # Close the stream chain deterministically in the task that consumed
        # it. On an aborted turn the ``async for`` above unwinds on
        # CancelledError without closing the wrapped ``TurnRunner.run``
        # generator, so finalization used to fall to asyncio's async-generator
        # finalizer, which runs ``athrow()`` in a fresh Context — where the
        # run generator's scope stack (process/policy/git/toolchain
        # ContextVars) cannot reset its tokens and the orphan task crashed
        # with ``ValueError: ... was created in a different Context``
        # (issue #1569). Closing here unwinds the wrappers and, via the
        # heartbeat driver's own cleanup, the underlying run generator in the
        # Context that entered its scope stack.
        if "composed_stream" in locals():
            stream_to_close: Any | None = composed_stream
        elif "raw_stream" in locals():
            stream_to_close = raw_stream
        else:
            stream_to_close = None
        close = getattr(stream_to_close, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        if guest_profile is not None:
            guest_profile.cleanup()
        if "turn_scope" in locals():
            turn_scope.__exit__(None, None, None)
        if not terminal_emitted:
            try:
                await emit_terminal_once(
                    "session.event.error",
                    {
                        "message": "Agent task terminated unexpectedly",
                        "code": "task_cancelled",
                    },
                )
            except Exception:
                pass
