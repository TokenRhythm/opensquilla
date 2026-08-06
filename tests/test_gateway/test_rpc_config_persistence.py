"""Regressions for the gateway config-RPC persistence contract.

The incident this pins: ``config.set``/``patch``/``patch.safe``/``apply``
used to (a) rewrite config.toml as a FULL materialized dump with no backup
and no log line — a Web-UI chat-composer toggle silently rewrote the file
and forensics had nothing to go on — and (b) mutate the live config BEFORE
persisting, so a failed write left memory and disk silently diverged until
a restart reverted memory.

Everything below runs against synthetic in-memory configs; no network, no
credentials (tests/conftest.py strips provider keys from the environment).
"""

from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest
import structlog.testing

import opensquilla.gateway.rpc_config as rpc_config
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_patch_safe,
    _handle_config_set,
)


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "config.toml"


def _ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(conn_id="test", config=config)


def _write_small_config(path) -> None:
    path.write_text(
        'config_version = 1\n\n'
        '[llm]\nprovider = "openai"\nmodel = "gpt-test"\n\n'
        '[channels.demo_extra]\nnote = "keep-me"\n'
    )


# --- sparse persist -----------------------------------------------------------


@pytest.mark.parametrize("writer", ["set", "patch", "apply"])
async def test_config_writers_reject_provider_only_compaction(
    cfg_path,
    writer: str,
) -> None:
    cfg = GatewayConfig(config_path=str(cfg_path))
    ctx = _ctx(cfg)

    with pytest.raises(
        ValueError,
        match="compaction.provider requires compaction.model",
    ):
        if writer == "set":
            await _handle_config_set(
                {"path": "compaction.provider", "value": "openai"},
                ctx,
            )
        elif writer == "patch":
            await _handle_config_patch(
                {"patches": {"compaction.provider": "openai"}},
                ctx,
            )
        else:
            payload = cfg.model_dump(mode="python")
            payload["compaction"]["provider"] = "openai"
            payload["compaction"]["model"] = None
            await _handle_config_apply({"config": payload}, ctx)

    assert cfg.compaction.provider is None
    assert cfg.compaction.model is None
    assert not cfg_path.exists()


async def test_config_writer_accepts_complete_compaction_deployment(cfg_path) -> None:
    cfg = GatewayConfig(config_path=str(cfg_path))

    await _handle_config_set(
        {
            "path": "compaction",
            "value": {"provider": "openai", "model": "gpt-summary"},
        },
        _ctx(cfg),
    )

    assert cfg.compaction.provider == "openai"
    assert cfg.compaction.model == "gpt-summary"
    persisted = tomllib.loads(cfg_path.read_text())
    assert persisted["compaction"]["provider"] == "openai"
    assert persisted["compaction"]["model"] == "gpt-summary"


async def test_config_set_is_sparse_and_preserves_foreign_disk_keys(cfg_path) -> None:
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    before_lines = len(cfg_path.read_text().splitlines())

    await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    text = cfg_path.read_text()
    data = tomllib.loads(text)
    # The changed key landed; defaults were NOT materialized; a raw key the
    # model never saw survived the save.
    assert data["naming"]["enabled"] is False
    assert data["channels"]["demo_extra"]["note"] == "keep-me"
    assert "[memory]" not in text
    assert "host =" not in text
    assert len(text.splitlines()) < before_lines + 10


async def test_config_apply_force_persists_explicit_default_provider(cfg_path) -> None:
    cfg_path.write_text(
        '[llm]\napi_key = "sk_tr_abcdefghijklmnop"\n',
        encoding="utf-8",
    )
    cfg = GatewayConfig.load(str(cfg_path))
    payload = cfg.model_dump(mode="python")
    payload["config_path"] = str(cfg_path)
    payload["llm"]["provider"] = "tokenrhythm"

    await _handle_config_apply({"config": payload}, _ctx(cfg))

    persisted = tomllib.loads(cfg_path.read_text())
    assert persisted["llm"]["provider"] == "tokenrhythm"


async def test_config_apply_accepts_public_ensemble_activation_fields(cfg_path) -> None:
    cfg = GatewayConfig(config_path=str(cfg_path))
    payload = cfg.to_public_dict()

    await _handle_config_apply({"config": payload}, _ctx(cfg))

    assert cfg.llm_ensemble.enabled is False
    persisted = tomllib.loads(cfg_path.read_text())
    ensemble = persisted.get("llm_ensemble", {})
    assert "selection_configured" not in ensemble
    assert "activation_preview" not in ensemble


async def test_config_set_and_patch_accept_public_ensemble_slice(cfg_path) -> None:
    cfg = GatewayConfig(config_path=str(cfg_path))
    public_ensemble = dict(cfg.to_public_dict()["llm_ensemble"])
    public_ensemble["min_successful_proposers"] = 2

    await _handle_config_set(
        {"path": "llm_ensemble", "value": public_ensemble},
        _ctx(cfg),
    )
    assert cfg.llm_ensemble.min_successful_proposers == 2

    public_ensemble = dict(cfg.to_public_dict()["llm_ensemble"])
    public_ensemble["min_successful_proposers"] = 3
    await rpc_config._handle_config_patch(
        {"patch": {"llm_ensemble": public_ensemble}},
        _ctx(cfg),
    )
    assert cfg.llm_ensemble.min_successful_proposers == 3


async def test_config_set_preserves_known_field_edited_after_gateway_load(cfg_path) -> None:
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    cfg_path.write_text(f"port = 2222\n{cfg_path.read_text()}")

    await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    data = tomllib.loads(cfg_path.read_text())
    assert data["port"] == 2222
    assert data["naming"]["enabled"] is False


async def test_config_set_creates_timestamped_backup(cfg_path, tmp_path) -> None:
    _write_small_config(cfg_path)
    original = cfg_path.read_text()
    cfg = GatewayConfig.load(str(cfg_path))

    await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


async def test_config_set_emits_persist_log_without_values(cfg_path) -> None:
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    sentinel = "sk-sentinel-secret-value"
    cfg.llm.api_key = sentinel

    with structlog.testing.capture_logs() as captured:
        await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    events = [entry["event"] for entry in captured]
    assert "gateway.config_persisted" in events
    persisted = next(e for e in captured if e["event"] == "gateway.config_persisted")
    assert persisted["path"] == str(cfg_path)
    assert sentinel not in str(captured)


# --- transactionality ---------------------------------------------------------


async def test_persist_failure_leaves_live_config_and_disk_unchanged(
    cfg_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_small_config(cfg_path)
    original = cfg_path.read_text()
    cfg = GatewayConfig.load(str(cfg_path))
    naming_before = cfg.naming.enabled

    import opensquilla.onboarding.config_store as config_store

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_store.os, "replace", _boom)

    with pytest.raises(OSError):
        await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    # Neither surface moved: live config still serves the old value, disk
    # bytes are untouched, so a restart cannot silently revert anything.
    assert cfg.naming.enabled == naming_before
    assert cfg_path.read_text() == original


# --- the incident payload -----------------------------------------------------


async def test_routing_mode_toggle_persists_only_its_paths(cfg_path) -> None:
    """The exact silent Web-UI chat toggle that rewrote the file in the
    incident: three keys in, three keys (plus migration stamp) out."""
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))

    res = await _handle_config_patch_safe(
        {
            "patches": {
                "llm_ensemble.enabled": True,
                "squilla_router.enabled": True,
                "squilla_router.rollout_phase": "full",
            }
        },
        _ctx(cfg),
    )

    assert set(res["patched"]) == {
        "llm_ensemble.enabled",
        "squilla_router.enabled",
        "squilla_router.rollout_phase",
    }
    data = tomllib.loads(cfg_path.read_text())
    assert data["llm_ensemble"]["enabled"] is True
    # First activation materializes a provider-aware custom lineup, which is
    # independent of Router execution. Canonical reconciliation therefore
    # overrides the legacy conflicting router=true field while preserving
    # the explicit full rollout marker.
    reloaded = GatewayConfig.load(str(cfg_path))
    assert reloaded.llm_ensemble.enabled is True
    assert reloaded.squilla_router.enabled is False
    assert reloaded.squilla_router.rollout_phase == "full"
    # No default-bake: sections the toggle never touched stay absent.
    assert "memory" not in data
    assert "auth" not in data


async def test_safe_patch_persists_the_gateway_channel_notice_locale(cfg_path) -> None:
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))

    result = await _handle_config_patch_safe(
        {"patches": {"control_ui.default_locale": "zh-Hans"}},
        _ctx(cfg),
    )

    assert result["restartRequired"] is False
    assert cfg.control_ui.default_locale == "zh-Hans"
    data = tomllib.loads(cfg_path.read_text())
    assert data["control_ui"]["default_locale"] == "zh-Hans"


# --- set-heartbeats (third _persist_config caller) -----------------------------


async def test_set_heartbeats_persists_sparsely_and_atomically(cfg_path) -> None:
    import opensquilla.gateway.rpc_system as rpc_system

    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    ctx = SimpleNamespace(config=cfg, heartbeat_loop=None)

    res = await rpc_system._handle_set_heartbeats(
        {"enabled": True, "intervalMs": 120000}, ctx
    )

    assert res["enabled"] is True
    assert cfg.heartbeat.enabled is True
    data = tomllib.loads(cfg_path.read_text())
    assert data["heartbeat"]["enabled"] is True
    assert data["heartbeat"]["interval_ms"] == 120000
    assert "memory" not in data


async def test_set_heartbeats_validation_failure_leaves_live_config_clean(
    cfg_path,
) -> None:
    """A ValueError on the second parameter must not leave the first one
    half-applied on the live config (the old code mutated field-by-field)."""
    import opensquilla.gateway.rpc_system as rpc_system

    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    assert cfg.heartbeat.enabled is False
    ctx = SimpleNamespace(config=cfg, heartbeat_loop=None)

    with pytest.raises(ValueError):
        await rpc_system._handle_set_heartbeats(
            {"enabled": True, "intervalMs": -5}, ctx
        )

    assert cfg.heartbeat.enabled is False
    assert "heartbeat" not in tomllib.loads(cfg_path.read_text())


# --- env-secret hygiene --------------------------------------------------------


async def test_env_absorbed_memory_embedding_key_never_persisted(
    cfg_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-envonly-memembed"
    monkeypatch.setenv("OPENSQUILLA_MEMORY_EMBEDDING__REMOTE__API_KEY", secret)
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    assert cfg.memory.embedding.remote.api_key == secret

    await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    assert secret not in cfg_path.read_text()


async def test_explicit_memory_embedding_key_still_persisted(cfg_path) -> None:
    """The counterpart contract: a key the operator wrote INTO the file must
    survive an unrelated save (env-absorption marking must not eat it)."""
    cfg_path.write_text(
        'config_version = 1\n\n'
        '[memory.embedding.remote]\napi_key = "sk-explicit-on-disk"\n'
    )
    cfg = GatewayConfig.load(str(cfg_path))

    await _handle_config_set({"path": "naming.enabled", "value": False}, _ctx(cfg))

    data = tomllib.loads(cfg_path.read_text())
    assert data["memory"]["embedding"]["remote"]["api_key"] == "sk-explicit-on-disk"


async def test_explicit_memory_key_equal_to_env_is_persisted(
    cfg_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-explicit-equals-env"
    monkeypatch.setenv("OPENSQUILLA_MEMORY_EMBEDDING__REMOTE__API_KEY", secret)
    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))

    await _handle_config_set(
        {"path": "memory.embedding.remote.api_key", "value": secret}, _ctx(cfg)
    )

    monkeypatch.delenv("OPENSQUILLA_MEMORY_EMBEDDING__REMOTE__API_KEY")
    assert GatewayConfig.load(str(cfg_path)).memory.embedding.remote.api_key == secret


# --- onboarding handlers persist-first ------------------------------------------


async def test_onboarding_persist_failure_leaves_live_config_unchanged(
    cfg_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.rpc_onboarding as rpc_onboarding
    import opensquilla.onboarding.config_store as config_store

    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    original = cfg_path.read_text()
    ensemble_before = cfg.llm_ensemble.enabled

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_store.os, "replace", _boom)

    with pytest.raises(OSError):
        await rpc_onboarding._ensemble_configure({"enabled": True}, _ctx(cfg))

    assert cfg.llm_ensemble.enabled == ensemble_before
    assert cfg_path.read_text() == original


async def test_onboarding_live_snapshot_advances_after_successful_persist(cfg_path) -> None:
    import opensquilla.gateway.rpc_onboarding as rpc_onboarding

    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    ctx = _ctx(cfg)

    await rpc_onboarding._ensemble_configure({"enabled": True}, ctx)
    assert cfg._persist_baseline["llm_ensemble"]["enabled"] is True
    cfg_path.write_text(
        cfg_path.read_text().replace(
            "[llm_ensemble]\nenabled = true",
            "[llm_ensemble]\nenabled = false",
            1,
        )
    )

    await rpc_onboarding._search_configure({"providerId": "duckduckgo"}, ctx)

    assert tomllib.loads(cfg_path.read_text())["llm_ensemble"]["enabled"] is False


async def test_onboarding_first_save_establishes_path_and_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.rpc_onboarding as rpc_onboarding

    home = tmp_path / "home"
    target = home / "config.toml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    cfg = GatewayConfig.load(None)
    assert cfg.config_path is None
    ctx = _ctx(cfg)

    await rpc_onboarding._ensemble_configure({"enabled": True}, ctx)

    assert cfg.config_path == str(target)
    assert cfg._persist_baseline["llm_ensemble"]["enabled"] is True
    target.write_text(
        target.read_text().replace(
            "[llm_ensemble]\nenabled = true",
            "[llm_ensemble]\nenabled = false",
            1,
        )
    )

    await rpc_onboarding._search_configure({"providerId": "duckduckgo"}, ctx)

    assert tomllib.loads(target.read_text())["llm_ensemble"]["enabled"] is False


async def test_heartbeat_live_snapshot_advances_after_successful_persist(cfg_path) -> None:
    import opensquilla.gateway.rpc_system as rpc_system

    _write_small_config(cfg_path)
    cfg = GatewayConfig.load(str(cfg_path))
    ctx = SimpleNamespace(config=cfg, heartbeat_loop=None)

    await rpc_system._handle_set_heartbeats({"enabled": True}, ctx)
    assert cfg._persist_baseline["heartbeat"]["enabled"] is True
    cfg_path.write_text(cfg_path.read_text().replace("enabled = true", "enabled = false"))

    await rpc_system._handle_set_heartbeats({"intervalMs": 120000}, ctx)

    data = tomllib.loads(cfg_path.read_text())
    assert data["heartbeat"]["enabled"] is False
    assert data["heartbeat"]["interval_ms"] == 120000


def test_rpc_config_persist_delegates_to_sparse_persister(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.onboarding.config_store as config_store

    cfg = GatewayConfig()
    cfg.config_path = str(tmp_path / "config.toml")
    calls: list[tuple[GatewayConfig, str]] = []

    def _record(config, *, path):
        calls.append((config, str(path)))
        return SimpleNamespace(path=path, backup_path=None)

    monkeypatch.setattr(config_store, "persist_config", _record)

    rpc_config._persist_config(cfg)

    assert calls == [(cfg, cfg.config_path)]
