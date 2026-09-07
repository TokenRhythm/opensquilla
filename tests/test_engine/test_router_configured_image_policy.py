"""Router image policy: only the configured c0-c3 ladder may execute."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.squilla_router import (
    _tier_deployment_vision_support,
    apply_squilla_router,
    finalize_squilla_router_capacity,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog


def _catalog_evidence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supported: tuple[str, ...] = (),
    unsupported: tuple[str, ...] = (),
) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": model,
                "architecture": {"input_modalities": modalities},
            }
            for models, modalities in (
                (supported, ["text", "image"]),
                (unsupported, ["text"]),
            )
            for model in models
        ]
    )
    monkeypatch.setattr("opensquilla.engine.steps.squilla_router.shared_catalog", lambda: catalog)
    return catalog


def _context(
    tiers: dict[str, dict[str, object]],
    *,
    message: str = "Describe the image.",
) -> TurnContext:
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.tiers = tiers
    return TurnContext(
        message=message,
        session_key="router-configured-image-policy",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )


@pytest.mark.asyncio
async def test_image_model_is_not_an_implicit_router_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(monkeypatch, supported=("configured/vision",))
    ctx = _context(
        {
            "c1": {"model": "configured/vision", "supports_image": True},
            "image_model": {
                "model": "unconfigured/dedicated-vision",
                "supports_image": True,
                "image_only": True,
            },
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "configured/vision"
    assert routed.metadata["image_input_mode"] == "native"
    assert routed.metadata["routed_model_vision_support"] == "supported"
    assert all(entry["tier"] != "image_model" for entry in routed.metadata["router_fallback_chain"])


@pytest.mark.asyncio
async def test_all_catalog_text_only_c_tiers_use_direct_marker_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(monkeypatch, unsupported=tuple(f"configured/c{index}" for index in range(4)))
    ctx = _context(
        {
            "c0": {"model": "configured/c0", "supports_image": False},
            "c1": {"model": "configured/c1", "supports_image": False},
            "c2": {"model": "configured/c2", "supports_image": False},
            "c3": {"model": "configured/c3", "supports_image": False},
            "image_model": {
                "model": "unconfigured/dedicated-vision",
                "supports_image": True,
                "image_only": True,
            },
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "configured/c1"
    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["image_input_mode"] == "marker"
    assert routed.metadata["image_input_projection_required"] is True
    assert routed.metadata["router_image_capability_exhausted"] is True
    assert "image_input_forced_rejection_reason" not in routed.metadata
    assert routed.metadata["router_fallback_chain"] == []
    assert routed.metadata["router_fallback_strict"] is True
    assert routed.metadata["routed_model_vision_support"] == "unsupported"


@pytest.mark.asyncio
async def test_omitted_support_is_probeable_but_image_model_is_still_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Catalog:
        def resolve_deployment_vision_support(self, _model: str, **_: object) -> str:
            return "unknown"

    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.shared_catalog",
        lambda: _Catalog(),
    )
    ctx = _context(
        {
            "c0": {"model": "configured/probeable"},
            "image_model": {
                "model": "unconfigured/dedicated-vision",
                "supports_image": True,
                "image_only": True,
            },
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["image_input_mode"] == "native"
    assert routed.metadata["router_image_tier_support"] == {"c0": "unknown"}
    assert routed.metadata["routed_model_vision_support"] == "unknown"


@pytest.mark.asyncio
async def test_image_fallback_chain_carries_each_configured_tier_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(
        monkeypatch,
        supported=("configured/c0", "configured/c1"),
        unsupported=("configured/c2",),
    )
    ctx = _context(
        {
            "c0": {"model": "configured/c0", "supports_image": True},
            "c1": {"model": "configured/c1", "supports_image": True},
            "c2": {"model": "configured/c2", "supports_image": False},
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["router_fallback_chain"] == [
        {
            "tier": "c1",
            "model": "configured/c1",
            "vision_support": "supported",
        }
    ]


@pytest.mark.asyncio
async def test_ensemble_c3_is_text_only_and_lower_configured_vision_tier_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(monkeypatch, supported=("configured/vision", "configured/ensemble-draft"))
    ctx = _context(
        {
            "c0": {"model": "configured/vision", "supports_image": True},
            "c3": {
                "model": "configured/ensemble-draft",
                "supports_image": True,
                "ensemble_enabled": True,
            },
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["image_input_mode"] == "native"


@pytest.mark.asyncio
async def test_structural_edit_image_route_and_fallbacks_obey_c3_execution_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(monkeypatch, supported=tuple(f"configured/c{index}" for index in range(4)))
    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.model_has_request_capacity",
        lambda **_: True,
    )
    ctx = _context(
        {
            "c0": {"model": "configured/c0", "supports_image": True},
            "c1": {"model": "configured/c1", "supports_image": True},
            "c2": {"model": "configured/c2", "supports_image": True},
            "c3": {"model": "configured/c3", "supports_image": True},
        }
    )
    ctx.metadata.update(
        {
            "artifact_format": "html",
            "artifact_operation_class": "structural_edit",
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c3"
    assert routed.model == "configured/c3"
    assert routed.metadata["image_input_mode"] == "native"
    assert routed.metadata["router_fallback_chain"] == []

    finalized = await finalize_squilla_router_capacity(routed)

    assert finalized.metadata["routed_tier"] == "c3"
    assert finalized.model == "configured/c3"
    assert finalized.metadata["router_fallback_chain"] == []


@pytest.mark.asyncio
async def test_image_shortcut_reselects_active_provider_when_mismatch_is_vetoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_evidence(monkeypatch, supported=("foreign/vision", "configured/vision"))
    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.model_has_request_capacity",
        lambda **_: True,
    )
    ctx = _context(
        {
            "c0": {
                "provider": "foreign",
                "model": "foreign/vision",
                "supports_image": True,
            },
            "c1": {
                "provider": "openrouter",
                "model": "configured/vision",
                "supports_image": True,
            },
        }
    )
    ctx.config.squilla_router.tier_provider_mismatch = "veto"
    ctx.config.llm_ensemble.enabled = True

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "configured/vision"
    assert routed.metadata["provider_mismatch_veto_applied"] is True
    assert routed.metadata["provider_mismatch_veto_from_tier"] == "c0"
    assert routed.metadata["provider_mismatch_veto_to_tier"] == "c1"
    assert routed.metadata["router_tier_provider_role"] == "direct"
    assert "router_tier_provider_mismatch" not in routed.metadata

    finalized = await finalize_squilla_router_capacity(routed)

    assert finalized.metadata["routed_tier"] == "c1"
    assert finalized.metadata["router_tier_provider_role"] == "direct"


@pytest.mark.parametrize("legacy_flag", [True, False, None])
@pytest.mark.parametrize("support", ["supported", "unsupported", "unknown"])
async def test_deployment_evidence_ignores_legacy_tier_switch(
    monkeypatch: pytest.MonkeyPatch,
    legacy_flag: bool | None,
    support: str,
) -> None:
    _catalog_evidence(
        monkeypatch,
        supported=("configured/c1",) if support == "supported" else (),
        unsupported=("configured/c1",) if support == "unsupported" else (),
    )
    raw: dict[str, object] = {"model": "configured/c1"}
    if legacy_flag is not None:
        raw["supports_image"] = legacy_flag
    ctx = _context({"c1": raw})

    routed = await apply_squilla_router(ctx)

    assert routed.model == "configured/c1"
    assert routed.metadata["router_image_tier_support"] == {"c1": support}
    assert routed.metadata["routed_model_vision_support"] == support
    assert routed.metadata["image_input_mode"] == (
        "marker" if support == "unsupported" else "native"
    )
    assert ctx.config.squilla_router.tiers["c1"] == raw


@pytest.mark.parametrize("cross_provider", [False, True])
def test_tier_capability_lookup_uses_physical_deployment_authority(
    monkeypatch: pytest.MonkeyPatch, cross_provider: bool
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Catalog:
        def resolve_deployment_vision_support(self, model: str, **kwargs: object) -> str:
            calls.append((model, kwargs))
            return "unknown"

    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.shared_catalog", lambda: _Catalog()
    )
    raw = {"model": "configured/c1", "provider": "other-provider", "supports_image": True}
    ctx = _context({"c1": raw})
    ctx.config.squilla_router.cross_provider_tiers = cross_provider
    ctx.config.llm.api_key = "synthetic-key"
    ctx.config.llm.base_url = "https://synthetic.invalid/api"

    assert _tier_deployment_vision_support(ctx, raw) == "unknown"
    assert calls == [
        (
            "configured/c1",
            {
                "provider": "other-provider" if cross_provider else "openrouter",
                "api_key": "" if cross_provider else "synthetic-key",
                "base_url": "" if cross_provider else "https://synthetic.invalid/api",
                "proxy": "",
            },
        )
    ]


def test_image_history_capacity_includes_configured_marker_fallback_providers() -> None:
    ctx = _context(
        {
            "c0": {"provider": "anthropic", "model": "configured/unknown", "supports_image": False},
            "c1": {"provider": "ollama", "model": "configured/text", "supports_image": False},
            "c2": {"provider": "empty-provider", "model": ""},
            "c3": {"provider": "hidden-provider", "model": "hidden", "image_only": True},
            "image_model": {
                "provider": "legacy-provider",
                "model": "legacy",
                "supports_image": True,
            },
        }
    )
    ctx.config.squilla_router.cross_provider_tiers = True
    ctx.metadata["image_route_reason"] = "current_turn"

    assert TurnRunner._route_capacity_provider_kinds(
        ctx, initial_provider_config=SimpleNamespace(provider="openrouter")
    ) == frozenset({"openrouter", "anthropic", "ollama"})
