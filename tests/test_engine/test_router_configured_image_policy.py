"""Router image policy: only the configured c0-c3 ladder may execute."""

from __future__ import annotations

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.squilla_router import (
    apply_squilla_router,
    finalize_squilla_router_capacity,
)
from opensquilla.gateway.config import GatewayConfig


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
async def test_image_model_is_not_an_implicit_router_deployment() -> None:
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
    assert all(
        entry["tier"] != "image_model"
        for entry in routed.metadata["router_fallback_chain"]
    )


@pytest.mark.asyncio
async def test_all_explicitly_text_only_c_tiers_use_direct_marker_metadata() -> None:
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
        def resolve_deployment_vision_support(self, **_: object) -> str:
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
async def test_image_fallback_chain_carries_each_configured_tier_support() -> None:
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
async def test_ensemble_c3_is_text_only_and_lower_configured_vision_tier_wins() -> None:
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
