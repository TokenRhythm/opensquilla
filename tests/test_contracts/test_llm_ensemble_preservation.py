"""Contract freeze: unrelated config saves preserve the [llm_ensemble] subtree.

``[llm_ensemble]`` is the default routing surface, and an operator's explicit
customization of it (e.g. ``enabled = false`` or
``selection_mode = "router_dynamic"``) must survive every *other* config
write path byte-for-byte. Each case here runs a provider/router/patch save
against a config with a customized ``[llm_ensemble]`` section and asserts the
serialized ``[llm_ensemble]`` TOML subtree is identical before and after.

The ``upsert_llm_ensemble`` mutation itself is pinned to partial-payload
semantics: it seeds the merge from the current section and overrides only
keys explicitly present in the request. Omitted keys must never reset to
defaults (an enabled-only save must not clobber an explicit
``selection_mode``).

Conscious omission: ``onboarding.status`` does not gain an ``ensemble``
section in this pass — the status wire contract is frozen
(``tests/test_contracts/test_onboarding_status.py``), so extending its
``sections`` map is a separate, deliberate contract change.

Everything below runs against synthetic in-memory configs; no network, no
credentials (tests/conftest.py strips provider keys from the environment).
"""

from __future__ import annotations

import tomllib
from types import SimpleNamespace

import tomli_w

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc_config import _handle_config_patch
from opensquilla.onboarding.mutations import (
    upsert_llm_ensemble,
    upsert_llm_provider,
    upsert_router,
)

# An operator's explicit, non-default [llm_ensemble] customization.
CUSTOM_LLM_ENSEMBLE: dict[str, object] = {
    "enabled": False,
    "selection_mode": "router_dynamic",
    "model_options": ["custom/model-a", "custom/model-b"],
}


def _config_with_custom_ensemble(**overrides: object) -> GatewayConfig:
    return GatewayConfig(llm_ensemble=dict(CUSTOM_LLM_ENSEMBLE), **overrides)


def _ensemble_subtree_bytes(cfg: GatewayConfig) -> bytes:
    """Serialize just the [llm_ensemble] subtree of the TOML persist view."""
    return tomli_w.dumps({"llm_ensemble": cfg.to_toml_dict()["llm_ensemble"]}).encode()


def _assert_custom_values(cfg: GatewayConfig) -> None:
    assert cfg.llm_ensemble.enabled is False
    assert cfg.llm_ensemble.selection_mode == "router_dynamic"
    assert cfg.llm_ensemble.model_options == ["custom/model-a", "custom/model-b"]


# ---------------------------------------------------------------------------
# Provider saves
# ---------------------------------------------------------------------------


def test_openrouter_provider_save_preserves_llm_ensemble_subtree() -> None:
    cfg = _config_with_custom_ensemble()
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="z-ai/glm-5.2",
        api_key="sk-test",
    )

    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


def test_non_openrouter_provider_save_preserves_llm_ensemble_subtree() -> None:
    cfg = _config_with_custom_ensemble()
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-v4-flash",
        api_key="sk-test",
    )

    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


# ---------------------------------------------------------------------------
# Router saves
# ---------------------------------------------------------------------------


def test_router_recommended_save_preserves_llm_ensemble_subtree() -> None:
    cfg = _config_with_custom_ensemble(
        llm={"provider": "deepseek", "model": "deepseek-chat"}
    )
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_router(cfg, mode="recommended")

    assert res.config.squilla_router.tier_profile == "deepseek"
    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


def test_router_openrouter_mix_save_preserves_llm_ensemble_subtree() -> None:
    # openrouter-mix is only valid for the openrouter provider, which is no
    # longer the built-in default.
    cfg = _config_with_custom_ensemble(llm={"provider": "openrouter"})
    assert cfg.llm.provider == "openrouter"
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_router(cfg, mode="openrouter-mix")

    assert res.config.squilla_router.enabled is True
    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


def test_router_custom_save_preserves_llm_ensemble_subtree() -> None:
    # Mode "custom" writes squilla_router.* (plus the standard llm.model sync
    # every enabled mode performs) and must never touch [llm_ensemble].
    cfg = _config_with_custom_ensemble(
        llm={"provider": "deepseek", "model": "deepseek-chat"}
    )
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_router(cfg, mode="custom")

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


def test_router_disabled_save_preserves_llm_ensemble_subtree() -> None:
    cfg = _config_with_custom_ensemble()
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_router(cfg, mode="disabled")

    assert res.config.squilla_router.enabled is False
    assert _ensemble_subtree_bytes(res.config) == before
    _assert_custom_values(res.config)


# ---------------------------------------------------------------------------
# config.patch realigner path
# ---------------------------------------------------------------------------


async def test_config_patch_provider_realign_preserves_llm_ensemble_subtree(
    tmp_path,
) -> None:
    """A provider patch that fires the auto-router-profile realigner must not
    touch the [llm_ensemble] subtree — in memory or in the persisted file."""
    from opensquilla.onboarding.config_store import persist_config

    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openai", "api_key": "", "base_url": ""},
        llm_ensemble=dict(CUSTOM_LLM_ENSEMBLE),
    )
    ctx = SimpleNamespace(config=cfg)
    # Persist the customized config first: the sparse persister writes only
    # the operator's explicit [llm_ensemble] keys, and the on-disk subtree
    # is the contract an unrelated patch must leave byte-identical.
    persist_config(cfg, path=cfg.config_path)
    disk_before = tomllib.loads((tmp_path / "config.toml").read_text())["llm_ensemble"]
    disk_before_bytes = tomli_w.dumps({"llm_ensemble": disk_before}).encode()
    memory_before = _ensemble_subtree_bytes(cfg)

    await _handle_config_patch({"patch": {"llm": {"provider": "deepseek"}}}, ctx)

    # The realigner actually ran: the auto profile followed the provider.
    assert ctx.config.squilla_router.tier_profile == "deepseek"
    assert _ensemble_subtree_bytes(ctx.config) == memory_before
    _assert_custom_values(ctx.config)

    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["llm_ensemble"] == disk_before
    assert (
        tomli_w.dumps({"llm_ensemble": persisted["llm_ensemble"]}).encode()
        == disk_before_bytes
    )


# ---------------------------------------------------------------------------
# upsert_llm_ensemble partial-payload regressions
# ---------------------------------------------------------------------------


def test_enabled_only_upsert_keeps_selection_mode_and_model_options() -> None:
    cfg = _config_with_custom_ensemble()

    res = upsert_llm_ensemble(cfg, enabled=True)

    ensemble = res.config.llm_ensemble
    assert ensemble.enabled is True
    assert ensemble.selection_mode == "router_dynamic"
    assert ensemble.model_options == ["custom/model-a", "custom/model-b"]
    assert res.changed is True
    assert res.restart_required is False


def test_selection_mode_only_upsert_keeps_all_other_keys() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": False,
            "selection_mode": "router_dynamic",
            "model_options": ["custom/model-a"],
            "min_successful_proposers": 2,
            "target_successful_proposers": 3,
            "proposer_max_retries": 2,
            "all_failed_policy": "error",
        }
    )

    res = upsert_llm_ensemble(cfg, selection_mode="static_openrouter_b5")

    ensemble = res.config.llm_ensemble
    assert ensemble.selection_mode == "static_openrouter_b5"
    assert ensemble.enabled is False
    assert ensemble.model_options == ["custom/model-a"]
    assert ensemble.min_successful_proposers == 2
    assert ensemble.target_successful_proposers == 3
    assert ensemble.proposer_max_retries == 2
    assert ensemble.all_failed_policy == "error"
    assert res.warnings == []
    assert res.restart_required is False


def test_empty_upsert_is_a_no_op_and_reports_unchanged() -> None:
    cfg = _config_with_custom_ensemble()
    before = _ensemble_subtree_bytes(cfg)

    res = upsert_llm_ensemble(cfg)

    assert res.changed is False
    assert _ensemble_subtree_bytes(res.config) == before
