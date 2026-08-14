"""Provider-selector ordering for mutating config RPCs."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
import tomli_w

import opensquilla.gateway.rpc_config as rpc_config
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_reload,
    _handle_config_set,
)
from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog
from opensquilla.provider.tokenrhythm_catalog import (
    parse_tokenrhythm_declared,
    parse_tokenrhythm_published,
)


class _RecordingSelector:
    def __init__(self, *, fail: bool = False) -> None:
        self.configs: list[Any] = []
        self.fail = fail

    def sync_primary(self, config: Any) -> None:
        self.configs.append(config)
        if self.fail:
            raise RuntimeError("selector sync failed")


def _write_config(path) -> None:
    path.write_text(
        'config_version = 1\n\n'
        '[llm]\nprovider = "openai"\nmodel = "gpt-old"\n'
    )


async def _mutate_model(kind: str, ctx: RpcContext, model: str) -> dict[str, Any]:
    if kind == "set":
        return await _handle_config_set({"path": "llm.model", "value": model}, ctx)
    if kind == "patch":
        return await _handle_config_patch({"patches": {"llm.model": model}}, ctx)
    payload = ctx.config.model_dump(mode="python")
    payload["llm"]["model"] = model
    return await _handle_config_apply({"config": payload}, ctx)


async def _save_tokenrhythm_key(kind: str, ctx: RpcContext) -> None:
    if kind == "set":
        await _handle_config_set(
            {"path": "llm.api_key", "value": "dummy-tokenrhythm-key"}, ctx
        )
        return
    if kind == "patch":
        await _handle_config_patch(
            {"patches": {"llm.api_key": "dummy-tokenrhythm-key"}}, ctx
        )
        return
    payload = ctx.config.model_dump(mode="python")
    payload["llm"]["api_key"] = "dummy-tokenrhythm-key"
    await _handle_config_apply({"config": payload}, ctx)


async def _replace_tokenrhythm_profile_key(
    kind: str,
    ctx: RpcContext,
    value: str,
) -> None:
    path = "llm_profiles.tokenrhythm.api_key"
    if kind == "set":
        await _handle_config_set({"path": path, "value": value}, ctx)
        return
    if kind == "patch":
        await _handle_config_patch({"patches": {path: value}}, ctx)
        return
    payload = ctx.config.model_dump(mode="python")
    payload["llm_profiles"]["tokenrhythm"]["api_key"] = value
    await _handle_config_apply({"config": payload}, ctx)


@pytest.mark.parametrize("kind", ["set", "patch", "apply"])
async def test_config_hot_apply_refreshes_when_live_catalog_key_becomes_available(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    fetches: list[str] = []

    async def fake_public(**kwargs: Any) -> dict:
        fetches.append("published")
        return parse_tokenrhythm_published(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "type": "chat",
                        "status": "online",
                        "contextWindow": 1_000_000,
                        "maxOutputTokens": 131_072,
                    }
                ]
            }
        )

    async def fake_declared(*args: Any, **kwargs: Any) -> dict:
        fetches.append("declared")
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "max_completion_tokens": 131_072,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fake_public,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fake_declared,
    )
    config = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        state_dir=str(tmp_path / "state"),
        llm={"provider": "tokenrhythm", "model": "qwen3.7-max"},
    )
    catalog = ModelCatalog()
    set_shared_catalog(catalog)
    ctx = RpcContext(conn_id="test", config=config)

    try:
        await _save_tokenrhythm_key(kind, ctx)

        assert fetches == ["published", "declared"]
        assert catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm").source == "live"
        assert catalog.resolve_max_tokens("qwen3.7-max", provider="tokenrhythm") == 131_072

        # Model and unrelated UI settings do not affect a public catalog fetch.
        await _handle_config_set({"path": "llm.model", "value": "glm-5"}, ctx)
        await _handle_config_set({"path": "naming.enabled", "value": False}, ctx)
        assert fetches == ["published", "declared"]
    finally:
        from opensquilla.gateway.model_catalog_refresh import (
            install_tokenrhythm_catalog_coordinator,
        )

        install_tokenrhythm_catalog_coordinator(None)
        set_shared_catalog(None)


@pytest.mark.parametrize("kind", ["set", "patch", "apply"])
async def test_generic_config_profile_credential_change_reconciles_after_persist(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(path),
        llm={"provider": "openai", "model": "gpt-synthetic"},
        llm_profiles={
            "tokenrhythm": {
                "api_key": "synthetic-old-profile-key",
                "base_url": "https://tokenrhythm.studio/v1",
            }
        },
    )
    events: list[str] = []
    observed: list[tuple[str, str]] = []
    real_persist = rpc_config._persist_config

    def recording_persist(candidate: Any) -> None:
        real_persist(candidate)
        events.append("persist")

    async def recording_reconcile(previous: Any, current: Any, **kwargs: Any) -> None:
        del kwargs
        events.append("reconcile")
        observed.append(
            (
                previous.llm_profiles["tokenrhythm"].api_key,
                current.llm_profiles["tokenrhythm"].api_key,
            )
        )

    monkeypatch.setattr(rpc_config, "_persist_config", recording_persist)
    monkeypatch.setattr(
        "opensquilla.gateway.llm_runtime.discard_profile_credential_pool",
        lambda provider: events.append(f"discard:{provider}"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.reconcile_tokenrhythm_profile_transition",
        recording_reconcile,
    )

    ctx = RpcContext(conn_id="test", config=config)
    await _replace_tokenrhythm_profile_key(kind, ctx, "synthetic-new-profile-key")

    assert events == ["persist", "discard:tokenrhythm", "reconcile"]
    assert observed == [
        ("synthetic-old-profile-key", "synthetic-new-profile-key")
    ]
    assert GatewayConfig.load(path).llm_profiles["tokenrhythm"].api_key == (
        "synthetic-new-profile-key"
    )


async def test_config_reload_is_explicit_live_catalog_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider = "tokenrhythm"\nmodel = "qwen3.7-max"\n'
        'api_key = "dummy-tokenrhythm-key"\n',
        encoding="utf-8",
    )
    config = GatewayConfig.load(path)
    config.state_dir = str(tmp_path / "state")
    catalog = ModelCatalog()
    set_shared_catalog(catalog)
    fetches: list[str] = []

    async def fake_public(**kwargs: Any) -> dict:
        fetches.append("published")
        return parse_tokenrhythm_published(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "type": "chat",
                        "status": "online",
                        "contextWindow": 1_000_000,
                        "maxOutputTokens": 131_072,
                    }
                ]
            }
        )

    async def fake_declared(*args: Any, **kwargs: Any) -> dict:
        fetches.append("declared")
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "max_completion_tokens": 131_072,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fake_public,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fake_declared,
    )

    try:
        result = await _handle_config_reload(None, RpcContext(conn_id="test", config=config))

        assert result["ok"] is True
        assert fetches == ["published", "declared"]
        assert catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm").source == "live"
    finally:
        from opensquilla.gateway.model_catalog_refresh import (
            install_tokenrhythm_catalog_coordinator,
        )

        install_tokenrhythm_catalog_coordinator(None)
        set_shared_catalog(None)


@pytest.mark.parametrize("kind", ["set", "patch", "apply"])
async def test_config_mutation_refreshes_from_authoritative_live_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(path)
    seen: list[Any] = []

    async def fake_refresh(previous: Any, refreshed_config: Any) -> None:
        del previous
        seen.append(refreshed_config)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_config._refresh_live_catalog_after_change",
        fake_refresh,
    )
    ctx = RpcContext(conn_id="test", config=config)

    await _mutate_model(kind, ctx, "gpt-new")

    assert seen == [ctx.config]


async def test_config_reload_refreshes_from_authoritative_live_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(path)
    captured: list[Any] = []

    async def fake_refresh(refreshed_config: Any, *, force: bool = False) -> None:
        assert force is True
        captured.append(refreshed_config)

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )
    ctx = RpcContext(conn_id="test", config=config)

    await _handle_config_reload(None, ctx)

    assert captured == [ctx.config]


@pytest.mark.parametrize("kind", ["set", "patch", "apply"])
async def test_persist_failure_does_not_sync_live_selector(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    original = path.read_text()
    config = GatewayConfig.load(str(path))
    selector = _RecordingSelector()
    ctx = RpcContext(conn_id="test", config=config, provider_selector=selector)

    import opensquilla.onboarding.config_store as config_store

    def _fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_store.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="disk full"):
        await _mutate_model(kind, ctx, "gpt-new")

    assert ctx.config is config
    assert config.llm.model == "gpt-old"
    assert path.read_text() == original
    assert selector.configs == []


@pytest.mark.parametrize("kind", ["set", "patch", "apply"])
async def test_selector_failure_after_persist_keeps_disk_and_live_config_aligned(
    tmp_path, kind: str
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    selector = _RecordingSelector(fail=True)
    ctx = RpcContext(conn_id="test", config=config, provider_selector=selector)

    with pytest.raises(RuntimeError, match="selector sync failed"):
        await _mutate_model(kind, ctx, "gpt-new")

    assert ctx.config is config
    assert config.llm.model == "gpt-new"
    assert GatewayConfig.load(str(path)).llm.model == "gpt-new"
    assert config.force_persist_paths() == set()
    assert [provider_config.model for provider_config in selector.configs] == ["gpt-new"]


@pytest.mark.parametrize("kind", ["set", "patch"])
async def test_explicit_baseline_value_wins_over_disk_drift_once(
    tmp_path, kind: str
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="test", config=config)

    drifted = tomllib.loads(path.read_text())
    drifted["llm"]["model"] = "gpt-drifted"
    path.write_text(tomli_w.dumps(drifted))

    await _mutate_model(kind, ctx, "gpt-old")

    assert tomllib.loads(path.read_text())["llm"]["model"] == "gpt-old"
    assert config.force_persist_paths() == set()

    drifted_again = tomllib.loads(path.read_text())
    drifted_again["llm"]["model"] = "gpt-drifted-again"
    path.write_text(tomli_w.dumps(drifted_again))

    await _handle_config_set({"path": "naming.enabled", "value": False}, ctx)

    persisted = tomllib.loads(path.read_text())
    assert persisted["llm"]["model"] == "gpt-drifted-again"
    assert persisted["naming"]["enabled"] is False


async def test_apply_preserves_unchanged_disk_drift_without_materializing_defaults(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="test", config=config)

    drifted = tomllib.loads(path.read_text())
    drifted["llm"]["model"] = "gpt-drifted"
    path.write_text(tomli_w.dumps(drifted))

    await _mutate_model("apply", ctx, "gpt-old")

    persisted_text = path.read_text()
    assert tomllib.loads(persisted_text)["llm"]["model"] == "gpt-drifted"
    assert "[memory]" not in persisted_text


async def test_explicit_nullable_reset_removes_post_load_disk_value(tmp_path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="test", config=config)

    drifted = tomllib.loads(path.read_text())
    drifted["naming"] = {"model": "gpt-drifted"}
    path.write_text(tomli_w.dumps(drifted))

    await _handle_config_patch({"patches": {"naming.model": None}}, ctx)

    persisted = tomllib.loads(path.read_text())
    assert "model" not in persisted.get("naming", {})
    assert config.naming.model is None
    assert config.force_persist_paths() == set()


@pytest.mark.parametrize("kind", ["set", "patch", "merge"])
async def test_explicit_env_equal_base_url_is_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env_url = "https://env-equal.example/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", env_url)
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    resolve_llm_runtime_config(config)
    assert config.runtime_field_overrides()["llm.base_url"][1] == env_url
    ctx = RpcContext(conn_id="test", config=config)

    if kind == "set":
        await _handle_config_set({"path": "llm.base_url", "value": env_url}, ctx)
    elif kind == "patch":
        await _handle_config_patch({"patches": {"llm.base_url": env_url}}, ctx)
    else:
        await _handle_config_patch({"patch": {"llm": {"base_url": env_url}}}, ctx)

    persisted = tomllib.loads(path.read_text())
    assert persisted["llm"]["base_url"] == env_url
    assert config.llm.base_url == env_url


async def test_apply_roundtrip_does_not_bake_env_base_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_url = "https://env-roundtrip.example/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", env_url)
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    resolve_llm_runtime_config(config)
    ctx = RpcContext(conn_id="test", config=config)

    await _handle_config_apply(
        {"config": config.model_dump(mode="python")},
        ctx,
    )

    assert env_url not in path.read_text()


async def test_empty_merge_patch_is_sparse_noop_and_preserves_disk_drift(tmp_path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path)
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="test", config=config)

    drifted = tomllib.loads(path.read_text())
    drifted["memory"] = {"flush_enabled": True}
    path.write_text(tomli_w.dumps(drifted))

    response = await _handle_config_patch({"patch": {"memory": {}}}, ctx)

    persisted = tomllib.loads(path.read_text())
    assert response["patched"] == ["(merge)"]
    assert persisted["memory"] == {"flush_enabled": True}
    assert config.memory.flush_enabled is False


async def test_merge_patch_force_path_preserves_dotted_dynamic_key(tmp_path) -> None:
    path = tmp_path / "config.toml"
    initial = tomllib.loads(
        'config_version = 1\n\n'
        '[llm]\nprovider = "openai"\nmodel = "gpt-old"\n'
    )
    initial["models"] = {
        "openrouter": {"foo.bar": {"context_window": 8192}},
    }
    path.write_text(tomli_w.dumps(initial))
    config = GatewayConfig.load(str(path))
    ctx = RpcContext(conn_id="test", config=config)

    drifted = tomllib.loads(path.read_text())
    drifted["models"]["openrouter"]["foo.bar"]["context_window"] = 16384
    path.write_text(tomli_w.dumps(drifted))

    await _handle_config_patch(
        {
            "patch": {
                "models": {
                    "openrouter": {"foo.bar": {"context_window": 8192}},
                }
            }
        },
        ctx,
    )

    persisted = tomllib.loads(path.read_text())
    assert persisted["models"]["openrouter"]["foo.bar"]["context_window"] == 8192
    assert config.force_persist_paths() == set()
