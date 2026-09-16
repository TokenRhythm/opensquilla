"""Direct execution preserves terminal publication at the runtime boundary."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.context import control_terminal_event_for_context
from opensquilla.engine.types import AgentEvent, ControlTerminalReason, DoneEvent, TextDeltaEvent
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.direct_turn_runtime import run_direct_turn
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.models import SessionIntent, SessionNode
from opensquilla.session.turn_context import current_turn_context, turn_context_scope


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


async def _run_synthetic_direct_turn(
    runner: Any,
    config: GatewayConfig,
    profile: Any,
    publish: Callable[[str, str, dict[str, Any]], Awaitable[None]],
) -> None:
    session = SessionNode(session_key="agent:main:direct", session_id="session-synthetic")
    await run_direct_turn(
        runner=runner,
        sessions=SimpleNamespace(append_message=AsyncMock()),
        storage=SimpleNamespace(get_session=AsyncMock(return_value=session)),
        config=config,
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
        provider_message="synthetic input",
        semantic_message="synthetic input",
        attachments=[],
        session_intent=SessionIntent.CONTINUE,
        run_kind="user",
        no_memory_capture=True,
        fresh_user_session=False,
        user_message_id="message-synthetic",
        turn_context={"client_message_id": "client-synthetic"},
        publish=publish,
        normalize_terminal=lambda _event, payload: payload,
        session_model=lambda _session, _agent: None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", [True, False], ids=["heartbeat-control", "inline-publish"])
async def test_aborted_direct_turn_closes_real_runner_scopes(
    tmp_path: Path,
    heartbeat: bool,
) -> None:
    config = GatewayConfig(state_dir=str(tmp_path / "state"))
    if not heartbeat:
        config.agent_stream_heartbeat_interval_seconds = 0
    profile = SimpleNamespace(
        run_context=lambda: RunContext(run_mode=RunMode.SAFE),
        cleanup=Mock(),
    )
    lock = asyncio.Lock()
    ready_to_cancel = asyncio.Event()
    never = asyncio.Event()
    terminal_payloads: list[dict[str, Any]] = []
    contexts: list[Any] = []
    advance_tasks: list[asyncio.Task[Any] | None] = []
    close_tasks: list[asyncio.Task[Any] | None] = []
    control_yielded = False

    class Runner(TurnRunner):
        def __init__(self) -> None:
            self._config = config
            self._router_control_hold_store = None
            self._session_lock_provider = lambda _key: lock
            self._turn_compaction_attempted_sessions = set()
            self._turn_compacted_sessions = set()
            self.stream: AsyncIterator[AgentEvent] | None = None

        def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            # Retain the actual generator so GC cannot make a missed close pass.
            self.stream = super().run(*args, **kwargs)
            return self.stream

        async def _run_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            nonlocal control_yielded
            context = kwargs["execution_context"]
            contexts.append(context)
            advance_tasks.append(asyncio.current_task())
            yield TextDeltaEvent(text="synthetic partial answer")
            if heartbeat:
                ready_to_cancel.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                terminal = control_terminal_event_for_context(context, ControlTerminalReason.CANCEL)
                if terminal is not None:
                    control_yielded = True
                    yield terminal
                raise

        def clear_compaction_turn_state(self, session_key: str) -> None:
            close_tasks.append(asyncio.current_task())
            super().clear_compaction_turn_state(session_key)

    async def publish(_key: str, event: str, payload: dict[str, Any]) -> None:
        if event == "session.event.text_delta" and not heartbeat:
            ready_to_cancel.set()
            await never.wait()
        if event == "session.event.done":
            terminal_payloads.append(payload)

    runner = Runner()
    turn = asyncio.create_task(_run_synthetic_direct_turn(runner, config, profile, publish))
    try:
        await asyncio.wait_for(ready_to_cancel.wait(), timeout=2)
        assert lock.locked()
        turn.cancel()
        await asyncio.wait_for(turn, timeout=2)

        assert not lock.locked()
        assert len(contexts) == 1 and contexts[0].closed
        assert close_tasks == advance_tasks
        assert advance_tasks[0] is not None
        assert (advance_tasks[0] is turn) is not heartbeat
        assert control_yielded is heartbeat
        assert len(terminal_payloads) == 1
        assert terminal_payloads[0]["reason"] == "aborted"
        profile.cleanup.assert_called_once_with()
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        if runner.stream is not None:
            with contextlib.suppress(ValueError):
                await runner.stream.aclose()
        for context in contexts:
            await context.close()


@pytest.mark.asyncio
async def test_second_cancel_during_stream_close_preserves_direct_cleanup(tmp_path: Path) -> None:
    config = GatewayConfig(state_dir=str(tmp_path / "state"))
    profile = SimpleNamespace(
        run_context=lambda: RunContext(run_mode=RunMode.SAFE),
        cleanup=Mock(),
    )
    publishing = asyncio.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()
    never = asyncio.Event()
    driver_tasks: list[asyncio.Task[Any] | None] = []
    terminal_payloads: list[dict[str, Any]] = []
    restored_contexts: list[dict[str, Any] | None] = []
    outer_context = {"turn_id": "outer-turn"}

    class Runner:
        context_bound = True

        async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            driver_tasks.append(asyncio.current_task())
            try:
                yield TextDeltaEvent(text="synthetic partial answer")
                await never.wait()
            finally:
                closing.set()
                await release_close.wait()

    async def publish(_key: str, event: str, payload: dict[str, Any]) -> None:
        if event == "session.event.text_delta":
            publishing.set()
            await never.wait()
        if event == "session.event.done":
            terminal_payloads.append(payload)

    async def execute() -> None:
        with turn_context_scope(outer_context):
            try:
                await _run_synthetic_direct_turn(Runner(), config, profile, publish)
            finally:
                restored_contexts.append(current_turn_context())

    turn = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(publishing.wait(), timeout=2)
        turn.cancel()
        await asyncio.wait_for(closing.wait(), timeout=2)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=2)

        profile.cleanup.assert_called_once_with()
        assert restored_contexts == [outer_context]
        assert len(terminal_payloads) == 1
        assert terminal_payloads[0]["reason"] == "aborted"
    finally:
        release_close.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await asyncio.gather(
            *(task for task in driver_tasks if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_guest_cleanup_failure_still_restores_direct_turn_context(tmp_path: Path) -> None:
    config = GatewayConfig(state_dir=str(tmp_path / "state"))
    profile = SimpleNamespace(cleanup=Mock(side_effect=OSError("synthetic cleanup failure")))
    outer_context = {"turn_id": "outer-turn"}
    with turn_context_scope(outer_context):
        with pytest.raises(OSError, match="synthetic cleanup failure"):
            await _run_synthetic_direct_turn(None, config, profile, AsyncMock())
        assert current_turn_context() == outer_context
    profile.cleanup.assert_called_once_with()
