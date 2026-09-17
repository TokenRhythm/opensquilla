"""scheduler 在 step 开始/成功时分别发出 meta_step_state(running/succeeded)。"""

import asyncio

import pytest

from opensquilla.engine.types import (
    MetaPreflightEvent,
    MetaRunAnnouncedEvent,
    MetaRunCompletedEvent,
    MetaStepStateEvent,
    ToolResultEvent,
)
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.scheduler import _failure_hint, _failure_rescue_payload
from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaMatch,
    MetaPaused,
    MetaPlan,
    MetaPreflightRequired,
    MetaResult,
    MetaStep,
)


@pytest.fixture
def make_two_step_match():
    plan = MetaPlan(
        name="meta-fake",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(id="intake", skill="intake", kind="llm_chat", label="意图提取"),
            MetaStep(
                id="summary", skill="summary", kind="llm_chat",
                label="总结", depends_on=("intake",),
            ),
        ),
        final_text_mode="raw",
    )
    return MetaMatch(plan=plan, inputs={"user_message": "hi"})


@pytest.fixture
def fake_dispatch_stream():
    async def _dispatch(step, effective_skill, inputs, outputs):
        yield _StepDone(text=f"out:{step.id}")

    return _dispatch


@pytest.fixture
def fake_preface():
    async def _preface(step_id, effective_skill):
        return
        yield  # never reached; keeps it an async generator

    return _preface


async def _collect_all_events(match, dispatch, preface):
    from opensquilla.skills.meta.scheduler import run_dag

    events = []
    async for ev in run_dag(
        match,
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=preface,
    ):
        events.append(ev)
    return events


def test_running_emitted_at_step_start(
    make_two_step_match, fake_dispatch_stream, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_two_step_match, fake_dispatch_stream, fake_preface,
    ))

    step_states = [
        (ev.step_id, ev.state)
        for ev in events
        if isinstance(ev, MetaStepStateEvent)
    ]
    assert ("intake", "running") in step_states
    assert ("intake", "succeeded") in step_states


def test_required_preflight_pauses_before_announcing_run(fake_dispatch_stream, fake_preface):
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(MetaStep(id="draft", skill="draft", kind="llm_chat", label="Draft"),),
        request_template={
            "mode": "confirm",
            "fields": [
                {"name": "audience", "required": True},
                {"name": "deadline", "required": False},
            ],
            "assumptions": ["Use default depth unless the user says otherwise."],
        },
        final_text_mode="raw",
    )

    events = asyncio.run(_collect_all_events(
        MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        fake_dispatch_stream,
        fake_preface,
    ))

    preflight = next(e for e in events if isinstance(e, MetaPreflightEvent))
    result = next(e for e in events if isinstance(e, MetaResult))
    assert preflight.requires_confirmation is True
    assert preflight.can_skip is False
    assert preflight.missing_fields == ["audience"]
    assert not any(isinstance(e, MetaRunAnnouncedEvent) for e in events)
    assert not any(isinstance(e, MetaStepStateEvent) for e in events)
    assert result.paused is True
    assert isinstance(result.paused_payload, MetaPreflightRequired)
    assert result.paused_payload.missing_fields == ["audience"]


def test_preview_preflight_event_does_not_pause_without_explicit_gate(
    fake_dispatch_stream,
    fake_preface,
):
    plan = MetaPlan(
        name="meta-preflight-preview",
        triggers=("fake",),
        priority=0,
        steps=(MetaStep(id="draft", skill="draft", kind="llm_chat", label="Draft"),),
        request_template={
            "fields": [{"name": "audience", "required": True}],
            "assumptions": ["Use default depth unless the user says otherwise."],
        },
        final_text_mode="raw",
    )

    events = asyncio.run(_collect_all_events(
        MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        fake_dispatch_stream,
        fake_preface,
    ))

    preflight = next(e for e in events if isinstance(e, MetaPreflightEvent))
    result = next(e for e in events if isinstance(e, MetaResult))
    assert preflight.requires_confirmation is False
    assert preflight.can_skip is True
    assert preflight.missing_fields == ["audience"]
    assert any(isinstance(e, MetaRunAnnouncedEvent) for e in events)
    assert any(isinstance(e, MetaStepStateEvent) for e in events)
    assert result.paused is False
    assert result.ok is True


def test_confirmed_preflight_still_pauses_when_required_fields_missing(
    fake_dispatch_stream,
    fake_preface,
):
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(MetaStep(id="draft", skill="draft", kind="llm_chat", label="Draft"),),
        request_template={
            "mode": "confirm",
            "fields": [{"name": "audience", "required": True}],
            "assumptions": ["Use default depth unless the user says otherwise."],
        },
        final_text_mode="raw",
    )

    events = asyncio.run(_collect_all_events(
        MetaMatch(
            plan=plan,
            inputs={
                "user_message": "write a brief",
                "meta_preflight_confirmed": True,
            },
        ),
        fake_dispatch_stream,
        fake_preface,
    ))

    result = next(e for e in events if isinstance(e, MetaResult))
    assert not any(isinstance(e, MetaRunAnnouncedEvent) for e in events)
    assert result.paused is True
    assert isinstance(result.paused_payload, MetaPreflightRequired)
    assert result.paused_payload.missing_fields == ["audience"]


def test_running_precedes_succeeded(
    make_two_step_match, fake_dispatch_stream, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_two_step_match, fake_dispatch_stream, fake_preface,
    ))

    seq = [
        ev.state
        for ev in events
        if isinstance(ev, MetaStepStateEvent) and ev.step_id == "intake"
    ]
    assert seq.index("running") < seq.index("succeeded")


@pytest.fixture
def make_skipped_match():
    plan = MetaPlan(
        name="meta-skip-fake",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(id="intake", skill="intake", kind="llm_chat", label="意图提取"),
            MetaStep(
                id="optional", skill="optional", kind="llm_chat",
                label="可选", depends_on=("intake",), when="False",
            ),
        ),
        final_text_mode="raw",
    )
    return MetaMatch(plan=plan, inputs={"user_message": "hi"})


def test_skipped_emitted_on_when_false(
    make_skipped_match, fake_dispatch_stream, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_skipped_match, fake_dispatch_stream, fake_preface,
    ))

    states = [
        (ev.step_id, ev.state)
        for ev in events
        if isinstance(ev, MetaStepStateEvent)
    ]
    assert ("optional", "skipped") in states


@pytest.fixture
def failing_dispatch():
    async def _dispatch(step, effective_skill, inputs, outputs):
        if step.id == "search":
            raise RuntimeError("simulated step failure")
        yield _StepDone(text=f"out:{step.id}")

    return _dispatch


@pytest.fixture
def make_failover_match():
    plan = MetaPlan(
        name="meta-fail-fake",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(
                id="search", skill="search", kind="agent", label="检索",
                on_failure="search_fallback",
            ),
            MetaStep(
                id="search_fallback", skill="search_fallback",
                kind="llm_chat", label="替代检索",
            ),
        ),
        final_text_mode="raw",
    )
    return MetaMatch(plan=plan, inputs={"user_message": "hi"})


def test_failed_then_substituted(
    make_failover_match, failing_dispatch, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_failover_match, failing_dispatch, fake_preface,
    ))

    states = [
        (ev.step_id, ev.state, ev.substitute_for)
        for ev in events
        if isinstance(ev, MetaStepStateEvent)
    ]
    assert ("search", "failed", None) in states
    assert ("search_fallback", "substituted", "search") in states


def test_successful_substitute_completes_run_ok(
    make_failover_match, failing_dispatch, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_failover_match, failing_dispatch, fake_preface,
    ))
    completed = next(
        (e for e in events if isinstance(e, MetaRunCompletedEvent)), None,
    )

    assert completed is not None
    assert completed.outcome == "ok"
    assert completed.failed_steps == []
    assert completed.recovered_steps == ["search"]


def test_hard_failure_tool_result_includes_rescue_hints(fake_preface):
    plan = MetaPlan(
        name="meta-hard-fail",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(id="intake", skill="intake", kind="llm_chat", label="Intake"),
            MetaStep(
                id="render",
                skill="render",
                kind="skill_exec",
                label="Render PDF",
                depends_on=("intake",),
            ),
        ),
        final_text_mode="raw",
    )

    async def dispatch(step, effective_skill, inputs, outputs):
        if step.id == "render":
            raise RuntimeError("wkhtmltopdf not found")
        yield _StepDone(text=f"out:{step.id}")

    events = asyncio.run(_collect_all_events(
        MetaMatch(plan=plan, inputs={"user_message": "hi"}),
        dispatch,
        fake_preface,
    ))
    failure = next(
        (
            e for e in events
            if isinstance(e, ToolResultEvent) and e.tool_name == "meta-step:render"
        ),
        None,
    )

    assert failure is not None
    rescue = (failure.arguments or {}).get("rescue")
    assert rescue["failed_step_id"] == "render"
    assert rescue["partial_output_step_ids"] == ["intake"]
    assert [item["step_id"] for item in rescue["prior_outputs"]] == ["intake"]
    action_ids = [item["id"] for item in rescue["actions"]]
    assert "retry-run" in action_ids
    assert "retry-with-partial-context" in action_ids
    assert rescue["hint"]["category"] == "missing_dependency"
    failed_state = next(
        (
            e for e in events
            if isinstance(e, MetaStepStateEvent)
            and e.step_id == "render"
            and e.state == "failed"
        ),
        None,
    )
    assert failed_state is not None
    assert failed_state.rescue["hint"]["category"] == "missing_dependency"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        ("401 unauthorized: token expired", "auth_or_permission"),
        ("request timed out after 30s", "timeout"),
        ("unexpected parser crash", "runtime_error"),
    ],
)
def test_failure_hint_classifies_recovery_category(error: str, category: str) -> None:
    assert _failure_hint(error)["category"] == category


def test_failure_rescue_payload_includes_actionable_recovery_context() -> None:
    payload = _failure_rescue_payload(
        plan_name="meta-fake",
        step=MetaStep(id="render_artifact", skill="render", kind="skill_exec"),
        error="ffmpeg command not found",
        outputs={"draft": "x" * 900},
        has_substitute=False,
    )

    action_ids = [action["id"] for action in payload["actions"]]
    assert "retry-step" in action_ids
    assert "install-dependency" in action_ids
    assert "continue-text-only" in action_ids
    assert payload["prior_outputs"][0]["step_id"] == "draft"
    assert len(payload["prior_outputs"][0]["excerpt"]) < 700
    assert payload["prior_outputs"][0]["truncated"] is True


def test_run_completed_emitted_at_end(
    make_two_step_match, fake_dispatch_stream, fake_preface,
):
    events = asyncio.run(_collect_all_events(
        make_two_step_match, fake_dispatch_stream, fake_preface,
    ))
    completed = next(
        (e for e in events if isinstance(e, MetaRunCompletedEvent)), None,
    )

    assert completed is not None
    assert completed.outcome == "ok"
    assert sorted(completed.completed_steps) == ["intake", "summary"]


def test_paused_step_completes_run_cancelled(fake_preface):
    cfg = ClarifyStepConfig(
        mode="form",
        fields=(ClarifyField(name="topic", type="string", required=True),),
    )
    plan = MetaPlan(
        name="meta-pause-fake",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(id="collect", skill="collect", kind="user_input", label="澄清"),
        ),
        final_text_mode="raw",
    )

    async def paused_dispatch(step, effective_skill, inputs, outputs):
        raise MetaPaused(run_id="r-paused", step_id=step.id, schema=cfg)
        yield  # never reached; keeps it an async generator

    events = asyncio.run(_collect_all_events(
        MetaMatch(plan=plan, inputs={"user_message": "hi"}),
        paused_dispatch,
        fake_preface,
    ))
    completed = next(
        (e for e in events if isinstance(e, MetaRunCompletedEvent)), None,
    )

    assert completed is not None
    assert completed.outcome == "cancelled"
