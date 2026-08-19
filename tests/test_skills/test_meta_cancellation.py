"""End-to-end cancellation semantics for MetaOrchestrator / scheduler.

These tests pin down the "user pressed Abort halfway through a long DAG"
contract. They use a sub-Agent stub that simulates a long-running step
(via ``asyncio.sleep``) so we can cancel mid-flight and observe the
cleanup path.

Invariants asserted:

1. Cancelling the consumer task triggers cleanup of every in-flight
   sub-Agent task — no leaked / orphaned tasks remain.
2. ``CancelledError`` re-raises out of ``iter_events`` so upstream
   consumers can finalise their own resources.
3. ``finish_run_sync`` records ``status="cancelled"`` when a writer
   is attached, never leaving a row stuck on ``"running"``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.types import AgentEvent, DoneEvent, TextDeltaEvent
from opensquilla.persistence.migrator import apply_pending
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch
from opensquilla.skills.types import SkillLayer, SkillSpec

MIGRATIONS_DIR = Path(__file__).resolve().parents[1].parent / "migrations"


def _make_meta_spec(composition: dict[str, Any]) -> SkillSpec:
    return SkillSpec(
        name="meta-cancellation-test",
        description="test",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        kind="meta",
        meta_priority=0,
        composition_raw=composition,
        # raw so the auto-summary path does not gate the test on llm_chat
        final_text_mode="raw",
    )


def _make_skill_spec(name: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name}",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        kind="skill",
    )


class _FakeLoader:
    def __init__(self, specs: list[SkillSpec]) -> None:
        self._specs = specs

    def load_all(self) -> list[SkillSpec]:
        return list(self._specs)

    def get_by_name(self, name: str) -> SkillSpec | None:
        for s in self._specs:
            if s.name == name:
                return s
        return None


@pytest.mark.asyncio
async def test_orchestrator_cancelled_mid_step_cleans_up_running_tasks() -> None:
    """Cancelling the iter_events consumer cancels every in-flight
    sub-Agent task. Nothing leaks past the await boundary."""
    spec = _make_meta_spec({
        "steps": [
            {"id": "long_a", "skill": "summarize", "with": {"text": "x"}},
            {"id": "long_b", "skill": "summarize", "with": {"text": "y"}},
        ],
    })
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize")])

    # Track which runners started + which got cancelled mid-sleep.
    started: list[str] = []
    cancelled: list[str] = []

    async def slow_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        token = user_message[:30]
        started.append(token)
        try:
            await asyncio.sleep(5.0)  # plenty of room for the outer cancel
            yield TextDeltaEvent(text="never reached")
            yield DoneEvent(text="")
        except asyncio.CancelledError:
            cancelled.append(token)
            raise

    orch = MetaOrchestrator(agent_runner=slow_runner, skill_loader=loader)

    async def consume() -> None:
        async for _ in orch.iter_events(
            MetaMatch(plan=plan, inputs={"user_message": "test"}),
        ):
            pass  # consume everything

    task = asyncio.create_task(consume())
    # Wait long enough for both sub-Agent stubs to enter their sleeps.
    await asyncio.sleep(0.2)
    assert len(started) == 2, f"expected both steps to start, got {started!r}"

    # User pressed Abort.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # All in-flight runners must have observed the cancellation. Otherwise
    # the scheduler is leaking tasks that keep running after the user
    # walked away.
    assert sorted(cancelled) == sorted(started), (
        f"all started runners must be cancelled. started={started!r} "
        f"cancelled={cancelled!r}"
    )


@pytest.mark.asyncio
async def test_orchestrator_cancelled_writes_cancelled_status_to_db(tmp_path) -> None:
    """When a writer is wired, a cancelled run finalises with
    ``status='cancelled'`` — never stuck on ``running``."""
    from opensquilla.persistence.meta_run_writer import open_meta_run_writer

    db_path = tmp_path / "runs.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)
    writer = open_meta_run_writer(str(db_path))
    try:
        spec = _make_meta_spec({
            "steps": [
                {"id": "long_a", "skill": "summarize", "with": {"text": "x"}},
            ],
        })
        plan = parse_meta_plan(spec)
        assert plan is not None
        loader = _FakeLoader([_make_skill_spec("summarize")])

        async def slow_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
            try:
                await asyncio.sleep(5.0)
                yield TextDeltaEvent(text="never")
            except asyncio.CancelledError:
                raise

        orch = MetaOrchestrator(
            agent_runner=slow_runner,
            skill_loader=loader,
            run_writer=writer,
            session_key="test:cancel",
        )

        async def consume() -> None:
            async for _ in orch.iter_events(
                MetaMatch(plan=plan, inputs={"user_message": "test"}),
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Inspect the DB: the run should be marked cancelled, not still running.
        runs = writer.list_runs(name="meta-cancellation-test", limit=5)
        assert len(runs) == 1, f"expected exactly one run row, got {len(runs)}"
        assert runs[0].status == "cancelled", (
            f"expected status=cancelled after consumer abort, got {runs[0].status!r}"
        )
        assert runs[0].ended_at_ms is not None, (
            "cancelled run must have ended_at_ms set so duration is computable"
        )
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_orchestrator_cancellation_yields_no_partial_meta_result() -> None:
    """If we cancel mid-stream, the consumer must not receive a
    ``MetaResult`` (that would be a lie about completion). Items yielded
    before the cancel point can be anything, but the terminal sentinel
    must not surface."""
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec({
        "steps": [
            {"id": "long_a", "skill": "summarize", "with": {"text": "x"}},
        ],
    })
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize")])

    async def slow_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        await asyncio.sleep(5.0)
        yield TextDeltaEvent(text="never")

    orch = MetaOrchestrator(agent_runner=slow_runner, skill_loader=loader)

    items: list[Any] = []

    async def consume() -> None:
        async for item in orch.iter_events(
            MetaMatch(plan=plan, inputs={"user_message": "test"}),
        ):
            items.append(item)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(isinstance(it, MetaResult) for it in items), (
        f"no MetaResult should reach the consumer on cancel; items={items!r}"
    )
