"""Scheduler-side tests for MetaPaused handling (PR3, design §8.1)."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.engine.types import MetaRunCompletedEvent, MetaStepStateEvent
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.scheduler import run_dag
from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaMatch,
    MetaPaused,
    MetaPlan,
    MetaResult,
    MetaStep,
)


def _plan_with_paused_step() -> MetaPlan:
    return MetaPlan(
        name="t",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="collect",
                skill="collect",
                kind="user_input",
                clarify_config=ClarifyStepConfig(
                    mode="form",
                    fields=(ClarifyField(name="x", type="string", required=True),),
                ),
            ),
            MetaStep(id="downstream", skill="summarize", kind="agent",
                     depends_on=("collect",)),
        ),
    )


async def _yield_skill_view(step_id: str, skill_name: str):
    return
    yield  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_meta_paused_emits_paused_meta_result():
    """A user_input step that raises MetaPaused yields a single
    MetaResult(paused=True). Downstream step never runs."""

    cfg = _plan_with_paused_step().steps[0].clarify_config
    paused_signal = MetaPaused(run_id="r1", step_id="collect", schema=cfg)

    downstream_ran = False

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        nonlocal downstream_ran
        if step.id == "collect":
            raise paused_signal
        if step.id == "downstream":
            downstream_ran = True
            yield _StepDone(text="should not reach", status="ok")

    match = MetaMatch(plan=_plan_with_paused_step(), inputs={"user_message": "hi"})

    results: list[object] = []
    async for ev in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
    ):
        results.append(ev)

    terminal = [r for r in results if isinstance(r, MetaResult)]
    assert len(terminal) == 1, f"expected one MetaResult, got {terminal!r}"
    final = terminal[0]
    assert final.paused is True
    assert final.ok is False
    assert final.paused_payload is paused_signal
    assert downstream_ran is False


@pytest.mark.asyncio
async def test_meta_paused_cancels_parallel_siblings():
    """If a sibling step is in-flight when MetaPaused fires, it is cancelled."""

    cfg = ClarifyStepConfig(
        mode="form",
        fields=(ClarifyField(name="x", type="string", required=True),),
    )

    plan = MetaPlan(
        name="t",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="collect",
                skill="collect",
                kind="user_input",
                clarify_config=cfg,
            ),
            MetaStep(id="parallel", skill="other", kind="agent"),
        ),
    )

    parallel_observed_cancel = asyncio.Event()

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.id == "collect":
            await asyncio.sleep(0.05)
            raise MetaPaused(run_id="r1", step_id="collect", schema=cfg)
        if step.id == "parallel":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                parallel_observed_cancel.set()
                raise
            yield _StepDone(text="should not finish", status="ok")

    match = MetaMatch(plan=plan, inputs={"user_message": "hi"})

    async for _ in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
    ):
        pass

    assert parallel_observed_cancel.is_set(), \
        "parallel step should have been cancelled when MetaPaused fired"


@pytest.mark.asyncio
async def test_meta_paused_emits_matching_tool_result_event():
    """ToolUseStartEvent was already emitted by the step task body
    before MetaPaused fired. Without a matching ToolResultEvent, Web UI
    cards stay in-flight. Scheduler MUST emit a synthetic paused
    ToolResultEvent before returning the paused MetaResult."""

    from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent

    cfg = ClarifyStepConfig(
        mode="form",
        fields=(ClarifyField(name="x", type="string", required=True),),
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.id == "collect":
            raise MetaPaused(run_id="r1", step_id="collect", schema=cfg)
        return
        yield  # type: ignore[unreachable]  # make this an async generator

    match = MetaMatch(
        plan=MetaPlan(
            name="t", triggers=(), priority=0,
            steps=(MetaStep(id="collect", skill="collect",
                             kind="user_input", clarify_config=cfg),),
        ),
        inputs={"user_message": "hi"},
    )

    events: list[object] = []
    async for ev in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
    ):
        events.append(ev)

    start_evs = [e for e in events if isinstance(e, ToolUseStartEvent)]
    result_evs = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(start_evs) == len(result_evs), (
        f"unbalanced tool events: {len(start_evs)} starts vs "
        f"{len(result_evs)} results — paused branch missed ToolResultEvent"
    )
    paused_result = result_evs[-1]
    assert paused_result.is_error is False
    assert "paused" in paused_result.result.lower()


@pytest.mark.asyncio
async def test_meta_paused_tool_result_carries_clarify_schema_protocol():
    """PR5: the synthetic ToolResultEvent must include the surface
    protocol payload so Web/CLI/IM can render a form without parsing
    SkillSpec again."""
    from opensquilla.engine.types import ToolResultEvent

    cfg = ClarifyStepConfig(
        mode="form",
        fields=(
            ClarifyField(name="destination", type="string", required=True,
                         prompt="目的地"),
            ClarifyField(name="days", type="int", required=True, min=1, max=14,
                         prompt="天数"),
            ClarifyField(
                name="export_docx",
                type="enum",
                choices=("YES", "NO"),
                default="NO",
                prompt="是否导出 DOCX",
            ),
        ),
        intro="需要 4 个字段",
        cancel_keywords=("cancel",),
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.id == "collect":
            raise MetaPaused(
                run_id="r-clarify",
                step_id="collect",
                schema=cfg,
                language="zh",
            )
        return
        yield  # type: ignore[unreachable]

    match = MetaMatch(
        plan=MetaPlan(
            name="t", triggers=(), priority=0,
            steps=(MetaStep(id="collect", skill="collect",
                             kind="user_input", clarify_config=cfg),),
        ),
        inputs={"user_message": "hi"},
    )

    events: list[object] = []
    async for ev in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
    ):
        events.append(ev)

    paused_result = next(
        e for e in events
        if isinstance(e, ToolResultEvent) and e.arguments.get("paused") is True
    )
    args = paused_result.arguments
    assert args["kind"] == "user_input"
    assert args["step"] == "collect"
    assert args["run_id"] == "r-clarify"
    # Schema protocol payload is JSON-shaped, matches clarify_schema output.
    protocol = args["clarify_schema"]
    assert protocol["mode"] == "form"
    assert protocol["intro"] == "需要 4 个字段"
    assert len(protocol["fields"]) == 3
    assert protocol["fields"][0]["name"] == "destination"
    assert protocol["fields"][1]["min"] == 1
    assert protocol["fields"][2]["options"] == [
        {"value": "YES", "label": "是"},
        {"value": "NO", "label": "否"},
    ]
    assert protocol["cancel_keywords"] == ["cancel"]


@pytest.mark.asyncio
async def test_meta_paused_does_not_trigger_on_failure_substitute():
    """A step that raises MetaPaused does NOT cause on_failure substitute
    to spawn (pause ≠ failure, design §8.1)."""

    cfg = ClarifyStepConfig(
        mode="form",
        fields=(ClarifyField(name="x", type="string", required=True),),
    )
    plan = MetaPlan(
        name="t",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="collect",
                skill="collect",
                kind="user_input",
                clarify_config=cfg,
                on_failure="rescue",
            ),
            MetaStep(id="rescue", skill="summarize", kind="agent"),
        ),
    )

    rescue_ran = False

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        nonlocal rescue_ran
        if step.id == "collect":
            raise MetaPaused(run_id="r1", step_id="collect", schema=cfg)
        if step.id == "rescue":
            rescue_ran = True
            yield _StepDone(text="rescue", status="ok")

    match = MetaMatch(plan=plan, inputs={"user_message": "hi"})
    async for _ in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
    ):
        pass

    assert rescue_ran is False


@pytest.mark.asyncio
async def test_resume_from_skips_already_completed_steps():
    """When `seed_outputs` is set, steps whose id is in seed_outputs
    are treated as already-finished and not dispatched."""
    plan = MetaPlan(
        name="t",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(id="a", skill="a", kind="agent"),
            MetaStep(id="b", skill="b", kind="agent", depends_on=("a",)),
        ),
    )

    dispatched: list[str] = []

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        dispatched.append(step.id)
        yield _StepDone(text=f"out-{step.id}", status="ok")

    match = MetaMatch(plan=plan, inputs={"user_message": "hi"})

    seed_outputs = {"a": "previously completed"}

    events = [ev async for ev in run_dag(
        match,
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_yield_skill_view,
        seed_outputs=seed_outputs,
    )]

    # 'a' was skipped (already in outputs); 'b' ran.
    assert "a" not in dispatched
    assert "b" in dispatched
    reused = [
        event for event in events
        if isinstance(event, MetaStepStateEvent) and event.step_id == "a"
    ]
    assert reused and reused[0].state == "succeeded"
    completed = next(event for event in events if isinstance(event, MetaRunCompletedEvent))
    assert completed.completed_steps == ["a", "b"]
