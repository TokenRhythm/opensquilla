from __future__ import annotations

import httpx
import pytest

from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderBuildError,
    ProviderConfig,
    ProviderNotConfiguredError,
    SelectorConfig,
)
from opensquilla.provider.types import ModelInfo

HIGH_TIER_MODEL = "openrouter/high-tier-region-locked"
MID_TIER_MODEL = "openrouter/mid-tier-available"
LOW_TIER_MODEL = "openrouter/low-tier-available"
BASELINE_MODEL = "openrouter/baseline-available"


def test_clone_isolates_config_from_original_mutation() -> None:
    primary = ProviderConfig(
        provider="anthropic", model="a", api_key="ka", provider_routing={"a": "x"}
    )
    fallback = ProviderConfig(provider="ollama", model="b")
    selector = ModelSelector(SelectorConfig(primary=primary, fallbacks=[fallback]))

    clone = selector.clone()

    # The clone owns its own config objects, not the originals.
    assert clone.current_config is not primary
    assert clone.current_config.provider_routing is not primary.provider_routing

    # Rebinding the original primary and editing the original routing dict
    # in place must not leak into the already-cloned selector.
    selector.sync_primary(ProviderConfig(provider="openai", model="c"))
    primary.provider_routing["a"] = "MUTATED"

    assert clone.current_config.provider == "anthropic"
    assert clone.current_config.model == "a"
    assert clone.current_config.provider_routing == {"a": "x"}


def test_turn_clone_disables_replay_for_plugin_fallback_without_mutating_shared_selector(
    monkeypatch,
) -> None:
    plugin_fallback = ProviderConfig(
        provider="anthropic",
        model="plugin-fallback",
        api_key="plugin-test-key",
        replay_provider_state=True,
    )

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            del primary_failure
            return [plugin_fallback]

    built: list[ProviderConfig] = []

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    shared = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="primary",
                api_key="primary-test-key",
            )
        ),
        plugin=_Plugin(),
    )
    turn_selector = shared.clone()

    turn_selector.disable_provider_state_replay()
    fallback = turn_selector.next_fallback_after_failure(RuntimeError("primary failed"))

    assert fallback.replay_provider_state is False
    assert built[-1].replay_provider_state is False
    assert plugin_fallback.replay_provider_state is True
    assert shared.current_config.replay_provider_state is True


def test_matching_fallback_filters_plugin_chain_before_build(monkeypatch) -> None:
    text_fallback = ProviderConfig(
        "openrouter",
        "text-fallback",
        api_key="plugin-test-key",
    )
    unknown_fallback = ProviderConfig(
        "openrouter",
        "unknown-fallback",
        api_key="plugin-test-key",
    )
    vision_fallback = ProviderConfig(
        "openrouter",
        "vision-fallback",
        api_key="plugin-test-key",
    )
    second_vision_fallback = ProviderConfig(
        "openrouter",
        "second-vision-fallback",
        api_key="plugin-test-key",
    )
    observed_failures: list[Exception] = []

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            observed_failures.append(primary_failure)
            return [
                text_fallback,
                unknown_fallback,
                vision_fallback,
                second_vision_fallback,
            ]

    built: list[ProviderConfig] = []

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "primary",
                api_key="primary-test-key",
            ),
            fallbacks=[
                ProviderConfig(
                    "openrouter",
                    "static-fallback-must-not-run",
                    api_key="static-test-key",
                )
            ],
        ),
        plugin=_Plugin(),
    )
    failure = RuntimeError("classified failure")

    fallback = selector.next_fallback_after_failure_matching(
        failure,
        predicate=lambda cfg: cfg.model in {
            "vision-fallback",
            "second-vision-fallback",
        },
    )

    assert observed_failures == [failure]
    assert fallback is vision_fallback
    assert built == [vision_fallback]
    assert [cfg.model for cfg in selector.remaining_chain()] == [
        "vision-fallback",
        "second-vision-fallback",
    ]


def test_matching_fallback_honors_empty_plugin_veto_atomically(monkeypatch) -> None:
    static_fallback = ProviderConfig(
        "openrouter",
        "static-vision-fallback",
        api_key="static-test-key",
    )
    plugin_calls = 0

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            nonlocal plugin_calls
            del primary_failure
            plugin_calls += 1
            return []

    built: list[ProviderConfig] = []

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "primary",
                api_key="primary-test-key",
            ),
            fallbacks=[static_fallback],
        ),
        plugin=_Plugin(),
    )
    original_chain = selector.remaining_chain()

    with pytest.raises(IndexError, match="No fallback chain available"):
        selector.next_fallback_after_failure_matching(
            RuntimeError("classified failure"),
            predicate=lambda cfg: cfg.model == "static-vision-fallback",
        )

    assert plugin_calls == 1
    assert built == []
    assert selector.current_config is original_chain[0]
    assert selector.remaining_chain() == original_chain


def test_matching_fallback_applies_replay_and_capacity_before_predicate(
    monkeypatch,
) -> None:
    approved = ProviderConfig(
        "tokenrhythm",
        "vision-model",
        api_key="approved-key",
        base_url="https://approved.example/v1",
        replay_provider_state=True,
    )
    unapproved_endpoint = ProviderConfig(
        "tokenrhythm",
        "vision-model",
        api_key="other-key",
        base_url="https://unapproved.example/v1",
        replay_provider_state=True,
    )

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            del primary_failure
            return [unapproved_endpoint, approved]

    predicate_inputs: list[ProviderConfig] = []
    built: list[ProviderConfig] = []

    def predicate(cfg: ProviderConfig) -> bool:
        predicate_inputs.append(cfg)
        return True

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "baseline",
                api_key="primary-test-key",
            ),
            fallbacks=[approved],
        ),
        plugin=_Plugin(),
    )
    selector.override_model_with_bounded_fallback_chain(
        HIGH_TIER_MODEL,
        [approved],
    )
    selector.disable_provider_state_replay()

    fallback = selector.next_fallback_after_failure_matching(
        RuntimeError("classified failure"),
        predicate=predicate,
    )

    assert [cfg.base_url for cfg in predicate_inputs] == [
        "https://approved.example/v1"
    ]
    assert all(cfg.replay_provider_state is False for cfg in predicate_inputs)
    assert fallback.base_url == "https://approved.example/v1"
    assert fallback.replay_provider_state is False
    assert built == [fallback]
    assert approved.replay_provider_state is True
    assert unapproved_endpoint.replay_provider_state is True


def test_matching_fallback_build_failure_does_not_advance_selector(monkeypatch) -> None:
    primary = ProviderConfig(
        "openrouter",
        "primary",
        api_key="primary-test-key",
    )
    text_fallback = ProviderConfig(
        "openrouter",
        "text-fallback",
        api_key="text-test-key",
    )
    vision_fallback = ProviderConfig(
        "openrouter",
        "vision-fallback",
        api_key="vision-test-key",
    )
    built: list[ProviderConfig] = []

    def failing_build_provider(cfg: ProviderConfig):
        built.append(cfg)
        raise ProviderBuildError("synthetic build failure")

    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        failing_build_provider,
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[text_fallback, vision_fallback],
        )
    )
    original_chain = selector.remaining_chain()

    with pytest.raises(ProviderBuildError, match="synthetic build failure"):
        selector.next_fallback_after_failure_matching(
            RuntimeError("classified failure"),
            predicate=lambda cfg: cfg.model == "vision-fallback",
        )

    assert built == [vision_fallback]
    assert selector.current_config is primary
    assert selector.remaining_chain() == original_chain


def test_static_matching_fallback_build_failure_is_atomic(monkeypatch) -> None:
    primary = ProviderConfig("openrouter", "primary", api_key="primary-test-key")
    incompatible = ProviderConfig(
        "openrouter",
        "incompatible",
        api_key="incompatible-test-key",
    )
    target = ProviderConfig("openrouter", "target", api_key="target-test-key")
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[incompatible, target],
        )
    )
    original_chain = selector.remaining_chain()

    def failing_build_provider(cfg: ProviderConfig):
        assert cfg is target
        raise ProviderBuildError("synthetic target build failure")

    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        failing_build_provider,
    )

    with pytest.raises(ProviderBuildError, match="synthetic target build failure"):
        selector.next_fallback_matching(
            predicate=lambda cfg: cfg.model == "target",
        )

    assert selector.current_config is primary
    assert selector.remaining_chain() == original_chain


def test_next_fallback_build_failure_keeps_active_matching_candidate(monkeypatch) -> None:
    primary = ProviderConfig("openrouter", "primary", api_key="primary-test-key")
    first_vision = ProviderConfig(
        "openrouter",
        "first-vision",
        api_key="first-vision-test-key",
    )
    second_vision = ProviderConfig(
        "openrouter",
        "second-vision",
        api_key="second-vision-test-key",
    )
    built: list[ProviderConfig] = []

    def build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        if cfg is second_vision:
            raise ProviderBuildError("synthetic second-candidate build failure")
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[first_vision, second_vision],
        )
    )

    active_provider = selector.next_fallback_after_failure_matching(
        RuntimeError("classified failure"),
        predicate=lambda cfg: cfg.model in {"first-vision", "second-vision"},
    )

    assert active_provider is first_vision
    assert selector.current_config is first_vision
    with pytest.raises(
        ProviderBuildError,
        match="synthetic second-candidate build failure",
    ):
        selector.next_fallback()

    assert built == [first_vision, second_vision]
    assert selector.current_config is first_vision
    assert selector.remaining_chain() == [first_vision, second_vision]


def test_override_model_keeps_original_primary_as_first_fallback(monkeypatch) -> None:
    built: list[ProviderConfig] = []

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
                base_url="https://openrouter.ai/api",
            )
        )
    )

    selector.override_model(HIGH_TIER_MODEL)
    primary = selector.resolve()
    fallback = selector.next_fallback_after_failure(
        RuntimeError("HTTP 403: This model is not available in your region.")
    )

    assert primary.model == HIGH_TIER_MODEL
    assert fallback.model == BASELINE_MODEL
    assert fallback.provider == "openrouter"
    assert [cfg.model for cfg in built] == [
        HIGH_TIER_MODEL,
        BASELINE_MODEL,
    ]


def test_override_model_with_router_fallback_chain_prefers_lower_tiers(monkeypatch) -> None:
    built: list[ProviderConfig] = []

    def fake_build_provider(cfg: ProviderConfig) -> ProviderConfig:
        built.append(cfg)
        return cfg

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
                base_url="https://openrouter.ai/api",
            )
        )
    )

    selector.override_model_with_fallback_chain(
        HIGH_TIER_MODEL,
        [
            {"tier": "c2", "provider": "openrouter", "model": MID_TIER_MODEL},
            {"tier": "c1", "provider": "openrouter", "model": BASELINE_MODEL},
            {"tier": "c0", "provider": "openrouter", "model": LOW_TIER_MODEL},
        ],
    )

    resolved_models = [selector.resolve().model]
    for _ in range(3):
        resolved_models.append(
            selector.next_fallback_after_failure(
                RuntimeError("HTTP 403: This model is not available in your region.")
            ).model
        )

    assert resolved_models == [
        HIGH_TIER_MODEL,
        MID_TIER_MODEL,
        BASELINE_MODEL,
        LOW_TIER_MODEL,
    ]
    assert [cfg.model for cfg in built] == resolved_models


def test_strict_router_fallback_chain_discards_configured_lower_tail(monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda cfg: cfg)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
                base_url="https://openrouter.ai/api",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="openrouter",
                    model=LOW_TIER_MODEL,
                    api_key="sk-test",
                    base_url="https://openrouter.ai/api",
                )
            ],
        )
    )

    selector.override_model_with_fallback_chain(
        HIGH_TIER_MODEL,
        [{"tier": "c2", "provider": "openrouter", "model": MID_TIER_MODEL}],
        preserve_existing_tail=False,
    )

    assert [cfg.model for cfg in selector.remaining_chain()] == [
        HIGH_TIER_MODEL,
        MID_TIER_MODEL,
    ]


def test_strict_empty_router_fallback_chain_removes_every_lower_model(monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda cfg: cfg)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="openrouter",
                    model=LOW_TIER_MODEL,
                    api_key="sk-test",
                )
            ],
        )
    )

    selector.override_model_with_fallback_chain(
        HIGH_TIER_MODEL,
        [],
        preserve_existing_tail=False,
    )

    assert [cfg.model for cfg in selector.remaining_chain()] == [HIGH_TIER_MODEL]


@pytest.mark.parametrize(
    "router_chain, expected_models",
    [
        ([], [HIGH_TIER_MODEL]),
        (
            [{"tier": "c2", "provider": "openrouter", "model": MID_TIER_MODEL}],
            [HIGH_TIER_MODEL, MID_TIER_MODEL],
        ),
    ],
)
def test_capacity_bounded_fallback_chain_drops_configured_lower_models(
    router_chain: list[object],
    expected_models: list[str],
) -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="openrouter",
                    model=LOW_TIER_MODEL,
                    api_key="sk-test",
                )
            ],
        )
    )

    selector.override_model_with_bounded_fallback_chain(
        HIGH_TIER_MODEL,
        router_chain,
    )

    assert [config.model for config in selector.remaining_chain()] == expected_models


def test_capacity_bound_filters_plugin_failover_replacement() -> None:
    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            del primary_failure
            return [
                ProviderConfig("openrouter", "unknown-small", api_key="plugin-key")
            ]

    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openrouter", "baseline", api_key="test-key")
        ),
        plugin=_Plugin(),
    )
    selector.override_model_with_bounded_fallback_chain(
        HIGH_TIER_MODEL,
        [{"provider": "openrouter", "model": MID_TIER_MODEL}],
    )

    with pytest.raises(IndexError, match="No fallback chain available"):
        selector.next_fallback_after_failure(RuntimeError("primary failed"))


def test_capacity_bound_rejects_plugin_endpoint_swap_for_same_model() -> None:
    approved = ProviderConfig(
        "tokenrhythm",
        "same-model",
        api_key="approved-key",
        base_url="https://approved.example/v1",
    )

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            del primary_failure
            return [
                ProviderConfig(
                    "tokenrhythm",
                    "same-model",
                    api_key="other-key",
                    base_url="https://smaller.example/v1",
                )
            ]

    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openrouter", "baseline", api_key="test-key"),
            fallbacks=[approved],
        ),
        plugin=_Plugin(),
    )
    selector.override_model_with_bounded_fallback_chain(
        HIGH_TIER_MODEL,
        [approved],
    )

    with pytest.raises(IndexError, match="No fallback chain available"):
        selector.next_fallback_after_failure(RuntimeError("primary failed"))


# A synthetic, public-dummy credential: it only exists to prove redaction.
FAKE_LEAKED_KEY = "sk-test-000fakefakefakefake"


class _AuthRejectingProvider:
    async def list_models(self) -> list[ModelInfo]:
        raise RuntimeError(f"HTTP 401: invalid api key {FAKE_LEAKED_KEY}")


class _HealthyProvider:
    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(provider="ollama", model_id="test-model-good")]


class _CompatibilityProviderThatSwallowsByDefault:
    async def list_models(self, *, raise_on_error: bool = False) -> list[ModelInfo]:
        if raise_on_error:
            raise RuntimeError(f"HTTP 401: invalid api key {FAKE_LEAKED_KEY}")
        return []


def _selector_with_failing_primary(monkeypatch) -> ModelSelector:
    def fake_build_provider(cfg: ProviderConfig):
        if cfg.provider == "openrouter":
            return _AuthRejectingProvider()
        return _HealthyProvider()

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="openrouter/auth-locked",
                api_key=FAKE_LEAKED_KEY,
            ),
            fallbacks=[ProviderConfig(provider="ollama", model="test-model-good")],
        )
    )


async def test_list_models_detailed_classifies_and_redacts_auth_failures(monkeypatch) -> None:
    selector = _selector_with_failing_primary(monkeypatch)

    result = await selector.list_models_detailed()

    # The healthy provider's models still come through.
    assert [m["model_id"] for m in result.models] == ["test-model-good"]

    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.provider == "openrouter"
    assert error.model_hint == "openrouter/auth-locked"
    assert error.kind == ProviderFailureKind.AUTH_INVALID.value
    # The provider echoed the bad key back; the surfaced detail must not.
    assert FAKE_LEAKED_KEY not in error.detail
    assert "***" in error.detail
    assert "invalid api key" in error.detail


async def test_list_models_delegates_to_detailed_and_drops_errors(monkeypatch) -> None:
    selector = _selector_with_failing_primary(monkeypatch)

    models = await selector.list_models()

    # Public behavior unchanged: failed links are skipped, good models kept.
    assert models == (await selector.list_models_detailed()).models
    assert [m["model_id"] for m in models] == ["test-model-good"]


async def test_list_models_detailed_reports_every_failed_chain_link(monkeypatch) -> None:
    def fake_build_provider(cfg: ProviderConfig):
        return _AuthRejectingProvider()

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(provider="openrouter", model="openrouter/auth-locked-a"),
            fallbacks=[ProviderConfig(provider="deepseek", model="deepseek/auth-locked-b")],
        )
    )

    result = await selector.list_models_detailed()

    assert result.models == []
    assert [(e.provider, e.model_hint) for e in result.errors] == [
        ("openrouter", "openrouter/auth-locked-a"),
        ("deepseek", "deepseek/auth-locked-b"),
    ]


async def test_list_models_detailed_resolves_snapshots_per_chain_link(monkeypatch) -> None:
    built: list[str] = []

    def fake_build_provider(cfg: ProviderConfig):
        built.append(cfg.provider)
        if cfg.provider == "tokenrhythm":  # pragma: no cover - regression guard
            raise AssertionError("snapshot-backed chain link reached provider I/O")
        if cfg.provider == "openrouter":
            return _AuthRejectingProvider()
        return _HealthyProvider()

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="tokenrhythm",
                model="cached-model",
                api_key="sk_tr_synthetic_selector_key",
            ),
            fallbacks=[
                ProviderConfig(provider="ollama", model="test-model-good"),
                ProviderConfig(provider="openrouter", model="auth-locked"),
            ],
        )
    )
    resolved: list[str] = []

    def snapshot_resolver(cfg: ProviderConfig):
        resolved.append(cfg.provider)
        if cfg.provider != "tokenrhythm":
            return None
        return [
            ModelInfo(
                provider="tokenrhythm",
                model_id="cached-model",
                max_output_tokens=131_072,
            )
        ]

    result = await selector.list_models_detailed(snapshot_resolver=snapshot_resolver)

    assert resolved == ["tokenrhythm", "ollama", "openrouter"]
    assert built == ["ollama", "openrouter"]
    assert [model["model_id"] for model in result.models] == [
        "cached-model",
        "test-model-good",
    ]
    assert [(error.provider, error.kind) for error in result.errors] == [
        ("openrouter", ProviderFailureKind.AUTH_INVALID.value)
    ]


async def test_detailed_listing_enables_adapter_strict_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        lambda _cfg: _CompatibilityProviderThatSwallowsByDefault(),
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="openrouter/auth-locked",
                api_key=FAKE_LEAKED_KEY,
            )
        )
    )

    result = await selector.list_models_detailed()

    assert result.models == []
    assert result.errors[0].kind == ProviderFailureKind.AUTH_INVALID.value
    assert FAKE_LEAKED_KEY not in result.errors[0].detail
    assert await selector.list_models() == []


async def test_known_cross_provider_key_never_reaches_transport(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (
            requests.append(request)
            or httpx.Response(200, json={"data": []}, request=request)
        )
    )
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)
    leaked = "sk_tr_abcdefghijklmnop"
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="openrouter/model",
                api_key=leaked,
                base_url="https://openrouter.ai/api/v1",
            )
        )
    )

    result = await selector.list_models_detailed()

    assert requests == []
    assert result.models == []
    assert result.errors[0].kind == ProviderFailureKind.UNKNOWN.value
    assert "tokenrhythm" in result.errors[0].detail
    assert leaked not in result.errors[0].detail


async def test_known_key_never_reaches_conflicting_official_host(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (
            requests.append(request)
            or httpx.Response(200, json={"data": []}, request=request)
        )
    )
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)
    leaked = "sk_tr_abcdefghijklmnop"
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="tokenrhythm",
                model="deepseek-v4-pro",
                api_key=leaked,
                base_url="https://openrouter.ai/api/v1",
            )
        )
    )

    result = await selector.list_models_detailed()

    assert requests == []
    assert result.models == []
    assert "openrouter" in result.errors[0].detail
    assert leaked not in result.errors[0].detail


# ---------------------------------------------------------------------------
# Unconfigured-selector state (cold-boot gateways)
# ---------------------------------------------------------------------------


def test_is_configured_false_without_key_for_key_requiring_provider() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig(provider="openrouter", model="m", api_key=""))
    )
    assert selector.is_configured is False


def test_is_configured_false_without_provider_id() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig(provider="", model="", api_key=""))
    )
    assert selector.is_configured is False


def test_is_configured_true_for_keyless_local_provider() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig(provider="ollama", model="llama3", api_key=""))
    )
    assert selector.is_configured is True


def test_resolve_raises_not_configured_instead_of_building_keyless_provider() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig(provider="openrouter", model="m", api_key=""))
    )
    with pytest.raises(ProviderNotConfiguredError) as exc_info:
        selector.resolve()
    # Subclasses ProviderBuildError so existing resolve() handlers degrade the same.
    assert isinstance(exc_info.value, ProviderBuildError)


def test_sync_primary_transitions_unconfigured_selector_live() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig(provider="openrouter", model="m", api_key=""))
    )
    assert selector.is_configured is False

    selector.sync_primary(ProviderConfig(provider="openrouter", model="m", api_key="test-key"))

    assert selector.is_configured is True
    assert selector.resolve() is not None
