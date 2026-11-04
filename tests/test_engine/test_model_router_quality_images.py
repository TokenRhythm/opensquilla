from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.model_policy_recovery import ModelPolicyRecovery
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.provider.image_projection import count_image_blocks
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, ContentBlockImage, ContentBlockText, Message
from opensquilla.provider.types import ErrorEvent as ProviderError
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.image_bytes import image_bytes


@pytest.fixture
async def quality_execution(monkeypatch):
    classifier = SimpleNamespace(predict=lambda value: SimpleNamespace(
        intent_probabilities={"new_task": 1.0, "continue": 0.0, "redo": 0.0},
        tier_probabilities={"c0": 1.0, "c1": 0.0, "c2": 0.0, "c3": 0.0},
    ))
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: classifier)
    config = GatewayConfig(
        llm={
            "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
            "api_key": "synthetic-offline", "thinking": "off",
        },
        squilla_router={"rollout_phase": "full"},
    )
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openrouter", model=config.llm.model,
        api_key="synthetic-offline", base_url="https://openrouter.ai/api/v1",
    )))
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:quality-images")
        entry = await manager.append_message(node.session_key, "user", "Describe the shape.")
        runner = TurnRunner(provider_selector=selector, session_manager=manager, config=config)
        turn, provider = await runner._run_pipeline(
            "Describe the shape.", node.session_key, selector.resolve(), selector,
            [], "system", [], bound_user_message_id=entry.message_id,
            expected_session_id=node.session_id, expected_session_epoch=0,
        )
        wrapper = _SelectorFallbackProvider(provider, selector, turn.metadata)
        recovery = ModelPolicyRecovery(AgentConfig(metadata=turn.metadata, max_provider_retries=3))
        yield SimpleNamespace(
            config=config, turn=turn, selector=selector, runner=runner, manager=manager,
            node=node, entry=entry, wrapper=wrapper, recovery=recovery,
        )


def _image_messages() -> list[Message]:
    return [Message(role="user", content=[
        ContentBlockText(text="Describe the shape."),
        ContentBlockImage(
            media_type="image/png", data=base64.b64encode(image_bytes()).decode(),
            attachment_id="synthetic-image", durable_retained=True,
        ),
    ])]


def _capture_request(monkeypatch):
    import opensquilla.engine.model_router_quality as quality

    captured = []
    original = quality.project_provider_final_request

    def capture(provider, messages, tools, config):
        captured.append((deepcopy(messages), config))
        return original(provider, messages, tools, config)

    monkeypatch.setattr(quality, "project_provider_final_request", capture)
    return captured


@pytest.mark.asyncio
async def test_quality_image_retry_keeps_semantic_tier_and_replays_physical_model(
    quality_execution, monkeypatch,
):
    e = quality_execution
    e.config.squilla_router.tiers["c2"]["model"] = "qwen/qwen-vl-max"
    config_before = deepcopy(e.config.squilla_router.tiers)
    decision_before = deepcopy(e.turn.metadata["router_model_decision"]["decision"])
    captured = _capture_request(monkeypatch)
    messages = _image_messages()

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", messages,
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    assert updates["model_id"] == "qwen/qwen-vl-max"
    assert e.turn.metadata["routed_tier"] == "c2"
    assert e.config.squilla_router.tiers == config_before
    assert count_image_blocks(messages) == count_image_blocks(captured[0][0]) == 1
    rows = await e.manager.get_model_router_transcript(e.node.session_key)
    record = rows[0].turn_context["router_model_decision"]
    assert record["state"]["tier"] == "c1"
    assert record["execution_update"]["tier"] == "c1"
    assert record["execution_update"]["execution_tier"] == "c2"
    assert record["decision"] == decision_before
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: SimpleNamespace(
        predict=lambda _: pytest.fail("reclassified"),
        predict_tier=lambda _: pytest.fail("reclassified"),
    ))
    replayed, _ = await e.runner._run_pipeline(
        "Describe the shape.", e.node.session_key, e.selector.resolve(), e.selector,
        [], "system", [{"mime_type": "image/png"}],
        bound_user_message_id=e.entry.message_id,
        expected_session_id=e.node.session_id, expected_session_epoch=0,
        model_router_context={"router_model_decision": record},
    )
    assert replayed.model == "qwen/qwen-vl-max"
    assert replayed.metadata["routed_tier"] == "c2"
    assert replayed.metadata["router_model_decision"]["state"]["tier"] == "c1"


@pytest.mark.asyncio
@pytest.mark.parametrize("forward_vision", [False, True])
async def test_quality_image_retry_uses_original_target_marker_when_no_allowed_vision(
    quality_execution, monkeypatch, forward_vision,
):
    e = quality_execution
    for tier in ("c1", "c2", "c3"):
        e.config.squilla_router.tiers[tier]["model"] = "deepseek/deepseek-v4-flash"
    if forward_vision:
        # A known visual model beyond the budget cap must not become a side door
        # around the same budget used for ordinary quality retry admission.
        e.config.squilla_router.tiers["c2"]["model"] = "qwen/qwen-vl-max"
        budget = e.config.squilla_router.budget
        budget.limit_usd = 0.01
        budget.action = "cap"
        budget.cap_tier = "c1"
        e.turn.metadata["session_billed_cost_usd"] = 1.0
        e.turn.metadata["session_cost_source"] = "billed"
        monkeypatch.setattr(
            e.manager.storage, "reconcile_session_usage_totals_from_ledger",
            lambda **kwargs: _spent_session(e.node),
        )
    captured = _capture_request(monkeypatch)
    messages = _image_messages()

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", messages,
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    assert updates["model_id"] == "deepseek/deepseek-v4-flash"
    assert e.turn.metadata["routed_tier"] == "c1"
    assert count_image_blocks(messages) == 1
    assert count_image_blocks(captured[0][0]) == 0
    assert "图片未分析" in str(captured[0][0])
    assert e.turn.metadata["router_model_decision"]["state"]["tier"] == "c1"


async def _spent_session(node):
    return SimpleNamespace(
        session_id=node.session_id, epoch=0, billed_cost_usd=1.0,
        total_cost_usd=1.0, estimated_cost_usd=1.0, cost_source="billed",
    )


@pytest.mark.asyncio
async def test_quality_text_retry_ignores_stale_initial_image_metadata(
    quality_execution, monkeypatch,
):
    e = quality_execution
    e.config.squilla_router.tiers["c2"]["model"] = "qwen/qwen-vl-max"
    e.turn.attachments = [{"mime_type": "image/png"}]
    e.turn.metadata.update(
        image_context_has_images=True, router_vision_followup_needs_image=True,
        image_input_projection_required=True, image_input_mode="marker",
        router_model_image_target_tier="c0", attachment_material_estimated_tokens=10**8,
        router_model_image_execution_tier="c3", routed_model_vision_support="unsupported",
    )
    captured = _capture_request(monkeypatch)

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", [Message(role="user", content="Explain the text.")],
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    assert updates["model_id"] == e.config.squilla_router.tiers["c1"]["model"]
    assert e.turn.metadata["routed_tier"] == "c1"
    assert count_image_blocks(captured[0][0]) == 0
    assert "图片未分析" not in str(captured[0][0])
    assert "image_input_projection_required" not in e.turn.metadata
    assert "router_model_image_execution_tier" not in e.turn.metadata
    assert e.turn.attachments == [{"mime_type": "image/png"}]


@pytest.mark.asyncio
async def test_quality_recovery_uses_semantic_tier_when_images_already_execute_on_c3(
    quality_execution, monkeypatch,
):
    e = quality_execution
    for tier in ("c0", "c1", "c2"):
        e.config.squilla_router.tiers[tier]["model"] = "deepseek/deepseek-v4-flash"
    e.config.squilla_router.tiers["c3"]["model"] = "qwen/qwen-vl-max"
    classifier = SimpleNamespace(predict=lambda value: SimpleNamespace(
        intent_probabilities={"new_task": 1.0, "continue": 0.0, "redo": 0.0},
        tier_probabilities={"c0": 0.0, "c1": 1.0, "c2": 0.0, "c3": 0.0},
    ))
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: classifier)
    entry = await e.manager.append_message(e.node.session_key, "user", "Inspect the shape.")
    turn, provider = await e.runner._run_pipeline(
        "Inspect the shape.", e.node.session_key, e.selector.resolve(), e.selector,
        [], "system", [{"mime_type": "image/png"}],
        bound_user_message_id=entry.message_id,
        expected_session_id=e.node.session_id, expected_session_epoch=0,
        model_router_context={
            "router_model_state": e.turn.metadata["router_model_decision"]["state"],
        },
    )
    assert turn.metadata["routed_tier"] == "c3"
    assert turn.metadata["router_model_decision"]["state"]["tier"] == "c1"
    wrapper = _SelectorFallbackProvider(provider, e.selector, turn.metadata)
    recovery = ModelPolicyRecovery(AgentConfig(metadata=turn.metadata, max_provider_retries=3))

    updates = await recovery.prepare(
        wrapper, "validation_failure", _image_messages(),
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    assert updates["model_id"] == "qwen/qwen-vl-max"
    assert turn.metadata["routed_tier"] == "c3"
    assert turn.metadata["router_model_decision"]["state"]["tier"] == "c2"
    assert turn.metadata["router_model_decision"]["execution_update"]["execution_tier"] == "c3"
    assert recovery.used == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("later_vision", [False, True])
@pytest.mark.parametrize("blocked_by", ["missing_credentials", "provider_state"])
async def test_quality_image_retry_skips_unavailable_cross_provider_candidate(
    quality_execution, monkeypatch, later_vision, blocked_by,
):
    e = quality_execution
    e.config.squilla_router.cross_provider_tiers = True
    e.config.squilla_router.tiers["c2"] = {"provider": "openai", "model": "gpt-4o"}
    e.config.squilla_router.tiers["c3"] = {
        "provider": "openrouter",
        "model": "qwen/qwen-vl-max" if later_vision else "deepseek/deepseek-v4-flash",
    }
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if blocked_by == "provider_state":
        e.config.llm_profiles["openai"] = LlmProviderProfile(
            provider="openai", api_key="synthetic-other-provider",
        )
        e.turn.metadata["provider_state_continuity"] = {
            "decision": "discard_provider_state", "active_state_provider": "openrouter",
        }
    captured = _capture_request(monkeypatch)
    config_before = deepcopy(e.config.squilla_router.tiers)

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", _image_messages(),
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    expected_tier = "c3" if later_vision else "c1"
    assert updates["model_id"] == e.config.squilla_router.tiers[expected_tier]["model"]
    assert updates["provider_id"] == "openrouter"
    assert e.selector.current_config.api_key == "synthetic-offline"
    assert e.turn.metadata["routed_tier"] == expected_tier
    assert e.turn.metadata["router_model_decision"]["state"]["tier"] == "c1"
    assert "routed_provider_blocked" not in e.turn.metadata
    assert e.config.squilla_router.tiers == config_before
    assert count_image_blocks(captured[0][0]) == int(later_vision)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["admission", "final_proof"])
@pytest.mark.parametrize("outcome", ["later_vision", "marker", "refused"])
async def test_quality_image_retry_checks_exact_request_for_each_visual_candidate(
    quality_execution, monkeypatch, rejection, outcome,
):
    import opensquilla.engine.model_router_quality as quality

    e = quality_execution
    first_vision, later_vision = "qwen/qwen-vl-max", "gpt-4o"
    text_model = e.config.squilla_router.tiers["c1"]["model"]
    e.config.squilla_router.tiers["c2"]["model"] = first_vision
    e.config.squilla_router.tiers["c3"]["model"] = later_vision
    original_decision = deepcopy(e.turn.metadata["router_model_decision"])
    original_config = deepcopy(e.config.squilla_router.tiers)
    original_deployment = e.selector.current_config
    messages = _image_messages()
    original_messages = deepcopy(messages)
    advance = AsyncMock(wraps=e.manager.advance_model_router_quality_retry)
    monkeypatch.setattr(e.manager, "advance_model_router_quality_retry", advance)
    admit = quality.validate_provider_chat_admission
    project = quality.project_provider_final_request
    attempts = []

    def should_refuse(model):
        return model == first_vision or (
            model == later_vision and outcome != "later_vision"
        ) or outcome == "refused"

    def check_isolation():
        assert e.turn.metadata["router_model_decision"] == original_decision
        assert e.selector.current_config is original_deployment
        assert advance.await_count == 0
        assert messages == original_messages

    def admission(provider, request, config):
        check_isolation()
        projection = project(provider, request, [], config)
        assert projection is not None
        model = projection.payload["model"]
        attempts.append((model, count_image_blocks(request)))
        if rejection == "admission" and should_refuse(model):
            return ProviderError(message="Synthetic request refusal", code="request_refused")
        return admit(provider, request, config)

    def final_proof(provider, request, tools, config):
        check_isolation()
        projection = project(provider, request, tools, config)
        assert projection is not None
        if rejection == "final_proof" and should_refuse(projection.payload["model"]):
            return replace(projection, fits=False)
        return projection

    monkeypatch.setattr(quality, "validate_provider_chat_admission", admission)
    monkeypatch.setattr(quality, "project_provider_final_request", final_proof)

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", messages,
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert attempts[:2] == [(first_vision, 1), (later_vision, 1)]
    assert messages == original_messages
    assert e.config.squilla_router.tiers == original_config
    if outcome == "refused":
        assert updates is None
        assert e.turn.metadata["router_model_decision"] == original_decision
        assert e.selector.current_config is original_deployment
        assert advance.await_count == e.recovery.used == 0
        return
    expected_tier = "c3" if outcome == "later_vision" else "c1"
    assert updates is not None
    assert updates["model_id"] == (later_vision if outcome == "later_vision" else text_model)
    assert e.turn.metadata["routed_tier"] == expected_tier
    assert e.turn.metadata["router_model_decision"]["state"]["tier"] == "c1"
    assert e.turn.metadata["router_model_decision"]["decision"] == original_decision["decision"]
    assert advance.await_count == e.recovery.used == 1
    if outcome == "marker":
        assert attempts[-1] == (text_model, 0)


@pytest.mark.asyncio
async def test_quality_image_retry_does_not_prepare_a_refused_visual_target_twice(
    quality_execution, monkeypatch,
):
    import opensquilla.engine.model_router_quality as quality

    e = quality_execution
    for tier in ("c1", "c2", "c3"):
        e.config.squilla_router.tiers[tier]["model"] = "qwen/qwen-vl-max"
    decision_before = deepcopy(e.turn.metadata["router_model_decision"])
    deployment_before = e.selector.current_config
    project = quality.project_provider_final_request
    attempts = []

    def refuse(provider, request, tools, config):
        projection = project(provider, request, tools, config)
        assert projection is not None
        attempts.append(projection.payload["model"])
        return replace(projection, fits=False)

    monkeypatch.setattr(quality, "project_provider_final_request", refuse)

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", _image_messages(),
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is None
    assert attempts == ["qwen/qwen-vl-max"] * 3
    assert e.turn.metadata["router_model_decision"] == decision_before
    assert e.selector.current_config is deployment_before
    assert e.recovery.used == 0


@pytest.mark.asyncio
async def test_quality_marker_retry_prices_projected_input_instead_of_native_images(
    quality_execution, monkeypatch,
):
    e = quality_execution
    for tier in ("c1", "c2", "c3"):
        e.config.squilla_router.tiers[tier]["model"] = "deepseek/deepseek-v4-flash"
    budget = e.config.squilla_router.budget
    budget.limit_usd = 0.001
    budget.action = "cap"
    budget.cap_tier = "c0"
    budget.include_next_turn_estimate = True
    zero_spend = SimpleNamespace(
        session_id=e.node.session_id, epoch=0, billed_cost_usd=0.0,
        total_cost_usd=0.0, estimated_cost_usd=0.0, cost_source="billed",
    )
    monkeypatch.setattr(
        e.manager.storage, "reconcile_session_usage_totals_from_ledger",
        AsyncMock(return_value=zero_spend),
    )
    # Synthetic $1/M input pricing makes the 1,024-token native-image reserve
    # exceed this budget while the actual text-marker request still fits.
    monkeypatch.setattr(
        squilla_router, "_budget_next_turn_estimate", lambda _model, tokens: tokens / 1_000_000,
    )

    updates = await e.recovery.prepare(
        e.wrapper, "validation_failure", _image_messages(),
        chat_config=ChatConfig(max_tokens=1024, thinking=False), tools=[],
    )

    assert updates is not None
    assert e.turn.metadata["routed_tier"] == "c1"
    assert e.turn.metadata["image_input_mode"] == "marker"
