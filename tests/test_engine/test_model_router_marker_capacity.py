from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from opensquilla.engine.agent import Agent
from opensquilla.engine.history import (
    HistoryReplayMessageProvenance,
    HistoryReplayProjection,
    project_history_replay_capacity,
)
from opensquilla.engine.routing.model_policy import ModelPolicyState
from opensquilla.engine.runtime import RouterHistoryReplayRequest, TurnRunner
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.steps.model_router import ModelRouteError
from opensquilla.engine.turn_runner.attachment_stage import _materialization_stats
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.provider.image_projection import ImageProjectionMode, project_messages
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ContentBlockImage,
    ContentBlockText,
    Message,
    ModelCapabilities,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.test_model_router_step import Classifier, context, install


@pytest.fixture
def image_request():
    buffer = io.BytesIO()
    Image.new("RGB", (4096, 4096), "white").save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    attachments = [{"type": "image/png", "name": "shape.png", "data": encoded}]
    messages = [Message(role="user", content=[
        ContentBlockText(text="Describe the shape."),
        ContentBlockImage(media_type="image/png", data=encoded),
    ])]
    stats = _materialization_stats(
        messages, attachments=attachments, generated_normalization_attachment_count=0,
    )
    return SimpleNamespace(attachments=attachments, messages=messages, stats=stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_large_image_marker_route_fits_without_compacting_originals(
    image_request, monkeypatch, tmp_path, historical,
):
    config = GatewayConfig(
        llm={
            "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
            "api_key": "synthetic-offline", "thinking": "off",
            "context_window_tokens": 10_000, "max_tokens": 1024,
        },
        squilla_router={
            "rollout_phase": "full",
            "tiers": {f"c{i}": {
                "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
            } for i in range(4)},
        },
        attachments={"media_root": str(tmp_path / "media")},
    )
    install(monkeypatch, Classifier(tier="c2"))
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openrouter", model=config.llm.model, api_key="synthetic-offline",
    )))
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:marker-capacity")
        envelope, _ = build_transcript_attachment_envelope(
            text="Describe the shape.", attachments=image_request.attachments,
            session_id=node.session_id, media_root=tmp_path / "media", persist_enabled=True,
        )
        if historical:
            await manager.append_message(node.session_key, "user", envelope)
            await manager.append_message(node.session_key, "assistant", "A white rectangle.")
        entry = await manager.append_message(
            node.session_key, "user", "Continue the image task." if historical else envelope,
        )
        runner = TurnRunner(provider_selector=selector, session_manager=manager, config=config)
        before = [(row.message_id, row.content) for row in await manager.get_transcript(
            node.session_key,
        )]
        turn, _ = await runner._run_pipeline(
            "Continue the image task." if historical else "Describe the shape.",
            node.session_key, selector.resolve(), selector, [], "system",
            [] if historical else image_request.attachments,
            attachment_materialization=None if historical else image_request.stats,
            history_has_recent_image=historical,
            bound_user_message_id=entry.message_id,
            expected_session_id=node.session_id, expected_session_epoch=0,
            router_history_replay_request=RouterHistoryReplayRequest(
                exclude_last_user=True, bound_user_message_id=entry.message_id,
                expected_session_id=node.session_id, expected_session_epoch=0,
            ),
        )
        assert turn.metadata["image_input_mode"] == "marker"
        assert turn.metadata["routed_tier"] == "c2"
        assert turn.metadata["router_model_decision"]["state"]["tier"] == "c2"
        assert turn.metadata["router_model_decision"]["status"] == "admitted"
        assert not turn.metadata.get("router_model_capacity_retry_pending")
        assert squilla_router._complete_request_estimated_tokens(turn, turn.semantic_message) < 2000
        if historical:
            assert turn.metadata["routing_history_capacity_estimated_tokens"] > 16_000
            assert turn.metadata["routing_history_marker_capacity_estimated_tokens"] < 2000
        else:
            assert turn.metadata["attachment_material_estimated_tokens"] == 16_384
            assert turn.metadata["attachment_marker_material_estimated_tokens"] < 200
        after = [(row.message_id, row.content) for row in await manager.get_transcript(
            node.session_key,
        )]
        assert after == before
        assert (await manager.get_session(node.session_key)).compaction_count == 0
        assert image_request.messages[0].content[1].data == image_request.attachments[0]["data"]


def test_marker_attachment_estimate_keeps_non_image_material(image_request):
    messages = deepcopy(image_request.messages)
    messages[0].content.append(ContentBlockText(text="material " * 20_000))
    stats = _materialization_stats(
        messages, attachments=image_request.attachments,
        generated_normalization_attachment_count=0,
    )
    projected = project_messages(messages, mode=ImageProjectionMode.MARKER).messages
    projected_stats = _materialization_stats(
        projected, attachments=image_request.attachments,
        generated_normalization_attachment_count=0,
    )
    assert stats.marker_estimated_tokens == projected_stats.estimated_tokens
    assert stats.marker_estimated_tokens > 10_000
    assert stats.estimated_tokens > stats.marker_estimated_tokens


@pytest.mark.asyncio
async def test_marker_mode_does_not_admit_native_candidate_with_marker_estimates(
    image_request, monkeypatch,
):
    install(monkeypatch, Classifier(tier="c2"))
    turn = context()
    turn.config.llm.thinking = "off"
    turn.config.llm.context_window_tokens = 10_000
    turn.config.llm.max_tokens = 1024
    for tier in ("c0", "c1", "c2"):
        turn.config.squilla_router.tiers[tier]["model"] = "deepseek/deepseek-v4-flash"
    turn.config.squilla_router.tiers["c3"]["model"] = "qwen/qwen-vl-max"
    turn.attachments = image_request.attachments
    turn.metadata.update(
        attachment_material_estimated_tokens=image_request.stats.estimated_tokens,
        attachment_marker_material_estimated_tokens=image_request.stats.marker_estimated_tokens,
    )
    await squilla_router.apply_squilla_router(turn)
    for _ in range(2):
        await squilla_router.finalize_squilla_router_capacity(turn)
        assert turn.metadata["routed_tier"] == "c2"
        assert turn.metadata["image_input_mode"] == "marker"
    turn.metadata["attachment_marker_material_estimated_tokens"] = 50_000
    with pytest.raises(ModelRouteError, match="cannot fit"):
        await squilla_router.finalize_squilla_router_capacity(turn)


def test_marker_history_keeps_provenance_token_surplus(image_request):
    replay = HistoryReplayProjection(
        messages=tuple(image_request.messages),
        message_provenance=(HistoryReplayMessageProvenance(
            entry_index=0, raw_token_floor_applies=False, persisted_token_surplus=3000,
        ),),
    )
    marker = project_history_replay_capacity(replay, image_mode=ImageProjectionMode.MARKER)
    without_surplus = project_history_replay_capacity(
        HistoryReplayProjection(messages=tuple(image_request.messages)),
        image_mode=ImageProjectionMode.MARKER,
    )
    assert marker.estimated_tokens >= without_surplus.estimated_tokens + 2900
    assert marker.media_block_count == marker.media_reserve_tokens == 0
    assert isinstance(replay.messages[0].content[1], ContentBlockImage)


@pytest.mark.asyncio
@pytest.mark.parametrize("refreshed_marker_tokens", [1000, 15_000])
async def test_marker_readmission_preserves_bound_deployment_after_compaction(
    monkeypatch, refreshed_marker_tokens,
):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    install(monkeypatch, Classifier(tier="c2"))
    turn = context()
    turn.config.llm.thinking = "off"
    turn.config.llm.context_window_tokens = 10_000
    turn.config.llm.max_tokens = 1024
    for tier in ("c0", "c1", "c2"):
        turn.config.squilla_router.tiers[tier]["model"] = "deepseek/deepseek-v4-flash"
    turn.config.squilla_router.tiers["c3"]["model"] = "qwen/qwen-vl-max"
    turn.metadata.update(
        image_context_has_images=True,
        routing_history_capacity_estimated_tokens=30_000,
        routing_history_capacity_message_count=10,
        routing_history_capacity_estimate_complete=True,
        routing_history_marker_capacity_estimated_tokens=20_000,
        routing_history_marker_capacity_message_count=10,
        routing_history_marker_capacity_estimate_complete=True,
    )
    await squilla_router.apply_squilla_router(turn)
    await squilla_router.finalize_squilla_router_capacity(turn, allow_compaction_retry=True)
    assert turn.metadata["routed_tier"] == "c2"
    assert turn.metadata["image_input_mode"] == "marker"
    assert turn.metadata["router_model_capacity_retry_pending"] is True
    original_model = turn.model
    original_decision = deepcopy(turn.metadata["router_model_decision"])
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openrouter", model=original_model, api_key="synthetic-offline",
    )))
    original_deployment = selector.current_config
    runner = TurnRunner(provider_selector=selector, config=turn.config)
    refreshed = {
        "history_capacity_estimated_tokens": refreshed_marker_tokens + 1000,
        "history_capacity_message_count": 3,
        "history_capacity_estimate_complete": True,
        "history_marker_capacity_estimated_tokens": refreshed_marker_tokens,
        "history_marker_capacity_message_count": 3,
        "history_marker_capacity_estimate_complete": True,
    }
    monkeypatch.setattr(
        runner, "_router_history_capacity_for_request", AsyncMock(return_value=refreshed),
    )
    readmit = runner._readmit_attachment_capacity(
        turn, selector, RouterHistoryReplayRequest(exclude_last_user=True),
        initial_provider_config=original_deployment,
    )
    if refreshed_marker_tokens > 10_000:
        with pytest.raises(ModelRouteError, match="cannot fit"):
            await readmit
        assert turn.metadata["router_model_capacity_retry_pending"] is True
    else:
        await readmit
        assert not turn.metadata.get("router_model_capacity_retry_pending")
    assert turn.metadata["routed_tier"] == "c2"
    assert turn.model == original_model
    assert turn.metadata["image_input_mode"] == "marker"
    assert turn.metadata["image_input_projection_required"] is True
    assert selector.current_config is original_deployment
    assert turn.metadata["router_model_decision"] == original_decision
    assert turn.metadata["routing_history_capacity_estimated_tokens"] == (
        refreshed_marker_tokens + 1000
    )
    assert turn.metadata["routing_history_marker_capacity_estimated_tokens"] == (
        refreshed_marker_tokens
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("compaction_enabled", [False, True])
async def test_historical_marker_turn_keeps_caption_and_history_on_wire(
    image_request, monkeypatch, tmp_path, compaction_enabled,
):
    import httpx

    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolContext
    from scripts.live_reasoning_replay_e2e import WireObserver, _config

    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    endpoint, model = "https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash"
    config = _config(tmp_path, "openrouter", model, endpoint, thinking="off")
    config.llm.context_window_tokens = 10_000
    config.llm.max_tokens = 1024
    config.compaction.enabled = compaction_enabled
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = "full"
    config.squilla_router.tiers = {
        f"c{i}": {"provider": "openrouter", "model": model} for i in range(4)
    }
    config.tools.allow = []
    config.tools.profile = "minimal"
    install(monkeypatch, Classifier(intent="continue", tier="c1"))
    caption, previous_answer = "The synthetic rectangle has caption DELTA47.", "Image unread."

    def respond(request):
        assert request.method == "POST" and request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        wire_text = json.dumps(payload["messages"], ensure_ascii=False)
        assert "image_url" not in wire_text
        assert "图片未分析" in wire_text
        assert caption in wire_text and previous_answer in wire_text
        frame = {
            "id": "synthetic-marker-response", "model": model,
            "choices": [{"index": 0, "delta": {"content": "Caption DELTA47 retained."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode(),
        )

    observer = WireObserver(endpoint, transport=httpx.MockTransport(respond), max_calls=1)
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:historical-marker-wire")
        encoded, _ = build_transcript_attachment_envelope(
            text=caption, attachments=image_request.attachments, session_id=node.session_id,
            media_root=tmp_path / "media", persist_enabled=True,
        )
        prior = await manager.append_message(node.session_key, "user", encoded)
        state = ModelPolicyState(
            tier="c1", task_id=prior.message_id,
            task_start_input_message_id=prior.message_id,
            generation_start_input_message_id=prior.message_id,
        )
        await manager.update_message_turn_context(node.session_key, prior.message_id, {
            "router_model_decision": {
                "input_message_id": prior.message_id,
                "status": "admitted", "state_committed": True, "state": state.to_dict(),
            },
        })
        await manager.append_message(node.session_key, "assistant", previous_answer)
        original_history = [
            row.model_dump() for row in await storage.get_canonical_transcript(node.session_id)
        ]
        prompt = "Continue the image task and repeat its caption."
        current = await manager.append_message(node.session_key, "user", prompt)
        runner = TurnRunner(
            provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
                provider="openrouter", model=model, api_key="synthetic-offline", base_url=endpoint,
            ))),
            tool_registry=ToolRegistry(), session_manager=manager, config=config,
        )
        with observer.observe():
            events = [event async for event in runner.run(
                prompt, session_key=node.session_key, bound_user_message_id=current.message_id,
                tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            )]
        assert not [event for event in events if event.kind == "error"]
        assert any(event.kind == "done" for event in events)
        assert len(observer.calls) == 1 and observer.calls[0].completed
        after = await storage.get_canonical_transcript(node.session_id)
        assert [row.model_dump() for row in after[:2]] == original_history
        assert after[2].message_id == current.message_id and after[2].content == prompt
        assert len(after) == 4
        assert (await manager.get_session(node.session_key)).compaction_count == 0
        assert not runner.has_attempted_compaction_this_turn(node.session_key)
        assert image_request.messages[0].content[1].data == image_request.attachments[0]["data"]


@pytest.mark.parametrize("support", ["unsupported", "supported", "unknown"])
@pytest.mark.parametrize("distinct_consumer", [False, True])
@pytest.mark.parametrize("mismatched_model", [False, True])
def test_compaction_proof_uses_exact_consumer_vision_evidence(
    image_request, monkeypatch, support, distinct_consumer, mismatched_model,
):
    from opensquilla.provider.model_catalog import shared_catalog

    model = "synthetic/active-text"
    active = OpenAIProvider(api_key="synthetic-offline", model=model, provider_kind="openrouter")
    consumer_model = "synthetic/durable-image" if distinct_consumer else model
    consumer = (
        OpenAIProvider(
            api_key="synthetic-durable", model=consumer_model, provider_kind="openrouter",
        ) if distinct_consumer else active
    )
    agent = Agent(provider=active, config=AgentConfig(
        model_id=model, model_vision_support="unsupported" if distinct_consumer else support,
        model_capabilities=ModelCapabilities(supports_vision=False),
        thinking=False, max_tokens=1024, context_window_tokens=10_000,
    ))
    resolved = []

    def resolve(model, **kwargs):
        resolved.append((model, kwargs["api_key"]))
        return support

    monkeypatch.setattr(shared_catalog(), "resolve_deployment_vision_support", resolve)
    before = [message.model_dump() for message in image_request.messages]
    proof = agent._project_compaction_consumer_request(
        consumer_provider=consumer, replay_summary="", kept_entries=[],
        active_user_message="Describe the shape.", active_user_in_history=False,
        bound_user_message_id=None, attachment_messages=image_request.messages,
        runtime_context_message=Message(role="user", content="Synthetic runtime context."),
        context_window_tokens=10_000, max_output_tokens=1024,
        consumer_model_id="synthetic/other-model" if mismatched_model else consumer_model,
    )
    assert proof is not None
    marker = support == "unsupported" and not mismatched_model
    assert proof.proof.get("media_image_blocks", 0) == int(not marker)
    assert proof.fits is marker
    assert resolved == (
        [(consumer_model, "synthetic-durable")]
        if distinct_consumer and not mismatched_model else []
    )
    assert [message.model_dump() for message in image_request.messages] == before
