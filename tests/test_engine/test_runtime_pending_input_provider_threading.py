from __future__ import annotations

from collections.abc import AsyncIterator
from types import MethodType
from typing import Any

import pytest

import opensquilla.engine.runtime as runtime_module
from opensquilla.cli.tui import turn_bridge
from opensquilla.engine.agent_injection import ListPendingInputProvider
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent
from opensquilla.tools.types import ToolContext


@pytest.mark.asyncio
async def test_turnrunner_run_threads_pending_input_provider_to_run_turn(monkeypatch) -> None:
    runner = TurnRunner(provider_selector=None, config=None)
    pending = ListPendingInputProvider()
    pending.append("later")
    seen_kwargs: list[dict[str, Any]] = []
    created_contexts: list[Any] = []
    create_context = runtime_module.create_turn_execution_context

    def _record_context(**kwargs: Any) -> Any:
        context = create_context(**kwargs)
        created_contexts.append(context)
        return context

    monkeypatch.setattr(runtime_module, "create_turn_execution_context", _record_context)

    async def _recording_run_turn(
        self: TurnRunner,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        del self, args
        seen_kwargs.append(kwargs)
        yield DoneEvent(text="ok")

    runner._run_turn = MethodType(_recording_run_turn, runner)  # type: ignore[method-assign]

    async for _event in runner.run(
        message="hi",
        session_key="agent:main:pending-thread-test",
        tool_context=ToolContext(session_key="agent:main:pending-thread-test"),
        pending_input_provider=pending,
        root_turn_id="gateway-turn-1",
        assistant_message_id="assistant-turn-1",
    ):
        pass

    assert seen_kwargs[0]["pending_input_provider"] is pending
    assert seen_kwargs[0]["root_turn_id"] == "gateway-turn-1"
    context = seen_kwargs[0]["execution_context"]
    assert len(created_contexts) == 1
    assert context is created_contexts[0]
    assert context.identity.turn_id == "gateway-turn-1"
    assert context.identity.assistant_message_id == "assistant-turn-1"


@pytest.mark.asyncio
async def test_tui_turn_bridge_threads_pending_input_provider_to_turnrunner_run() -> None:
    runner = TurnRunner(provider_selector=None, config=None)
    pending = ListPendingInputProvider()
    pending.append("later")
    seen_kwargs: list[dict[str, Any]] = []

    def _recording_run(
        self: TurnRunner,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        del self, args
        seen_kwargs.append(kwargs)

        async def _iter() -> AsyncIterator[Any]:
            yield DoneEvent(text="ok")

        return _iter()

    runner.run = MethodType(_recording_run, runner)  # type: ignore[method-assign]

    await turn_bridge.stream_response_turnrunner(
        runner,
        "agent:main:tui-pending-thread-test",
        ToolContext(session_key="agent:main:tui-pending-thread-test"),
        "hi",
        pending_input_provider=pending,
    )

    assert seen_kwargs[0]["pending_input_provider"] is pending
