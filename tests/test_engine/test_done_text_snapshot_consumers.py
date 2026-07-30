from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.engine.types import AgentEvent, DoneEvent, TextDeltaEvent
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.executors.agent import run_step_with_skill_stream
from opensquilla.skills.meta.executors.llm_classify import _drain_agent_runner
from opensquilla.skills.meta.types import MetaStep


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


@pytest.mark.asyncio
async def test_meta_agent_step_prefers_authoritative_done_over_stale_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.eligibility.is_skill_available_live",
        lambda _name: True,
    )

    async def runner(_system: str, _user: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="stale")
        yield DoneEvent(text="canonical", text_snapshot="canonical")

    loader = SimpleNamespace(
        get_by_name=lambda _name: SimpleNamespace(
            kind="skill",
            content="Synthetic skill instructions.",
            base_dir="",
        )
    )
    events = [
        event
        async for event in run_step_with_skill_stream(
            MetaStep(id="step", skill="synthetic"),
            "synthetic",
            {},
            {},
            agent_runner=runner,
            skill_loader=loader,
        )
    ]

    assert [event.text for event in events if isinstance(event, _StepDone)] == [
        "canonical"
    ]


@pytest.mark.asyncio
async def test_meta_classifier_prefers_authoritative_done_over_stale_deltas() -> None:
    async def runner(_system: str, _user: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="stale")
        yield DoneEvent(text="canonical", text_snapshot="canonical")

    assert (
        await _drain_agent_runner("system", "user", agent_runner=runner)
        == "canonical"
    )
