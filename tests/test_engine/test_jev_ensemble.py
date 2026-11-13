"""Only a ready saved lineup can become JEV's additional execution tier."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine import DoneEvent as AgentDoneEvent
from opensquilla.engine.jev_ensemble import jev_ensemble_descriptor, prepare_jev_ensemble
from opensquilla.engine.jev_execution import JevExecution
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.jev_router import configured_jev_candidates
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_service import JevService
from opensquilla.provider import (
    ContentBlockToolResult,
    DoneEvent,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider.ensemble import EnsembleProvider
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ModelCapabilities
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

BASE = "synthetic-fusion-base"
ANALYST_A = "synthetic-fusion-analyst-a"
ANALYST_B = "synthetic-fusion-analyst-b"
AGGREGATOR = "synthetic-fusion-aggregator"


@pytest.fixture
def lineup(monkeypatch):
    monkeypatch.setattr("opensquilla.desktop_account.account_credential", lambda _provider: None)
    config = GatewayConfig(
        llm={"provider": "tokenrhythm", "model": BASE, "api_key": "synthetic-fusion-key"},
        jev_router={"enabled": True},
        llm_ensemble={
            "selection_mode": "custom_b5", "shuffle_candidates": False,
            "candidates": [
                {"provider": "tokenrhythm", "model": ANALYST_A},
                {"provider": "tokenrhythm", "model": ANALYST_B},
                {"provider": "tokenrhythm", "model": AGGREGATOR, "role": "aggregator"},
            ],
        },
    )
    catalog = ModelCatalog()
    catalog.set_user_overrides({f"tokenrhythm/{model}": {
        "context_window": window, "max_output_tokens": 4096, "supports_tools": True,
    } for model, window in [
        (BASE, 128_000), (ANALYST_A, 64_000), (ANALYST_B, 96_000), (AGGREGATOR, 128_000),
    ]})
    monkeypatch.setattr("opensquilla.provider.model_catalog.shared_catalog", lambda: catalog)
    monkeypatch.setattr("opensquilla.provider.ensemble.shared_catalog", lambda: catalog)
    inherited = ProviderConfig(
        provider="tokenrhythm", model=BASE, api_key="synthetic-fusion-key",
    )
    return config, inherited, catalog


def test_preparation_has_real_members_and_no_side_effects(lineup, monkeypatch):
    config, inherited, catalog = lineup
    dispatch = []
    monkeypatch.setattr(
        "opensquilla.provider.ensemble._build_provider", lambda cfg: dispatch.append(cfg),
    )
    prepared = prepare_jev_ensemble(config, inherited=inherited, catalog=catalog)
    assert prepared is not None
    assert isinstance(prepared.provider, EnsembleProvider)
    assert prepared.context_window_tokens == 64_000
    assert prepared.supports_tools is True
    assert dispatch == []
    assert config.llm_ensemble.enabled is False
    assert inherited.replay_provider_state is True
    descriptor = prepared.descriptor
    assert descriptor["tier"] == "c4"
    assert descriptor["aggregator"] == {"provider": "tokenrhythm", "model": AGGREGATOR}
    assert [row["model"] for row in descriptor["proposers"]] == [ANALYST_A, ANALYST_B]
    assert "synthetic-fusion-key" not in json.dumps(descriptor)
    assert jev_ensemble_descriptor(config, inherited=inherited) == descriptor


def test_unconfigured_or_unready_lineup_is_not_offered(lineup):
    config, inherited, catalog = lineup
    assert prepare_jev_ensemble(GatewayConfig(), inherited=inherited, catalog=catalog) is None
    assert prepare_jev_ensemble(
        config, inherited=replace(inherited, api_key=""), catalog=catalog,
    ) is None
    config.jev_router.ensemble_enabled = False
    assert prepare_jev_ensemble(config, inherited=inherited, catalog=catalog) is None


def test_changed_lineup_cannot_activate_an_old_approval(lineup):
    config, inherited, catalog = lineup
    descriptor = prepare_jev_ensemble(config, inherited=inherited, catalog=catalog).descriptor
    config.llm_ensemble.candidates[0].model = "synthetic-replacement-analyst"
    assert prepare_jev_ensemble(
        config, inherited=inherited, catalog=catalog,
        expected_fingerprint=descriptor["configurationFingerprint"],
    ) is None


def test_candidate_uses_real_aggregator_and_stays_out_of_image_context(lineup):
    config, inherited, _catalog = lineup
    turn = TurnContext(
        message="Compare synthetic system designs.", session_key="synthetic-fusion-candidate",
        config=config, model=BASE, provider=None, tool_defs=[], system_prompt="",
    )
    candidates = configured_jev_candidates(turn, inherited)
    fusion = next(candidate for candidate in candidates if candidate.kind == "ensemble")
    assert fusion.id == "ensemble:configured" and fusion.tier == "c4"
    assert fusion.provider == "tokenrhythm" and fusion.model == AGGREGATOR
    turn.metadata["image_context_has_images"] = True
    assert not any(c.kind == "ensemble" for c in configured_jev_candidates(turn, inherited))


def test_unverified_tools_prevent_fusion_candidate_for_an_agent(lineup):
    config, inherited, catalog = lineup
    catalog.set_user_overrides({f"tokenrhythm/{AGGREGATOR}": {"supports_tools": False}})
    turn = TurnContext(
        message="Inspect a synthetic system.", session_key="synthetic-fusion-tools",
        config=config, model=BASE, provider=None, system_prompt="", tool_defs=[ToolDefinition(
            name="probe", description="Inspect synthetic state.", input_schema=ToolInputSchema(),
        )],
    )
    assert not any(c.kind == "ensemble" for c in configured_jev_candidates(turn, inherited))


@pytest.mark.parametrize("enabled", [True, False])
def test_saved_fusion_session_keeps_the_jev_loop_available(lineup, monkeypatch, enabled):
    from opensquilla.engine.runtime import TurnRunner, accepted_turn_config_scope
    from opensquilla.gateway.model_routing import capture_model_routing_config

    config, _inherited, _catalog = lineup
    config.jev_router.enabled = enabled
    service = SimpleNamespace(is_enabled=lambda _key, defaults: defaults.enabled)
    monkeypatch.setattr("opensquilla.jev_service.get_jev_service", lambda *_args: service)
    runner = TurnRunner(provider_selector=None, config=config)
    accepted = capture_model_routing_config(config, session_mode="ensemble")
    with accepted_turn_config_scope(accepted):
        assert runner._uses_jev_router("agent:main:webchat:synthetic-saved-fusion") is enabled
        assert not runner._uses_jev_router("agent:main:subagent:synthetic-fusion-child")
    direct = capture_model_routing_config(config, session_mode="direct")
    with accepted_turn_config_scope(direct):
        assert runner._session_model_pin_applies()


async def test_saved_fusion_second_turn_still_wraps_while_jev_assesses(lineup, monkeypatch):
    from opensquilla.engine.runtime import TurnRunner, accepted_turn_config_scope
    from opensquilla.engine.steps.jev_router import jev_input_fingerprint
    from opensquilla.gateway.model_routing import capture_model_routing_config

    config, inherited, catalog = lineup
    selector = ModelSelector(SelectorConfig(primary=inherited))
    service = JevService()
    monkeypatch.setattr("opensquilla.jev_service.get_jev_service", lambda *_args: service)
    assessment = AsyncMock(side_effect=lambda turn, **_kwargs: turn)
    monkeypatch.setattr("opensquilla.engine.steps.jev_router.apply_jev_router", assessment)
    runner = TurnRunner(provider_selector=selector, config=config, model_catalog=catalog)
    accepted = capture_model_routing_config(config, session_mode="ensemble")
    key = "agent:main:webchat:synthetic-persisted-fusion"
    try:
        for message in ("Analyze the synthetic plan.", "Verify the previous synthetic findings."):
            cloned = selector.clone()
            with accepted_turn_config_scope(accepted):
                turn, provider = await runner._run_pipeline(
                    message, key, cloned.resolve(), cloned, [], "Synthetic system prompt.", [],
                )
            check = service.current_check(key, jev_input_fingerprint(message, []))
            assert check is not None
            await check
            assert isinstance(provider, EnsembleProvider)
            assert turn.metadata["ensemble_enabled"] is True
            assert turn.config.llm_ensemble.enabled is True
            assert assessment.await_args.kwargs["assessment_stage"] == "input"
        assert assessment.await_count == 2
        assert config.llm_ensemble.enabled is False
    finally:
        await service.close()


async def test_prepared_execution_runs_proposals_then_real_aggregation(lineup, monkeypatch):
    config, inherited, catalog = lineup
    calls = []

    def physical(cfg):
        # Keep the physical request projection and budget machinery; replace
        # only its network-producing streaming method for this offline test.
        provider = ModelSelector(SelectorConfig(primary=cfg)).resolve()

        async def chat(messages, *, tools=None, config=None):
            calls.append((cfg.model, messages))
            yield TextDeltaEvent(text=f"Synthetic result from {cfg.model}.")
            yield DoneEvent(model=cfg.model, input_tokens=4, output_tokens=2)

        provider.chat = chat
        return provider

    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", physical)
    prepared = prepare_jev_ensemble(config, inherited=inherited, catalog=catalog)
    events = [event async for event in prepared.provider.chat([
        Message(role="user", content="Compare the synthetic plans and justify a final plan."),
    ])]
    assert {model for model, _ in calls[:2]} == {ANALYST_A, ANALYST_B}
    assert calls[-1][0] == AGGREGATOR
    aggregate_input = "\n".join(str(message.content) for message in calls[-1][1])
    assert f"Synthetic result from {ANALYST_A}" in aggregate_input
    assert f"Synthetic result from {ANALYST_B}" in aggregate_input
    assert any(isinstance(event, TextDeltaEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_running_tool_boundary_automatically_activates_fusion_and_keeps_results(
    lineup, monkeypatch,
):
    config, inherited, catalog = lineup
    config.jev_router.mode = "trust"
    config.jev_router.auto_switch_seconds = 1
    key = "agent:main:webchat:synthetic-fusion-boundary"
    calls = []
    monkeypatch.setattr(
        "opensquilla.engine.jev_execution.apply_jev_router",
        AsyncMock(side_effect=lambda turn, **_kwargs: turn),
    )

    def physical(cfg):
        provider = ModelSelector(SelectorConfig(primary=cfg)).resolve()

        async def chat(messages, *, tools=None, config=None):
            calls.append((cfg.model, messages))
            yield TextDeltaEvent(text=f"Synthetic result from {cfg.model}.")
            yield DoneEvent(model=cfg.model, input_tokens=4, output_tokens=2)

        provider.chat = chat
        return provider

    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", physical)
    selector = ModelSelector(SelectorConfig(primary=inherited))
    original = selector.resolve()
    original_calls = []

    async def original_chat(messages, *, tools=None, config=None):
        original_calls.append(messages)
        yield ToolUseStartEvent(tool_use_id="synthetic-probe", tool_name="probe")
        yield ToolUseEndEvent(tool_use_id="synthetic-probe", tool_name="probe", arguments={})
        yield DoneEvent(model=BASE, stop_reason="tool_calls", input_tokens=4, output_tokens=2)

    original.chat = original_chat
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(key)
        service = JevService()
        service.defaults_provider = lambda: config.jev_router
        await service.bind_session(key, session.session_id, session.epoch)
        turn = TurnContext(
            message="Validate the synthetic system plans.", session_key=key, config=config,
            model=BASE, provider=original, tool_defs=[], system_prompt="",
        )
        controller = JevExecution(
            turn=turn, service=service, session_manager=manager, selector=selector,
            catalog=object(), ensemble_catalog=catalog, expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        )
        caps = ModelCapabilities(supports_tools=True)
        agent = Agent(provider=original, config=AgentConfig(
            model_id=BASE, provider_id="tokenrhythm", timeout=5,
            context_window_tokens=128_000, context_window_known=True, max_tokens=4096,
            model_capabilities=caps, model_vision_support="unsupported",
        ))
        agent.set_history([
            Message(role="user", content="The synthetic project label is amber."),
            Message(role="assistant", content="The project label will remain in context."),
        ])
        agent.tool_definitions = [ToolDefinition(
            name="probe", description="Inspect synthetic service.", input_schema=ToolInputSchema(),
        )]
        agent.set_model_advice_boundary(controller.before_request)
        tool_started = asyncio.Event()
        blocked = asyncio.Event()
        descriptor = prepare_jev_ensemble(config, inherited=inherited, catalog=catalog).descriptor

        async def evaluate(generation):
            await tool_started.wait()
            record = await service.publish(key, {
                "action": "ensemble", "current": {"provider": "tokenrhythm", "model": BASE},
                "target": None, "ensemble": descriptor,
                "reason": "Independent analyses are justified for the synthetic task.",
                "evidence": ["reasoning_fit"], "risk": "medium", "source": "loop",
            }, input_id="synthetic-fusion-loop", routing_revision=0,
                check_generation=generation, auto_switch_seconds=1)
            return replace(turn, metadata={"jev_decision_id": record["id"]})

        controller.initial_assessment_complete = True
        controller.task = await service.start_check(key, "synthetic-loop-check", evaluate)

        async def handler(call):
            tool_started.set()
            await controller.task
            return ToolResult(
                tool_use_id=call.tool_use_id, tool_name=call.tool_name,
                content="Synthetic service state: ready.",
            )

        async def changed(session_key):
            if (await service.snapshot(session_key, config.jev_router))["blocked"]:
                blocked.set()

        service.on_change = changed
        agent.tool_handler = handler
        try:
            run = asyncio.create_task(_collect(agent))
            await asyncio.wait_for(blocked.wait(), 2)
            assert len(original_calls) == 1
            assert not calls
            events = await asyncio.wait_for(run, 4)
            assert any(isinstance(event, AgentDoneEvent) for event in events)
            assert isinstance(agent.provider, EnsembleProvider)
            assert (await manager.get_session(key)).model_routing_mode == "ensemble"
            assert agent.config.metadata["jev_execution_mode"] == "ensemble"
            assert len(original_calls) == 1
            assert {model for model, _ in calls} == {ANALYST_A, ANALYST_B, AGGREGATOR}
            for _model, messages in calls:
                assert any("project label is amber" in str(message.content) for message in messages)
                results = [
                    block for message in messages if isinstance(message.content, list)
                    for block in message.content if isinstance(block, ContentBlockToolResult)
                ]
                assert any("service state: ready" in str(block.content) for block in results)
            snapshot = await service.snapshot(key, config.jev_router)
            assert snapshot["decisions"][0]["status"] == "applied"
        finally:
            await controller.close()
            await service.close()


async def _collect(agent):
    return [event async for event in agent.run_turn("Validate the synthetic system plans.")]


async def test_changed_approved_lineup_does_not_commit_a_running_switch(lineup):
    config, inherited, catalog = lineup
    key = "agent:main:webchat:synthetic-stale-fusion"
    descriptor = prepare_jev_ensemble(config, inherited=inherited, catalog=catalog).descriptor
    config.llm_ensemble.candidates[0].model = "synthetic-new-lineup-member"
    selector = ModelSelector(SelectorConfig(primary=inherited))
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(key)
        service = JevService()
        await service.bind_session(key, session.session_id, session.epoch)
        turn = TurnContext(
            message="Assess synthetic designs.", session_key=key, config=config,
            model=BASE, provider=None, tool_defs=[], system_prompt="",
        )
        controller = JevExecution(
            turn=turn, service=service, session_manager=manager, selector=selector,
            catalog=object(), ensemble_catalog=catalog, expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        )
        try:
            record = await service.publish(key, {
                "action": "ensemble", "current": {"provider": "tokenrhythm", "model": BASE},
                "target": None, "ensemble": descriptor, "reason": "Synthetic fusion plan.",
                "evidence": [], "risk": "medium", "source": "input",
            }, input_id="synthetic-stale-approval", routing_revision=0)
            await service.respond(key, record["id"], True)
            assert not await controller._activate_ensemble(object(), record["id"])
            assert (await service.get_decision(key, record["id"]))["status"] == "expired"
            session = await manager.get_session(key)
            assert session.model_routing_revision == 0
            assert session.model_routing_mode != "ensemble"
        finally:
            await controller.close()
            await service.close()
