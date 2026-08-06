"""DAG-parallelism contract tests for MetaOrchestrator (M7).

Three contracts:
1. Two steps with disjoint depends_on run concurrently — their
   ToolUseStartEvents both arrive before either's ToolResultEvent.
2. When one step in a parallel batch fails, the sibling task is
   cancelled — its ToolResultEvent never arrives.
3. A purely-linear DAG keeps the same event order as the old
   linear-topo scheduler (backwards compat).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.types import (
    AgentEvent,
    DoneEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch, MetaResult
from opensquilla.skills.types import SkillLayer, SkillSpec


def _meta_spec(steps: list[dict[str, Any]]) -> SkillSpec:
    return SkillSpec(
        name="meta-test",
        description="t",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=["t"],
        content="fallback",
        kind="meta",
        composition_raw={"steps": steps},
    )


def _skill(name: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} d",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content=name.upper(),
        kind="skill",
    )


class _FakeLoader:
    def __init__(self, specs: list[SkillSpec]) -> None:
        self._by_name = {s.name: s for s in specs}

    def get_by_name(self, name: str) -> SkillSpec | None:
        return self._by_name.get(name)


@pytest.mark.asyncio
async def test_independent_steps_run_concurrently() -> None:
    """Two steps with no deps both start before either finishes."""
    spec = _meta_spec(
        [
            {"id": "a", "skill": "skill_a", "with": {}},
            {"id": "b", "skill": "skill_b", "with": {}},  # no depends_on
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    started = asyncio.Event()
    second_started_first = asyncio.Event()

    async def runner(system_prompt: str, _u: str) -> AsyncIterator[AgentEvent]:
        # First runner blocks until the second has also started — proves
        # they overlap. With a serial scheduler this hangs forever.
        if "SKILL_A" in system_prompt:
            started.set()
            await asyncio.wait_for(second_started_first.wait(), timeout=2.0)
            yield TextDeltaEvent(text="A done")
        else:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            second_started_first.set()
            yield TextDeltaEvent(text="B done")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([_skill("skill_a"), _skill("skill_b")]),
    )

    starts: list[str] = []
    results: list[str] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent) and ev.tool_name.startswith("meta-step:"):
            starts.append(ev.tool_name)
        elif isinstance(ev, ToolResultEvent) and ev.tool_name.startswith("meta-step:"):
            results.append(ev.tool_name)

    assert final is not None and final.ok, final.error if final else "no final"
    # Both starts must arrive before either result.
    assert set(starts) == {"meta-step:a", "meta-step:b"}
    assert set(results) == {"meta-step:a", "meta-step:b"}
    # In the linear scheduler, starts[0] would always produce results[0]
    # before starts[1]. The parallel scheduler interleaves them, which we
    # already proved by the asyncio.Event coupling above — if we got here
    # without TimeoutError, the steps ran concurrently.


@pytest.mark.asyncio
async def test_failing_step_cancels_sibling() -> None:
    """When one parallel step raises, the sibling task is cancelled."""
    spec = _meta_spec(
        [
            {"id": "a", "skill": "skill_a", "with": {}},
            {"id": "b", "skill": "skill_b", "with": {}},
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    sibling_completed = False

    async def runner(system_prompt: str, _u: str) -> AsyncIterator[AgentEvent]:
        nonlocal sibling_completed
        if "SKILL_A" in system_prompt:
            # Fail fast.
            raise RuntimeError("a failed")
        # b is slow — must be cancelled before it finishes.
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            raise
        sibling_completed = True
        yield TextDeltaEvent(text="should not arrive")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([_skill("skill_a"), _skill("skill_b")]),
    )

    final: MetaResult | None = None
    error_results: list[ToolResultEvent] = []
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolResultEvent) and ev.is_error:
            error_results.append(ev)

    assert final is not None and final.ok is False
    assert sibling_completed is False
    assert len(error_results) >= 1


@pytest.mark.asyncio
async def test_failure_closes_sibling_brackets() -> None:
    """Every yielded ToolUseStart for a meta-step has a matching ToolResult.

    Concretely: when two siblings A/B both emit ToolUseStartEvents and A
    fails while B is still sleeping, the orchestrator must emit a
    ToolResultEvent for B carrying ``is_error=True`` and a "cancelled"
    message so the WebUI does not leak an open in-progress card.
    """
    spec = _meta_spec(
        [
            {"id": "a", "skill": "skill_a", "with": {}},
            {"id": "b", "skill": "skill_b", "with": {}},
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    b_running = asyncio.Event()

    async def runner(system_prompt: str, _u: str) -> AsyncIterator[AgentEvent]:
        if "SKILL_A" in system_prompt:
            # Make sure B has emitted its ToolUseStart before A fails — we
            # want to exercise the path where the outer loop yielded both
            # starts already.
            await asyncio.wait_for(b_running.wait(), timeout=2.0)
            raise RuntimeError("a blew up mid-flight")
        # B emits a TextDelta (forces _run_one to enter the streaming
        # body so its ToolUseStart has been pushed) then signals A and
        # sleeps. The sleep is interruptible so cancellation lands.
        yield TextDeltaEvent(text="b: starting")
        b_running.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            raise
        yield TextDeltaEvent(text="b: should not arrive")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([_skill("skill_a"), _skill("skill_b")]),
    )

    starts: dict[str, ToolUseStartEvent] = {}
    results: dict[str, ToolResultEvent] = {}
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent) and ev.tool_name.startswith(
            "meta-step:",
        ):
            starts[ev.tool_use_id] = ev
        elif isinstance(ev, ToolResultEvent) and ev.tool_name.startswith(
            "meta-step:",
        ):
            # Keep the FIRST result we see per id — a real ToolResult
            # from the failing task should not be overwritten by a
            # synthetic one (and the orchestrator should not emit two
            # results for the same start anyway).
            results.setdefault(ev.tool_use_id, ev)

    assert final is not None and final.ok is False
    # Close-bracket invariant: every start has a matching result with
    # the same tool_use_id.
    assert set(starts.keys()) == set(results.keys()), (
        f"unbalanced brackets: starts={set(starts.keys())} "
        f"results={set(results.keys())}"
    )
    # Both meta-step:a and meta-step:b must be present.
    assert "meta_step_a" in starts
    assert "meta_step_b" in starts

    # Sibling B (cancelled) must carry is_error=True and a "cancelled" hint.
    b_result = results["meta_step_b"]
    assert b_result.is_error is True
    assert "cancel" in str(b_result.result).lower()


@pytest.mark.asyncio
async def test_linear_dag_event_order_preserved() -> None:
    """A→B→C chain keeps deterministic event ordering."""
    spec = _meta_spec(
        [
            {"id": "a", "skill": "skill_a", "with": {}},
            {"id": "b", "skill": "skill_b", "depends_on": ["a"], "with": {}},
            {"id": "c", "skill": "skill_c", "depends_on": ["b"], "with": {}},
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def runner(system_prompt: str, _u: str) -> AsyncIterator[AgentEvent]:
        for letter in ("A", "B", "C"):
            if f"SKILL_{letter}" in system_prompt:
                yield TextDeltaEvent(text=f"{letter}-done")
                break
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader(
            [_skill("skill_a"), _skill("skill_b"), _skill("skill_c")],
        ),
    )

    ordering: list[tuple[str, str]] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent) and ev.tool_name.startswith("meta-step:"):
            ordering.append(("start", ev.tool_name))
        elif isinstance(ev, ToolResultEvent) and ev.tool_name.startswith("meta-step:"):
            ordering.append(("end", ev.tool_name))

    assert final is not None and final.ok
    # Strict interleaving: start(a) → end(a) → start(b) → end(b) → start(c) → end(c)
    assert ordering == [
        ("start", "meta-step:a"),
        ("end", "meta-step:a"),
        ("start", "meta-step:b"),
        ("end", "meta-step:b"),
        ("start", "meta-step:c"),
        ("end", "meta-step:c"),
    ]


@pytest.mark.asyncio
async def test_max_parallelism_caps_concurrent_steps() -> None:
    """With max_parallelism=2 and 4 independent steps, no more than 2 are
    in-flight at any moment."""

    spec = _meta_spec(
        [
            {"id": f"s{i}", "skill": f"skill_{i}", "with": {}}
            for i in range(4)
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    in_flight = 0
    high_water_mark = 0
    lock = asyncio.Lock()

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        nonlocal in_flight, high_water_mark
        async with lock:
            in_flight += 1
            high_water_mark = max(high_water_mark, in_flight)
        try:
            await asyncio.sleep(0.05)
            yield TextDeltaEvent(text="done")
            yield DoneEvent(text="")
        finally:
            async with lock:
                in_flight -= 1

    loader = _FakeLoader([_skill(f"skill_{i}") for i in range(4)])
    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=loader,
        max_parallelism=2,
    )

    async for _ in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        pass

    assert high_water_mark <= 2, f"high_water_mark={high_water_mark}"


@pytest.mark.asyncio
async def test_max_parallelism_none_unbounded() -> None:
    """max_parallelism=None preserves the current 5-way fan-out."""

    spec = _meta_spec(
        [
            {"id": f"s{i}", "skill": f"skill_{i}", "with": {}}
            for i in range(5)
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    started_count = 0
    target_lock = asyncio.Lock()
    all_started = asyncio.Event()

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        nonlocal started_count
        async with target_lock:
            started_count += 1
            if started_count == 5:
                all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1.0)
        yield TextDeltaEvent(text="done")
        yield DoneEvent(text="")

    loader = _FakeLoader([_skill(f"skill_{i}") for i in range(5)])
    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=loader,
        max_parallelism=None,
    )

    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev

    assert final is not None and final.ok


@pytest.mark.asyncio
async def test_orchestrator_default_parallelism_caps_at_four() -> None:
    spec = _meta_spec(
        [
            {"id": f"s{i}", "skill": f"skill_{i}", "with": {}}
            for i in range(5)
        ],
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    in_flight = 0
    high_water_mark = 0
    lock = asyncio.Lock()

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        nonlocal in_flight, high_water_mark
        async with lock:
            in_flight += 1
            high_water_mark = max(high_water_mark, in_flight)
        try:
            await asyncio.sleep(0.05)
            yield TextDeltaEvent(text="done")
            yield DoneEvent(text="")
        finally:
            async with lock:
                in_flight -= 1

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([_skill(f"skill_{i}") for i in range(5)]),
    )

    async for _ in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        pass

    assert high_water_mark <= 4, f"high_water_mark={high_water_mark}"


@pytest.mark.asyncio
async def test_run_dag_emits_three_callback_types() -> None:
    """C3: scheduler must externalise step begin / done / failover events."""
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaStep

    plan = MetaPlan(
        name="cb-test",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(id="a", skill="alpha", kind="agent", on_failure="b"),
            MetaStep(id="b", skill="beta", kind="agent"),
        ),
    )
    match = MetaMatch(plan=plan, inputs={})
    begin_calls: list[tuple[str, str]] = []
    finish_calls: list[tuple[str, str]] = []
    failover_calls: list[tuple[str, str, str]] = []

    async def begin_cb(step_id: str, effective_skill: str, rendered_inputs: dict) -> None:
        begin_calls.append((step_id, effective_skill))

    async def finish_cb(
        step_id: str,
        status: str,
        output_text: str | None,
        error: str | None,
    ) -> None:
        finish_calls.append((step_id, status))

    async def failover_cb(failed_step_id: str, substitute_step_id: str, error: str) -> None:
        failover_calls.append((failed_step_id, substitute_step_id, error))

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        if step.id == "a":
            raise RuntimeError("alpha exploded")
        from opensquilla.skills.meta.events import _StepDone
        yield _StepDone(text="beta-output")

    async def preface_stub(step_id: str, effective_skill: str):
        if False:
            yield None
        return

    async for _ in run_dag(
        match,
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        max_parallelism=4,
        on_step_begin=begin_cb,
        on_step_finish=finish_cb,
        on_step_failover=failover_cb,
    ):
        pass

    assert ("a", "alpha") in begin_calls
    assert ("b", "beta") in begin_calls
    assert ("b", "ok") in finish_calls
    assert len(failover_calls) == 1
    assert failover_calls[0][:2] == ("a", "b")


@pytest.mark.asyncio
async def test_failed_fallback_replay_skips_paid_primary_and_runs_only_fallback() -> None:
    from types import SimpleNamespace

    from opensquilla.engine.agent import _trusted_meta_replay_seed_outputs
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.replay_safety import encode_paid_replay_safety
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="paid-fallback-replay",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(
                id="paid",
                skill="seedance-2-prompt",
                kind="skill_exec",
                on_failure="local_fallback",
                side_effect="external_paid_submit",
            ),
            MetaStep(id="local_fallback", skill="local-renderer", kind="agent"),
            MetaStep(
                id="deliver",
                skill="delivery",
                kind="agent",
                # Real workflows normally depend on the primary alias only;
                # successful failover resolves that alias for downstream work.
                depends_on=("paid",),
            ),
        ),
    )
    persisted = (
        SimpleNamespace(
            step_id="paid",
            status="substituted",
            substitute_step_id="local_fallback",
            output_text=None,
            error=encode_paid_replay_safety("lost POST response", safe_no_submit=False),
            truncated_fields=(),
        ),
        SimpleNamespace(
            step_id="local_fallback",
            status="failed",
            substitute_step_id=None,
            output_text=None,
            error="ffmpeg failed",
            truncated_fields=(),
        ),
    )
    aliases: dict[str, str] = {}
    seeds = _trusted_meta_replay_seed_outputs(
        plan=plan,
        persisted_steps=persisted,
        failed_step_id="local_fallback",
        replay_failover_aliases=aliases,
    )
    calls: list[str] = []
    fallback_completed = False

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        nonlocal fallback_completed
        calls.append(step.id)
        if step.id == "paid":
            raise AssertionError("paid primary must never be resubmitted")
        if step.id == "local_fallback":
            # With parallel scheduling, an empty primary seed used to release
            # `deliver` during this await. The primary must stay dependency-
            # pending until this fallback has produced the aliased output.
            await asyncio.sleep(0.02)
            fallback_completed = True
        if step.id == "deliver":
            assert fallback_completed is True
            assert outputs["paid"] == "output:local_fallback"
        yield _StepDone(text=f"output:{step.id}")

    async def preface_stub(step_id: str, effective_skill: str):
        if False:
            yield None

    result: MetaResult | None = None
    async for event in run_dag(
        MetaMatch(plan=plan, inputs={}),
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        seed_outputs=seeds,
        replay_failover_aliases=aliases,
        trusted_preflight_replay=True,
        max_parallelism=2,
    ):
        if isinstance(event, MetaResult):
            result = event

    assert seeds == {"paid": ""}
    assert aliases == {"local_fallback": "paid"}
    assert calls == ["local_fallback", "deliver"]
    assert result is not None and result.ok is True
    assert result.step_outputs["paid"] == "output:local_fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("safe_no_submit", "expected_disposition"),
    [(True, "safe_no_submit"), (False, "maybe_accepted")],
)
async def test_scheduler_exposes_only_parent_owned_paid_disposition_to_audit(
    safe_no_submit: bool,
    expected_disposition: str,
) -> None:
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.replay_safety import (
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        encode_paid_replay_safety,
    )
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="paid-disposition-channel",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(
                id="shot1_video",
                skill="seedance-2-prompt",
                kind="skill_exec",
                on_failure="shot1_video_fallback",
                side_effect="external_paid_submit",
            ),
            MetaStep(
                id="shot1_video_fallback",
                skill="video-still-animator",
                kind="skill_exec",
            ),
            MetaStep(
                id="delivery_audit",
                skill="short-drama-delivery-audit",
                kind="skill_exec",
                depends_on=("shot1_video", "shot1_video_fallback"),
            ),
        ),
    )
    observed: dict[str, str] = {}

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        del effective_skill, inputs
        if step.id == "shot1_video":
            raise RuntimeError(
                encode_paid_replay_safety(
                    "raw provider detail sk-secret must not cross",
                    safe_no_submit=safe_no_submit,
                )
            )
        if step.id == "delivery_audit":
            observed.update(
                json.loads(outputs[PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY])
            )
        yield _StepDone(text=f"output:{step.id}")

    async def preface_stub(step_id: str, effective_skill: str):
        del step_id, effective_skill
        if False:
            yield None

    result: MetaResult | None = None
    async for event in run_dag(
        MetaMatch(plan=plan, inputs={}),
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        max_parallelism=2,
    ):
        if isinstance(event, MetaResult):
            result = event

    assert observed == {"shot1_video": expected_disposition}
    assert "raw provider detail" not in json.dumps(observed)
    assert "sk-secret" not in json.dumps(observed)
    assert result is not None and result.ok is True
    assert PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY not in result.step_outputs


@pytest.mark.asyncio
async def test_scheduler_exposes_current_run_receipt_proof_only_to_runtime() -> None:
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.replay_safety import (
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
        PaidReceiptProofText,
    )
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaResult, MetaStep

    proof = "sha256:" + "a" * 64
    plan = MetaPlan(
        name="paid-receipt-proof-channel",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(
                id="shot1_video",
                skill="seedance-2-prompt",
                kind="skill_exec",
                side_effect="external_paid_submit",
            ),
            MetaStep(
                id="delivery_audit",
                skill="short-drama-delivery-audit",
                kind="skill_exec",
                depends_on=("shot1_video",),
                with_args={
                    "proofs": (
                        "{{ outputs.get("
                        "'__opensquilla_paid_submission_receipt_proofs_v1__', "
                        "'{}') }}"
                    ),
                },
            ),
        ),
    )
    observed: dict[str, dict[str, str]] = {}
    observed_begin: list[dict[str, Any]] = []

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        del effective_skill, inputs
        if step.id == "shot1_video":
            yield _StepDone(text=PaidReceiptProofText("clip.mp4", proof))
            return
        observed["dispositions"] = json.loads(
            outputs[PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY]
        )
        observed["proofs"] = json.loads(
            outputs[PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY]
        )
        yield _StepDone(text="audited")

    async def preface_stub(step_id: str, effective_skill: str):
        del step_id, effective_skill
        if False:
            yield None

    async def begin_stub(
        step_id: str,
        effective_skill: str,
        rendered_inputs: dict[str, Any],
    ) -> None:
        del effective_skill
        if step_id == "delivery_audit":
            observed_begin.append(rendered_inputs)

    result: MetaResult | None = None
    async for event in run_dag(
        MetaMatch(plan=plan, inputs={}),
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        on_step_begin=begin_stub,
    ):
        if isinstance(event, MetaResult):
            result = event

    assert observed == {
        "dispositions": {"shot1_video": "receipt"},
        "proofs": {"shot1_video": proof},
    }
    assert observed_begin == [{"proofs": "{}"}]
    assert result is not None and result.ok is True
    assert result.step_outputs["shot1_video"] == "clip.mp4"
    assert type(result.step_outputs["shot1_video"]) is str
    assert PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY not in result.step_outputs
    assert PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY not in result.step_outputs


@pytest.mark.asyncio
async def test_scheduler_preserves_current_receipt_proof_across_paid_failover() -> None:
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.replay_safety import (
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
        PaidReceiptProofError,
        encode_paid_replay_safety,
    )
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaStep

    proof = "sha256:" + "b" * 64
    plan = MetaPlan(
        name="paid-policy-receipt-failover",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(
                id="shot1_video",
                skill="seedance-2-prompt",
                kind="skill_exec",
                side_effect="external_paid_submit",
                on_failure="shot1_video_fallback",
            ),
            MetaStep(
                id="shot1_video_fallback",
                skill="video-still-animator",
                kind="skill_exec",
            ),
            MetaStep(
                id="delivery_audit",
                skill="short-drama-delivery-audit",
                kind="skill_exec",
                depends_on=("shot1_video", "shot1_video_fallback"),
            ),
        ),
    )
    observed: dict[str, dict[str, str]] = {}

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        del effective_skill, inputs
        if step.id == "shot1_video":
            raise PaidReceiptProofError(
                encode_paid_replay_safety(
                    "provider policy rejection recorded",
                    safe_no_submit=False,
                ),
                receipt_proof=proof,
            )
        if step.id == "delivery_audit":
            observed["dispositions"] = json.loads(
                outputs[PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY]
            )
            observed["proofs"] = json.loads(
                outputs[PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY]
            )
        yield _StepDone(text=f"output:{step.id}")

    async def preface_stub(step_id: str, effective_skill: str):
        del step_id, effective_skill
        if False:
            yield None

    async for _event in run_dag(
        MetaMatch(plan=plan, inputs={}),
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
    ):
        pass

    assert observed == {
        "dispositions": {"shot1_video": "maybe_accepted"},
        "proofs": {"shot1_video": proof},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved_key",
    [
        "__opensquilla_paid_submission_dispositions_v1__",
        "__opensquilla_paid_submission_receipt_proofs_v1__",
    ],
)
async def test_paid_runtime_key_cannot_be_claimed_as_a_plan_step(
    reserved_key: str,
) -> None:
    from opensquilla.skills.meta.replay_safety import (
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
    )
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaResult, MetaStep

    plan = MetaPlan(
        name="reserved-runtime-step",
        triggers=("x",),
        priority=1,
        steps=(
            MetaStep(
                id=reserved_key,
                skill="untrusted-child",
                kind="skill_exec",
            ),
        ),
    )
    dispatched = False

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        nonlocal dispatched
        del step, effective_skill, inputs, outputs
        dispatched = True
        if False:
            yield None

    async def preface_stub(step_id: str, effective_skill: str):
        del step_id, effective_skill
        if False:
            yield None

    result: MetaResult | None = None
    async for event in run_dag(
        MetaMatch(plan=plan, inputs={}),
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
    ):
        if isinstance(event, MetaResult):
            result = event

    assert dispatched is False
    assert reserved_key in {
        PAID_SUBMISSION_DISPOSITIONS_OUTPUT_KEY,
        PAID_SUBMISSION_RECEIPT_PROOFS_OUTPUT_KEY,
    }
    assert result is not None and result.ok is False
    assert result.error == "meta-skill plan uses a reserved runtime step id"


def test_paid_disposition_encoder_is_bounded_and_rejects_free_form_values() -> None:
    from opensquilla.skills.meta.replay_safety import (
        encode_paid_submission_dispositions,
    )

    dispositions = {
        f"paid_{index:03d}_{'x' * 80}": "receipt"
        for index in range(100)
    }
    dispositions["provider_error"] = "HTTP 429 sk-secret raw provider prose"

    encoded = encode_paid_submission_dispositions(dispositions)
    decoded = json.loads(encoded)

    assert len(encoded.encode("utf-8")) <= 8_000
    assert len(decoded) == 64
    assert set(decoded.values()) == {"receipt"}
    assert "HTTP 429" not in encoded
    assert "sk-secret" not in encoded


def test_paid_receipt_proof_encoder_accepts_only_canonical_digests() -> None:
    from opensquilla.skills.meta.replay_safety import (
        encode_paid_submission_receipt_proofs,
    )

    encoded = encode_paid_submission_receipt_proofs(
        {
            "shot1_video": "sha256:" + "a" * 64,
            "shot2_video": "sha256:not-a-digest",
            "provider_error": "sk-secret raw provider prose",
        }
    )

    assert json.loads(encoded) == {"shot1_video": "sha256:" + "a" * 64}
    assert "sk-secret" not in encoded


def test_paid_failure_rescue_requires_review_unless_pre_submit_is_proven() -> None:
    from opensquilla.skills.meta.replay_safety import encode_paid_replay_safety
    from opensquilla.skills.meta.scheduler import _failure_rescue_payload
    from opensquilla.skills.meta.types import MetaStep

    step = MetaStep(
        id="paid",
        skill="seedance-2-prompt",
        kind="skill_exec",
        side_effect="external_paid_submit",
    )
    unsafe = _failure_rescue_payload(
        plan_name="short-drama",
        step=step,
        error=encode_paid_replay_safety("lost response", safe_no_submit=False),
        outputs={},
        has_substitute=False,
        plan_has_external_paid_steps=True,
    )
    safe = _failure_rescue_payload(
        plan_name="short-drama",
        step=step,
        error=encode_paid_replay_safety("bad local input", safe_no_submit=True),
        outputs={},
        has_substitute=False,
        plan_has_external_paid_steps=True,
    )

    assert {action["id"] for action in unsafe["actions"]} == {
        "review-paid-submit",
        "switch-meta-skill",
        "continue-text-only",
    }
    assert "retry-step" in {action["id"] for action in safe["actions"]}
    assert "retry-run" not in {action["id"] for action in safe["actions"]}


@pytest.mark.parametrize("record_shape", ["missing", "duplicate", "wrong-status", "wrong-link"])
def test_paid_fallback_replay_blocks_without_exact_substituted_primary(
    record_shape: str,
) -> None:
    from types import SimpleNamespace

    from opensquilla.skills.meta.replay_safety import paid_live_replay_block_reason
    from opensquilla.skills.meta.types import MetaPlan, MetaStep

    plan = MetaPlan(
        name="paid-fallback",
        triggers=("x",),
        priority=1,
        steps=(
            MetaStep(
                id="paid",
                skill="seedance-2-prompt",
                kind="skill_exec",
                on_failure="fallback",
                side_effect="external_paid_submit",
            ),
            MetaStep(id="fallback", skill="local", kind="agent"),
        ),
    )
    valid = SimpleNamespace(
        step_id="paid",
        status="substituted",
        substitute_step_id="fallback",
        error="possibly accepted",
    )
    if record_shape == "missing":
        records = ()
    elif record_shape == "duplicate":
        records = (valid, valid)
    elif record_shape == "wrong-status":
        records = (SimpleNamespace(**{**vars(valid), "status": "failed"}),)
    else:
        records = (SimpleNamespace(**{**vars(valid), "substitute_step_id": "other"}),)

    reason = paid_live_replay_block_reason(
        plan=plan,
        persisted_steps=records,
        failed_step_id="fallback",
    )

    assert reason is not None
    assert "paid" in reason.lower() or "provider" in reason.lower()


@pytest.mark.parametrize("evidence", ["missing-output", "truncated-output", "duplicate"])
def test_live_replay_blocks_unseedable_successful_paid_sibling(evidence: str) -> None:
    from types import SimpleNamespace

    from opensquilla.skills.meta.replay_safety import paid_live_replay_block_reason
    from opensquilla.skills.meta.types import MetaPlan, MetaStep

    plan = MetaPlan(
        name="paid-sibling",
        triggers=("x",),
        priority=1,
        steps=(
            MetaStep(
                id="paid",
                skill="seedance-2-prompt",
                kind="skill_exec",
                side_effect="external_paid_submit",
            ),
            MetaStep(id="later", skill="audit", kind="agent", depends_on=("paid",)),
        ),
    )
    paid = SimpleNamespace(
        step_id="paid",
        status="ok",
        substitute_step_id=None,
        output_text=(None if evidence == "missing-output" else "video.mp4"),
        error=None,
        truncated_fields=("output_text",) if evidence == "truncated-output" else (),
    )
    records = (paid, paid) if evidence == "duplicate" else (paid,)

    reason = paid_live_replay_block_reason(
        plan=plan,
        persisted_steps=records,
        failed_step_id="later",
    )

    assert reason is not None
    assert "paid" in reason.lower()


def test_paid_plan_failed_fallback_recovery_omits_fresh_run_retry() -> None:
    from opensquilla.skills.meta.run_reports import _recovery_actions
    from opensquilla.skills.meta.types import MetaStep

    actions = _recovery_actions(
        MetaStep(id="local_fallback", skill="local-renderer", kind="skill_exec"),
        "local ffmpeg failed",
        plan_has_external_paid_steps=True,
    )
    action_ids = {action["id"] for action in actions}

    assert "retry-run" not in action_ids
    assert "retry-step" in action_ids


@pytest.mark.asyncio
async def test_run_dag_on_step_begin_gets_rendered_inputs() -> None:
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaStep

    plan = MetaPlan(
        name="rendered-inputs-test",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(
                id="a",
                skill="alpha",
                kind="agent",
                with_args={"q": "hello {{ inputs.user_message }}"},
            ),
        ),
    )
    match = MetaMatch(plan=plan, inputs={"user_message": "world"})
    begin_inputs: list[dict[str, Any]] = []

    async def begin_cb(
        step_id: str,
        effective_skill: str,
        rendered_inputs: dict[str, Any],
    ) -> None:
        begin_inputs.append(rendered_inputs)

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        from opensquilla.skills.meta.events import _StepDone
        yield _StepDone(text="ok")

    async def preface_stub(step_id: str, effective_skill: str):
        if False:
            yield None
        return

    async for _ in run_dag(
        match,
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        on_step_begin=begin_cb,
    ):
        pass

    assert begin_inputs == [{"q": "hello world"}]


@pytest.mark.asyncio
async def test_run_dag_tool_result_includes_scoped_step_usage() -> None:
    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaStep

    plan = MetaPlan(
        name="step-usage-test",
        triggers=("x",),
        priority=10,
        steps=(
            MetaStep(id="a", skill="alpha", kind="llm_chat"),
            MetaStep(id="b", skill="beta", kind="llm_chat"),
        ),
    )
    match = MetaMatch(plan=plan, inputs={})
    tracker = UsageTracker()
    results: dict[str, ToolResultEvent] = {}

    async def dispatch_stub(step, effective_skill, inputs, outputs):
        tracker.add(
            "session-a",
            input_tokens=10 if step.id == "a" else 20,
            output_tokens=1 if step.id == "a" else 2,
            model_id=f"model-{step.id}",
            billed_cost=0.123 if step.id == "a" else 0.0,
        )
        yield _StepDone(text=f"{step.id}-done")

    async def preface_stub(step_id: str, effective_skill: str):
        if False:
            yield None
        return

    async for event in run_dag(
        match,
        dispatch_step_stream=dispatch_stub,
        yield_skill_view_preface=preface_stub,
        max_parallelism=2,
        usage_tracker=tracker,
        session_key="session-a",
        usage_scope_prefix="run-1",
    ):
        if isinstance(event, ToolResultEvent):
            results[event.tool_name] = event

    usage_a = (results["meta-step:a"].arguments or {})["usage"]
    usage_b = (results["meta-step:b"].arguments or {})["usage"]
    assert usage_a["input_tokens"] == 10
    assert usage_a["output_tokens"] == 1
    assert usage_a["total_tokens"] == 11
    assert usage_a["model"] == "model-a"
    assert usage_a["cost_usd"] == pytest.approx(0.123)
    assert usage_a["billed_cost"] == pytest.approx(0.123)
    assert usage_a["billed_cost_usd"] == pytest.approx(0.123)
    assert usage_a["cost_source"] == "provider_billed"
    assert usage_a["is_provider_billed"] is True
    assert usage_b["input_tokens"] == 20
    assert usage_b["output_tokens"] == 2
    assert usage_b["total_tokens"] == 22
    assert usage_b["model"] == "model-b"
    assert usage_b["billed_cost"] == 0.0
    assert usage_b["is_provider_billed"] is False
