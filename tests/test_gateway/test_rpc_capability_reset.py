"""Regression contracts for the simplified capability reset RPC."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401 - register handlers
import opensquilla.onboarding.config_store as config_store
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import (
    AudioConfig,
    GatewayConfig,
    ImageGenerationConfig,
    MemoryEmbeddingConfig,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import reset_capability


def _ctx(config: GatewayConfig, *, admin: bool = True) -> RpcContext:
    scope = "operator.admin" if admin else "operator.read"
    return RpcContext(
        conn_id="capability-reset",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({scope}),
            is_owner=admin,
            authenticated=True,
        ),
    )


async def _reset(config: GatewayConfig, capability_id: str):
    return await get_dispatcher().dispatch(
        f"reset-{capability_id}",
        "onboarding.capability.reset",
        {"capabilityId": capability_id},
        _ctx(config),
    )


@pytest.fixture(autouse=True)
def _isolate_capability_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capability reset tests never use real credentials or live runtimes."""

    for name in (
        "BRAVE_SEARCH_API_KEY",
        "CUSTOM_SEARCH_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "QWEN_TOKEN_PLAN_API_KEY",
        "CUSTOM_IMAGE_KEY",
        "ELEVENLABS_API_KEY",
        "CUSTOM_AUDIO_KEY",
        "CUSTOM_MEMORY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_search_provider",
        lambda config: None,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_image_generation",
        lambda config: None,
    )


def _persisted(config_path: Path) -> dict:
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def _transaction_temp_files(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.iterdir()
        if path.name.endswith((".replacement.tmp", ".restore.tmp", ".rollback.tmp"))
    ]


@pytest.mark.asyncio
async def test_search_reset_restores_duckduckgo_and_clears_only_search_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        search_provider="brave",
        search_api_key="synthetic-search-secret",
        search_api_key_env="CUSTOM_SEARCH_KEY",
        search_max_results=17,
        search_proxy="http://127.0.0.1:7890",
        search_use_env_proxy=True,
        search_fallback_policy="network",
        search_diagnostics=True,
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-shared-llm-secret",
        },
        audio={
            "enabled": True,
            "providers": {
                "elevenlabs": {"api_key": "synthetic-audio-untouched"}
            },
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await _reset(cfg, "search")

    assert response.error is None, response.error
    assert response.payload == {
        "changed": True,
        "restartRequired": False,
        "configPath": str(config_path),
        "entry": {"capabilityId": "search", "reset": True},
        "warnings": [],
    }
    assert cfg.search_provider == "duckduckgo"
    assert cfg.search_api_key == ""
    assert cfg.search_api_key_env == ""
    assert cfg.search_max_results == GatewayConfig.model_fields[
        "search_max_results"
    ].default
    assert cfg.search_proxy == ""
    assert cfg.search_use_env_proxy is False
    assert cfg.search_fallback_policy == "off"
    assert cfg.search_diagnostics is False

    stored = _persisted(config_path)
    assert stored["search_provider"] == "duckduckgo"
    assert {key for key in stored if key.startswith("search_")} == {
        "search_provider"
    }
    assert stored["llm"]["api_key"] == "synthetic-shared-llm-secret"
    assert stored["audio"]["providers"]["elevenlabs"]["api_key"] == (
        "synthetic-audio-untouched"
    )


@pytest.mark.asyncio
async def test_image_reset_disables_and_removes_image_only_configuration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "synthetic-shared-openai-secret",
        },
        image_generation={
            "enabled": True,
            "primary": "openrouter/test-image-model",
            "fallbacks": ["openai/gpt-image-1"],
            "size": "1536x1024",
            "output_format": "webp",
            "providers": {
                "openai": {
                    "base_url": "https://image.example.test/v1",
                    "api_key": "synthetic-openai-image-secret",
                    "api_key_env": "CUSTOM_IMAGE_KEY",
                },
                "openrouter": {
                    "base_url": "https://router.example.test/v1",
                    "api_key": "synthetic-router-image-secret",
                    "api_key_env": "CUSTOM_IMAGE_KEY",
                },
            },
        },
        audio={
            "enabled": True,
            "providers": {
                "elevenlabs": {"api_key": "synthetic-audio-untouched"}
            },
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await _reset(cfg, "image_generation")

    assert response.error is None, response.error
    assert response.payload["changed"] is True
    assert response.payload["restartRequired"] is False
    assert response.payload["entry"] == {
        "capabilityId": "image_generation",
        "reset": True,
    }
    assert cfg.image_generation == ImageGenerationConfig(enabled=False)

    stored = _persisted(config_path)
    assert stored["image_generation"] == {"enabled": False}
    assert stored["llm"]["api_key"] == "synthetic-shared-openai-secret"
    assert stored["audio"]["providers"]["elevenlabs"]["api_key"] == (
        "synthetic-audio-untouched"
    )


@pytest.mark.asyncio
async def test_audio_reset_disables_and_removes_audio_only_configuration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        search_provider="brave",
        search_api_key="synthetic-search-untouched",
        audio={
            "enabled": True,
            "tts": {
                "model": "synthetic-tts-model",
                "voice": "synthetic-voice",
                "language_code": "zh",
                "speed": 1.2,
            },
            "providers": {
                "elevenlabs": {
                    "base_url": "https://audio.example.test",
                    "api_key": "synthetic-audio-secret",
                    "api_key_env": "CUSTOM_AUDIO_KEY",
                    "speech_to_text_model": "synthetic-stt-model",
                }
            },
        },
        image_generation={
            "enabled": True,
            "providers": {
                "openai": {"api_key": "synthetic-image-untouched"}
            },
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await _reset(cfg, "audio")

    assert response.error is None, response.error
    assert response.payload["changed"] is True
    assert response.payload["restartRequired"] is False
    assert response.payload["entry"] == {
        "capabilityId": "audio",
        "reset": True,
    }
    assert cfg.audio == AudioConfig(enabled=False)

    stored = _persisted(config_path)
    assert stored["audio"] == {"enabled": False}
    assert stored["search_api_key"] == "synthetic-search-untouched"
    assert stored["image_generation"]["providers"]["openai"]["api_key"] == (
        "synthetic-image-untouched"
    )


@pytest.mark.asyncio
async def test_audio_can_be_configured_again_through_the_shared_apply_path(
    tmp_path: Path,
) -> None:
    """Reset must not fork or disable the RPC/agent shared audio write path."""

    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        audio={
            "enabled": True,
            "providers": {"elevenlabs": {"api_key": "synthetic-old-audio-key"}},
        },
    )
    persist_config(cfg, path=config_path, backup=False)
    reset = await _reset(cfg, "audio")
    assert reset.error is None, reset.error

    configured = await get_dispatcher().dispatch(
        "configure-audio-after-reset",
        "onboarding.audio.configure",
        {
            "providerId": "elevenlabs",
            "apiKey": "synthetic-new-audio-key",
        },
        _ctx(cfg),
    )

    assert configured.error is None, configured.error
    assert configured.payload["entry"]["enabled"] is True
    assert cfg.audio.enabled is True
    assert cfg.audio.providers.elevenlabs.api_key == "synthetic-new-audio-key"
    stored = _persisted(config_path)
    assert stored["audio"]["enabled"] is True
    assert (
        stored["audio"]["providers"]["elevenlabs"]["api_key"]
        == "synthetic-new-audio-key"
    )


@pytest.mark.asyncio
async def test_memory_embedding_reset_preserves_privacy_and_other_memory_settings(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        memory={
            "auto_capture_enabled": False,
            "inject_limit": 1234,
            "embedding": {
                "provider": "openai-compatible",
                "model": "synthetic-legacy-model",
                "remote": {
                    "api_key": "synthetic-memory-secret",
                    "api_key_env": "CUSTOM_MEMORY_KEY",
                    "base_url": "https://memory.example.test/v1",
                    "model": "synthetic-embedding-model",
                    "dimensions": 768,
                },
            },
        },
    )
    persist_config(cfg, path=config_path, backup=False)

    response = await _reset(cfg, "memory_embedding")

    assert response.error is None, response.error
    assert response.payload["changed"] is True
    assert response.payload["restartRequired"] is True
    assert cfg.memory.embedding == MemoryEmbeddingConfig(provider="auto")
    assert cfg.memory.auto_capture_enabled is False
    assert cfg.memory.inject_limit == 1234

    stored = _persisted(config_path)
    assert stored["memory"]["embedding"] == {"provider": "auto"}
    assert stored["memory"]["auto_capture_enabled"] is False
    assert stored["memory"]["inject_limit"] == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability_id",
    ["search", "image_generation", "audio", "memory_embedding"],
)
async def test_capability_reset_is_idempotent(
    tmp_path: Path,
    capability_id: str,
) -> None:
    config_path = tmp_path / f"{capability_id}.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    persist_config(cfg, path=config_path, backup=False)

    first = await _reset(cfg, capability_id)
    second = await _reset(cfg, capability_id)

    assert first.error is None, first.error
    assert second.error is None, second.error
    assert first.payload["changed"] is False
    assert second.payload["changed"] is False


@pytest.mark.asyncio
async def test_onboarding_status_advertises_dynamic_reset_support(
    tmp_path: Path,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        search_provider="brave",
        image_generation={"enabled": True},
        memory={"embedding": {"provider": "none"}},
    )

    response = await get_dispatcher().dispatch(
        "capability-status",
        "onboarding.status",
        {},
        _ctx(cfg, admin=False),
    )

    assert response.error is None, response.error
    assert response.payload["capabilityConfiguration"] == {
        "search": {"resettable": True},
        "image_generation": {"resettable": True},
        "audio": {"resettable": False},
        "memory_embedding": {"resettable": True},
    }


_FULL_CONFIG = f"""\
config_version = {GatewayConfig.model_fields["config_version"].default}
search_provider = "brave"
search_api_key = "synthetic-search-secret"
search_api_key_env = "CUSTOM_SEARCH_KEY"
search_max_results = 17
search_proxy = "http://127.0.0.1:7890"
search_use_env_proxy = true
search_fallback_policy = "network"
search_diagnostics = true

[llm]
provider = "openrouter"
model = "openai/gpt-test"
api_key = "synthetic-shared-llm-secret"

[image_generation]
enabled = true
primary = "openrouter/test-image-model"
fallbacks = ["openai/gpt-image-1"]
size = "1536x1024"
output_format = "webp"

[image_generation.providers.openai]
base_url = "https://image.example.test/v1"
api_key = "synthetic-openai-image-secret"
api_key_env = "CUSTOM_IMAGE_KEY"

[image_generation.providers.openrouter]
base_url = "https://router.example.test/v1"
api_key = "synthetic-router-image-secret"
api_key_env = "CUSTOM_IMAGE_KEY"

[image_generation.providers.qwen_token_plan]
base_url = "https://qwen.example.test/v1"
api_key = "synthetic-qwen-image-secret"
api_key_env = "CUSTOM_IMAGE_KEY"

[audio]
enabled = true

[audio.tts]
model = "synthetic-tts-model"
voice = "synthetic-voice"

[audio.providers.elevenlabs]
base_url = "https://audio.example.test"
api_key = "synthetic-audio-secret"
api_key_env = "CUSTOM_AUDIO_KEY"

[memory]
auto_capture_enabled = false
inject_limit = 1234

[memory.embedding]
provider = "openai-compatible"

[memory.embedding.remote]
api_key = "synthetic-memory-secret"
api_key_env = "CUSTOM_MEMORY_KEY"
base_url = "https://memory.example.test/v1"
model = "synthetic-embedding-model"
"""


def _set_capability_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_SEARCH_API_KEY",
        "synthetic-env-search-key",
    )
    monkeypatch.setenv("OPENSQUILLA_IMAGE_GENERATION_ENABLED", "true")
    monkeypatch.setenv(
        "OPENSQUILLA_IMAGE_GENERATION_PROVIDERS__QWEN_TOKEN_PLAN__API_KEY",
        "synthetic-env-qwen-key",
    )
    monkeypatch.setenv("OPENSQUILLA_AUDIO_ENABLED", "true")
    monkeypatch.setenv(
        "OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY",
        "synthetic-env-audio-key",
    )
    monkeypatch.setenv("OPENSQUILLA_MEMORY_EMBEDDING__PROVIDER", "ollama")


@pytest.mark.asyncio
async def test_environment_only_capabilities_are_not_advertised_as_resettable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_capability_environment(monkeypatch)
    cfg = load_config(tmp_path / "missing.toml")

    # Prove the runtime model did absorb external configuration; resettable
    # must still describe only OpenSquilla-managed TOML.
    assert cfg.search_provider == "brave"
    assert cfg.image_generation.enabled is True
    assert cfg.audio.enabled is True
    assert cfg.memory.embedding.requested_provider == "ollama"

    response = await get_dispatcher().dispatch(
        "environment-only-capability-status",
        "onboarding.status",
        {},
        _ctx(cfg, admin=False),
    )

    assert response.error is None, response.error
    assert response.payload["capabilityConfiguration"] == {
        "search": {"resettable": False},
        "image_generation": {"resettable": False},
        "audio": {"resettable": False},
        "memory_embedding": {"resettable": False},
    }


@pytest.mark.asyncio
async def test_reset_reload_does_not_claim_external_configuration_is_removable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_capability_environment(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    cfg = load_config(config_path)

    initial_status = await get_dispatcher().dispatch(
        "managed-capability-status-before-reset",
        "onboarding.status",
        {},
        _ctx(cfg, admin=False),
    )
    assert initial_status.error is None, initial_status.error
    assert initial_status.payload["capabilityConfiguration"] == {
        "search": {"resettable": True},
        "image_generation": {"resettable": True},
        "audio": {"resettable": True},
        "memory_embedding": {"resettable": True},
    }

    for capability_id in (
        "search",
        "image_generation",
        "audio",
        "memory_embedding",
    ):
        response = await _reset(cfg, capability_id)
        assert response.error is None, response.error

    reloaded = load_config(config_path)
    assert os.environ["OPENSQUILLA_GATEWAY_SEARCH_API_KEY"] == (
        "synthetic-env-search-key"
    )
    assert (
        os.environ[
            "OPENSQUILLA_IMAGE_GENERATION_PROVIDERS__QWEN_TOKEN_PLAN__API_KEY"
        ]
        == "synthetic-env-qwen-key"
    )
    assert os.environ["OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY"] == (
        "synthetic-env-audio-key"
    )

    status = await get_dispatcher().dispatch(
        "reset-reloaded-capability-status",
        "onboarding.status",
        {},
        _ctx(reloaded, admin=False),
    )

    assert status.error is None, status.error
    assert status.payload["capabilityConfiguration"] == {
        "search": {"resettable": False},
        "image_generation": {"resettable": False},
        "audio": {"resettable": False},
        "memory_embedding": {"resettable": False},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_id", "removed_paths"),
    [
        ("search", (("search_api_key",), ("search_api_key_env",), ("search_proxy",))),
        (
            "image_generation",
            (
                ("image_generation", "providers", "openai", "api_key"),
                ("image_generation", "providers", "openrouter", "api_key"),
                ("image_generation", "providers", "qwen_token_plan", "api_key"),
            ),
        ),
        ("audio", (("audio", "providers", "elevenlabs", "api_key"),)),
        ("memory_embedding", (("memory", "embedding", "remote", "api_key"),)),
    ],
)
async def test_reset_scrubs_current_and_managed_backup_but_preserves_other_secrets(
    tmp_path: Path,
    capability_id: str,
    removed_paths: tuple[tuple[str, ...], ...],
) -> None:
    config_path = tmp_path / "config[prod].toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    managed_backup = config_path.with_name(
        "config[prod].toml.backup.synthetic-capabilities"
    )
    managed_backup.write_text(_FULL_CONFIG, encoding="utf-8")
    cfg = load_config(config_path)

    response = await _reset(cfg, capability_id)

    assert response.error is None, response.error
    current = _persisted(config_path)
    historical = _persisted(managed_backup)

    def get_path(payload: dict, path: tuple[str, ...]):
        current_value: object = payload
        for part in path:
            if not isinstance(current_value, dict) or part not in current_value:
                return None
            current_value = current_value[part]
        return current_value

    for removed_path in removed_paths:
        assert get_path(current, removed_path) is None
        assert get_path(historical, removed_path) is None

    assert current["llm"]["api_key"] == "synthetic-shared-llm-secret"
    assert historical["llm"]["api_key"] == "synthetic-shared-llm-secret"
    if capability_id != "search":
        assert current["search_api_key"] == "synthetic-search-secret"
        assert historical["search_api_key"] == "synthetic-search-secret"
    if capability_id != "image_generation":
        assert current["image_generation"]["providers"]["openai"]["api_key"] == (
            "synthetic-openai-image-secret"
        )
        assert historical["image_generation"]["providers"]["openai"]["api_key"] == (
            "synthetic-openai-image-secret"
        )
        assert current["image_generation"]["providers"]["qwen_token_plan"]["api_key"] == (
            "synthetic-qwen-image-secret"
        )
        assert historical["image_generation"]["providers"]["qwen_token_plan"][
            "api_key"
        ] == "synthetic-qwen-image-secret"
    if capability_id != "audio":
        assert current["audio"]["providers"]["elevenlabs"]["api_key"] == (
            "synthetic-audio-secret"
        )
        assert historical["audio"]["providers"]["elevenlabs"]["api_key"] == (
            "synthetic-audio-secret"
        )
    if capability_id != "memory_embedding":
        assert current["memory"]["embedding"]["remote"]["api_key"] == (
            "synthetic-memory-secret"
        )
        assert historical["memory"]["embedding"]["remote"]["api_key"] == (
            "synthetic-memory-secret"
        )


@pytest.mark.asyncio
async def test_reset_deletes_unparseable_managed_backup_before_committing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    corrupt_backup = config_path.with_name("config.toml.backup.synthetic-corrupt")
    corrupt_backup.write_text(
        '[image_generation.providers.openai\n'
        'api_key = "synthetic-image-secret"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)

    response = await _reset(cfg, "image_generation")

    assert response.error is None, response.error
    assert not corrupt_backup.exists()
    assert _persisted(config_path)["image_generation"] == {"enabled": False}
    assert _transaction_temp_files(tmp_path) == []


def test_second_backup_replace_failure_rolls_back_every_managed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    first_backup = tmp_path / "config.toml.backup.01"
    second_backup = tmp_path / "config.toml.backup.02"
    first_backup.write_text(_FULL_CONFIG, encoding="utf-8")
    second_backup.write_text(_FULL_CONFIG, encoding="utf-8")
    cfg = load_config(config_path)
    mutation = reset_capability(cfg, capability_id="image_generation")
    before = {
        path: path.read_bytes()
        for path in (config_path, first_backup, second_backup)
    }
    real_replace = os.replace
    first_backup_replace_calls = 0

    def fail_second_backup(source, destination):
        nonlocal first_backup_replace_calls
        if Path(destination).name == first_backup.name:
            first_backup_replace_calls += 1
            if first_backup_replace_calls == 2:
                raise OSError("synthetic transient rollback replace failure")
        if Path(destination).name == second_backup.name:
            raise OSError("synthetic second backup replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(config_store.os, "replace", fail_second_backup)

    with pytest.raises(OSError, match="second backup"):
        persist_config(
            mutation.config,
            path=config_path,
            remove_paths=mutation.remove_paths,
        )

    for path, original in before.items():
        assert path.read_bytes() == original
    assert first_backup_replace_calls == 3
    assert "image_generation.enabled" in mutation.config.force_persist_paths()
    assert _transaction_temp_files(tmp_path) == []


def test_current_replace_failure_restores_rewritten_and_corrupt_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    corrupt_backup = tmp_path / "config.toml.backup.00-corrupt"
    corrupt_backup.write_bytes(
        b'[audio.providers.elevenlabs\napi_key = "synthetic-corrupt-secret"\n'
    )
    valid_backup = tmp_path / "config.toml.backup.01-valid"
    valid_backup.write_text(_FULL_CONFIG, encoding="utf-8")
    cfg = load_config(config_path)
    mutation = reset_capability(cfg, capability_id="audio")
    before = {
        path: path.read_bytes()
        for path in (config_path, corrupt_backup, valid_backup)
    }
    real_replace = os.replace

    def fail_current_replace(source, destination):
        if Path(destination).name == config_path.name:
            raise OSError("synthetic current config replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(config_store.os, "replace", fail_current_replace)

    with pytest.raises(OSError, match="current config"):
        persist_config(
            mutation.config,
            path=config_path,
            remove_paths=mutation.remove_paths,
        )

    for path, original in before.items():
        assert path.exists()
        assert path.read_bytes() == original
    assert "audio.enabled" in mutation.config.force_persist_paths()
    assert _transaction_temp_files(tmp_path) == []


def test_transient_temp_cleanup_failure_is_retried_without_secret_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FULL_CONFIG, encoding="utf-8")
    managed_backup = tmp_path / "config.toml.backup.01"
    managed_backup.write_text(_FULL_CONFIG, encoding="utf-8")
    cfg = load_config(config_path)
    mutation = reset_capability(cfg, capability_id="image_generation")
    before = {
        path: path.read_bytes()
        for path in (config_path, managed_backup)
    }
    real_replace = os.replace
    real_unlink = os.unlink
    cleanup_failures = 0

    def fail_current_replace(source, destination):
        if Path(destination).name == config_path.name:
            raise OSError("synthetic current config replace failure")
        return real_replace(source, destination)

    def fail_first_live_temp_unlink(path, *args, **kwargs):
        nonlocal cleanup_failures
        candidate = Path(path)
        if (
            cleanup_failures == 0
            and candidate.name.endswith(".replacement.tmp")
            and candidate.exists()
        ):
            cleanup_failures += 1
            raise OSError("synthetic transient temp cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(config_store.os, "replace", fail_current_replace)
    monkeypatch.setattr(config_store.os, "unlink", fail_first_live_temp_unlink)

    with pytest.raises(OSError, match="current config"):
        persist_config(
            mutation.config,
            path=config_path,
            remove_paths=mutation.remove_paths,
        )

    assert cleanup_failures == 1
    for path, original in before.items():
        assert path.read_bytes() == original
    assert _transaction_temp_files(tmp_path) == []
    assert "synthetic-openai-image-secret" not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.iterdir()
        if path not in before
    )


@pytest.mark.asyncio
async def test_reset_persistence_failure_keeps_runtime_and_disk_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        search_provider="brave",
        search_api_key="synthetic-search-secret",
        search_api_key_env="CUSTOM_SEARCH_KEY",
    )
    persist_config(cfg, path=config_path, backup=False)
    disk_before = config_path.read_bytes()

    def fail_persist(*args, **kwargs):
        raise OSError("synthetic capability reset write failure")

    monkeypatch.setattr("opensquilla.gateway.rpc_onboarding._persist", fail_persist)

    response = await _reset(cfg, "search")

    assert response.error is not None
    assert cfg.search_provider == "brave"
    assert cfg.search_api_key == "synthetic-search-secret"
    assert cfg.search_api_key_env == "CUSTOM_SEARCH_KEY"
    assert config_path.read_bytes() == disk_before


@pytest.mark.asyncio
async def test_live_sync_failure_reports_restart_without_undoing_saved_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        search_provider="brave",
        search_api_key="synthetic-search-secret",
    )
    persist_config(cfg, path=config_path, backup=False)

    def fail_live_sync(config):
        raise RuntimeError("synthetic live sync failure")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_search_provider",
        fail_live_sync,
    )

    response = await _reset(cfg, "search")

    assert response.error is None, response.error
    assert response.payload["restartRequired"] is True
    assert response.payload["warnings"] == [
        "Capability reset was saved, but the live runtime could not be "
        "updated. Restart the gateway to apply it."
    ]
    assert cfg.search_provider == "duckduckgo"
    assert _persisted(config_path)["search_provider"] == "duckduckgo"


@pytest.mark.asyncio
async def test_capability_reset_rejects_unknown_and_non_admin_calls(
    tmp_path: Path,
) -> None:
    assert METHOD_SCOPES["onboarding.capability.reset"] == ADMIN_SCOPE
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        search_provider="brave",
        search_api_key="synthetic-search-secret",
    )

    unknown = await _reset(cfg, "not-a-capability")
    unauthorized = await get_dispatcher().dispatch(
        "reset-with-read-scope",
        "onboarding.capability.reset",
        {"capabilityId": "search"},
        _ctx(cfg, admin=False),
    )

    assert unknown.error is not None
    assert unauthorized.error is not None
    assert "Insufficient scope" in unauthorized.error.message
    assert cfg.search_provider == "brave"
    assert not config_path.exists()


@pytest.mark.parametrize(
    ("raw", "assert_unchanged"),
    [
        (
            "[image_generation]\nenabled = false\n",
            lambda cfg: cfg.image_generation.enabled is False,
        ),
        ("[audio]\nenabled = false\n", lambda cfg: cfg.audio.enabled is False),
        ('search_provider = ""\n', lambda cfg: cfg.search_provider == ""),
        (
            '[memory.embedding]\nprovider = "none"\n',
            lambda cfg: cfg.memory.embedding.requested_provider == "none",
        ),
    ],
)
def test_loading_legacy_capability_state_does_not_migrate_or_enable_it(
    tmp_path: Path,
    raw: str,
    assert_unchanged,
) -> None:
    config_path = tmp_path / "legacy.toml"
    config_path.write_text(raw, encoding="utf-8")

    cfg = load_config(config_path)

    assert assert_unchanged(cfg)
    assert config_path.read_text(encoding="utf-8") == raw
