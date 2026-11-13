from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.steps.jev_router import configured_jev_candidates, jev_input_fingerprint
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_jev import _handle_jev_preview
from opensquilla.jev_service import JevService
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ProviderConfig

SESSION = "agent:main:webchat:synthetic-preview-context"
TEXT = "Compare the synthetic system designs."


@pytest.fixture
async def preview_ctx(monkeypatch):
    config = GatewayConfig(jev_router={"enabled": True})
    service = JevService()
    session = SimpleNamespace(
        session_id="synthetic-preview-session", epoch=0, model_routing_revision=3,
        model_routing_mode="direct",
    )
    history = {
        "history_user_texts": ["A previous synthetic question."],
        "prev_assistant_text": "The previous answer.",
        "history_capacity_estimated_tokens": 400,
        "history_capacity_message_count": 4,
        "history_capacity_estimate_complete": True,
        "history_has_recent_image": False,
        "jev_turn_count": 3,
    }
    reader = AsyncMock(side_effect=lambda *args, **kwargs: dict(history))
    ctx = RpcContext(
        conn_id="synthetic-preview-context", config=config,
        session_manager=SimpleNamespace(storage=SimpleNamespace(
            get_session=AsyncMock(return_value=session),
        )),
        turn_runner=SimpleNamespace(jev_service=service, _router_previous_assistant_context=reader),
    )
    inherited = ProviderConfig(provider="openai", model="synthetic-preview-model")
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._emit_to_subscribers", AsyncMock())
    monkeypatch.setattr(
        "opensquilla.engine.steps.jev_router.resolve_jev_deployment",
        lambda *args, **kwargs: SimpleNamespace(ready=True, provider_config=inherited),
    )
    await service.bind_session(SESSION, session.session_id, session.epoch)
    yield SimpleNamespace(
        ctx=ctx, service=service, session=session, history=history, reader=reader,
        inherited=inherited,
    )
    await service.close()


async def _evaluate_preview(env, monkeypatch, *, text=TEXT):
    captured = []

    async def evaluate(turn, **kwargs):
        captured.append(turn)
        return turn

    monkeypatch.setattr("opensquilla.engine.steps.jev_router.apply_jev_router", evaluate)
    await _handle_jev_preview(
        {"sessionKey": SESSION, "draftId": "synthetic-draft", "text": text}, env.ctx,
    )
    task = env.service.current_check(SESSION, jev_input_fingerprint(text))
    assert task is not None
    await asyncio.wait_for(task, 1)
    return captured[0]


@pytest.mark.parametrize("session_mode,global_ensemble,expected", [
    ("ensemble", False, "ensemble"),
    ("direct", True, "direct"),
    ("router", True, "direct"),
    (None, True, "ensemble"),
    (None, False, "direct"),
])
async def test_preview_uses_session_execution_mode_before_global_default(
    preview_ctx, monkeypatch, session_mode, global_ensemble, expected,
):
    env = preview_ctx
    env.session.model_routing_mode = session_mode
    env.ctx.config.llm_ensemble.enabled = global_ensemble
    turn = await _evaluate_preview(env, monkeypatch)
    assert turn.metadata["jev_execution_mode"] == expected
    assert turn.metadata["routing_history_capacity_estimated_tokens"] == 400
    assert turn.metadata["routing_history_capacity_message_count"] == 4
    assert turn.metadata["routing_history_capacity_estimate_complete"] is True
    env.reader.assert_awaited_once_with(
        SESSION, expected_session_id=env.session.session_id,
        expected_session_epoch=0, include_capacity=True,
    )


@pytest.mark.parametrize("has_images", [True, False])
async def test_preview_image_history_controls_c4_eligibility(
    preview_ctx, monkeypatch, has_images,
):
    env = preview_ctx
    env.history.update(
        history_has_recent_image=has_images, history_image_turn_count=int(has_images),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog.shared_catalog", lambda: ModelCatalog())
    prepared = SimpleNamespace(
        supports_tools=True, context_window_tokens=128_000,
        descriptor={"aggregator": {"provider": "openai", "model": "combined"}},
    )
    monkeypatch.setattr(
        "opensquilla.engine.jev_ensemble.prepare_jev_ensemble", lambda *a, **kw: prepared,
    )
    turn = await _evaluate_preview(env, monkeypatch)
    assert turn.metadata["image_context_has_images"] is has_images
    assert turn.metadata["router_history_has_recent_image"] is has_images
    candidates = configured_jev_candidates(turn, env.inherited)
    assert any(candidate.kind == "ensemble" for candidate in candidates) is not has_images


async def test_large_preview_draft_keeps_old_history_pressure_separate(preview_ctx, monkeypatch):
    from opensquilla.engine.jev_context import build_context_restart_assessment

    env = preview_ctx
    env.ctx.config.jev_router.context_restart_min_tokens = 1024
    env.history.update(jev_completed_conversation_turns=20, jev_turn_count=21)
    large_input = "A large newly supplied document. " * 350
    turn = await _evaluate_preview(env, monkeypatch, text=large_input)
    assert len(turn.semantic_message) > 10_000
    assert turn.metadata["jev_completed_conversation_turns"] == 20
    assert turn.metadata["routing_history_capacity_estimated_tokens"] == 400
    assert build_context_restart_assessment(
        turn, config=env.ctx.config.jev_router, context_window_tokens=2048,
    ) is None
