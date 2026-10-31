from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from opensquilla.engine.model_policy_recovery import ModelPolicyRecovery
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.types import AgentConfig, ThinkingLevel
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, Message
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


class ScriptedClassifier:
    def __init__(self, tier="c0"):
        self.tier = tier

    def predict(self, value):
        return SimpleNamespace(
            intent_probabilities={"continue": 0.0, "redo": 0.0, "new_task": 1.0},
            tier_probabilities={f"c{i}": float(self.tier == f"c{i}") for i in range(4)},
        )


@pytest.fixture
async def execution(monkeypatch, request):
    phase = getattr(request, "param", "full")
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: ScriptedClassifier())
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:model-execution")
        entry = await manager.append_message(node.session_key, "user", "Hello")
        config = GatewayConfig(
            llm={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "api_key": "synthetic-offline",
                "thinking": "high",
            },
            squilla_router={"rollout_phase": phase},
            llm_ensemble={"enabled": phase != "observe"},
        )
        selector = ModelSelector(
            SelectorConfig(
                primary=ProviderConfig(
                    provider="openrouter",
                    model=config.llm.model,
                    api_key="synthetic-offline",
                    base_url="https://openrouter.ai/api/v1",
                ),
                fallbacks=[
                    ProviderConfig(
                        provider="openrouter",
                        model="synthetic-forbidden-fallback",
                        api_key="synthetic-offline",
                        base_url="https://openrouter.ai/api/v1",
                    )
                ],
            )
        )
        runner = TurnRunner(provider_selector=selector, session_manager=manager, config=config)
        turn, provider = await runner._run_pipeline(
            "Hello",
            node.session_key,
            selector.resolve(),
            selector,
            [],
            "system",
            [],
            bound_user_message_id=entry.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
        )
        yield SimpleNamespace(
            turn=turn,
            provider=provider,
            selector=selector,
            runner=runner,
            manager=manager,
            storage=storage,
            node=node,
            entry=entry,
        )


@pytest.mark.asyncio
async def test_applied_model_route_is_single_and_removes_configured_fallback(execution):
    e = execution
    assert e.turn.metadata["routed_tier"] == "c0"
    assert [item.model for item in e.selector.remaining_chain()] == ["qwen/qwen3.7-flash"]
    assert not e.selector.has_fallback()
    assert type(e.provider).__name__ != "EnsembleProvider"
    assert e.runner._resolve_turn_thinking(e.turn) is ThinkingLevel.HIGH
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    assert (
        wrapper._config_for_active_leg(ChatConfig(physical_attempt_limit=4)).physical_attempt_limit
        == 1
    )
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    assert rows[0].turn_context["router_model_decision"]["state"]["tier"] == "c0"


@pytest.mark.asyncio
async def test_real_quality_callback_rebinds_once_and_persists_effective_tier(execution):
    e = execution
    original = deepcopy(e.turn.metadata["router_model_decision"]["decision"])
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    wrapper._last_model_chat_config = ChatConfig(max_tokens=1024)
    wrapper._last_model_tools = []
    recovery = ModelPolicyRecovery(
        AgentConfig(
            model_id=e.turn.model,
            metadata=e.turn.metadata,
            max_provider_retries=3,
        )
    )
    messages = [Message(role="user", content="Hello")]
    updates = await recovery.prepare(wrapper, "validation_failure", messages)
    assert updates is not None
    assert updates["model_id"] == "deepseek/deepseek-v4-flash"
    assert e.turn.metadata["routed_tier"] == "c1"
    assert not e.selector.has_fallback()
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    durable = rows[0].turn_context["router_model_decision"]
    assert durable["decision"] == original
    assert durable["state"]["tier"] == "c1"
    assert durable["quality_retry"]["used"] is True
    assert durable["execution_update"]["model"] == updates["model_id"]
    assert await recovery.prepare(wrapper, "validation_failure", messages) is None
    # A reconstructed helper after restart must respect the durable one-use flag.
    restarted = ModelPolicyRecovery(
        AgentConfig(
            model_id=e.turn.model,
            metadata=e.turn.metadata,
            max_provider_retries=3,
        )
    )
    assert await restarted.prepare(wrapper, "validation_failure", messages) is None


@pytest.mark.asyncio
async def test_quality_callback_refuses_unprojectable_request_without_mutation(
    execution, monkeypatch
):
    e = execution
    import opensquilla.engine.model_router_quality as quality

    monkeypatch.setattr(quality, "project_provider_final_request", lambda *args: None)
    callback = e.turn.metadata["router_model_quality_retry_callback"]
    before = deepcopy(e.turn.metadata["router_model_decision"])
    updates = await callback(
        reason="validation_failure",
        scope_id="test-scope",
        remaining_budget=1,
        messages=[Message(role="user", content="Hello")],
        chat_config=ChatConfig(max_tokens=1024),
        tools=[],
        activate_prepared=_SelectorFallbackProvider(
            e.provider,
            e.selector,
            e.turn.metadata,
        ).activate_prepared_deployment,
    )
    assert updates is None
    assert e.turn.metadata["router_model_decision"] == before
    assert e.selector.current_config.model == "qwen/qwen3.7-flash"


@pytest.mark.asyncio
async def test_quality_callback_cannot_activate_after_session_reset(execution):
    from opensquilla.session.storage import StaleEpochError

    e = execution
    callback = e.turn.metadata["router_model_quality_retry_callback"]
    await e.storage.advance_reset_epoch(e.node.session_key)
    with pytest.raises(StaleEpochError):
        await callback(
            reason="validation_failure",
            scope_id="stale-worker",
            remaining_budget=1,
            messages=[Message(role="user", content="Hello")],
            chat_config=ChatConfig(max_tokens=1024),
            tools=[],
            activate_prepared=_SelectorFallbackProvider(
                e.provider,
                e.selector,
                e.turn.metadata,
            ).activate_prepared_deployment,
        )
    assert e.selector.current_config.model == "qwen/qwen3.7-flash"
    assert e.turn.metadata["routed_tier"] == "c0"


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["observe"], indirect=True)
async def test_observe_preserves_baseline_model_fallbacks_and_effort(execution):
    from opensquilla.engine.types import ThinkingLevel

    e = execution
    assert e.turn.metadata["routing_applied"] is False
    assert e.selector.current_config.model == "deepseek/deepseek-v4-pro"
    assert e.selector.has_fallback()
    assert e.runner._resolve_turn_thinking(e.turn) == ThinkingLevel.HIGH
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    assert (
        wrapper._config_for_active_leg(ChatConfig(physical_attempt_limit=4)).physical_attempt_limit
        == 4
    )
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    assert "router_model_decision" not in (rows[0].turn_context or {})


@pytest.mark.asyncio
async def test_failed_selected_deployment_keeps_audit_but_not_active_state(execution, monkeypatch):
    from opensquilla.engine.steps.model_router import ModelRouteError
    from opensquilla.session.model_router_state import build_model_router_context

    e = execution
    previous = deepcopy(e.turn.metadata["router_model_decision"]["state"])
    invalid = await e.manager.append_message(e.node.session_key, "user", "inspect this image")

    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: ScriptedClassifier("c2"))
    with pytest.raises(ModelRouteError, match="image support"):
        await e.runner._run_pipeline(
            "inspect this image",
            e.node.session_key,
            e.selector.resolve(),
            e.selector,
            [],
            "system",
            [{"mime_type": "image/png"}],
            bound_user_message_id=invalid.message_id,
            expected_session_id=e.node.session_id,
            expected_session_epoch=0,
            model_router_context={"router_model_state": previous},
        )
    next_input = await e.manager.append_message(e.node.session_key, "user", "continue")
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    rejected = next(row for row in rows if row.message_id == invalid.message_id)
    audit = rejected.turn_context["router_model_decision"]
    assert audit["status"] == "staged"
    assert audit["state_committed"] is False
    assert audit["decision"]["tier"] == "c2"
    recovered = build_model_router_context(
        rows,
        bound_user_message_id=next_input.message_id,
        exclude_last_user=True,
    )
    assert recovered["router_model_state"] == previous
    assert e.selector.current_config.model == "qwen/qwen3.7-flash"


@pytest.mark.asyncio
async def test_missing_cross_provider_credentials_does_not_admit_wrong_model(
    execution,
    monkeypatch,
):
    from opensquilla.engine import selector_override
    from opensquilla.engine.steps.model_router import ModelRouteError
    from opensquilla.session.model_router_state import build_model_router_context

    e = execution
    previous = deepcopy(e.turn.metadata["router_model_decision"]["state"])
    config = e.runner._config.squilla_router
    config.cross_provider_tiers = True
    config.tiers["c0"] = {"model": "qwen3.7-flash", "provider": "tokenrhythm"}
    monkeypatch.setattr(selector_override, "resolve_tier_provider_config", lambda *a, **k: None)
    entry = await e.manager.append_message(e.node.session_key, "user", "next request")
    with pytest.raises(ModelRouteError, match="deployment could not be resolved"):
        await e.runner._run_pipeline(
            "next request",
            e.node.session_key,
            e.selector.resolve(),
            e.selector,
            [],
            "system",
            [],
            bound_user_message_id=entry.message_id,
            expected_session_id=e.node.session_id,
            expected_session_epoch=0,
            model_router_context={"router_model_state": previous},
        )
    following = await e.manager.append_message(e.node.session_key, "user", "continue")
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    rejected = next(row for row in rows if row.message_id == entry.message_id)
    audit = rejected.turn_context["router_model_decision"]
    assert audit["status"] == "staged"
    assert audit["state_committed"] is False
    assert (
        build_model_router_context(
            rows,
            bound_user_message_id=following.message_id,
            exclude_last_user=True,
        )["router_model_state"]
        == previous
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_provider", [False, True])
@pytest.mark.parametrize("explicit_proof_cap", [0, 777777])
@pytest.mark.parametrize("overflow_threshold", [0.6, 0.85])
async def test_quality_retry_activates_exact_preflight_and_thinking_budget(
    execution, monkeypatch, cross_provider, explicit_proof_cap, overflow_threshold,
):
    import opensquilla.engine.model_router_quality as quality
    from opensquilla.engine import Agent
    from opensquilla.engine.types import THINKING_BUDGETS, ThinkingLevel
    from opensquilla.gateway.config import LlmProviderProfile

    e = execution
    if cross_provider:
        e.turn.config.squilla_router.cross_provider_tiers = True
        e.turn.config.squilla_router.tiers["c1"] = {
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
        }
        e.turn.config.llm_profiles["tokenrhythm"] = LlmProviderProfile(
            provider="tokenrhythm", api_key="synthetic-offline"
        )
    target = e.turn.config.squilla_router.tiers["c1"]
    # Model-policy execution remains direct even with a static ensemble enabled.
    assert e.turn.config.llm_ensemble.enabled is True
    e.turn.config.llm_ensemble.selection_mode = "static"
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    config = AgentConfig(
        model_id=e.turn.model,
        metadata=e.turn.metadata,
        max_provider_retries=3,
        thinking=e.runner._resolve_turn_thinking(e.turn),
        thinking_budget_tokens=999 if explicit_proof_cap else -1,
        max_tokens=1024,
        context_window_tokens=32000,
        context_window_known=False,
        model_vision_support="supported",
        provider_request_proof_max_chars=explicit_proof_cap,
        context_overflow_threshold=overflow_threshold,
    )
    agent = Agent(provider=wrapper, config=config)
    wrapper._last_model_chat_config = agent._provider_admission_chat_config(
        "Hi", context_window_tokens=config.context_window_tokens
    )
    wrapper._last_model_tools = []
    projected = []
    project = quality.project_provider_final_request

    def capture(provider, messages, tools, config):
        projected.append((provider, config))
        return project(provider, messages, tools, config)

    monkeypatch.setattr(quality, "project_provider_final_request", capture)
    # A second resolution could construct a different physical adapter.
    monkeypatch.setattr(e.selector, "resolve", lambda: pytest.fail("resolved after preflight"))
    recovery = ModelPolicyRecovery(config)
    updates = await recovery.prepare(
        wrapper, "validation_failure", [Message(role="user", content="Hi")]
    )
    assert updates is not None
    assert len(projected) == 1
    admitted_provider, admitted_config = projected[0]
    assert wrapper._provider is admitted_provider
    assert e.selector.current_config.provider == target["provider"]
    assert e.selector.current_config.model == target["model"]
    assert e.turn.metadata["router_tier_provider_role"] == "direct"
    assert e.turn.metadata["routed_provider"] == target["provider"]
    assert e.turn.metadata["requested_provider"] == target["provider"]
    assert e.turn.metadata["requested_model"] == target["model"]
    assert e.turn.metadata["resolved_model"] == target["model"]
    assert e.turn.metadata["selector_execution_chain"] == [
        {"provider": target["provider"], "model": target["model"]}
    ]
    if cross_provider:
        assert e.turn.metadata["routed_provider_applied"] == "tokenrhythm"
    level = ThinkingLevel(admitted_config.thinking_level)
    assert admitted_config.thinking_budget_tokens == (
        999 if explicit_proof_cap else THINKING_BUDGETS[level]
    )
    # The actual next Agent request must retain the admitted budget, including
    # a user-specified budget that takes priority over automatic tier effort.
    for key, value in updates.items():
        setattr(config, key, value)
    assert config.resolve_thinking() == (
        level != ThinkingLevel.OFF,
        admitted_config.thinking_budget_tokens,
    )
    actual_config = agent._provider_admission_chat_config(
        "Hi", context_window_tokens=config.context_window_tokens
    )
    for field in (
        "max_tokens", "provider_context_window_tokens", "model_capabilities",
        "model_vision_support", "thinking", "thinking_level", "thinking_budget_tokens",
        "thinking_budget_explicit", "provider_request_max_chars",
        "provider_request_max_chars_explicit_cap",
    ):
        assert getattr(actual_config, field) == getattr(admitted_config, field), field


@pytest.mark.asyncio
async def test_quality_retry_missing_credentials_keeps_admitted_state(execution, monkeypatch):
    from opensquilla.engine import selector_override

    e = execution
    e.turn.config.squilla_router.cross_provider_tiers = True
    e.turn.config.squilla_router.tiers["c1"] = {
        "provider": "tokenrhythm",
        "model": "deepseek-v4-flash-0731",
    }
    monkeypatch.setattr(selector_override, "resolve_tier_provider_config", lambda *a, **k: None)
    before = deepcopy(e.turn.metadata["router_model_decision"])
    wrapper = _SelectorFallbackProvider(e.provider, e.selector, e.turn.metadata)
    wrapper._last_model_chat_config = ChatConfig(max_tokens=1024)
    wrapper._last_model_tools = []
    recovery = ModelPolicyRecovery(AgentConfig(metadata=e.turn.metadata, max_provider_retries=3))
    result = await recovery.prepare(
        wrapper, "validation_failure", [Message(role="user", content="Hi")]
    )
    assert result is None
    assert recovery.used == 0
    assert e.turn.metadata["router_model_decision"] == before
    assert e.turn.metadata["routed_tier"] == "c0"
    assert e.selector.current_config.model == "qwen/qwen3.7-flash"
    assert wrapper._provider is e.provider
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    assert rows[0].turn_context["router_model_decision"] == before


@pytest.mark.asyncio
async def test_quality_retry_returns_to_global_provider_when_tier_omits_provider(execution):
    from opensquilla.gateway.config import LlmProviderProfile

    e = execution
    e.turn.config.squilla_router.cross_provider_tiers = True
    e.turn.config.squilla_router.tiers["c0"] = {
        "provider": "tokenrhythm", "model": "qwen3.7-flash",
    }
    e.turn.config.squilla_router.tiers["c1"].pop("provider")
    e.turn.config.llm_profiles["tokenrhythm"] = LlmProviderProfile(
        provider="tokenrhythm", api_key="synthetic-offline"
    )
    entry = await e.manager.append_message(e.node.session_key, "user", "next request")
    turn, provider = await e.runner._run_pipeline(
        "next request", e.node.session_key, e.selector.resolve(), e.selector,
        [], "system", [], bound_user_message_id=entry.message_id,
        expected_session_id=e.node.session_id, expected_session_epoch=0,
        model_router_context={
            "router_model_state": e.turn.metadata["router_model_decision"]["state"]
        },
    )
    assert e.selector.current_config.provider == "tokenrhythm"
    assert turn.metadata["routed_provider"] == "tokenrhythm"
    wrapper = _SelectorFallbackProvider(provider, e.selector, turn.metadata)
    wrapper._last_model_chat_config = ChatConfig(max_tokens=1024)
    wrapper._last_model_tools = []
    recovery = ModelPolicyRecovery(AgentConfig(metadata=turn.metadata, max_provider_retries=3))

    updates = await recovery.prepare(
        wrapper, "validation_failure", [Message(role="user", content="next request")]
    )

    assert updates is not None
    assert updates["provider_id"] == e.turn.config.llm.provider == "openrouter"
    assert e.selector.current_config.provider == "openrouter"
    assert turn.metadata["routed_provider"] == "openrouter"
    assert turn.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,tier,auto_thinking,explicit_thinking,has_image,expected_level",
    [
        (None, "c3", False, "off", False, "off"),
        (None, "c3", True, "off", False, "off"),
        (None, "c3", False, None, False, "off"),
        (None, "c3", True, None, False, "high"),
        (None, "c0", False, "high", False, "high"),
        (None, "c0", True, "high", False, "high"),
        ("qwen/qwen-vl-max", "c0", True, None, False, "off"),
        ("qwen/qwen-vl-max", "c0", True, None, True, "off"),
        ("qwen/qwen-vl-max", "c3", False, "off", False, "off"),
        ("qwen/qwen-vl-max", "c3", False, "off", True, "off"),
    ],
)
async def test_model_execution_respects_actual_thinking_requirement(
    execution, monkeypatch, model, tier, auto_thinking, explicit_thinking, has_image,
    expected_level,
):
    from opensquilla.engine import Agent
    from opensquilla.engine.types import THINKING_BUDGETS
    from opensquilla.provider.ensemble import EnsembleProvider

    e = execution
    e.turn.config.llm.thinking = explicit_thinking
    e.turn.config.squilla_router.auto_thinking = auto_thinking
    if model is not None:
        e.turn.config.squilla_router.tiers[tier]["model"] = model

    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: ScriptedClassifier(tier))
    entry = await e.manager.append_message(e.node.session_key, "user", "Hello")
    turn, provider = await e.runner._run_pipeline(
        "Hello", e.node.session_key, e.selector.resolve(), e.selector,
        [], "system", [{"mime_type": "image/png"}] if has_image else [],
        bound_user_message_id=entry.message_id,
        expected_session_id=e.node.session_id,
        expected_session_epoch=0,
        model_router_context={
            "router_model_state": e.turn.metadata["router_model_decision"]["state"]
        },
    )
    assert turn.metadata["routed_tier"] == tier
    assert not isinstance(provider, EnsembleProvider)
    assert not e.selector.has_fallback()
    assert turn.metadata["thinking_mode"] == ("T3" if tier == "c3" else "T0")
    assert turn.metadata["router_model_decision"]["state_committed"] is True
    if not auto_thinking:
        assert "thinking_requested" not in turn.metadata
    # Exercise the execution resolver and the actual Agent request-config builder,
    # not only the controller's suggested thinking metadata.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id=turn.model,
            metadata=turn.metadata,
            thinking=e.runner._resolve_turn_thinking(turn),
        ),
    )
    request = agent._provider_admission_chat_config(
        "Hello", context_window_tokens=agent.config.context_window_tokens
    )
    assert request.thinking is (expected_level != "off")
    assert request.thinking_budget_tokens == THINKING_BUDGETS[ThinkingLevel(expected_level)]
    if expected_level == "off":
        assert request.thinking_level in {None, ThinkingLevel.OFF}
    else:
        assert request.thinking_level is ThinkingLevel(expected_level)
