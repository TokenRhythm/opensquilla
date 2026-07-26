"""Typed router-tier contract: TierConfig, misroute detection, override helper."""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.engine.selector_override import apply_model_override
from opensquilla.engine.steps.squilla_router import _flag_tier_provider_mismatch
from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import _cross_provider_tier_warnings, upsert_router
from opensquilla.router_tiers import TierConfig

# ---------------------------------------------------------------------------
# TierConfig
# ---------------------------------------------------------------------------


def test_tier_config_from_dict() -> None:
    tier = TierConfig.from_value(
        {
            "provider": " openrouter ",
            "model": "deepseek/deepseek-v4-flash",
            "thinking_level": "low",
            "supports_image": True,
        }
    )
    assert tier.provider == "openrouter"
    assert tier.model == "deepseek/deepseek-v4-flash"
    assert tier.thinking_level == "low"
    assert tier.supports_image is True
    assert tier.image_only is False


def test_tier_config_from_object_and_none() -> None:
    obj = SimpleNamespace(provider="openai", model="gpt-5.4-nano", image_only=True)
    tier = TierConfig.from_value(obj)
    assert tier.provider == "openai"
    assert tier.image_only is True
    assert TierConfig.from_value(None) == TierConfig()
    assert TierConfig.from_value({}) == TierConfig()


# ---------------------------------------------------------------------------
# Tier provider mismatch detection
# ---------------------------------------------------------------------------


def _ctx(active_provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={},
        config=SimpleNamespace(llm=SimpleNamespace(provider=active_provider)),
        session_key="s1",
    )


def test_mismatched_tier_provider_is_flagged() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=True)
    assert ctx.metadata["router_tier_provider_mismatch"] == "openai"


def test_matching_tier_provider_is_not_flagged() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openrouter", "model": "z-ai/glm-5.1"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=True)
    assert "router_tier_provider_mismatch" not in ctx.metadata


def test_observe_phase_does_not_flag() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=False)
    assert "router_tier_provider_mismatch" not in ctx.metadata


# ---------------------------------------------------------------------------
# Shared selector-override helper
# ---------------------------------------------------------------------------


class _StubSelector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def override_model(self, model: str) -> None:
        self.calls.append(("override_model", model))

    def override_model_with_fallback_chain(self, model: str, chain: list) -> None:
        self.calls.append(("override_with_chain", (model, chain)))

    def resolve(self) -> object:
        return "provider-sentinel"


def test_override_uses_fallback_chain_when_routing_applied() -> None:
    selector = _StubSelector()
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [{"tier": "c0", "model": "cheap"}],
        "routed_model": "routed",
    }
    provider = apply_model_override(
        selector, "routed", turn_metadata=metadata, realign_routed_model=False
    )
    assert provider == "provider-sentinel"
    assert selector.calls[0][0] == "override_with_chain"
    assert metadata["routed_model"] == "routed"


def test_override_without_routing_uses_plain_override() -> None:
    selector = _StubSelector()
    metadata = {"routing_applied": False, "routed_model": "would-be-routed"}
    apply_model_override(
        selector, "baseline", turn_metadata=metadata, realign_routed_model=False
    )
    assert selector.calls[0][0] == "override_model"
    # Observe phase: routed_model intentionally keeps the would-be choice.
    assert metadata["routed_model"] == "would-be-routed"


def test_explicit_override_realigns_routed_model_and_drops_savings() -> None:
    selector = _StubSelector()
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "routed_model": "routed",
        "savings_pct": 10.0,
        "savings_max_price_per_m": 3.0,
    }
    apply_model_override(
        selector, "explicit", turn_metadata=metadata, realign_routed_model=True
    )
    assert metadata["routed_model"] == "explicit"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0


# ---------------------------------------------------------------------------
# Onboarding warnings for cross-provider tiers
# ---------------------------------------------------------------------------


def test_cross_provider_tier_warning_text() -> None:
    warnings = _cross_provider_tier_warnings(
        {"c2": {"provider": "openai", "model": "gpt-5.5"}},
        "openrouter",
        tier_provider_mismatch="veto",
    )
    assert len(warnings) == 1
    assert "'c2'" in warnings[0]
    assert "'openai'" in warnings[0]
    assert "not enabled" in warnings[0]
    assert "choice is vetoed" in warnings[0]
    assert "current 'openrouter' deployment" in warnings[0]
    assert "model will be requested" not in warnings[0]


def test_cross_provider_tier_warning_preserves_legacy_route_semantics() -> None:
    warnings = _cross_provider_tier_warnings(
        {"c2": {"provider": "openai", "model": "gpt-5.5"}},
        "openrouter",
        tier_provider_mismatch="route",
    )
    assert len(warnings) == 1
    assert "model will be requested from 'openrouter'" in warnings[0]
    assert "choice is vetoed" not in warnings[0]


def test_cross_provider_warning_flips_to_credential_check_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    warnings = _cross_provider_tier_warnings(
        tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
    )
    assert len(warnings) == 1
    assert "no credentials resolve" in warnings[0]
    assert "llm_profiles.openai" in warnings[0]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert (
        _cross_provider_tier_warnings(
            tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
        )
        == []
    )


def test_cross_provider_warning_matches_runtime_for_operator_owned_endpoints(
    monkeypatch,
) -> None:
    """Save-time warnings mirror the runtime deployment resolver: an azure-style
    tier with the env key set but no profile base_url is vetoed at turn time,
    so the save must warn (previously the plain env lookup stayed silent)."""
    from opensquilla.gateway.config import LlmProviderProfile

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-env")
    tiers = {"c2": {"provider": "azure", "model": "gpt-tier"}}

    warnings = _cross_provider_tier_warnings(
        tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
    )
    assert len(warnings) == 1
    assert "no endpoint resolves" in warnings[0]
    assert "llm_profiles.azure" in warnings[0]

    # With the operator-supplied endpoint stored, the registry env key
    # resolves at runtime — and the save-time check must agree.
    assert (
        _cross_provider_tier_warnings(
            tiers,
            "openrouter",
            cross_provider_enabled=True,
            llm_profiles={
                "azure": LlmProviderProfile(
                    base_url="https://acct.azure-endpoint.example/v1"
                )
            },
        )
        == []
    )


def test_cross_provider_warning_accepts_case_variant_profile_keys(monkeypatch) -> None:
    from opensquilla.gateway.config import LlmProviderProfile

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    tiers = {"c2": {"provider": "deepseek", "model": "deepseek-tier"}}

    warnings = _cross_provider_tier_warnings(
        tiers,
        "openrouter",
        cross_provider_enabled=True,
        # Hand-authored configs predate key normalization; the runtime
        # resolver accepts the case variant, so the save-time check must too.
        llm_profiles={"DeepSeek": LlmProviderProfile(api_key="sk-profile")},
    )
    assert warnings == []


def test_upsert_router_surfaces_cross_provider_warning() -> None:
    cfg = GatewayConfig()  # defaults: openrouter provider + openrouter tiers
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={"c2": {"provider": "openai", "model": "gpt-5.5"}},
    )
    assert any("cross-provider" in w.lower() for w in res.warnings)


def test_upsert_router_no_warning_for_matching_tiers() -> None:
    cfg = GatewayConfig()
    res = upsert_router(cfg, mode="recommended")
    assert res.warnings == []


def test_upsert_router_redacts_secret_like_tier_fields() -> None:
    # Tiers are untyped dicts: a hand-written api_key must not be echoed
    # back through the router-configure RPC response.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={"c2": {"provider": "openrouter", "model": "z-ai/glm-5.1", "api_key": "sk-leak"}},
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["api_key"] == "***"
    assert echoed["model"] == "z-ai/glm-5.1"
    # The stored config keeps the real value; only the echo is redacted.
    assert res.config.squilla_router.tiers["c2"]["api_key"] == "sk-leak"


def test_upsert_router_redacts_camel_and_kebab_tier_secrets() -> None:
    # Only three known display aliases are canonicalized on write, so an
    # apiKey/accessToken passes into the stored tier verbatim — the echo
    # redaction must match secret-shaped keys in any spelling.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={
            "c2": {
                "provider": "openrouter",
                "model": "z-ai/glm-5.1",
                "apiKey": "sk-camel",
                "accessToken": "tok-camel",
                "api-key": "sk-kebab",
                "clientSecret": "sec-camel",
            }
        },
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["apiKey"] == "***"
    assert echoed["accessToken"] == "***"
    assert echoed["api-key"] == "***"
    assert echoed["clientSecret"] == "***"
    assert echoed["model"] == "z-ai/glm-5.1"
    assert echoed["provider"] == "openrouter"


def test_upsert_router_redacts_acronym_style_tier_secrets() -> None:
    # Acronym runs have no lowercase->uppercase boundary (APIKey, APIKEY):
    # the acronym rule and the separator-free fallback must still match.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={
            "c2": {
                "provider": "openrouter",
                "model": "z-ai/glm-5.1",
                "APIKey": "sk-acronym",
                "APIKEY": "sk-caps",
                "API_KEY": "sk-shout",
                "AccessTOKEN": "tok-caps",
                "key": "sk-bare",
                "KEY": "sk-bare-caps",
                # Ordinary words must NOT be redacted by the fallback.
                "monkey": "keep-me",
            }
        },
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["APIKey"] == "***"
    assert echoed["APIKEY"] == "***"
    assert echoed["API_KEY"] == "***"
    assert echoed["AccessTOKEN"] == "***"
    assert echoed["key"] == "***"
    assert echoed["KEY"] == "***"
    assert echoed["monkey"] == "keep-me"
