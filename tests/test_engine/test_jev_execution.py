"""Background advice pauses only a ready switch at a complete model boundary."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolResult
from opensquilla.engine.jev_execution import JevExecution
from opensquilla.engine.pipeline import TurnContext
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_service import JevService
from opensquilla.provider import ContentBlockToolResult, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ModelCapabilities
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:checkpoint-tests"
LARGE = "synthetic-reasoning-model"
SMALL = "synthetic-efficient-model"


class RecordingProvider:
    provider_name = "tokenrhythm"

    def __init__(self, model, *, tools=False):
        self.model = model
        self.uses_tools = tools
        self.requests = []
        self.configs = []

    async def chat(self, messages, *, tools=None, config=None):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        self.configs.append(config)
        if self.uses_tools and len(self.requests) == 1:
            yield ProviderToolStart(tool_use_id="probe-1", tool_name="probe")
            yield ProviderToolEnd(tool_use_id="probe-1", tool_name="probe", arguments={})
            yield ProviderDone(stop_reason="tool_calls", input_tokens=4, output_tokens=2)
        else:
            yield ProviderText(text="Synthetic task complete.")
            yield ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=2)

    async def list_models(self):
        return []


@pytest.fixture
async def runtime(monkeypatch):
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(KEY)
        config = GatewayConfig(
            llm={"provider": "tokenrhythm", "model": LARGE, "api_key": "synthetic-key"},
            jev_router={"enabled": True, "auto_switch_seconds": 0},
            squilla_router={"enabled": False},
        )
        selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
            provider="tokenrhythm", model=LARGE, api_key="synthetic-key",
        )))
        providers = {LARGE: RecordingProvider(LARGE), SMALL: RecordingProvider(SMALL)}
        monkeypatch.setattr(
            ModelSelector, "resolve", lambda self: providers[self.current_config.model],
        )
        service = JevService()
        await service.bind_session(KEY, session.session_id, session.epoch)
        caps = ModelCapabilities(supports_tools=True)
        catalog = SimpleNamespace(lookup_deployment=lambda *_args, **_kwargs: SimpleNamespace(
            capabilities=caps, vision_support="unsupported", context_window=32_768,
            context_window_known=True, max_tokens=2_048, provider_request_proof_max_chars=0,
        ))
        turn = TurnContext(
            message="Inspect a synthetic service.", session_key=KEY, config=config,
            provider=providers[LARGE], model=LARGE, tool_defs=[], system_prompt="",
        )
        controller = JevExecution(
            turn=turn, service=service, session_manager=manager, selector=selector, catalog=catalog,
            expected_session_id=session.session_id, expected_session_epoch=session.epoch,
        )
        agent = Agent(
            provider=providers[LARGE], config=AgentConfig(
                model_id=LARGE, provider_id="tokenrhythm", timeout=5,
                context_window_tokens=65_536, context_window_known=True, max_tokens=4_096,
                model_capabilities=caps, model_vision_support="unsupported",
            ),
        )
        agent.set_model_advice_boundary(controller.before_request)
        yield SimpleNamespace(
            manager=manager, session=session, config=config, selector=selector,
            providers=providers, service=service, turn=turn, controller=controller, agent=agent,
        )
        await controller.close()
        await service.close()


async def prepare(runtime, *, status="pending", action="switch"):
    r = runtime

    async def evaluate(generation):
        result = replace(r.turn, metadata={})
        wire = {
            "action": action, "current": {"provider": "tokenrhythm", "model": LARGE},
            "target": {"provider": "tokenrhythm", "model": SMALL},
            "reason": "This synthetic task needs a lower cost deployment.",
            "evidence": ["cost_fit"], "risk": "low", "source": "input",
        }
        record = await r.service.publish(
            KEY, wire, input_id="ready-check", routing_revision=0,
            check_generation=generation, auto_switch_seconds=0,
        )
        if status != "pending":
            await r.service.respond(KEY, record["id"], status == "accepted")
        result.metadata["jev_decision_id"] = record["id"]
        return result

    task = await r.service.start_check(KEY, r.controller.fingerprint, evaluate)
    result = await task
    return result.metadata["jev_decision_id"]


async def test_initial_assessment_finishes_before_provider_dispatch(runtime) -> None:
    r = runtime
    started = asyncio.Event()
    finish = asyncio.Event()

    async def evaluate(generation):
        started.set()
        await finish.wait()
        return replace(r.turn, metadata={})

    check = await r.service.start_check(KEY, r.controller.fingerprint, evaluate)
    await started.wait()
    run = asyncio.create_task(_collect(r.agent))
    await asyncio.sleep(0.02)
    assert not r.providers[LARGE].requests and not run.done()
    finish.set()
    events = await asyncio.wait_for(run, 1)
    assert len(r.providers[LARGE].requests) == 1
    assert not r.providers[SMALL].requests
    assert check.done()
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_pending_loop_assessment_does_not_block_provider_dispatch(runtime) -> None:
    r = runtime
    r.controller.initial_assessment_complete = True
    started = asyncio.Event()

    async def evaluate(generation):
        started.set()
        await asyncio.Event().wait()

    r.controller.task = await r.service.start_check(KEY, "checkpoint-pending", evaluate)
    await started.wait()
    events = await asyncio.wait_for(_collect(r.agent), 1)
    assert len(r.providers[LARGE].requests) == 1
    assert not r.controller.task.done()
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_failed_initial_assessment_retains_original_provider(runtime) -> None:
    r = runtime

    async def evaluate(generation):
        raise RuntimeError("Synthetic assessment outage")

    await r.service.start_check(KEY, r.controller.fingerprint, evaluate)
    events = await asyncio.wait_for(_collect(r.agent), 1)
    assert len(r.providers[LARGE].requests) == 1
    assert not r.providers[SMALL].requests
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_default_checkpoint_runs_after_five_completed_requests(runtime, monkeypatch) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    assert r.config.jev_router.loop_interval == 5
    for iteration in range(1, 6):
        await r.controller.before_request(r.agent, iteration, [])
    assert check.await_count == 1
    await r.controller.before_request(r.agent, 6, [])
    await r.controller.task
    assert check.await_count == 2
    assert check.await_args.kwargs["assessment_stage"] == "loop"
    assert check.await_args.kwargs["loop_iteration"] == 6


def _tool_outcome(identity, *, failed=True, text="Synthetic tool failed."):
    return Message(role="user", content=[ContentBlockToolResult(
        tool_use_id=identity, content=text, is_error=failed,
    )])


async def test_two_new_tool_failures_start_background_assessment_before_five_rounds(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    messages = [_tool_outcome("failure-one", text="No such file: synthetic-input.txt.")]
    await r.controller.before_request(r.agent, 2, messages)
    assert check.await_count == 1
    messages.append(_tool_outcome("failure-two", text="Permission denied for synthetic service."))
    assert not await r.controller.before_request(r.agent, 3, messages)
    await r.controller.task
    assert check.await_count == 2
    context = check.await_args.args[0]
    assert context.metadata["jev_tool_failure_count"] == 2
    assert context.metadata["jev_assessment_trigger"] == "tool_failures"
    assert check.await_args.kwargs["loop_iteration"] == 3
    evidence = check.await_args.kwargs["recent_tool_results"]
    assert len(evidence) == 2 and all(len(item) <= 1200 for item in evidence)
    assert any("No such file" in item for item in evidence)
    assert any("Permission denied" in item for item in evidence)
    assert r.selector.current_config.model == LARGE
    assert not (await r.service.snapshot(KEY, r.config.jev_router))["blocked"]


async def test_failure_assessment_stays_async_and_does_not_repeat_the_first_input_barrier(
    runtime, monkeypatch,
) -> None:
    r = runtime
    started = asyncio.Event()

    async def check(turn, **options):
        if options["assessment_stage"] == "loop":
            started.set()
            await asyncio.Event().wait()
        return turn

    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    failures = [_tool_outcome("fresh-a"), _tool_outcome("fresh-b")]
    await asyncio.wait_for(r.controller.before_request(r.agent, 2, failures), 0.5)
    await asyncio.wait_for(started.wait(), 1)
    events = await asyncio.wait_for(_collect(r.agent), 1)
    assert len(r.providers[LARGE].requests) == 1
    assert not r.controller.task.done()
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_historical_failure_replay_and_duplicate_retry_ids_do_not_count_twice(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    messages = [_tool_outcome("old-a"), _tool_outcome("old-b")]
    await r.controller.before_request(r.agent, 1, messages)
    messages.extend([_tool_outcome("new-a"), _tool_outcome("new-a")])
    await r.controller.before_request(r.agent, 2, messages)
    await r.controller.before_request(r.agent, 3, list(messages))
    assert check.await_count == 1
    messages.append(_tool_outcome("new-b"))
    await r.controller.before_request(r.agent, 4, messages)
    await r.controller.task
    assert check.await_count == 2
    assert check.await_args.args[0].metadata["jev_tool_failure_count"] == 2


async def test_success_between_failures_resets_consecutive_count_and_error_words_do_not_count(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    messages = [
        Message(role="assistant", content="The sample text contains error and failed."),
        _tool_outcome("first-error"),
        _tool_outcome("success", failed=False, text="Search found the quoted text: error failed."),
        _tool_outcome("second-error"),
    ]
    await r.controller.before_request(r.agent, 2, messages)
    assert check.await_count == 1
    messages.append(_tool_outcome("third-error"))
    await r.controller.before_request(r.agent, 3, messages)
    await r.controller.task
    assert check.await_count == 2


async def test_failures_do_not_replace_an_in_flight_check(runtime, monkeypatch) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    started, finish = asyncio.Event(), asyncio.Event()

    async def pending(generation):
        started.set()
        await finish.wait()
        return replace(r.turn, metadata={})

    pending_task = await r.service.start_check(KEY, "synthetic-pending-assessment", pending)
    r.controller.task = pending_task
    await started.wait()
    messages = [_tool_outcome("pending-a"), _tool_outcome("pending-b")]
    await r.controller.before_request(r.agent, 2, messages)
    assert r.controller.task is pending_task and not pending_task.cancelled()
    assert check.await_count == 1
    finish.set()
    await pending_task
    await r.controller.before_request(r.agent, 3, messages)
    await r.controller.task
    assert check.await_count == 2
    assert check.await_args.args[0].metadata["jev_tool_failure_count"] == 2


async def test_each_early_assessment_needs_new_failures_and_keeps_five_round_schedule(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    messages = [_tool_outcome("batch-a"), _tool_outcome("batch-b")]
    await r.controller.before_request(r.agent, 2, messages)
    await r.controller.task
    messages.append(_tool_outcome("batch-c"))
    await r.controller.before_request(r.agent, 3, messages)
    assert check.await_count == 2
    messages.append(_tool_outcome("batch-d"))
    await r.controller.before_request(r.agent, 4, messages)
    await r.controller.task
    assert check.await_count == 3
    await r.controller.before_request(r.agent, 5, messages)
    assert check.await_count == 3
    await r.controller.before_request(r.agent, 6, messages)
    await r.controller.task
    assert check.await_count == 4
    assert check.await_args.args[0].metadata["jev_tool_failure_count"] == 0


async def test_parallel_failure_pair_is_observed_even_if_another_call_succeeds_after_it(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    await r.controller.before_request(r.agent, 2, [
        _tool_outcome("parallel-a"), _tool_outcome("parallel-b"),
        _tool_outcome("parallel-c", failed=False, text="A separate probe succeeded."),
    ])
    await r.controller.task
    assert check.await_count == 2
    assert check.await_args.args[0].metadata["jev_tool_failure_count"] == 2


async def test_an_existing_positive_choice_keeps_priority_over_new_tool_failures(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    await r.controller.before_request(r.agent, 1, [])
    await r.service.cancel_checks(KEY)
    identifier = await prepare(r)
    r.controller.task = r.service.current_check(KEY, r.controller.fingerprint)
    blocked = asyncio.Event()

    async def changed(key):
        if (await r.service.snapshot(key, r.config.jev_router))["blocked"]:
            blocked.set()

    r.service.on_change = changed
    boundary = asyncio.create_task(r.controller.before_request(r.agent, 2, [
        _tool_outcome("waiting-a"), _tool_outcome("waiting-b"),
    ]))
    await asyncio.wait_for(blocked.wait(), 1)
    assert check.await_count == 1 and not boundary.done()
    await r.service.respond(KEY, identifier, True)
    assert await asyncio.wait_for(boundary, 1)
    assert r.selector.current_config.model == SMALL
    assert check.await_count == 1
    assert r.controller._pending_tool_failure_count == 0


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("action", ["switch", "review"])
async def test_ready_draft_pauses_first_request_until_choice(runtime, accepted, action) -> None:
    r = runtime
    identifier = await prepare(r, action=action)
    blocked = asyncio.Event()

    async def changed(key):
        snapshot = await r.service.snapshot(key, r.config.jev_router)
        if snapshot["blocked"]:
            blocked.set()

    r.service.on_change = changed
    run = asyncio.create_task(_collect(r.agent))
    await asyncio.wait_for(blocked.wait(), 1)
    assert not r.providers[LARGE].requests and not r.providers[SMALL].requests
    await r.service.respond(KEY, identifier, accepted)
    events = await asyncio.wait_for(run, 2)
    chosen = SMALL if accepted else LARGE
    assert len(r.providers[chosen].requests) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)
    if accepted:
        assert r.agent.config.context_window_tokens == 32_768
        assert r.providers[SMALL].configs[0].max_tokens == 2_048
        assert r.agent._durable_consumer_window_tokens == 32_768
        assert r.agent._durable_consumer_model_id == SMALL
        session = await r.manager.get_session(KEY)
        assert session.model == SMALL
        assert (await r.service.get_decision(KEY, identifier))["status"] == "applied"


async def test_switch_after_tool_keeps_history_and_tool_results(runtime) -> None:
    r = runtime
    r.controller.initial_assessment_complete = True
    r.providers[LARGE].uses_tools = True
    r.agent.tool_definitions = [ToolDefinition(
        name="probe", description="Inspect a synthetic service.", input_schema=ToolInputSchema(),
    )]
    r.agent.set_history([
        Message(role="user", content="The synthetic project label is amber."),
        Message(role="assistant", content="I will retain that project label."),
    ])
    tool_started = asyncio.Event()
    blocked = asyncio.Event()

    async def evaluate(generation):
        await tool_started.wait()
        result = replace(r.turn, metadata={})
        record = await r.service.publish(KEY, {
            "action": "switch", "current": {"provider": "tokenrhythm", "model": LARGE},
            "target": {"provider": "tokenrhythm", "model": SMALL},
            "reason": "A different model can finish this synthetic task.",
            "evidence": ["task_change"], "risk": "low", "source": "input",
        }, input_id="tool-switch", routing_revision=0, check_generation=generation,
            auto_switch_seconds=0)
        result.metadata["jev_decision_id"] = record["id"]
        return result

    await r.service.start_check(KEY, r.controller.fingerprint, evaluate)

    async def handler(call):
        tool_started.set()
        await r.controller.task
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name,
            content="Synthetic service state: ready.",
        )

    r.agent.tool_handler = handler

    async def changed(key):
        snapshot = await r.service.snapshot(key, r.config.jev_router)
        if snapshot["blocked"]:
            blocked.set()

    r.service.on_change = changed
    run = asyncio.create_task(_collect(r.agent))
    await asyncio.wait_for(blocked.wait(), 2)
    assert len(r.providers[LARGE].requests) == 1
    assert not r.providers[SMALL].requests
    recommendation = (await r.service.snapshot(KEY, r.config.jev_router))["recommendation"]
    await r.service.respond(KEY, recommendation["id"], True)
    events = await asyncio.wait_for(run, 2)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    request = r.providers[SMALL].requests[0]
    assert any("project label is amber" in str(message.content) for message in request)
    assert any("Inspect a synthetic service" in str(message.content) for message in request)
    results = [
        block for message in request if isinstance(message.content, list)
        for block in message.content if isinstance(block, ContentBlockToolResult)
    ]
    assert len(results) == 1
    assert "service state: ready" in str(results[0].content)


@pytest.mark.parametrize("interval", [3, 5])
async def test_loop_check_starts_at_configured_completed_round_interval(
    runtime, monkeypatch, interval,
) -> None:
    r = runtime
    await r.service.configure(KEY, {"loopInterval": interval}, r.config.jev_router)
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    for iteration in range(1, interval + 2):
        assert not await r.controller.before_request(r.agent, iteration, [])
        await r.controller.task
    assert check.await_count == 2
    assert check.await_args.kwargs["assessment_stage"] == "loop"


@pytest.mark.parametrize("mode", ["suggest", "trust"])
async def test_expired_deadline_respects_mode_without_ui_response(runtime, mode) -> None:
    r = runtime
    await r.service.configure(KEY, {"mode": mode}, r.config.jev_router)
    r.service._clock = lambda: int(time.time() * 1000) - 20_000
    identifier = await prepare(r)
    events = await asyncio.wait_for(_collect(r.agent), 2)
    assert len(r.providers[SMALL if mode == "trust" else LARGE].requests) == 1
    assert len(r.providers[LARGE if mode == "trust" else SMALL].requests) == 0
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert (await r.service.get_decision(KEY, identifier))["status"] == (
        "applied" if mode == "trust" else "dismissed"
    )


async def test_checkpoint_includes_bounded_tool_outcome_and_tool_requirement(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    messages = [Message(role="user", content=[ContentBlockToolResult(
        tool_use_id="synthetic-probe", content="Ready for synthetic verification. " * 30,
    )])]
    task = await r.controller._start(4, messages, stage="loop")
    await task
    context = check.await_args.args[0]
    assert context.metadata["jev_requires_tools"] is True
    evidence = check.await_args.kwargs["recent_tool_results"]
    assert "Ready for synthetic verification" in evidence[0]
    assert len(evidence[0]) <= 1200
    assert context.metadata["router_prev_assistant_text"] == ""
    assert check.await_args.kwargs["loop_iteration"] == 4


async def test_checkpoint_keeps_recent_tool_evidence_separate_from_assistant_metadata(
    runtime, monkeypatch,
) -> None:
    r = runtime
    check = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.jev_execution.apply_jev_router", check)
    messages = [
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="older", content="Earlier synthetic extraction complete.",
        )]),
        Message(role="assistant", content="Reading the next evidence item."),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="latest", content="New evidence: concurrent old and new coordinators.",
        )]),
    ]
    task = await r.controller._start(6, messages, stage="loop")
    await task
    assert check.await_args.kwargs["recent_tool_results"] == (
        "New evidence: concurrent old and new coordinators.",
        "Earlier synthetic extraction complete.",
    )
    context = check.await_args.args[0]
    assert context.semantic_message == r.turn.semantic_message
    assert context.metadata["router_prev_assistant_text"] == "Reading the next evidence item."
    assert "recent_tool_results" not in context.metadata


async def test_new_manual_route_invalidates_ready_recommendation(runtime) -> None:
    r = runtime
    identifier = await prepare(r, status="accepted")
    await r.manager.set_session_routing(
        KEY, "direct", expected_revision=0, update_model=True,
        model="synthetic-manual-choice", provider="tokenrhythm",
    )
    assert not await r.controller.before_request(r.agent, 1, [])
    assert (await r.service.get_decision(KEY, identifier))["status"] == "expired"
    assert r.selector.current_config.model == LARGE


async def test_global_settings_retire_an_already_read_accepted_choice(runtime) -> None:
    r = runtime
    identifier = await prepare(r, status="accepted")
    await r.service.apply_global_settings(r.config.jev_router, {"mode"})
    assert not await r.controller._activate(
        r.agent, identifier, {"provider": "tokenrhythm", "model": SMALL},
    )
    assert r.selector.current_config.model == LARGE
    assert (await r.service.get_decision(KEY, identifier))["status"] == "expired"


async def test_failed_target_preparation_cannot_persist_or_use_another_deployment(
    runtime, monkeypatch,
) -> None:
    r = runtime
    identifier = await prepare(r, status="accepted")
    setter = AsyncMock(wraps=r.manager.set_session_routing)
    monkeypatch.setattr(r.manager, "set_session_routing", setter)
    monkeypatch.setattr(
        "opensquilla.engine.selector_override.apply_model_override",
        lambda *_args, **_kwargs: r.providers[LARGE],
    )
    assert not await r.controller.before_request(r.agent, 1, [])
    setter.assert_not_awaited()
    assert r.selector.current_config.model == LARGE
    assert (await r.service.get_decision(KEY, identifier))["status"] == "expired"


async def test_stop_during_gate_does_not_dispatch_or_leave_auto_switch(runtime) -> None:
    r = runtime
    identifier = await prepare(r)
    blocked = asyncio.Event()

    async def changed(key):
        if (await r.service.snapshot(key, r.config.jev_router))["blocked"]:
            blocked.set()

    r.service.on_change = changed
    run = asyncio.create_task(_collect(r.agent))
    await asyncio.wait_for(blocked.wait(), 1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await r.controller.close()
    assert not r.service.execution_owned(KEY)
    assert not (await r.service.snapshot(KEY, r.config.jev_router))["blocked"]
    assert (await r.service.get_decision(KEY, identifier))["status"] == "expired"
    assert not r.providers[LARGE].requests and not r.providers[SMALL].requests


async def test_stop_fences_a_background_check_that_delays_cancellation(runtime) -> None:
    r = runtime
    started = asyncio.Event()
    finish = asyncio.Event()

    async def evaluate(generation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()
        return await r.service.publish(KEY, {
            "action": "switch", "current": {"provider": "tokenrhythm", "model": LARGE},
            "target": {"provider": "tokenrhythm", "model": SMALL},
            "reason": "A late synthetic result must be discarded.",
            "evidence": [], "risk": "low",
        }, input_id="late-stop", check_generation=generation, auto_switch_seconds=0)

    r.controller.task = await r.service.start_check(KEY, r.controller.fingerprint, evaluate)
    await started.wait()
    await asyncio.wait_for(r.controller.close(), 0.8)
    assert not r.service.execution_owned(KEY)
    finish.set()
    assert await asyncio.wait_for(r.controller.task, 1) is None
    assert (await r.service.snapshot(KEY, r.config.jev_router))["recommendation"] is None


async def test_execution_close_preserves_manual_context_notice_and_expires_model_advice(
    runtime,
) -> None:
    r = runtime
    await r.manager.append_message(KEY, "user", "Keep the synthetic source conversation intact.")
    source_before = await r.manager.get_transcript(KEY)
    model_id = await prepare(r)
    notice = await r.service.publish(KEY, {
        "action": "new_session", "current": {"provider": "tokenrhythm", "model": LARGE},
        "target": None, "reason": "A synthetic long conversation can continue from a summary.",
        "evidence": ["20 completed conversational turns"], "risk": "medium",
        "handoff": {"sourceSessionKey": KEY, "contextTokens": 40_000,
                    "contextWindowTokens": 50_000, "completedTurns": 20},
    }, input_id="synthetic-context-notice")

    await r.controller.close()

    assert not r.service.execution_owned(KEY)
    assert (await r.service.get_decision(KEY, model_id))["status"] == "expired"
    remaining = await r.service.get_decision(KEY, notice["id"])
    assert remaining["status"] == "pending" and "autoSwitchAt" not in remaining
    assert not (await r.service.snapshot(KEY, r.config.jev_router))["blocked"]
    assert [entry.model_dump() for entry in await r.manager.get_transcript(KEY)] == [
        entry.model_dump() for entry in source_before
    ]


async def _collect(agent):
    return [event async for event in agent.run_turn("Inspect a synthetic service.")]
