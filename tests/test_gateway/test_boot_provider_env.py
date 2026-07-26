from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_patch_safe,
    _handle_config_set,
    _sync_provider_selector,
)
from opensquilla.onboarding.config_store import load_config


class _CapturingSelector:
    def __init__(self) -> None:
        self.synced = None

    def sync_primary(self, cfg) -> None:
        self.synced = cfg


def test_boot_resolves_direct_provider_env_key_and_base_url(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("VOLCENGINE_API_KEY", "volc-key")
    monkeypatch.setenv("VOLCENGINE_BASE_URL", "https://ark.example/api/v3")

    cfg = GatewayConfig(llm={"provider": "volcengine", "api_key": "", "base_url": ""})

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.api_key == "volc-key"
    assert runtime.base_url == "https://ark.example/api/v3"
    assert runtime.api_key_from_env is True
    assert runtime.base_url_from_env is True
    assert cfg.llm.api_key == "volc-key"
    assert cfg.llm.base_url == "https://ark.example/api/v3"


def test_boot_uses_explicit_key_and_base_url_before_standard_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("VOLCENGINE_API_KEY", "volc-key")
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "api_key": "config-key",
            "base_url": "https://config.example/api/v1",
        }
    )

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.api_key == "config-key"
    assert runtime.api_key_from_env is False
    # An operator-chosen endpoint beats the derived env var, mirroring the
    # explicit-key rule above (#484).
    assert runtime.base_url == "https://config.example/api/v1"
    assert runtime.base_url_from_env is False


def test_existing_custom_provider_keeps_implicit_env_compatibility(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "custom-env-key")
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY_ENV", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "custom-model",
            "base_url": "https://existing.example.test/v1",
        }
    )

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.api_key == "custom-env-key"
    assert runtime.api_key_from_env is True


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("custom", "custom-model", "https://custom.example.test/v1"),
        ("openrouter", "openai/gpt-test", "https://openrouter.ai/api/v1"),
    ],
)
def test_configured_missing_env_does_not_fall_back_to_generic_key(
    monkeypatch,
    provider: str,
    model: str,
    base_url: str,
) -> None:
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-old-origin-key")
    cfg = GatewayConfig(
        llm={
            "provider": provider,
            "model": model,
            "api_key": "",
            "api_key_env": "NEW_ENDPOINT_KEY",
            "base_url": base_url,
        }
    )

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.api_key == ""
    assert runtime.api_key_from_env is False
    assert runtime.api_key_env_name == "NEW_ENDPOINT_KEY"


def test_generic_key_remains_fallback_without_configured_env(monkeypatch) -> None:
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY_ENV", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "custom-model",
            "api_key": "",
            "api_key_env": "",
            "base_url": "https://custom.example.test/v1",
        }
    )

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.api_key == "synthetic-generic-key"
    assert runtime.api_key_from_env is True
    assert runtime.api_key_env_name == "OPENSQUILLA_LLM_API_KEY"


def test_configured_env_does_not_reuse_runtime_cache_after_variable_removed(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NEW_ENDPOINT_KEY", "synthetic-new-origin-key")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-old-origin-key")
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "custom-model",
            "api_key": "",
            "api_key_env": "NEW_ENDPOINT_KEY",
            "base_url": "https://custom.example.test/v1",
        }
    )

    first = resolve_llm_runtime_config(cfg)
    monkeypatch.delenv("NEW_ENDPOINT_KEY")
    second = resolve_llm_runtime_config(cfg)

    assert first.api_key == "synthetic-new-origin-key"
    assert second.api_key == ""
    assert second.api_key_env_name == "NEW_ENDPOINT_KEY"


def test_loaded_config_missing_env_rejects_absorbed_generic_key(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-old-origin-key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "custom"
model = "custom-model"
api_key_env = "NEW_ENDPOINT_KEY"
base_url = "https://custom.example.test/v1"
""".strip()
        + "\n"
    )

    cfg = load_config(config_path)
    absorbed_key = cfg.llm.api_key
    runtime = resolve_llm_runtime_config(cfg)

    assert absorbed_key == "synthetic-old-origin-key"
    assert "llm.api_key" in cfg._runtime_secret_paths
    assert cfg.llm.api_key == ""
    assert runtime.api_key == ""
    assert runtime.api_key_env_name == "NEW_ENDPOINT_KEY"


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("custom", "custom-model", "https://custom.example.test/v1"),
        ("openrouter", "openai/gpt-test", "https://openrouter.ai/api/v1"),
    ],
)
def test_load_from_toml_marks_absorbed_generic_key_before_resolution(
    tmp_path,
    monkeypatch,
    provider: str,
    model: str,
    base_url: str,
) -> None:
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[llm]",
                f'provider = "{provider}"',
                f'model = "{model}"',
                'api_key_env = "NEW_ENDPOINT_KEY"',
                f'base_url = "{base_url}"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load_from_toml(config_path)
    absorbed_key = cfg.llm.api_key
    runtime = resolve_llm_runtime_config(cfg)

    assert absorbed_key == "synthetic-generic-key"
    assert "llm.api_key" in cfg._runtime_secret_paths
    assert runtime.api_key == ""
    assert runtime.api_key_env_name == "NEW_ENDPOINT_KEY"


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("custom", "custom-model", "https://custom.example.test/v1"),
        ("openrouter", "openai/gpt-test", "https://openrouter.ai/api/v1"),
    ],
)
async def test_config_apply_marks_absorbed_generic_key_before_persist_and_resolution(
    tmp_path,
    monkeypatch,
    provider: str,
    model: str,
    base_url: str,
) -> None:
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    selector = _CapturingSelector()
    ctx = SimpleNamespace(config=cfg, provider_selector=selector)

    await _handle_config_apply(
        {
            "config": {
                "llm": {
                    "provider": provider,
                    "model": model,
                    "api_key_env": "NEW_ENDPOINT_KEY",
                    "base_url": base_url,
                }
            }
        },
        ctx,
    )

    runtime = resolve_llm_runtime_config(ctx.config)
    persisted = config_path.read_text(encoding="utf-8")
    assert "llm.api_key" in ctx.config._runtime_secret_paths
    assert runtime.api_key == ""
    assert runtime.api_key_env_name == "NEW_ENDPOINT_KEY"
    assert "synthetic-generic-key" not in persisted


@pytest.mark.parametrize("mutation", ["set", "patch"])
async def test_config_section_replacement_marks_absorbed_generic_key(
    tmp_path,
    monkeypatch,
    mutation: str,
) -> None:
    monkeypatch.delenv("OLD_ENDPOINT_KEY", raising=False)
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "openai/gpt-old"',
                'api_key = ""',
                'api_key_env = "OLD_ENDPOINT_KEY"',
                'base_url = "https://openrouter.ai/api/v1"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    selector = _CapturingSelector()
    ctx = SimpleNamespace(config=cfg, provider_selector=selector)
    replacement = {
        "provider": "openrouter",
        "model": "openai/gpt-new",
        "api_key_env": "NEW_ENDPOINT_KEY",
        "base_url": "https://openrouter.ai/api/v1",
    }

    if mutation == "set":
        await _handle_config_set({"path": "llm", "value": replacement}, ctx)
    else:
        await _handle_config_patch({"patches": {"llm": replacement}}, ctx)

    runtime = resolve_llm_runtime_config(ctx.config)
    persisted = config_path.read_text(encoding="utf-8")
    assert "llm.api_key" in ctx.config._runtime_secret_paths
    assert selector.synced.api_key == ""
    assert runtime.api_key == ""
    assert runtime.api_key_env_name == "NEW_ENDPOINT_KEY"
    assert "synthetic-generic-key" not in persisted


def test_openrouter_runtime_uses_default_provider_routing() -> None:
    cfg = GatewayConfig(llm={"provider": "openrouter"})

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.provider_routing["deepseek/deepseek-v4-flash"] == "deepseek"
    assert runtime.provider_routing["z-ai/glm-5.1"] == "z-ai"
    assert runtime.provider_routing["z-ai/glm-5.2"] == "z-ai"
    assert runtime.provider_routing["anthropic/claude-opus-4.8"] == "anthropic"
    assert runtime.provider_routing["moonshotai/kimi-k2.6"] == "moonshotai"
    assert runtime.provider_routing["openai/gpt-5.5"] == "openai"


def test_openrouter_runtime_provider_routing_overrides_default() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "provider_routing": {
                "z-ai/glm-5.2": "z-ai/special",
                "custom/model": "custom-provider",
            },
        }
    )

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.provider_routing["deepseek/deepseek-v4-flash"] == "deepseek"
    assert runtime.provider_routing["z-ai/glm-5.2"] == "z-ai/special"
    assert runtime.provider_routing["anthropic/claude-opus-4.8"] == "anthropic"
    assert runtime.provider_routing["moonshotai/kimi-k2.6"] == "moonshotai"
    assert runtime.provider_routing["custom/model"] == "custom-provider"


def test_direct_provider_runtime_does_not_inherit_openrouter_provider_routing() -> None:
    cfg = GatewayConfig(llm={"provider": "deepseek", "api_key": "", "base_url": ""})

    runtime = resolve_llm_runtime_config(cfg)

    assert runtime.provider_routing == {}


def test_squilla_router_visual_mode_defaults_to_real_candidates() -> None:
    cfg = GatewayConfig()

    assert cfg.squilla_router.visual_mode == "real_candidates"
    assert cfg.to_public_dict()["squilla_router"]["visual_mode"] == "real_candidates"


def test_squilla_router_visual_mode_accepts_legacy_grid_and_model_space_alias() -> None:
    legacy_cfg = GatewayConfig(squilla_router={"visual_mode": "legacy_grid"})
    alias_cfg = GatewayConfig(squilla_router={"visual_mode": "model_space"})
    dashed_alias_cfg = GatewayConfig(squilla_router={"visual_mode": "model-space"})

    assert legacy_cfg.squilla_router.visual_mode == "legacy_grid"
    assert alias_cfg.squilla_router.visual_mode == "legacy_grid"
    assert dashed_alias_cfg.squilla_router.visual_mode == "legacy_grid"


def test_squilla_router_visual_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="visual_mode must be one of"):
        GatewayConfig(squilla_router={"visual_mode": "local_storage"})


def test_runtime_config_sync_resolves_selected_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example")
    cfg = GatewayConfig(llm={"provider": "deepseek", "api_key": "", "base_url": ""})
    selector = _CapturingSelector()
    ctx = type("Ctx", (), {"provider_selector": selector})()

    _sync_provider_selector(ctx, cfg)

    assert selector.synced.provider == "deepseek"
    assert selector.synced.api_key == "deepseek-key"
    assert selector.synced.base_url == "https://deepseek.example"


async def test_config_patch_runtime_env_key_is_not_persisted(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example")
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openai", "api_key": "", "base_url": ""},
    )
    selector = _CapturingSelector()
    ctx = SimpleNamespace(config=cfg, provider_selector=selector)

    await _handle_config_patch({"patch": {"llm": {"provider": "deepseek"}}}, ctx)

    assert ctx.config.squilla_router.tier_profile == "deepseek"
    assert ctx.config.llm.api_key == "deepseek-key"
    assert selector.synced.api_key == "deepseek-key"
    assert "api_key" not in ctx.config.to_toml_dict()["llm"]
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["squilla_router"]["tier_profile"] == "deepseek"
    assert "api_key" not in persisted["llm"]


async def test_config_patch_safe_accepts_router_visual_mode(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_patch_safe(
        {"patches": {"squilla_router.visual_mode": "legacy_grid"}},
        ctx,
    )

    assert res["patched"] == ["squilla_router.visual_mode"]
    assert res["restartRequired"] is False
    assert ctx.config.squilla_router.visual_mode == "legacy_grid"
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["squilla_router"]["visual_mode"] == "legacy_grid"


async def test_config_patch_safe_accepts_session_title_toggle(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_patch_safe(
        {"patches": {"naming.enabled": False}},
        ctx,
    )

    assert res["patched"] == ["naming.enabled"]
    assert res["restartRequired"] is False
    assert ctx.config.naming.enabled is False
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["naming"]["enabled"] is False


async def test_config_patch_safe_accepts_privacy_network_observability_toggle(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_patch_safe(
        {"patches": {"privacy.disable_network_observability": True}},
        ctx,
    )

    assert res["patched"] == ["privacy.disable_network_observability"]
    assert res["restartRequired"] is False
    assert ctx.config.privacy.disable_network_observability is True
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["privacy"]["disable_network_observability"] is True


async def test_config_patch_safe_accepts_llm_ensemble_toggle(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_patch_safe(
        {"patches": {"llm_ensemble.enabled": True}},
        ctx,
    )

    assert res["patched"] == ["llm_ensemble.enabled"]
    assert res["restartRequired"] is False
    assert ctx.config.llm_ensemble.enabled is True
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["llm_ensemble"]["enabled"] is True


async def test_config_patch_safe_rejects_session_title_advanced_paths(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    with pytest.raises(ValueError, match="naming.model"):
        await _handle_config_patch_safe(
            {"patches": {"naming.model": "deepseek/deepseek-v4-pro"}},
            ctx,
        )
