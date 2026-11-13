"""Session advice respects deployment authority and explicit trust boundaries."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps import jev_router as step
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_router import JevCandidate, JevDecision, JevEvaluation
from opensquilla.provider.selector import ProviderConfig


@pytest.mark.parametrize("completed,history,window,answer,eligible,expected", [
    (2, 90_000, 100_000, "new_session", False, False),
    (19, 90_000, 100_000, "new_session", False, False),
    (20, 10_000, 1_000_000, "new_session", False, False),
    (20, 32_000, 1_000_000, "new_session", False, False),
    (20, 79_999, 100_000, "new_session", False, False),
    (20, 80_000, 100_000, "new_session", True, True),
    (20, 90_000, 100_000, "keep_context", True, False),
    (20, 90_000, 100_000, None, True, False),
    (20, 90_000, 100_000, "new_session", True, True),
    (20, 100_000, 0, "new_session", False, False),
])
async def test_context_notice_requires_age_and_native_positive_judgement(
    monkeypatch, completed, history, window, answer, eligible, expected,
):
    from opensquilla.jev_context import ContextRestartDecision
    from opensquilla.jev_service import JevService

    cfg = config()
    ctx = turn(cfg)
    ctx.metadata.update(
        jev_completed_conversation_turns=completed,
        routing_history_capacity_estimated_tokens=history,
    )
    monkeypatch.setattr(step, "configured_jev_candidates", lambda *_args: (
        JevCandidate("large", "synthetic-large", "tokenrhythm", context_window_tokens=window),
        JevCandidate("small", "synthetic-small", "tokenrhythm", context_window_tokens=window),
    ))

    async def evaluate(request, *_args, **_kwargs):
        assert (request.context_assessment is not None) is eligible
        return JevEvaluation(
            status="decided", context_decision=(
                ContextRestartDecision(action=answer, confidence=0.95) if answer else None
            ),
        )

    monkeypatch.setattr(step, "evaluate_jev", evaluate)
    service = JevService()
    try:
        await step.apply_jev_router(ctx, service=service, input_id="synthetic-continuity")
        snapshot = await service.snapshot(ctx.session_key, cfg.jev_router)
        notice = snapshot["recommendation"]
        assert (notice is not None) is expected
        assert not snapshot["blocked"]
        assert "jev_decision_id" not in ctx.metadata
        if expected:
            assert notice["action"] == "new_session"
            assert notice["handoff"]["completedTurns"] == 20
            assert "autoSwitchAt" not in notice
            # A context reminder consumes the shared prompt budget but does
            # not start the cooldown for model suggestions.
            assert await service.can_evaluate(
                ctx.session_key, "next-model-assessment", cooldown_turns=100,
            )
    finally:
        await service.close()


async def test_model_recommendation_has_priority_in_shared_budget(monkeypatch):
    from opensquilla.jev_context import ContextRestartDecision
    from opensquilla.jev_service import JevService

    cfg = config(max_prompts_per_hour=1)
    ctx = turn(cfg)
    ctx.metadata.update(
        jev_completed_conversation_turns=20,
        routing_history_capacity_estimated_tokens=90_000,
    )
    monkeypatch.setattr(step, "configured_jev_candidates", lambda *_args: (
        JevCandidate("large", "synthetic-large", "tokenrhythm", context_window_tokens=100_000),
        JevCandidate("small", "synthetic-small", "tokenrhythm", context_window_tokens=100_000),
    ))

    async def evaluate(request, *_args, **_kwargs):
        assert request.context_assessment is not None
        return JevEvaluation(
            status="decided", decision=JevDecision(
                action="switch", candidate_id="small", title="Use a smaller deployment",
                reason="The remaining task is a short synthetic transformation.",
                confidence=0.95, signal="cost_fit", phase="execution", evidence=(), risk="low",
            ), context_decision=ContextRestartDecision(action="new_session", confidence=0.95),
        )

    monkeypatch.setattr(step, "evaluate_jev", evaluate)
    service = JevService()
    try:
        await step.apply_jev_router(ctx, service=service, input_id="synthetic-budget")
        snapshot = await service.snapshot(ctx.session_key, cfg.jev_router)
        assert [record["action"] for record in snapshot["decisions"]] == ["switch"]
        assert snapshot["recommendation"]["id"] == ctx.metadata["jev_decision_id"]
    finally:
        await service.close()


@pytest.fixture(autouse=True)
def manual_credentials(monkeypatch):
    monkeypatch.setattr("opensquilla.desktop_account.account_credential", lambda provider: None)


def config(**changes):
    return GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "synthetic-large",
            "api_key": "synthetic-primary-key",
            "base_url": "https://tokenrhythm.studio/v1",
        },
        jev_router={"enabled": True, **changes},
    )


def test_primary_credential_resolves_without_selector():
    cfg = config()
    resolved = step.resolve_jev_deployment(cfg, cfg.jev_router)
    assert resolved.ready
    assert resolved.credential_source == "inherited"
    assert resolved.provider_config.api_key == "synthetic-primary-key"
    assert resolved.provider_config.model == cfg.jev_router.model
    assert not resolved.provider_config.replay_provider_state


def test_session_inherited_connection_remains_authoritative():
    cfg = config()
    inherited = ProviderConfig(
        provider="tokenrhythm",
        model="synthetic-session",
        api_key="synthetic-session-key",
        base_url="https://session.example.test/v1",
    )
    resolved = step.resolve_jev_deployment(cfg, cfg.jev_router, inherited=inherited)
    assert resolved.ready
    assert resolved.provider_config.api_key == "synthetic-session-key"
    assert resolved.provider_config.base_url == inherited.base_url


def test_other_provider_inheritance_does_not_hide_primary_key():
    cfg = config()
    inherited = ProviderConfig(
        provider="openrouter", model="synthetic-other", api_key="synthetic-other-key"
    )
    resolved = step.resolve_jev_deployment(cfg, cfg.jev_router, inherited=inherited)
    assert resolved.ready and resolved.provider_config.api_key == "synthetic-primary-key"


def test_nonprimary_credential_comes_from_its_own_profile():
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "api_key": "synthetic-primary-key"},
        llm_profiles={"tokenrhythm": {"api_key": "synthetic-profile-key"}},
    )
    resolved = step.resolve_jev_deployment(cfg, cfg.jev_router)
    assert resolved.ready
    assert resolved.provider_config.api_key == "synthetic-profile-key"


def test_account_mode_does_not_fall_back_to_stored_manual_key():
    cfg = config()
    cfg.llm.credential_source = "account"
    resolved = step.resolve_jev_deployment(cfg, cfg.jev_router)
    assert not resolved.ready and resolved.reason == "account_model_unavailable"


@pytest.mark.parametrize(
    ("provider", "endpoint", "expected"),
    [
        ("ollama", "http://localhost:11434", True),
        ("custom", "https://127.0.0.1:8000/v1", True),
        ("lm_studio", "http://[::1]:1234/v1", True),
        ("ollama", "https://remote.example.test/v1", False),
        ("custom", "https://remote.example.test/v1", False),
        ("tokenrhythm", "http://127.0.0.1:8000/v1", False),
        ("custom", "file:///tmp/example", False),
        ("custom", "", False),
        ("custom", "https://localhost.remote.example.test", False),
        ("custom", "https://[invalid", False),
    ],
)
def test_local_classification_requires_explicit_loopback(provider, endpoint, expected):
    assert step._deployment_is_local(provider, endpoint) is expected


class Service:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.records = []
        self.statuses = []
        self.notifications = 0

    async def settings(self, key, defaults):
        return defaults.model_dump()

    async def applied_target(self, key, revision):
        return None

    async def can_evaluate(self, *args, **kwargs):
        return self.allowed

    async def set_status(self, key, status):
        self.statuses.append(status)

    async def notify(self, key):
        self.notifications += 1

    async def publish(self, key, wire, **kwargs):
        self.records.append(wire)
        return {"id": "synthetic-decision"}


def turn(cfg):
    return TurnContext(
        message="A short synthetic task.",
        session_key="synthetic-session",
        config=cfg,
        model="synthetic-large",
        provider=None,
        tool_defs=[],
        system_prompt="",
    )


def test_current_candidate_retains_configured_role_after_deduplication(monkeypatch):
    from opensquilla.provider.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    monkeypatch.setattr("opensquilla.provider.model_catalog.shared_catalog", lambda: catalog)
    cfg = config()
    cfg.squilla_router.tiers = {
        "t0": {"model": "synthetic-small"},
        "c2": {"model": "synthetic-large"},
        "c3": {"model": "synthetic-large"},
    }
    candidates = step.configured_jev_candidates(turn(cfg))
    assert [(item.model, item.tier) for item in candidates] == [
        ("synthetic-large", "c2"), ("synthetic-small", "c0"),
    ]


def test_candidate_facts_use_exact_credential_sidecar_instead_of_active_provider_projection(
    monkeypatch,
):
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.tokenrhythm_catalog import (
        parse_tokenrhythm_declared,
        parse_tokenrhythm_published,
        tokenrhythm_authority_identity,
        tokenrhythm_pricing_fields,
        tokenrhythm_published_catalog_entries,
    )

    catalog = ModelCatalog()
    model = "synthetic-large"
    published = parse_tokenrhythm_published({"data": [{
        "id": model, "type": "chat", "currency": "CNY", "billingUnit": 1_000_000,
        "inputPrice": "20", "outputPrice": "40", "contextWindow": 500_000,
        "capabilities": {"tools": True, "vision": True},
    }]})
    declared = parse_tokenrhythm_declared({"data": [{
        "id": model, "currency": "CNY", "input_price_per_million": "7",
        "output_price_per_million": "14", "context_length": 250_000,
        "capabilities": {"tools": False, "vision": False},
    }]})
    foreign = parse_tokenrhythm_declared({"data": [{
        "id": model, "currency": "CNY", "input_price_per_million": "1",
        "output_price_per_million": "2", "context_length": 900_000,
        "capabilities": {"tools": True, "vision": True},
    }]})
    identity = tokenrhythm_authority_identity(
        provider="tokenrhythm", base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-primary-key",
    )
    other_identity = tokenrhythm_authority_identity(
        provider="tokenrhythm", base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-other-key",
    )
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published),
    )
    catalog.set_tokenrhythm_snapshot_sidecars(
        published=published, declared_by_authority={identity: declared, other_identity: foreign},
    )
    # The compatibility row can belong to another recently selected profile.
    catalog.set_live_provider_entries("tokenrhythm", {
        model: {"input_cost_per_mtok": 0.001, "output_cost_per_mtok": 0.002},
    })
    monkeypatch.setattr("opensquilla.provider.model_catalog.shared_catalog", lambda: catalog)
    cfg = config()
    cfg.squilla_router.tiers = {"c3": {"model": model}}
    candidate = step.configured_jev_candidates(turn(cfg))[0]
    expected = tokenrhythm_pricing_fields(declared[model].pricing)
    assert candidate.context_window_tokens == 250_000
    assert candidate.supports_tools is False
    assert candidate.vision_support == "unsupported"
    assert candidate.input_cost_per_million == expected["input_cost_per_mtok"]
    assert candidate.output_cost_per_million == expected["output_cost_per_mtok"]
    catalog.set_user_overrides({f"tokenrhythm/{model}": {"input_cost_per_mtok": 0.5}})
    assert step.configured_jev_candidates(turn(cfg))[0].input_cost_per_million == 0.5


def test_custom_tokenrhythm_endpoint_cannot_inherit_official_prices_or_capabilities(monkeypatch):
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.tokenrhythm_catalog import (
        parse_tokenrhythm_published,
        tokenrhythm_published_catalog_entries,
    )

    catalog = ModelCatalog()
    published = parse_tokenrhythm_published({"data": [{
        "id": "synthetic-large", "type": "chat", "currency": "CNY",
        "billingUnit": 1_000_000, "inputPrice": "7", "outputPrice": "14",
        "contextWindow": 900_000, "capabilities": {"tools": True, "vision": True},
    }]})
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog.shared_catalog", lambda: catalog)
    cfg = config()
    cfg.squilla_router.tiers = {"c3": {"model": "synthetic-large"}}
    inherited = ProviderConfig(
        provider="tokenrhythm", model="synthetic-large", api_key="synthetic-primary-key",
        base_url="https://custom.example.test/v1",
    )
    candidate = step.configured_jev_candidates(turn(cfg), inherited)[0]
    assert candidate.input_cost_per_million is None
    assert candidate.output_cost_per_million is None
    assert candidate.context_window_tokens == 0
    assert candidate.supports_tools is None
    assert candidate.vision_support == "unknown"


def route(**changes):
    return replace(
        JevDecision(
            action="switch",
            candidate_id="small",
            title="Try the smaller model",
            reason="A short synthetic task can use the cheaper configured deployment.",
            confidence=0.9,
            signal="cost_fit",
            phase="unknown",
            evidence=("Known catalog prices",),
            risk="low",
            estimated_savings_percent=80,
        ),
        **changes,
    )


def stub_decision(monkeypatch, decision):
    candidates = (
        JevCandidate("current", "synthetic-large", "tokenrhythm"),
        JevCandidate("small", "synthetic-small", "tokenrhythm"),
    )
    monkeypatch.setattr(step, "configured_jev_candidates", lambda *args: candidates)
    monkeypatch.setattr(
        step,
        "resolve_jev_deployment",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True,
            provider_config=None,
        ),
    )
    captured = []

    async def evaluate(request, config, **kwargs):
        captured.append(request)
        return JevEvaluation(status="decided", decision=decision)

    monkeypatch.setattr(step, "evaluate_jev", evaluate)
    return captured


async def test_suggest_mode_publishes_without_changing_execution(monkeypatch):
    captured = stub_decision(monkeypatch, route())
    service = Service()
    ctx = turn(config(mode="suggest"))
    await step.apply_jev_router(ctx, service=service)
    assert len(captured) == 1 and len(service.records) == 1
    assert ctx.model == "synthetic-large" and "jev_target" not in ctx.metadata
    assert service.records[0]["target"]["model"] == "synthetic-small"


async def test_trust_savings_still_wait_for_confirmation_deadline(monkeypatch):
    stub_decision(monkeypatch, route())
    service = Service()
    ctx = turn(config(mode="trust"))
    await step.apply_jev_router(ctx, service=service)
    assert ctx.model == "synthetic-large"
    assert "jev_pending_application" not in ctx.metadata
    assert "jev_target" not in ctx.metadata
    assert ctx.metadata["jev_decision_id"] == "synthetic-decision"


@pytest.mark.parametrize(
    "changes",
    [
        {"risk": "medium"},
        {"risk": "high"},
        {"estimated_savings_percent": None},
        {"estimated_savings_percent": 0},
        {"action": "review"},
    ],
)
async def test_trust_keeps_higher_risk_or_unpriced_choice_pending(monkeypatch, changes):
    stub_decision(monkeypatch, route(**changes))
    service = Service()
    ctx = turn(config(mode="trust"))
    await step.apply_jev_router(ctx, service=service)
    assert ctx.model == "synthetic-large" and "jev_target" not in ctx.metadata
    assert len(service.records) == 1


async def test_budget_or_cooldown_prevents_decision_call(monkeypatch):
    captured = stub_decision(monkeypatch, route())
    service = Service(allowed=False)
    ctx = turn(config())
    await step.apply_jev_router(ctx, service=service)
    assert not captured and not service.records


async def test_keep_decision_does_not_create_a_prompt(monkeypatch):
    stub_decision(monkeypatch, route(action="keep", candidate_id=""))
    service = Service()
    await step.apply_jev_router(turn(config()), service=service)
    assert not service.records


async def test_provider_failure_keeps_current_model(monkeypatch):
    stub_decision(monkeypatch, None)

    async def unavailable(*args, **kwargs):
        return JevEvaluation(status="unavailable", error="timeout")

    monkeypatch.setattr(step, "evaluate_jev", unavailable)
    service = Service()
    ctx = turn(config())
    await step.apply_jev_router(ctx, service=service)
    assert ctx.model == "synthetic-large" and not service.records
    assert service.statuses[-1] == "unavailable"


async def test_request_uses_full_turn_count_and_complete_capacity_estimate(monkeypatch):
    captured = stub_decision(monkeypatch, route())
    service = Service()
    ctx = turn(config())
    ctx.system_prompt = "Synthetic system instructions. " * 20
    ctx.metadata.update(
        {
            "jev_turn_count": 30,
            "router_history_user_texts": ["A recent user message."],
            "routing_history_capacity_estimated_tokens": 2_000,
            "routing_history_capacity_message_count": 15,
        }
    )
    await step.apply_jev_router(ctx, service=service)
    assert captured[0].turn_count == 30
    assert captured[0].context_tokens > 2_000
    assert captured[0].context_tokens == ctx.metadata["jev_context_estimated_tokens"]
