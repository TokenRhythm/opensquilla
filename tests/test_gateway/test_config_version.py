"""Tests for the config_version stamp and version-gated config migrations.

migrate_config_payload runs two classes of transforms: always-run compat
normalizations (deprecated-field strips, renames, range clamps) and
version-gated one-time value migrations walked from ``_MIGRATIONS``. Every
returned payload is stamped with ``LATEST_CONFIG_VERSION``, but stamping
alone never marks the result as changed, so a current config file is never
rewritten just to receive the stamp.
"""

from __future__ import annotations

import tomllib
import warnings
from pathlib import Path
from typing import Any

import pytest
import tomli_w

import opensquilla.gateway.config_migration as migration_module
import opensquilla.gateway.rpc_config  # noqa: F401  ensures RPC registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_migration import (
    LATEST_CONFIG_VERSION,
    migrate_config_payload,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.onboarding import config_store
from opensquilla.search.types import MAX_SEARCH_RESULTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEGACY_ENSEMBLE_TIMEOUTS = {
    "enabled": True,
    "proposer_timeout_seconds": 300.0,
    "aggregator_timeout_seconds": 300.0,
}


def _write_toml(path: Path, payload: dict[str, Any]) -> Path:
    with path.open("wb") as fh:
        tomli_w.dump(payload, fh)
    return path


def _reset_legacy_warn_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_WARNED", False)
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_SEEN", set())
    monkeypatch.setattr(migration_module, "_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED", False)
    monkeypatch.setattr(migration_module, "_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN", set())
    # Keep legacy-field detail logs inside tmp_path.
    monkeypatch.setattr(migration_module, "default_opensquilla_home", lambda: tmp_path)


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


# ---------------------------------------------------------------------------
# Framework consistency
# ---------------------------------------------------------------------------


def test_config_version_field_default_matches_latest_constant() -> None:
    """GatewayConfig.config_version and LATEST_CONFIG_VERSION must agree."""
    assert GatewayConfig.model_fields["config_version"].default == LATEST_CONFIG_VERSION
    assert GatewayConfig().config_version == LATEST_CONFIG_VERSION


def test_migration_walk_is_strictly_increasing_and_capped_at_latest() -> None:
    versions = [version for version, _ in migration_module._MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[-1] == LATEST_CONFIG_VERSION


# ---------------------------------------------------------------------------
# Stamping without change
# ---------------------------------------------------------------------------


def test_unstamped_current_payload_is_stamped_but_unchanged(tmp_path: Path) -> None:
    payload = {
        "host": "127.0.0.1",
        "port": 18791,
        "memory": {"capture_mode": "turn_pair"},
        "llm_ensemble": {"enabled": False},
    }

    result = migrate_config_payload(payload)

    assert result.changed is False
    assert result.changes == ()
    assert result.removed_fields == ()
    assert result.payload["config_version"] == LATEST_CONFIG_VERSION
    cfg = GatewayConfig.model_validate(result.payload)
    assert cfg.config_version == LATEST_CONFIG_VERSION

    # A load of the same payload from disk must not rewrite the file just to
    # persist the stamp.
    toml_path = _write_toml(tmp_path / "config.toml", payload)
    original_bytes = toml_path.read_bytes()
    loaded = GatewayConfig.load(toml_path)
    assert loaded.config_version == LATEST_CONFIG_VERSION
    assert toml_path.read_bytes() == original_bytes
    assert "config_version" not in toml_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("config.toml.backup.*"))


def test_config_parse_error_names_the_offending_file(tmp_path: Path) -> None:
    from opensquilla.gateway.config_migration import ConfigParseError

    toml_path = tmp_path / "config.toml"
    toml_path.write_text("workspace_dir = [\n", encoding="utf-8")

    with pytest.raises(ConfigParseError) as excinfo:
        GatewayConfig.load(toml_path)
    assert str(toml_path) in str(excinfo.value)
    assert excinfo.value.path == toml_path

    with pytest.raises(ConfigParseError):
        GatewayConfig.load_from_toml(toml_path)


def test_migrated_config_write_syncs_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_module

    toml_path = _write_toml(
        tmp_path / "config.toml",
        {"llm_ensemble": {"stage1_timeout_seconds": 120}},
    )
    synced_fds: list[int] = []
    real_fsync = os_module.fsync

    def record_fsync(fd: int) -> None:
        synced_fds.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(migration_module.os, "fsync", record_fsync)

    payload = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    result = migrate_config_payload(payload)
    migration_module.backup_and_write_migrated_config(toml_path, result.payload, result)

    assert synced_fds, "the migrated config bytes must reach disk before os.replace"
    assert list(tmp_path.glob("config.toml.backup.*"))


def test_config_backups_keep_only_the_newest_n(tmp_path: Path) -> None:
    toml_path = _write_toml(tmp_path / "config.toml", {"config_version": 1})
    keep = migration_module._CONFIG_BACKUP_KEEP
    stale = [
        tmp_path / f"config.toml.backup.2025010100000000{index:04d}"
        for index in range(keep + 3)
    ]
    for path in stale:
        path.write_text("stale backup\n", encoding="utf-8")

    migration_module.make_config_backup(toml_path)

    survivors = sorted(tmp_path.glob("config.toml.backup.*"))
    assert len(survivors) == keep
    # Name order is timestamp order; the oldest names are the ones removed.
    assert survivors == sorted(stale)[4:] + survivors[-1:]


def test_config_backup_pruning_skips_non_files(tmp_path: Path) -> None:
    toml_path = _write_toml(tmp_path / "config.toml", {"config_version": 1})
    keep = migration_module._CONFIG_BACKUP_KEEP
    for index in range(keep + 2):
        (tmp_path / f"config.toml.backup.2025010100000000{index:04d}").write_text(
            "stale backup\n", encoding="utf-8"
        )
    decoy = tmp_path / "config.toml.backup.00000000000000000000"
    decoy.mkdir()

    migration_module.make_config_backup(toml_path)

    assert decoy.is_dir()
    assert len([p for p in tmp_path.glob("config.toml.backup.*") if p.is_file()]) == keep


# ---------------------------------------------------------------------------
# Version-gated migration: llm_ensemble legacy timeout bump
# ---------------------------------------------------------------------------


def test_unstamped_legacy_ensemble_timeouts_bump_exactly_once() -> None:
    result = migrate_config_payload({"llm_ensemble": dict(_LEGACY_ENSEMBLE_TIMEOUTS)})

    assert result.changed is True
    assert result.payload["llm_ensemble"]["proposer_timeout_seconds"] == 3600.0
    assert result.payload["llm_ensemble"]["aggregator_timeout_seconds"] == 3600.0
    assert result.payload["config_version"] == LATEST_CONFIG_VERSION

    # Feeding the stamped result back through must be a no-op.
    second = migrate_config_payload(result.payload)
    assert second.changed is False
    assert second.payload == result.payload


def test_stamped_payload_skips_ensemble_timeout_bump() -> None:
    """Post-migration, an operator deliberately re-setting 300/300 keeps it."""
    result = migrate_config_payload(
        {
            "config_version": LATEST_CONFIG_VERSION,
            "llm_ensemble": dict(_LEGACY_ENSEMBLE_TIMEOUTS),
        }
    )

    assert result.changed is False
    assert result.payload["llm_ensemble"]["proposer_timeout_seconds"] == 300.0
    assert result.payload["llm_ensemble"]["aggregator_timeout_seconds"] == 300.0


def test_stamped_file_load_keeps_deliberate_legacy_timeouts(tmp_path: Path) -> None:
    toml_path = _write_toml(
        tmp_path / "config.toml",
        {
            "config_version": LATEST_CONFIG_VERSION,
            "llm_ensemble": dict(_LEGACY_ENSEMBLE_TIMEOUTS),
        },
    )

    cfg = GatewayConfig.load(toml_path)

    assert cfg.llm_ensemble.proposer_timeout_seconds == 300.0
    assert cfg.llm_ensemble.aggregator_timeout_seconds == 300.0
    assert not list(tmp_path.glob("config.toml.backup.*"))


# ---------------------------------------------------------------------------
# Always-run compat normalizations still fire on stamped payloads
# ---------------------------------------------------------------------------


def test_stamped_payload_still_strips_deprecated_memory_field() -> None:
    result = migrate_config_payload(
        {
            "config_version": LATEST_CONFIG_VERSION,
            "memory": {"index_captured_turns": False},
        }
    )

    assert result.changed is True
    assert "memory.index_captured_turns" in result.removed_fields
    cfg = GatewayConfig.model_validate(result.payload)
    assert cfg.config_version == LATEST_CONFIG_VERSION


def test_stamped_payload_still_normalizes_capture_mode_and_clamps_search() -> None:
    result = migrate_config_payload(
        {
            "config_version": LATEST_CONFIG_VERSION,
            "memory": {"capture_mode": "archive_turn_pair"},
            "search_max_results": MAX_SEARCH_RESULTS + 100,
        }
    )

    assert result.changed is True
    assert result.payload["memory"]["capture_mode"] == "turn_pair"
    assert result.payload["search_max_results"] == MAX_SEARCH_RESULTS
    GatewayConfig.model_validate(result.payload)


# ---------------------------------------------------------------------------
# Env exclusion: the payload stamp always outranks the environment
# ---------------------------------------------------------------------------


def test_env_config_version_never_overrides_payload_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_VERSION", "99")
    toml_path = _write_toml(tmp_path / "config.toml", {"port": 18791})

    assert GatewayConfig.load_from_toml(toml_path).config_version == LATEST_CONFIG_VERSION
    assert GatewayConfig.load(toml_path).config_version == LATEST_CONFIG_VERSION
    assert config_store.load_config(toml_path).config_version == LATEST_CONFIG_VERSION

    # A persist must write the real stamp, never the env value.
    cfg = config_store.load_config(toml_path)
    persist_target = tmp_path / "persisted.toml"
    config_store.persist_config(cfg, path=persist_target, backup=False)
    text = persist_target.read_text(encoding="utf-8")
    assert "config_version = 1" in text
    assert "config_version = 99" not in text


def test_env_config_version_is_excluded_for_bare_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bare GatewayConfig() has no payload, so the env var is filtered at the
    settings-source level (see _EnvWithoutConfigVersion in gateway/config.py);
    without that filter OPENSQUILLA_GATEWAY_CONFIG_VERSION would leak into the
    no-file default branches of GatewayConfig.load and config_store.load_config.
    """
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_VERSION", "99")

    assert GatewayConfig().config_version == LATEST_CONFIG_VERSION
    assert (
        config_store.load_config(tmp_path / "missing.toml").config_version
        == LATEST_CONFIG_VERSION
    )


# ---------------------------------------------------------------------------
# Persist round-trip
# ---------------------------------------------------------------------------


def test_round_trip_persists_stamp_and_stays_unchanged(tmp_path: Path) -> None:
    toml_path = _write_toml(tmp_path / "config.toml", {"port": 18791})

    cfg = config_store.load_config(toml_path)
    toml_dict = config_store._config_to_toml_dict(cfg)
    assert toml_dict["config_version"] == LATEST_CONFIG_VERSION
    assert cfg.to_toml_dict()["config_version"] == LATEST_CONFIG_VERSION

    persist_target = tmp_path / "roundtrip.toml"
    config_store.persist_config(cfg, path=persist_target, backup=False)

    reloaded = config_store.load_config(persist_target)
    assert reloaded.config_version == LATEST_CONFIG_VERSION
    with persist_target.open("rb") as fh:
        raw = tomllib.load(fh)
    assert migrate_config_payload(raw).changed is False


def test_migrating_load_writes_stamp_into_rewritten_file(tmp_path: Path) -> None:
    """When a real migration fires, the rewritten file must carry the stamp;
    a missed stamp would re-run the one-time migration on every future load."""
    toml_path = _write_toml(
        tmp_path / "config.toml",
        {"llm_ensemble": dict(_LEGACY_ENSEMBLE_TIMEOUTS)},
    )

    cfg = GatewayConfig.load(toml_path)

    assert cfg.llm_ensemble.proposer_timeout_seconds == 3600.0
    assert list(tmp_path.glob("config.toml.backup.*"))
    text = toml_path.read_text(encoding="utf-8")
    assert "config_version = 1" in text
    assert "proposer_timeout_seconds = 3600.0" in text

    # The stamped file must load cleanly with no further rewrite.
    stamped_bytes = toml_path.read_bytes()
    again = GatewayConfig.load(toml_path)
    assert again.llm_ensemble.proposer_timeout_seconds == 3600.0
    assert toml_path.read_bytes() == stamped_bytes


# ---------------------------------------------------------------------------
# Idempotency across fixture shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"port": 18791},
        {"llm_ensemble": dict(_LEGACY_ENSEMBLE_TIMEOUTS)},
        {"memory": {"capture_mode": "archive_turn_pair", "index_captured_turns": True}},
        {"memory": {"prefetch_enabled": True, "cost": {"embedding_cache": "true"}}},
        {
            "agent_token_saving": {
                "tool_result_compression_enabled": False,
                "tool_result_compression_summary_input_max_chars": 43210,
            }
        },
        {"search_max_results": MAX_SEARCH_RESULTS + 5},
    ],
    ids=[
        "clean",
        "ensemble-timeouts",
        "memory-capture",
        "memory-deprecated",
        "token-saving",
        "search-clamp",
    ],
)
def test_migrate_twice_equals_migrate_once(
    payload: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_legacy_warn_state(monkeypatch, tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        once = migrate_config_payload(payload)
        twice = migrate_config_payload(once.payload)

    assert once.payload["config_version"] == LATEST_CONFIG_VERSION
    assert twice.changed is False
    assert twice.payload == once.payload


# ---------------------------------------------------------------------------
# RPC guard: config_version is read-only for clients
# ---------------------------------------------------------------------------


async def test_config_set_rejects_config_version(tmp_path: Path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "config_version", "value": 99},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert "read-only" in res.error.message
    assert cfg.config_version == LATEST_CONFIG_VERSION


async def test_config_patch_skips_config_version(tmp_path: Path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"config_version": 99, "diagnostics_enabled": True}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.config_version == LATEST_CONFIG_VERSION
    assert cfg.diagnostics_enabled is True
    persisted = config_path.read_text(encoding="utf-8")
    assert "config_version = 1" in persisted
    assert "config_version = 99" not in persisted


async def test_config_patch_merge_form_honors_readonly_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    cfg.auth = cfg.auth.model_copy(update={"mode": "token", "token": "BOOT_SECRET"})
    cfg.mark_runtime_secret("auth.token")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patch": {"auth": {"token": "CLIENT_CHOSEN"}, "config_version": 987654}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.auth.token == "BOOT_SECRET"
    assert cfg.config_version == LATEST_CONFIG_VERSION
    persisted = config_path.read_text(encoding="utf-8")
    assert "CLIENT_CHOSEN" not in persisted
    assert "987654" not in persisted


@pytest.mark.parametrize("replacement", [None, "disabled", []])
async def test_config_patch_merge_form_drops_scalar_readonly_parent(
    tmp_path: Path, replacement: object
) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    cfg.auth = cfg.auth.model_copy(update={"mode": "token", "token": "BOOT_SECRET"})
    cfg.mark_runtime_secret("auth.token")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patch": {"auth": replacement, "diagnostics_enabled": True}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.auth.mode == "token"
    assert cfg.auth.token == "BOOT_SECRET"
    assert "auth.token" in cfg._runtime_secret_paths
    assert cfg.diagnostics_enabled is True


async def test_config_set_rejects_parent_of_readonly_auth_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    cfg.auth = cfg.auth.model_copy(update={"mode": "token", "token": "BOOT_SECRET"})
    cfg.mark_runtime_secret("auth.token")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "auth", "value": {"mode": "token", "token": "CLIENT_CHOSEN"}},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert "read-only" in res.error.message
    assert cfg.auth.token == "BOOT_SECRET"
    assert "auth.token" in cfg._runtime_secret_paths
    assert not config_path.exists()


async def test_config_patch_dot_parent_skips_readonly_auth_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    cfg.auth = cfg.auth.model_copy(update={"mode": "token", "token": "BOOT_SECRET"})
    cfg.mark_runtime_secret("auth.token")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"auth": {"mode": "token", "token": "CLIENT_CHOSEN"}}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.auth.token == "BOOT_SECRET"
    assert "auth.token" in cfg._runtime_secret_paths
    assert "CLIENT_CHOSEN" not in config_path.read_text(encoding="utf-8")
