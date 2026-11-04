from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest

from opensquilla.engine.model_policy_recovery import ModelPolicyRecovery
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.provider.image_projection import count_image_blocks
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, ContentBlockImage, ContentBlockText, Message
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
