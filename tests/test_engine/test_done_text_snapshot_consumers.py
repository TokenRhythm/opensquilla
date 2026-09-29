from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.engine.types import AgentEvent, DoneEvent, TextDeltaEvent


class _ScriptedChildAgent:
    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    async def run_turn(self, _task: str) -> AsyncIterator[AgentEvent]:
        for event in self._events:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("done", "expected"),
    [
        (DoneEvent(text="", text_snapshot=""), ""),
        (DoneEvent(text=""), "partial"),
        (DoneEvent(text="canonical", text_snapshot="canonical"), "canonical"),
    ],
)
async def test_subagent_manager_honors_terminal_snapshot_presence(
    done: DoneEvent,
    expected: str,
) -> None:
    manager = SubagentManager()
    child = _ScriptedChildAgent([TextDeltaEvent(text="partial"), done])

    handle = await manager.spawn(
        SubagentSpec(task="synthetic task", timeout=0),
        lambda _spec, _depth: child,
    )

    assert await handle.task == expected
