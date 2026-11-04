from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.turn_runner.agent_bootstrap_stage import _ResolvedCatalog
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.protocol import count_provider_image_blocks
from opensquilla.provider.types import ErrorEvent, ModelCapabilities
from tests.test_engine import test_model_router_quality_images as quality_fixtures
from tests.test_engine.test_model_router_execution import _model_image_continuation_runtime

quality_execution = quality_fixtures.quality_execution


@pytest.fixture
def image_candidates(monkeypatch):
    runtime = _model_image_continuation_runtime(monkeypatch)
    router = runtime.gateway.squilla_router
    text = deepcopy(router.tiers["c2"])
    first_model, later_model = "synthetic/image-first", "synthetic/image-later"
    router.tiers = {
        "c0": deepcopy(text), "c1": text,
        "c2": {"provider": "openrouter", "model": first_model},
        "c3": {"provider": "openrouter", "model": later_model},
    }
    runtime.metadata.update(
        routed_tier="c1",
        router_model_decision={"state": {"tier": "c1"}, "decision": {"tier": "c1"}},
    )
    catalog = ModelCatalog()
    catalog._populate_from_data([
        {
            "id": model, "context_length": 96_000,
            "top_provider": {"max_completion_tokens": 16_000},
            "supported_parameters": ["tools"],
            "architecture": {
                "input_modalities": ["text"] if model == text["model"] else ["text", "image"],
            },
        }
        for model in (text["model"], first_model, later_model)
    ])
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    limits = {
        model: _ResolvedCatalog(
            max_tokens=8192, context_window=96_000,
            capabilities=ModelCapabilities(supports_vision=True, supports_tools=True),
            vision_support="supported",
            provider_request_proof_max_chars=256 if model == first_model else 100_000,
        )
        for model in (first_model, later_model)
    }
    looked_up = []

    def lookup(deployment, **kwargs):
        looked_up.append(deployment.model)
        return limits[deployment.model]

    runtime.wrapper.configure_image_continuation_catalog(lookup)
    projections = []
    project_request = runtime_module.project_provider_final_request

    def observe_projection(provider, messages, tools, config, **kwargs):
        projection = project_request(provider, messages, tools, config, **kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(runtime_module, "project_provider_final_request", observe_projection)
    return SimpleNamespace(
        runtime=runtime, first=first_model, later=later_model, limits=limits,
        looked_up=looked_up, projections=projections,
    )


@pytest.mark.asyncio
async def test_tool_image_continues_after_first_visual_candidate_fails_final_proof(
    image_candidates,
):
    candidates = image_candidates
    runtime = candidates.runtime
    before_messages = [message.model_dump(mode="json") for message in runtime.messages]
    before_policy = deepcopy(runtime.metadata["router_model_decision"])
    before_tiers = deepcopy(runtime.gateway.squilla_router.tiers)
    original_selector = runtime.wrapper._selector

    rebound = await runtime.wrapper.prepare_image_continuation(runtime.messages, runtime.config)

    assert rebound is not None
    assert candidates.looked_up == [candidates.first, candidates.later]
    assert [projection.fits for projection in candidates.projections] == [False, True]
    assert candidates.projections[0].proof["fits_char_budget"] is False
    assert candidates.projections[0].proof["fits_token_budget"] is True
    assert runtime.wrapper._selector is not original_selector
    assert runtime.wrapper.active_deployment_config().model == candidates.later
    assert runtime.metadata["routed_tier"] == "c3"
    assert runtime.metadata["router_model_decision"] == before_policy
    assert runtime.gateway.squilla_router.tiers == before_tiers
    assert [message.model_dump(mode="json") for message in runtime.messages] == before_messages
    assert all(not provider.calls for provider in runtime.built)
    events = [event async for event in runtime.wrapper.chat(runtime.messages, config=rebound)]
    assert any(event.kind == "done" for event in events)
    assert len(runtime.built[-1].calls) == 1
    assert count_provider_image_blocks(runtime.built[-1].calls[0]["messages"]) == 1
    assert not runtime.primary.calls


@pytest.mark.asyncio
async def test_tool_image_rejections_preserve_active_selector_and_metadata(image_candidates):
    candidates = image_candidates
    runtime = candidates.runtime
    candidates.limits[candidates.later] = replace(
        candidates.limits[candidates.later], provider_request_proof_max_chars=256,
    )
    original_selector = runtime.wrapper._selector
    before_metadata = deepcopy(runtime.metadata)
    before_messages = [message.model_dump(mode="json") for message in runtime.messages]

    rebound = await runtime.wrapper.prepare_image_continuation(runtime.messages, runtime.config)

    assert rebound is None
    assert candidates.looked_up == [candidates.first, candidates.later]
    assert [projection.fits for projection in candidates.projections] == [False, False]
    assert runtime.wrapper._selector is original_selector
    assert runtime.wrapper.active_deployment_config().model == "synthetic/text-small"
    assert runtime.metadata == before_metadata
    assert [message.model_dump(mode="json") for message in runtime.messages] == before_messages
    assert all(not provider.calls for provider in runtime.built)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["construction", "capability", "admission"])
async def test_tool_image_skips_unavailable_visual_candidate(
    image_candidates, monkeypatch, refusal,
):
    candidates = image_candidates
    runtime = candidates.runtime
    candidates.limits[candidates.first] = replace(
        candidates.limits[candidates.first], provider_request_proof_max_chars=100_000,
        vision_support="unsupported" if refusal == "capability" else "supported",
    )
    from opensquilla.provider import selector as selector_module

    build_provider = selector_module._build_provider

    def build_candidate(deployment):
        if deployment.model == candidates.first and refusal == "construction":
            raise ValueError("Synthetic unavailable deployment")
        provider = build_provider(deployment)
        if deployment.model == candidates.first and refusal == "admission":
            provider.validate_chat_admission = lambda *args: ErrorEvent(
                message="Synthetic request refusal", code="synthetic_refusal",
            )
        return provider

    monkeypatch.setattr(selector_module, "_build_provider", build_candidate)

    rebound = await runtime.wrapper.prepare_image_continuation(runtime.messages, runtime.config)

    assert rebound is not None
    assert runtime.wrapper.active_deployment_config().model == candidates.later
    assert runtime.metadata["routed_tier"] == "c3"
    assert all(not provider.calls for provider in runtime.built)


@pytest.mark.asyncio
async def test_quality_retry_after_image_hop_keeps_its_exact_context_window(
    quality_execution, monkeypatch,
):
    from opensquilla.engine import model_router_quality
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.model_catalog import shared_catalog
    from opensquilla.provider.types import ChatConfig, Message

    execution = quality_execution
    execution.config.squilla_router.tiers = {
        "c0": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
        "c1": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
        "c2": {"provider": "openrouter", "model": "qwen/qwen-vl-max"},
        "c3": {"provider": "openrouter", "model": "qwen/qwen-vl-max"},
    }
    entry = await execution.manager.append_message(
        execution.node.session_key, "user", "Inspect a synthetic image from a tool.",
    )
    turn, provider = await execution.runner._run_pipeline(
        "Inspect a synthetic image from a tool.", execution.node.session_key,
        execution.selector.resolve(), execution.selector, [], "system", [],
        bound_user_message_id=entry.message_id,
        expected_session_id=execution.node.session_id, expected_session_epoch=0,
        model_router_context={
            "router_model_state": execution.turn.metadata["router_model_decision"]["state"],
        },
    )
    wrapper = _SelectorFallbackProvider(
        provider, execution.selector, turn.metadata, image_routing_config=execution.config,
        image_routing_session_key=execution.node.session_key,
    )
    wrapper.configure_image_continuation_catalog(lambda deployment, **kwargs: _ResolvedCatalog(
        max_tokens=1024, context_window=32_000,
        capabilities=shared_catalog().get_capabilities(
            deployment.model, provider_name=deployment.provider,
        ),
        vision_support="supported",
    ))
    image_config = await wrapper.prepare_image_continuation(
        quality_fixtures._image_messages(),
        ChatConfig(max_tokens=1024, model_vision_support="unsupported"),
    )
    assert image_config is not None
    assert wrapper.active_deployment_config().model == "qwen/qwen-vl-max"
    assert wrapper._image_continuation_rebound is True
    assert turn.metadata["router_model_decision"]["state"]["tier"] == "c0"
    admitted_configs, proofs = [], []
    project_request = model_router_quality.project_provider_final_request

    def observe(provider, messages, tools, config):
        projection = project_request(provider, messages, tools, config)
        admitted_configs.append(config)
        proofs.append(projection)
        return projection

    monkeypatch.setattr(model_router_quality, "project_provider_final_request", observe)
    # Compaction has replaced the image with its text summary before recovery.
    updates = await wrapper.prepare_quality_retry(
        reason="validation_failure", scope_id="synthetic-image-recovery", remaining_budget=1,
        messages=[Message(role="user", content="Synthetic compacted image summary.")],
        chat_config=image_config, tools=[],
    )

    assert updates is not None
    assert wrapper.active_deployment_config().model == "deepseek/deepseek-v4-pro"
    assert updates["context_window_tokens"] > 0
    assert admitted_configs[-1].provider_context_window_tokens == updates["context_window_tokens"]
    assert proofs[-1].proof["token_budget_source"] == "physical_context_window"
    assert wrapper._config_for_active_leg(
        admitted_configs[-1],
    ).provider_context_window_tokens == updates["context_window_tokens"]
    assert wrapper._image_continuation_rebound is False
