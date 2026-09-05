"""Direct execution preserves terminal publication at the runtime boundary."""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.direct_turn_runtime import run_direct_turn
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.models import SessionIntent, SessionNode


@pytest.mark.asyncio
async def test_unavailable_direct_runner_reports_one_terminal_with_turn_identity() -> None:
    published: list[tuple[str, str, dict[str, Any]]] = []
    sessions = SimpleNamespace(append_message=AsyncMock())

    async def publish(key: str, event: str, payload: dict[str, Any]) -> None:
        published.append((key, event, payload))

    await asyncio.create_task(
        run_direct_turn(
            runner=None,
            sessions=sessions,
            storage=SimpleNamespace(),
            config=GatewayConfig(),
            principal_is_owner=True,
            host_execute_allowed=False,
            configured_workspace_dir=None,
            route_envelope=RouteEnvelope(SourceKind.WEB, "web", "main", "agent:main:direct"),
            guest_profile=None,
            accepted_run_mode_override=None,
            session_key="agent:main:direct",
            agent_id="main",
            turn_id="turn-synthetic",
            session_id="session-synthetic",
            provider_message="synthetic input",
            semantic_message="synthetic input",
            attachments=[],
            session_intent=SessionIntent.CONTINUE,
            run_kind="user",
            no_memory_capture=False,
            fresh_user_session=False,
            user_message_id="message-synthetic",
            turn_context={
                "client_message_id": "client-synthetic",
                "surface_id": "surface-synthetic",
            },
            publish=publish,
            normalize_terminal=lambda _event, payload: {**payload, "normalized": True},
            session_model=lambda _session, _agent: None,
        )
    )

    assert published == [
        (
            "agent:main:direct",
            "session.event.error",
            {
                "message": "No turn runner available",
                "code": "no_turn_runner",
                "normalized": True,
                "session_id": "session-synthetic",
                "turn_id": "turn-synthetic",
                "client_message_id": "client-synthetic",
                "user_message_id": "message-synthetic",
                "surface_id": "surface-synthetic",
            },
        )
    ]
    sessions.append_message.assert_awaited_once_with(
        "agent:main:direct",
        role="system",
        content="Error: No turn runner available",
    )


@pytest.mark.asyncio
async def test_direct_stream_publishes_only_first_terminal_and_releases_guest_profile() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    run_inputs: list[dict[str, Any]] = []
    session = SessionNode(session_key="agent:main:direct", session_id="session-synthetic")
    sessions = SimpleNamespace(append_message=AsyncMock())
    profile = SimpleNamespace(
        run_context=lambda: RunContext(run_mode=RunMode.SAFE),
        cleanup=Mock(),
    )

    class Runner:
        async def run(self, message: str, key: str, **kwargs: Any):
            run_inputs.append({"message": message, "key": key, **kwargs})
            yield DoneEvent(text="first answer")
            yield DoneEvent(text="duplicate answer")

    async def publish(_key: str, event: str, payload: dict[str, Any]) -> None:
        published.append((event, payload))

    await asyncio.create_task(
        run_direct_turn(
            runner=Runner(),
            sessions=sessions,
            storage=SimpleNamespace(get_session=AsyncMock(return_value=session)),
            config=GatewayConfig(),
            principal_is_owner=False,
            host_execute_allowed=False,
            configured_workspace_dir=None,
            route_envelope=RouteEnvelope(SourceKind.WEB, "web", "main", session.session_key),
            guest_profile=profile,
            accepted_run_mode_override=None,
            session_key=session.session_key,
            agent_id="main",
            turn_id="turn-synthetic",
            session_id=session.session_id,
            provider_message="wrapped synthetic input",
            semantic_message="synthetic input",
            attachments=[],
            session_intent=SessionIntent.CONTINUE,
            run_kind="user",
            no_memory_capture=True,
            fresh_user_session=False,
            user_message_id="message-synthetic",
            turn_context={
                "client_message_id": "client-synthetic",
                "surface_id": "surface-synthetic",
            },
            publish=publish,
            normalize_terminal=lambda _event, payload: payload,
            session_model=lambda _session, _agent: "synthetic-model",
        )
    )

    assert [name for name, _payload in published] == ["session.event.done", "sessions.changed"]
    assert published[0][1]["text"] == "first answer"
    assert published[0][1]["turn_id"] == "turn-synthetic"
    assert published[0][1]["user_message_id"] == "message-synthetic"
    assert run_inputs[0]["root_turn_id"] == "turn-synthetic"
    assert run_inputs[0]["semantic_message"] == "synthetic input"
    assert run_inputs[0]["no_memory_capture"] is True
    assert run_inputs[0]["model"] == "synthetic-model"
    profile.cleanup.assert_called_once_with()
    sessions.append_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_aborted_direct_turn_closes_runner_stream_in_its_own_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An aborted turn must not leave the runner stream to the GC finalizer.

    Regression for issue #1569: the ``async for`` unwinds on CancelledError
    without closing the wrapped ``TurnRunner.run`` generator, so asyncio's
    async-generator finalizer finalized it in a fresh Context and the run
    generator's scope stack crashed resetting its ContextVar tokens
    (``ValueError: ... was created in a different Context``), 5 aborts → 5
    orphan-task crashes on a live gateway.
    """

    published: list[tuple[str, dict[str, Any]]] = []
    scope_state: dict[str, Any] = {
        "advance_task": None,
        "finally_task": None,
        "closed": False,
        "reset_error": None,
    }
    scope_var: ContextVar[str | None] = ContextVar("direct_turn_abort_scope_test", default=None)
    first_event_consumed = asyncio.Event()
    session = SessionNode(session_key="agent:main:direct", session_id="session-synthetic")
    sessions = SimpleNamespace(append_message=AsyncMock())

    class Runner:
        async def run(self, message: str, key: str, **kwargs: Any):
            scope_state["advance_task"] = asyncio.current_task()
            token = scope_var.set("turn-scope")
            try:
                yield DoneEvent(text="partial answer")
                first_event_consumed.set()
                # Suspended mid-generation when the turn is cancelled, exactly
                # like a real runner waiting on the provider stream.
                await asyncio.sleep(30)
                yield DoneEvent(text="never delivered")
            except ValueError as exc:
                scope_state["reset_error"] = exc
                raise
            finally:
                scope_state["finally_task"] = asyncio.current_task()
                # Deliberately the plain, fragile reset: a scope teardown that
                # runs in a foreign Context (the async-generator finalizer)
                # must not be able to hide here.
                scope_var.reset(token)
                scope_state["closed"] = True

    async def publish(_key: str, event: str, payload: dict[str, Any]) -> None:
        published.append((event, payload))

    # No heartbeat driver: the consuming task itself advances the runner
    # generator, so its scope stack is entered in the consumer's Context —
    # and only a deterministic consumer-side close can unwind it there.
    config = GatewayConfig()
    config.agent_stream_heartbeat_interval_seconds = 0

    turn = asyncio.create_task(
        run_direct_turn(
            runner=Runner(),
            sessions=sessions,
            storage=SimpleNamespace(get_session=AsyncMock(return_value=session)),
            config=config,
            principal_is_owner=True,
            host_execute_allowed=False,
            configured_workspace_dir=None,
            route_envelope=RouteEnvelope(SourceKind.WEB, "web", "main", session.session_key),
            guest_profile=None,
            accepted_run_mode_override=None,
            session_key=session.session_key,
            agent_id="main",
            turn_id="turn-synthetic",
            session_id=session.session_id,
            provider_message="wrapped synthetic input",
            semantic_message="synthetic input",
            attachments=[],
            session_intent=SessionIntent.CONTINUE,
            run_kind="user",
            no_memory_capture=True,
            fresh_user_session=False,
            user_message_id="message-synthetic",
            turn_context={
                "client_message_id": "client-synthetic",
                "surface_id": "surface-synthetic",
            },
            publish=publish,
            normalize_terminal=lambda _event, payload: payload,
            session_model=lambda _session, _agent: "synthetic-model",
        )
    )
    await asyncio.wait_for(first_event_consumed.wait(), timeout=5.0)
    turn.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await turn

    # Give the loop a beat so an abandoned generator would be picked up by
    # asyncio's async-generator finalizer right about now.
    await asyncio.sleep(0.1)

    # The runner stream was closed deterministically by the task that entered
    # its scope stack — the consuming task — not by asyncio's async-generator
    # finalizer, which runs the teardown in a foreign Context where the
    # ContextVar tokens cannot be reset (issue #1569).
    assert scope_state["closed"] is True
    assert scope_state["reset_error"] is None
    assert scope_state["finally_task"] is scope_state["advance_task"]

    # One terminal event only, and the aborted turn is reported as terminal —
    # a subscriber can distinguish "cancelled" from "still running".
    terminal_names = [name for name, _payload in published if "done" in name or "error" in name]
    assert terminal_names == ["session.event.done"]

    # Give the loop a beat for any finalizer task, then require silence.
    await asyncio.sleep(0.1)
    unretrieved = [
        record
        for record in caplog.records
        if record.name == "asyncio" and "Task exception was never retrieved" in record.getMessage()
    ]
    assert unretrieved == []
