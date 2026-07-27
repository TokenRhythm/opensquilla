"""Credential-clear RPC contracts for primary and profile LLM providers."""

from __future__ import annotations

import tomllib

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401 - register handlers
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import (
    clear_llm_profile_credentials,
    clear_llm_provider_credentials,
)
from opensquilla.onboarding.status import get_onboarding_status


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="credential-clear-rpc",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_runtime_syncs(monkeypatch):
    for name in (
        "API_KEY",
        "API_KEY_ENV",
        "API_KEY_ENV_POOL",
        "OPENSQUILLA_LLM_API_KEY",
        "OPENSQUILLA_LLM_API_KEY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_image_generation",
        lambda config: None,
    )

    async def no_catalog_refresh(config):
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        no_catalog_refresh,
    )


def test_active_clear_forgets_runtime_secret_provenance() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-runtime-secret",
            "api_key_env": "OPENROUTER_API_KEY",
        }
    )
    cfg.mark_runtime_secret("llm.api_key")

    result = clear_llm_provider_credentials(cfg, provider_id="openrouter")

    assert result.changed is True
    assert result.config.llm.api_key == ""
    assert result.config.llm.api_key_env == ""
    assert "llm.api_key" not in result.config._runtime_secret_paths
    assert "llm.api_key" not in result.config._explicit_secret_paths
    assert cfg.llm.api_key == "synthetic-runtime-secret"


def test_profile_clear_forgets_all_case_variant_secret_provenance() -> None:
    cfg = GatewayConfig(
        llm_profiles={
            "openai": {"api_key": "synthetic-lower-secret"},
            "OpenAI": {
                "api_key_env": "OPENAI_CUSTOM_KEY",
                "api_key_env_pool": ["OPENAI_POOL_A"],
            },
        }
    )
    cfg.mark_runtime_secret("llm_profiles.openai.api_key")
    cfg.mark_runtime_secret("llm_profiles.OpenAI.api_key")

    result = clear_llm_profile_credentials(cfg, provider_id="OPENAI")

    assert result.changed is True
    for key in ("openai", "OpenAI"):
        profile = result.config.llm_profiles[key]
        assert profile.api_key == ""
        assert profile.api_key_env == ""
        assert profile.api_key_env_pool == []
        assert f"llm_profiles.{key}.api_key" not in (
            result.config._runtime_secret_paths
        )
        assert f"llm_profiles.{key}.api_key" not in (
            result.config._explicit_secret_paths
        )


@pytest.mark.asyncio
async def test_active_credential_clear_removes_stored_sources_and_preserves_setup(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Brackets are legal filename characters but have special meaning in glob
    # patterns. Backup discovery must treat the configured filename literally.
    config_path = tmp_path / "config[prod].toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-primary-secret",
            "api_key_env": "CUSTOM_OPENROUTER_KEY",
            "base_url": "https://openrouter.ai/api/v1",
            "proxy": "http://127.0.0.1:9876",
            "max_tokens": 4321,
            "provider_routing": {"openai/gpt-test": "openai"},
        },
        llm_profiles={"deepseek": {"api_key": "synthetic-profile-untouched"}},
        squilla_router={"enabled": True, "default_tier": "c2"},
        llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"},
    )
    persist_config(cfg, path=config_path, backup=False)
    managed_backup = config_path.with_name(
        "config[prod].toml.backup.synthetic-primary"
    )
    managed_backup.write_text(
        """
config_version = 1
search_api_key = "synthetic-historical-primary-secret"

[llm]
model = "openai/gpt-old"
api_key = "synthetic-historical-primary-secret"
api_key_env = "HISTORICAL_PRIMARY_KEY"

[llm_profiles.deepseek]
api_key = "synthetic-historical-primary-secret"

[llm_profiles.OpenRouter]
api_key = "synthetic-historical-primary-secret"
api_key_env = "HISTORICAL_OPENROUTER_PROFILE_KEY"
api_key_env_pool = ["HISTORICAL_OPENROUTER_PROFILE_POOL_A"]

[image_generation.providers.openrouter]
api_key = "synthetic-historical-primary-secret"
""".strip()
        + "\n"
    )
    other_provider_backup = config_path.with_name(
        "config[prod].toml.backup.synthetic-other-provider"
    )
    other_provider_backup.write_text(
        """
config_version = 1

[llm]
provider = "deepseek"
api_key = "synthetic-historical-primary-secret"
api_key_env = "HISTORICAL_DEEPSEEK_KEY"
""".strip()
        + "\n"
    )
    router_before = cfg.squilla_router.model_dump(mode="python")
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    response = await get_dispatcher().dispatch(
        "clear-primary",
        "onboarding.provider.credential.clear",
        {"providerId": "OpenRouter"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert response.payload["changed"] is True
    assert response.payload["restartRequired"] is False
    assert response.payload["entry"] == {
        "provider": "openrouter",
        "active": True,
        "storedCredentialsCleared": True,
        "credentialAvailable": False,
        "credentialSource": "missing_env",
        "credentialEnv": "OPENROUTER_API_KEY",
        "externalCredentialActive": False,
    }
    assert "synthetic-primary-secret" not in repr(response.payload)
    assert cfg.llm.provider == "openrouter"
    assert cfg.llm.model == "openai/gpt-test"
    assert cfg.llm.base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm.proxy == "http://127.0.0.1:9876"
    assert cfg.llm.max_tokens == 4321
    assert cfg.llm.provider_routing == {"openai/gpt-test": "openai"}
    assert cfg.llm.api_key == ""
    assert cfg.llm.api_key_env == ""
    assert cfg.llm_profiles["deepseek"].api_key == "synthetic-profile-untouched"
    assert cfg.squilla_router.model_dump(mode="python") == router_before
    assert cfg.llm_ensemble.model_dump(mode="python") == ensemble_before

    persisted = tomllib.loads(config_path.read_text())
    assert "api_key" not in persisted["llm"]
    assert "api_key_env" not in persisted["llm"]
    assert persisted["llm"]["model"] == "openai/gpt-test"
    assert persisted["llm_profiles"]["deepseek"]["api_key"] == (
        "synthetic-profile-untouched"
    )
    backups = sorted(
        path
        for path in config_path.parent.iterdir()
        if path.name.startswith(f"{config_path.name}.backup.")
    )
    assert backups == sorted([managed_backup, other_provider_backup])
    sanitized_backup = tomllib.loads(managed_backup.read_text())
    assert "api_key" not in sanitized_backup["llm"]
    assert "api_key_env" not in sanitized_backup["llm"]
    assert sanitized_backup["search_api_key"] == "synthetic-historical-primary-secret"
    assert sanitized_backup["llm_profiles"]["deepseek"]["api_key"] == (
        "synthetic-historical-primary-secret"
    )
    historical_profile = sanitized_backup["llm_profiles"]["OpenRouter"]
    assert "api_key" not in historical_profile
    assert "api_key_env" not in historical_profile
    assert "api_key_env_pool" not in historical_profile
    assert sanitized_backup["image_generation"]["providers"]["openrouter"][
        "api_key"
    ] == "synthetic-historical-primary-secret"
    untouched_backup = tomllib.loads(other_provider_backup.read_text())
    assert untouched_backup["llm"]["api_key"] == (
        "synthetic-historical-primary-secret"
    )
    assert untouched_backup["llm"]["api_key_env"] == "HISTORICAL_DEEPSEEK_KEY"


@pytest.mark.asyncio
async def test_active_credential_clear_survives_unparseable_managed_backup(
    tmp_path,
    monkeypatch,
) -> None:
    """A corrupt historical backup must not block a security-motivated clear.

    The unparseable file cannot serve as a restore source but may still hold
    the leaked secret in plain text, so the clear deletes it and proceeds to
    sanitize the readable backups and the live config.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-primary-secret",
        },
    )
    persist_config(cfg, path=config_path, backup=False)
    corrupt_backup = config_path.with_name("config.toml.backup.synthetic-corrupt")
    corrupt_backup.write_text('[llm\napi_key = "synthetic-primary-secret"\n')
    readable_backup = config_path.with_name("config.toml.backup.synthetic-readable")
    readable_backup.write_text(
        """
config_version = 1

[llm]
model = "openai/gpt-old"
api_key = "synthetic-historical-primary-secret"
""".strip()
        + "\n"
    )

    response = await get_dispatcher().dispatch(
        "clear-primary-corrupt-backup",
        "onboarding.provider.credential.clear",
        {"providerId": "openrouter"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert response.payload["changed"] is True
    assert cfg.llm.api_key == ""
    persisted = tomllib.loads(config_path.read_text())
    assert "api_key" not in persisted["llm"]
    assert not corrupt_backup.exists()
    sanitized_backup = tomllib.loads(readable_backup.read_text())
    assert "api_key" not in sanitized_backup["llm"]
    assert sanitized_backup["llm"]["model"] == "openai/gpt-old"


@pytest.mark.asyncio
async def test_active_credential_clear_reports_registry_environment_still_active(
    tmp_path,
    monkeypatch,
) -> None:
    external_secret = "synthetic-external-openrouter-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", external_secret)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-stored-secret",
            "api_key_env": "CUSTOM_OPENROUTER_KEY",
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    class RecordingSelector:
        def __init__(self) -> None:
            self.synced = []

        def sync_primary(self, provider_config) -> None:
            self.synced.append(provider_config)

    selector = RecordingSelector()
    ctx = _admin_ctx(cfg)
    ctx.provider_selector = selector
    response = await get_dispatcher().dispatch(
        "clear-primary-external-env",
        "onboarding.provider.credential.clear",
        {"providerId": "openrouter"},
        ctx,
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["credentialAvailable"] is True
    assert response.payload["entry"]["credentialSource"] == "env"
    assert response.payload["entry"]["credentialEnv"] == "OPENROUTER_API_KEY"
    assert response.payload["entry"]["externalCredentialActive"] is True
    assert external_secret not in repr(response.payload)
    assert selector.synced[-1].api_key == external_secret
    assert cfg.llm.api_key == ""
    assert cfg.llm.api_key_env == ""
    assert "llm.api_key" not in cfg._runtime_secret_paths
    persisted = tomllib.loads(config_path.read_text())
    assert "api_key" not in persisted["llm"]
    assert "api_key_env" not in persisted["llm"]


@pytest.mark.asyncio
async def test_active_credential_clear_rejects_non_active_provider(tmp_path) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-stored-secret",
        },
        llm_profiles={"deepseek": {"api_key": "synthetic-profile-secret"}},
    )

    response = await get_dispatcher().dispatch(
        "clear-wrong-primary",
        "onboarding.provider.credential.clear",
        {"providerId": "deepseek"},
        _admin_ctx(cfg),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.provider.invalid"
    assert cfg.llm.api_key == "synthetic-stored-secret"
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_profile_credential_clear_removes_all_sources_without_removing_profile(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-primary-untouched",
        },
        llm_profiles={
            "deepseek": {
                "model": "deepseek-chat",
                "api_key": "synthetic-profile-secret",
                "api_key_env": "CUSTOM_DEEPSEEK_KEY",
                "api_key_env_pool": ["DEEPSEEK_POOL_A", "DEEPSEEK_POOL_B"],
                "base_url": "https://api.deepseek.com/v1",
                "proxy": "http://127.0.0.1:7890",
            }
        },
        squilla_router={"preset_binding": "custom", "cross_provider_tiers": True},
        llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"},
    )
    cfg.squilla_router.tiers["c0"] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
    }
    persist_config(cfg, path=config_path, backup=False)
    managed_backup = config_path.with_name("config.toml.backup.synthetic-profile")
    managed_backup.write_text(
        """
config_version = 1
search_api_key = "synthetic-historical-profile-secret"

[llm]
provider = "openrouter"
api_key = "synthetic-historical-profile-secret"

[llm_profiles.DeepSeek]
model = "deepseek-chat"
api_key = "synthetic-historical-profile-secret"
api_key_env = "HISTORICAL_PROFILE_KEY"
api_key_env_pool = ["HISTORICAL_PROFILE_POOL_A"]

[llm_profiles.openai]
api_key = "synthetic-historical-profile-secret"

[image_generation.providers.deepseek]
api_key = "synthetic-historical-profile-secret"
""".strip()
        + "\n"
    )
    active_history_backup = config_path.with_name(
        "config.toml.backup.synthetic-profile-active-history"
    )
    active_history_backup.write_text(
        """
config_version = 1

[llm]
provider = "DeepSeek"
model = "deepseek-chat"
api_key = "synthetic-historical-profile-secret"
api_key_env = "HISTORICAL_ACTIVE_DEEPSEEK_KEY"

[llm_profiles.openai]
api_key = "synthetic-historical-profile-secret"
""".strip()
        + "\n"
    )
    router_before = cfg.squilla_router.model_dump(mode="python")
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")
    discarded: list[str] = []
    monkeypatch.setattr(
        "opensquilla.gateway.llm_runtime.discard_profile_credential_pool",
        lambda provider: discarded.append(provider),
    )

    response = await get_dispatcher().dispatch(
        "clear-profile",
        "onboarding.llmProfile.credential.clear",
        {"providerId": "DeepSeek"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert response.payload["entry"] == {
        "provider": "deepseek",
        "active": False,
        "storedCredentialsCleared": True,
        "credentialAvailable": False,
        "credentialSource": "none",
        "credentialEnv": "",
        "externalCredentialActive": False,
    }
    assert "synthetic-profile-secret" not in repr(response.payload)
    assert discarded == ["DeepSeek"]
    profile = cfg.llm_profiles["deepseek"]
    assert profile.model == "deepseek-chat"
    assert profile.api_key == ""
    assert profile.api_key_env == ""
    assert profile.api_key_env_pool == []
    assert profile.base_url == "https://api.deepseek.com/v1"
    assert profile.proxy == "http://127.0.0.1:7890"
    assert cfg.llm.api_key == "synthetic-primary-untouched"
    assert cfg.squilla_router.model_dump(mode="python") == router_before
    assert cfg.llm_ensemble.model_dump(mode="python") == ensemble_before

    persisted = tomllib.loads(config_path.read_text())
    stored_profile = persisted["llm_profiles"]["deepseek"]
    assert "api_key" not in stored_profile
    assert "api_key_env" not in stored_profile
    assert "api_key_env_pool" not in stored_profile
    assert stored_profile["model"] == "deepseek-chat"
    assert stored_profile["base_url"] == "https://api.deepseek.com/v1"
    backups = sorted(config_path.parent.glob("config.toml.backup.*"))
    assert backups == sorted([managed_backup, active_history_backup])
    sanitized_backup = tomllib.loads(managed_backup.read_text())
    backup_profile = sanitized_backup["llm_profiles"]["DeepSeek"]
    assert "api_key" not in backup_profile
    assert "api_key_env" not in backup_profile
    assert "api_key_env_pool" not in backup_profile
    historical_secret = "synthetic-historical-profile-secret"
    assert sanitized_backup["search_api_key"] == historical_secret
    assert sanitized_backup["llm"]["api_key"] == historical_secret
    assert sanitized_backup["llm_profiles"]["openai"]["api_key"] == historical_secret
    assert sanitized_backup["image_generation"]["providers"]["deepseek"][
        "api_key"
    ] == historical_secret
    sanitized_active_history = tomllib.loads(active_history_backup.read_text())
    assert "api_key" not in sanitized_active_history["llm"]
    assert "api_key_env" not in sanitized_active_history["llm"]
    assert sanitized_active_history["llm_profiles"]["openai"]["api_key"] == (
        historical_secret
    )


@pytest.mark.asyncio
async def test_profile_credential_clear_reports_registry_environment_still_active(
    tmp_path,
    monkeypatch,
) -> None:
    external_secret = "synthetic-external-deepseek-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", external_secret)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm_profiles={
            "deepseek": {
                "model": "deepseek-chat",
                "api_key": "synthetic-profile-secret",
                "api_key_env": "CUSTOM_DEEPSEEK_KEY",
                "api_key_env_pool": ["DEEPSEEK_POOL_A"],
            }
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await get_dispatcher().dispatch(
        "clear-profile-external-env",
        "onboarding.llmProfile.credential.clear",
        {"providerId": "deepseek"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["credentialAvailable"] is True
    assert response.payload["entry"]["credentialSource"] == "env"
    assert response.payload["entry"]["credentialEnv"] == "DEEPSEEK_API_KEY"
    assert response.payload["entry"]["externalCredentialActive"] is True
    assert external_secret not in repr(response.payload)


@pytest.mark.asyncio
async def test_profile_credential_clear_stays_clear_across_reload_with_ambient_settings(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "synthetic-unscoped-ambient-key")
    monkeypatch.setenv("API_KEY_ENV", "UNSCOPED_AMBIENT_KEY_ENV")
    monkeypatch.setenv("API_KEY_ENV_POOL", '["UNSCOPED_POOL_A"]')
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm_profiles={
            "deepseek": {
                "model": "deepseek-chat",
                "api_key": "synthetic-profile-secret",
                "api_key_env": "SAVED_PROFILE_KEY_ENV",
                "api_key_env_pool": ["SAVED_POOL_A"],
            }
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await get_dispatcher().dispatch(
        "clear-profile-before-reload",
        "onboarding.llmProfile.credential.clear",
        {"providerId": "deepseek"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    reloaded = load_config(config_path)
    profile = reloaded.llm_profiles["deepseek"]
    assert profile.api_key == ""
    assert profile.api_key_env == ""
    assert profile.api_key_env_pool == []


@pytest.mark.asyncio
async def test_active_clear_keeps_generic_settings_key_effective_and_reported(
    tmp_path,
    monkeypatch,
) -> None:
    external_secret = "synthetic-settings-primary-secret"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY_ENV", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", external_secret)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-stored-secret",
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    class RecordingSelector:
        def __init__(self) -> None:
            self.synced = []

        def sync_primary(self, provider_config) -> None:
            self.synced.append(provider_config)

    selector = RecordingSelector()
    ctx = _admin_ctx(cfg)
    ctx.provider_selector = selector
    response = await get_dispatcher().dispatch(
        "clear-primary-settings-key",
        "onboarding.provider.credential.clear",
        {"providerId": "openrouter"},
        ctx,
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["externalCredentialActive"] is True
    assert response.payload["entry"]["credentialSource"] == "env"
    assert response.payload["entry"]["credentialEnv"] == "OPENSQUILLA_LLM_API_KEY"
    assert external_secret not in repr(response.payload)
    assert selector.synced[-1].api_key == external_secret
    assert cfg.llm.api_key == ""
    status = get_onboarding_status(cfg)
    assert status.llm_credential_status["source"] == "env"
    assert status.llm_credential_status["envKey"] == "OPENSQUILLA_LLM_API_KEY"

    reloaded = load_config(config_path)
    runtime = resolve_llm_runtime_config(reloaded)
    assert runtime.api_key == external_secret
    assert runtime.api_key_from_env is True
    assert runtime.api_key_env_name == "OPENSQUILLA_LLM_API_KEY"


@pytest.mark.asyncio
async def test_active_clear_keeps_settings_env_reference_effective_and_reported(
    tmp_path,
    monkeypatch,
) -> None:
    external_secret = "synthetic-settings-env-reference-secret"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY_ENV", "CUSTOM_PRIMARY_KEY")
    monkeypatch.setenv("CUSTOM_PRIMARY_KEY", external_secret)
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-stored-secret",
            "api_key_env": "SAVED_PRIMARY_KEY_ENV",
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    class RecordingSelector:
        def __init__(self) -> None:
            self.synced = []

        def sync_primary(self, provider_config) -> None:
            self.synced.append(provider_config)

    selector = RecordingSelector()
    ctx = _admin_ctx(cfg)
    ctx.provider_selector = selector
    response = await get_dispatcher().dispatch(
        "clear-primary-settings-env-reference",
        "onboarding.provider.credential.clear",
        {"providerId": "openrouter"},
        ctx,
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["externalCredentialActive"] is True
    assert response.payload["entry"]["credentialSource"] == "env"
    assert response.payload["entry"]["credentialEnv"] == "CUSTOM_PRIMARY_KEY"
    assert external_secret not in repr(response.payload)
    assert selector.synced[-1].api_key == external_secret
    assert cfg.llm.api_key == ""
    assert cfg.llm.api_key_env == ""
    status = get_onboarding_status(cfg)
    assert status.llm_credential_status["source"] == "env"
    assert status.llm_credential_status["envKey"] == "CUSTOM_PRIMARY_KEY"

    reloaded = load_config(config_path)
    runtime = resolve_llm_runtime_config(reloaded)
    assert runtime.api_key == external_secret
    assert runtime.api_key_from_env is True
    assert runtime.api_key_env_name == "CUSTOM_PRIMARY_KEY"


@pytest.mark.asyncio
async def test_active_credential_clear_persist_failure_leaves_runtime_untouched(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-stored-secret",
        },
    )
    ctx = _admin_ctx(cfg)
    sync_attempts: list[str] = []
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._persist",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_provider_selector",
        lambda *args: sync_attempts.append("selector"),
    )

    response = await get_dispatcher().dispatch(
        "clear-primary-write-failure",
        "onboarding.provider.credential.clear",
        {"providerId": "openrouter"},
        ctx,
    )

    assert response.error is not None
    assert cfg.llm.api_key == "synthetic-stored-secret"
    assert sync_attempts == []


@pytest.mark.asyncio
async def test_profile_credential_clear_persist_failure_keeps_config_and_pool(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.gateway.llm_runtime import reset_profile_credential_pools

    pool_secret = "synthetic-pooled-secret"
    monkeypatch.setenv("DEEPSEEK_POOL_A", pool_secret)
    pool_manager = reset_profile_credential_pools()
    acquired = pool_manager.acquire_for_session(
        "deepseek", ["DEEPSEEK_POOL_A"], "existing-session"
    )
    assert acquired is not None
    monkeypatch.delenv("DEEPSEEK_POOL_A")
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={
            "deepseek": {
                "api_key": "synthetic-profile-secret",
                "api_key_env_pool": ["DEEPSEEK_POOL_A"],
            }
        },
    )
    ctx = _admin_ctx(cfg)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._persist",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )
    try:
        response = await get_dispatcher().dispatch(
            "clear-profile-write-failure",
            "onboarding.llmProfile.credential.clear",
            {"providerId": "deepseek"},
            ctx,
        )

        assert response.error is not None
        assert cfg.llm_profiles["deepseek"].api_key == "synthetic-profile-secret"
        assert cfg.llm_profiles["deepseek"].api_key_env_pool == ["DEEPSEEK_POOL_A"]
        # The environment was removed after the pool was built. A cached
        # credential remains visible only if the failed transaction did not
        # discard or rebuild the process-local pool.
        cached = pool_manager.peek_available("deepseek", ["DEEPSEEK_POOL_A"])
        assert cached is not None
        assert cached.api_key == pool_secret
    finally:
        reset_profile_credential_pools()


def test_credential_clear_methods_require_admin_scope() -> None:
    assert METHOD_SCOPES["onboarding.provider.credential.clear"] == ADMIN_SCOPE
    assert METHOD_SCOPES["onboarding.llmProfile.credential.clear"] == ADMIN_SCOPE
