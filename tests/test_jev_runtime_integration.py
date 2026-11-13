from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.jev_router import jev_input_fingerprint
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    PromptAssemblerStage,
    PromptAssemblerStageInput,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_router import JevCandidate, JevDecision, JevEvaluation
from opensquilla.jev_service import get_jev_service
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

SESSION = "agent:main:webchat:jev-runtime"
LARGE = "synthetic-large"
SMALL = "synthetic-small"


@pytest.fixture
async def execution(monkeypatch):
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(SESSION)
        entry = await manager.append_message(SESSION, "user", "Classify this synthetic label.")
        config = GatewayConfig(
            llm={"provider": "tokenrhythm", "model": LARGE, "api_key": "synthetic-offline"},
            jev_router={"enabled": True}, squilla_router={"enabled": True},
            llm_ensemble={"enabled": False},
        )
        selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
            provider="tokenrhythm", model=LARGE, api_key="synthetic-offline",
            base_url="https://tokenrhythm.studio/v1",
        )))
        runner = TurnRunner(provider_selector=selector, session_manager=manager, config=config)
        service = get_jev_service(config, manager, runner)
        candidates = (
            JevCandidate("large", LARGE, "tokenrhythm", input_cost_per_million=10,
                         output_cost_per_million=20),
            JevCandidate("small", SMALL, "tokenrhythm", input_cost_per_million=1,
                         output_cost_per_million=2),
        )
        monkeypatch.setattr(
            "opensquilla.engine.steps.jev_router.configured_jev_candidates",
            lambda *_args, **_kwargs: candidates,
        )
        evaluate = AsyncMock(return_value=JevEvaluation(
            status="decided", decision=JevDecision(
                action="switch", candidate_id="small", title="Use a smaller model",
                reason="This short synthetic classification can use a smaller model.",
                confidence=0.95, signal="cost_fit", phase="execution",
                evidence=("short_input",), risk="low", estimated_savings_percent=90,
            ),
        ))
        monkeypatch.setattr("opensquilla.engine.steps.jev_router.evaluate_jev", evaluate)
        staged = AsyncMock(side_effect=AssertionError("JEV must not stage a classifier decision"))
        monkeypatch.setattr(manager, "stage_model_router_decision", staged)
        yield SimpleNamespace(
            runner=runner, selector=selector, service=service, config=config,
            session=session, entry=entry, manager=manager, evaluate=evaluate, staged=staged,
        )
        await service.close()


async def run_pipeline(execution):
    e = execution
    return await e.runner._run_pipeline(
        "Classify this synthetic label.", SESSION, e.selector.resolve(), e.selector,
        [], "system", [], bound_user_message_id=e.entry.message_id,
        expected_session_id=e.session.session_id, expected_session_epoch=e.session.epoch,
    )


async def test_enabled_advice_bypasses_classifier_and_preserves_current_model(
    execution, monkeypatch,
) -> None:
    e = execution
    old_router = AsyncMock(side_effect=AssertionError("The retired route should not run"))
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", old_router)
    assert e.runner._uses_model_tier_policy(SESSION) is False
    turn, _ = await run_pipeline(e)
    assert turn.model == LARGE
    assert e.selector.current_config.model == LARGE
    assert e.config.squilla_router.enabled is True
    assert turn.config.squilla_router.enabled is False
    assert "router_model_decision" not in turn.metadata
    e.staged.assert_not_awaited()
    old_router.assert_not_awaited()
    await e.service.current_check(SESSION, jev_input_fingerprint(turn.semantic_message))
    recommendation = (await e.service.snapshot(SESSION, e.config.jev_router))["recommendation"]
    assert recommendation["target"]["model"] == SMALL
    assert recommendation["status"] == "pending"


async def test_trust_prepares_choice_without_mutating_provider_in_background(execution) -> None:
    e = execution
    await e.service.configure(SESSION, {"mode": "trust"}, e.config.jev_router)
    turn, provider = await run_pipeline(e)
    assert turn.model == LARGE
    assert e.selector.current_config.model == LARGE
    assert provider is not None
    assessment = await e.service.current_check(
        SESSION, jev_input_fingerprint(turn.semantic_message),
    )
    assert "jev_target" not in assessment.metadata
    assert "jev_pending_application" not in assessment.metadata
    ledger = (await e.service.snapshot(SESSION, e.config.jev_router))["decisions"]
    assert ledger[0]["status"] == "pending"
    assert ledger[0]["target"]["model"] == SMALL
    assert ledger[0]["autoSwitchAt"] > ledger[0]["createdAt"]
    e.staged.assert_not_awaited()


async def test_disabled_advice_keeps_the_existing_router_path(execution, monkeypatch) -> None:
    e = execution
    await e.service.configure(SESSION, {"enabled": False}, e.config.jev_router)
    assert e.runner._uses_model_tier_policy(SESSION) is True
    e.config.squilla_router.strategy = "legacy_v4_phase3"
    calls = []

    async def existing_router(ctx):
        calls.append(ctx.semantic_message)
        ctx.model = "synthetic-existing-route"
        return ctx

    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", existing_router)
    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.prepare_model_routing_runtime", AsyncMock(),
    )
    turn, _ = await run_pipeline(e)
    assert calls == ["Classify this synthetic label."]
    assert turn.model == "synthetic-existing-route"
    assert e.selector.current_config.model == "synthetic-existing-route"
    e.evaluate.assert_not_awaited()


async def test_prompt_assembly_keeps_execution_model_while_advice_runs_in_background(
    execution,
) -> None:
    e = execution
    await e.service.configure(SESSION, {"mode": "trust"}, e.config.jev_router)

    async def execute(request):
        return await e.runner._run_pipeline(
            request.runtime_message, request.session_key, request.provider,
            request.cloned_selector, request.tool_defs, request.base_prompt, request.attachments,
            semantic_message=request.semantic_message,
            bound_user_message_id=request.bound_user_message_id,
            expected_session_id=request.expected_session_id,
            expected_session_epoch=request.expected_session_epoch,
        )

    stage = PromptAssemblerStage(
        prompt_assembler=SimpleNamespace(assemble_prompt=lambda *_args, **_kwargs: "system"),
        pipeline_executor=SimpleNamespace(run_pipeline=execute),
        router_context=SimpleNamespace(fetch_router_context=AsyncMock(return_value={})),
        prompt_config_resolver=SimpleNamespace(
            resolve_prompt_config=lambda _turn: ("system", None, None),
        ),
        prompt_report_builder=SimpleNamespace(build_prompt_report=lambda **_kwargs: None),
        session_id_resolver=SimpleNamespace(
            resolve_session_id_for_log=AsyncMock(return_value=e.session.session_id),
        ),
        memory_fingerprint=SimpleNamespace(memory_mode_fingerprint=lambda: None),
    )
    outcome = await stage.run(PromptAssemblerStageInput(
        runtime_message="Classify this synthetic label.",
        semantic_input="Classify this synthetic label.", extra_prompt_context=None,
        provider=e.selector.resolve(), cloned_selector=e.selector, tool_defs=[],
        effective_tool_context=None, tool_metadata={}, session_key=SESSION, agent_id="main",
        turn_id="synthetic-turn", attachments=[], bootstrap_context_mode=None,
        model=LARGE, history_has_persisted_user=True, persist_input=False,
        bound_user_message_id=e.entry.message_id, expected_session_id=e.session.session_id,
        expected_session_epoch=e.session.epoch,
    ))
    assert outcome.output is not None
    assert outcome.output.resolved_model == LARGE
    assert outcome.output.selector_model == LARGE
    assert "jev_target" not in outcome.output.turn.metadata
    assessment = await e.service.current_check(
        SESSION, jev_input_fingerprint("Classify this synthetic label."),
    )
    assert "jev_target" not in assessment.metadata
    assert "jev_pending_application" not in assessment.metadata
    recommendation = (await e.service.snapshot(SESSION, e.config.jev_router))["recommendation"]
    assert recommendation["target"]["model"] == SMALL
    assert recommendation["status"] == "pending"
