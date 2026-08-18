from __future__ import annotations

import pickle

import pytest

from opensquilla.provider import cache_affinity
from opensquilla.provider.cache_affinity import (
    CacheAffinityEvidenceInput,
    CachePriceQuote,
    build_cache_affinity_receipt,
    build_cache_domain_guard,
    build_credential_namespace_token,
    cache_affinity_decay_factor,
    cache_affinity_score_adjustment,
    canonical_cache_endpoint,
)


def test_credential_hmac_key_is_lazy_and_process_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def token_bytes(size: int) -> bytes:
        nonlocal calls
        calls += 1
        return b"x" * size

    cache_affinity._credential_hmac_key.cache_clear()
    monkeypatch.setattr(cache_affinity.secrets, "token_bytes", token_bytes)
    try:
        first = _credential()
        second = _credential()
        assert first == second
        assert calls == 1
    finally:
        cache_affinity._credential_hmac_key.cache_clear()


def _credential(*, secret: str = "secret-a", org_id: str = "org-a"):
    token = build_credential_namespace_token(
        provider="openai",
        resolved_secret=secret,
        org_id=org_id,
    )
    assert token is not None
    return token


def _guard():
    guard = build_cache_domain_guard(
        session_epoch=3,
        role="single",
        topology="single",
        provider="openai",
        requested_model="gpt-test",
        base_url="https://API.EXAMPLE.test/v1/",
        thinking_enabled=False,
        effective_thinking_level="off",
        thinking_budget_tokens=0,
        credential_namespace_token=_credential(),
    )
    assert guard is not None
    return guard


def _quote() -> CachePriceQuote:
    return CachePriceQuote(
        provider="openai",
        canonical_model="gpt-test",
        endpoint_scope="https://api.example.test:443/v1/",
        upstream_scope="openai",
        price_source="static_table",
        normal_input_per_million=2.0,
        normal_output_per_million=8.0,
        cache_read_per_million=0.2,
        cache_write_per_million=2.5,
    )


def _evidence(
    *,
    evidence_kind: str = "read_hit",
    decay_factor: float = 0.75,
    role: str = "single",
    quote: CachePriceQuote | None = None,
) -> CacheAffinityEvidenceInput:
    resolved_quote = quote or _quote()
    return CacheAffinityEvidenceInput(
        identity="openai:gpt-test",
        role=role,  # type: ignore[arg-type]
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        cached_tokens=60_000 if evidence_kind == "read_hit" else 0,
        cache_write_tokens=80_000,
        decay_factor=decay_factor,
        price_quote=resolved_quote,
        ranking_price_source="static_table",
        endpoint_scope=resolved_quote.endpoint_scope,
        upstream_scope=resolved_quote.upstream_scope,
    )


def test_credential_namespace_is_opaque_and_binds_org() -> None:
    secret = "never-render-this-secret"
    first = _credential(secret=secret, org_id="org-a")
    same = _credential(secret=secret, org_id="org-a")
    other_org = _credential(secret=secret, org_id="org-b")

    assert first == same
    assert first != other_org
    assert secret not in repr(first)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(first)


def test_credential_namespace_binds_tenant_headers_without_exposing_them() -> None:
    first = build_credential_namespace_token(
        provider="openai",
        resolved_secret="secret",
        tenant_headers={"OpenAI-Project": "project-a"},
    )
    same = build_credential_namespace_token(
        provider="openai",
        resolved_secret="secret",
        tenant_headers={"openai-project": "project-a"},
    )
    changed = build_credential_namespace_token(
        provider="openai",
        resolved_secret="secret",
        tenant_headers={"OpenAI-Project": "project-b"},
    )

    assert first is not None
    assert first == same
    assert first != changed
    assert "project-a" not in repr(first)


def test_cache_domain_guard_is_role_epoch_endpoint_and_credential_specific() -> None:
    common = {
        "session_epoch": 1,
        "role": "proposer",
        "topology": "multiple",
        "provider": "anthropic",
        "requested_model": "claude-test",
        "base_url": "https://api.example.test/v1",
        "thinking_enabled": True,
        "effective_thinking_level": "high",
        "thinking_budget_tokens": 4_096,
    }
    first = build_cache_domain_guard(
        **common,
        credential_namespace_token=_credential(secret="a"),
    )
    same = build_cache_domain_guard(
        **common,
        credential_namespace_token=_credential(secret="a"),
    )
    changed = build_cache_domain_guard(
        **common,
        credential_namespace_token=_credential(secret="b"),
    )

    assert first is not None
    assert first == same
    assert first != changed
    assert "api.example" not in repr(first)


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.test/v1",
        "https://example.test/v1?tenant=a",
        "https://example.test/v1#fragment",
        "ftp://example.test/v1",
    ],
)
def test_cache_endpoint_fails_closed_on_ambiguous_urls(url: str) -> None:
    assert canonical_cache_endpoint(url) is None


def test_openrouter_guard_requires_strict_upstream_without_fallbacks() -> None:
    common = {
        "session_epoch": 1,
        "role": "aggregator",
        "topology": "multiple",
        "provider": "openrouter",
        "requested_model": "anthropic/claude-test",
        "base_url": "https://openrouter.ai/api/v1",
        "thinking_enabled": False,
        "effective_thinking_level": "off",
        "thinking_budget_tokens": 0,
        "credential_namespace_token": _credential(),
    }

    assert (
        build_cache_domain_guard(
            **common,
            upstream_provider="anthropic",
            provider_routing_strict=True,
            allow_fallbacks=False,
        )
        is not None
    )
    assert (
        build_cache_domain_guard(
            **common,
            upstream_provider="anthropic",
            provider_routing_strict=False,
            allow_fallbacks=False,
        )
        is None
    )
    assert (
        build_cache_domain_guard(
            **common,
            upstream_provider="auto",
            provider_routing_strict=True,
            allow_fallbacks=False,
        )
        is None
    )


def test_receipt_requires_exact_usage_and_prefers_read_hit() -> None:
    receipt = build_cache_affinity_receipt(
        physical_attempt_id="attempt-1",
        role="single",
        topology="single",
        execution_slot="direct",
        requested_identity="openai:gpt-test",
        actual_identity="openai:gpt-test",
        cache_domain_guard=_guard(),
        cached_tokens=10,
        cache_write_tokens=1_000,
        observed_at_monotonic=100.0,
    )
    invalid_bool = build_cache_affinity_receipt(
        physical_attempt_id="attempt-2",
        role="single",
        topology="single",
        execution_slot="direct",
        requested_identity="openai:gpt-test",
        actual_identity="openai:gpt-test",
        cache_domain_guard=_guard(),
        cached_tokens=True,
        cache_write_tokens=0,
        observed_at_monotonic=100.0,
    )
    mismatch = build_cache_affinity_receipt(
        physical_attempt_id="attempt-3",
        role="single",
        topology="single",
        execution_slot="direct",
        requested_identity="openai:gpt-test",
        actual_identity="openai:other",
        cache_domain_guard=_guard(),
        cached_tokens=10,
        cache_write_tokens=0,
        observed_at_monotonic=100.0,
    )

    assert receipt is not None
    assert receipt.evidence_kind == "read_hit"
    assert receipt.cached_tokens == 10
    assert invalid_bool is None
    assert mismatch is None


def test_decay_is_per_receipt_and_has_no_hidden_constant() -> None:
    assert (
        cache_affinity_decay_factor(
            observed_at_monotonic=100.0,
            now_monotonic=125.0,
            ttl_seconds=100.0,
            age_decay="none",
        )
        == 1.0
    )
    assert cache_affinity_decay_factor(
        observed_at_monotonic=100.0,
        now_monotonic=125.0,
        ttl_seconds=100.0,
        age_decay="linear",
    ) == pytest.approx(0.75)
    assert (
        cache_affinity_decay_factor(
            observed_at_monotonic=100.0,
            now_monotonic=201.0,
            ttl_seconds=100.0,
            age_decay="linear",
        )
        == 0.0
    )


def _adjustment(*, policy, evidence, role="single", **overrides):
    values = {
        "policy": policy,
        "evidence": evidence,
        "role": role,
        "topology": "single" if role == "single" else "multiple",
        "intent_type": "continue",
        "intent_confidence": 0.9,
        "intent_confidence_threshold": 0.8,
        "estimated_input_tokens": 100_000,
        "tool_log_tokens": 0,
        "candidate_output_tokens": 10_000,
        "proposer_count": 2,
        "ranking_input_per_million": 2.0,
        "ranking_output_per_million": 8.0,
        "ranking_price_source": "static_table",
        "price_input_weight": 0.7,
        "price_output_weight": 0.3,
        "price_reference_usd_per_million": 10.0,
        "cost_weight": 0.25,
    }
    values.update(overrides)
    return cache_affinity_score_adjustment(**values)


def test_bonus_uses_only_configured_value_and_decay() -> None:
    result = _adjustment(
        policy={
            "strategy": "bonus",
            "topologies": ["single"],
            "bonus_by_evidence": {"read_hit": 0.04, "write_only": 0.01},
        },
        evidence=_evidence(),
    )

    assert result.score_adjustment == pytest.approx(0.03)
    assert result.trace() == {
        "identity": "openai:gpt-test",
        "role": "single",
        "strategy": "bonus",
        "evidence_kind": "read_hit",
        "decay_factor": 0.75,
        "score_adjustment": pytest.approx(0.03),
    }


@pytest.mark.parametrize(
    ("confidence", "threshold"),
    [
        (float("nan"), 0.8),
        (float("inf"), 0.8),
        (0.9, float("nan")),
        (1.1, 0.8),
    ],
)
def test_cache_intent_confidence_fails_closed(
    confidence: float,
    threshold: float,
) -> None:
    result = _adjustment(
        policy={
            "strategy": "bonus",
            "topologies": ["single"],
            "bonus_by_evidence": {"read_hit": 0.04, "write_only": 0.01},
        },
        evidence=_evidence(),
        intent_confidence=confidence,
        intent_confidence_threshold=threshold,
    )

    assert result.score_adjustment == 0.0


@pytest.mark.parametrize("role", ["single", "proposer", "aggregator"])
@pytest.mark.parametrize(
    ("intent_type", "intent_confidence", "expected_adjustment"),
    [
        ("continue", 0.8 - 1e-9, 0.0),
        ("continue", 0.8, 0.03),
        ("redo", 1.0, 0.0),
        ("unknown", 1.0, 0.0),
    ],
)
def test_affinity_intent_and_confidence_gate_is_role_symmetric(
    role: str,
    intent_type: str,
    intent_confidence: float,
    expected_adjustment: float,
) -> None:
    result = _adjustment(
        policy={
            "strategy": "bonus",
            "topologies": ["single" if role == "single" else "multiple"],
            "bonus_by_evidence": {"read_hit": 0.04, "write_only": 0.01},
        },
        evidence=_evidence(role=role),
        role=role,
        intent_type=intent_type,
        intent_confidence=intent_confidence,
        intent_confidence_threshold=0.8,
    )

    assert result.score_adjustment == pytest.approx(expected_adjustment)


def test_expected_cost_preserves_units_and_read_hit_token_bucket() -> None:
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
        evidence=_evidence(),
    )

    assert result.input_tokens == 100_000
    assert result.cache_tokens == 60_000
    assert result.observed_cache_tokens == 60_000
    assert result.hit_probability == pytest.approx(0.6)
    assert result.cache_read_per_million == pytest.approx(0.2)
    assert result.cache_write_per_million == pytest.approx(2.5)
    assert result.baseline_input_cost_usd == pytest.approx(0.2)
    assert result.cache_hit_input_cost_usd == pytest.approx(0.092)
    assert result.cache_miss_input_cost_usd == pytest.approx(0.23)
    assert result.effective_input_per_million == pytest.approx(1.472)
    assert result.score_adjustment == pytest.approx(0.00924)


@pytest.mark.parametrize(
    ("role", "expected_input_tokens"),
    [
        ("single", 1_250),
        ("proposer", 1_250),
        ("aggregator", 2_750),
    ],
)
def test_expected_cost_uses_role_specific_pure_input_projection(
    role: str,
    expected_input_tokens: int,
) -> None:
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single" if role == "single" else "multiple"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
        evidence=_evidence(role=role),
        role=role,
        estimated_input_tokens=1_000,
        tool_log_tokens=250,
        candidate_output_tokens=500,
        proposer_count=3,
    )

    assert result.input_tokens == expected_input_tokens


def test_expected_cost_zero_input_and_clamp_are_explicit() -> None:
    policy = {
        "strategy": "expected_cost",
        "topologies": ["single"],
        "hit_probability_by_evidence": {
            "read_hit": 1.0,
            "write_only": 0.0,
        },
    }
    zero = _adjustment(
        policy=policy,
        evidence=_evidence(decay_factor=1.0),
        estimated_input_tokens=0,
        tool_log_tokens=0,
    )

    clamp_quote = CachePriceQuote(
        provider="openai",
        canonical_model="gpt-test",
        endpoint_scope="https://api.example.test:443/v1/",
        upstream_scope="openai",
        price_source="static_table",
        normal_input_per_million=10.0,
        normal_output_per_million=0.0,
        cache_read_per_million=0.0,
        cache_write_per_million=10.0,
    )
    clamped = _adjustment(
        policy=policy,
        evidence=_evidence(decay_factor=1.0, quote=clamp_quote),
        estimated_input_tokens=60_000,
        tool_log_tokens=0,
        ranking_input_per_million=10.0,
        ranking_output_per_million=0.0,
        price_input_weight=1.0,
        price_output_weight=0.0,
        price_reference_usd_per_million=5.0,
        cost_weight=0.25,
    )

    assert zero.score_adjustment == 0.0
    assert zero.input_tokens == 0
    assert clamped.cost_normalized_before == 1.0
    assert clamped.cost_normalized_after == 0.0
    assert clamped.score_adjustment == pytest.approx(0.25)


def test_expected_cost_can_apply_negative_cache_write_delta() -> None:
    quote = _quote()
    evidence = CacheAffinityEvidenceInput(
        identity="openai:gpt-test",
        role="single",
        evidence_kind="write_only",
        cached_tokens=0,
        cache_write_tokens=100_000,
        decay_factor=1.0,
        price_quote=quote,
        ranking_price_source="static_table",
        endpoint_scope=quote.endpoint_scope,
        upstream_scope=quote.upstream_scope,
    )
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.0,
            },
        },
        evidence=evidence,
    )

    assert result.score_adjustment < 0.0


def test_expected_cost_fails_closed_on_output_rate_mismatch() -> None:
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
        evidence=_evidence(),
        ranking_output_per_million=9.0,
    )

    assert result.score_adjustment == 0.0


def test_cache_domain_guard_binds_thinking_mode_and_budget() -> None:
    common = {
        "session_epoch": 1,
        "role": "single",
        "topology": "single",
        "provider": "openai",
        "requested_model": "gpt-test",
        "base_url": "https://api.example.test/v1",
        "credential_namespace_token": _credential(),
    }
    disabled = build_cache_domain_guard(
        **common,
        thinking_enabled=False,
        effective_thinking_level="off",
        thinking_budget_tokens=0,
    )
    enabled = build_cache_domain_guard(
        **common,
        thinking_enabled=True,
        effective_thinking_level="HIGH",
        thinking_budget_tokens=4_096,
    )
    other_budget = build_cache_domain_guard(
        **common,
        thinking_enabled=True,
        effective_thinking_level="high",
        thinking_budget_tokens=8_192,
    )

    assert disabled is not None
    assert enabled is not None
    assert other_budget is not None
    assert disabled != enabled
    assert enabled != other_budget


def test_receipt_rejects_role_topology_mismatch() -> None:
    assert build_cache_affinity_receipt(
        physical_attempt_id="attempt-mismatch",
        role="single",
        topology="multiple",
        execution_slot="direct",
        requested_identity="openai:gpt-test",
        actual_identity="openai:gpt-test",
        cache_domain_guard=_guard(),
        cached_tokens=10,
        cache_write_tokens=0,
        observed_at_monotonic=100.0,
    ) is None


def test_expected_cost_requires_exact_ranking_price_source() -> None:
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
        evidence=_evidence(),
        ranking_price_source="catalog",
    )

    assert result.score_adjustment == 0.0


def test_expected_cost_evidence_rejects_quote_deployment_scope_mismatch() -> None:
    quote = _quote()
    with pytest.raises(ValueError, match="deployment scope"):
        CacheAffinityEvidenceInput(
            identity="openai:gpt-test",
            role="single",
            evidence_kind="read_hit",
            cached_tokens=10,
            cache_write_tokens=0,
            decay_factor=1.0,
            price_quote=quote,
            ranking_price_source="static_table",
            endpoint_scope="https://other.example.test:443/v1/",
            upstream_scope="openai",
        )


def test_expected_cost_overflow_fails_closed() -> None:
    evidence = CacheAffinityEvidenceInput(
        identity="openai:gpt-test",
        role="single",
        evidence_kind="read_hit",
        cached_tokens=10**400,
        cache_write_tokens=0,
        decay_factor=1.0,
        price_quote=_quote(),
        ranking_price_source="static_table",
        endpoint_scope="https://api.example.test:443/v1/",
        upstream_scope="openai",
    )
    result = _adjustment(
        policy={
            "strategy": "expected_cost",
            "topologies": ["single"],
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
        evidence=evidence,
        estimated_input_tokens=10**400,
    )

    assert result.score_adjustment == 0.0
