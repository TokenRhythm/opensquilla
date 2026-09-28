"""Synthetic upgrade profiles: retired learning state must never gate routing."""

from __future__ import annotations

import builtins
import io
import os
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest
import tomli_w
from pydantic import ValidationError

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps import squilla_router as router_step
from opensquilla.gateway import config_migration
from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.paths import native_io_path

LOADERS = [GatewayConfig.load, GatewayConfig.load_from_toml, load_config]


def _legacy_payload(enabled: bool = True) -> dict:
    return {
        "config_version": config_migration.LATEST_CONFIG_VERSION,
        "llm": {"provider": "openrouter", "api_key": "synthetic-upgrade-secret"},
        "squilla_router": {
            "preset_binding": "custom",
            "v4_bundle_dir": "router/learned/explicit-base",
            "tiers": {"c1": {"provider": "openrouter", "model": "synthetic-base-model"}},
            "self_learning": {
                "enabled": enabled,
                "capture_enabled": True,
                "enable_mlp": True,
                "store_audit_summary": True,
                "train_min_samples": 200,
                "idle_hours": 2.0,
                "cooldown_hours": 72.0,
                "retention_days": 30,
                "num_boost_round": 60,
                "train_timeout_seconds": 900.0,
                "auto_rollback": True,
                "golden_eval_path": "synthetic-retired-evaluation.json",
                "cost_tolerance_pct": 5.0,
                "max_critical_under_routing": 0.3,
                "min_golden_agreement": 0.5,
                "min_monitor_samples": 30,
                "complaint_regression_delta": 0.05,
                "min_feedback_monitor_samples": 5,
                "downvote_regression_delta": 0.15,
                "holdout_pct": 0.1,
                "holdout_repeats": 5,
                "holdout_min_size": 30,
                "holdout_granularity": "session",
                "future_parameter": {"opaque": "synthetic-retired-content"},
            },
        },
        "memory": {
            "dream": {
                "enabled": True,
                "auto_schedule": True,
                "interval_h": 7,
                "preview_mode": False,
            }
        },
    }


def _write(path: Path, payload: dict) -> bytes:
    data = tomli_w.dumps(payload).encode()
    path.write_bytes(data)
    return data


def _assert_preserved(cfg: GatewayConfig) -> None:
    assert not hasattr(cfg.squilla_router, "self_learning")
    assert cfg.llm.api_key == "synthetic-upgrade-secret"
    assert cfg.squilla_router.tiers["c1"]["model"] == "synthetic-base-model"
    assert cfg.squilla_router.v4_bundle_dir == "router/learned/explicit-base"
    assert cfg.memory.dream.enabled and cfg.memory.dream.auto_schedule
    assert cfg.memory.dream.interval_h == 7
    assert cfg.memory.dream.preview_mode is False


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_settings_migrate_idempotently_and_survive_restore(
    loader,
    enabled,
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SQUILLA_ROUTER_SELF_LEARNING", '{"enabled": true}')
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SQUILLA_ROUTER__SELF_LEARNING__ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_ROUTER_SELFLEARN_DISABLED", "0")
    path = tmp_path / "config.toml"
    original = _write(path, _legacy_payload(enabled))
    _assert_preserved(loader(path))
    migrated = path.read_bytes()
    assert "self_learning" not in tomllib.loads(migrated.decode())["squilla_router"]
    assert list(tmp_path.glob("config.toml.backup.*"))[0].read_bytes() == original
    _assert_preserved(loader(path))
    assert path.read_bytes() == migrated
    assert len(list(tmp_path.glob("config.toml.backup.*"))) == 1
    path.write_bytes(original)
    cfg = loader(path)
    _assert_preserved(cfg)
    cfg.naming.enabled = False
    persist_config(cfg, path=path)
    assert "self_learning" not in path.read_text()
    _assert_preserved(loader(path))
    assert "synthetic-upgrade-secret" not in caplog.text
    assert "synthetic-retired-content" not in caplog.text


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("failure", ["backup", "tempfile", "replace"])
def test_automatic_rewrite_failure_still_loads_but_explicit_save_fails(
    loader,
    failure,
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    path = tmp_path / "config.toml"
    original = _write(path, _legacy_payload())
    target = {
        "backup": (config_migration, "make_config_backup"),
        "tempfile": (config_migration.tempfile, "mkstemp"),
        "replace": (config_migration.os, "replace"),
    }[failure]
    with monkeypatch.context() as patch:
        patch.setattr(*target, Mock(side_effect=PermissionError("synthetic read-only profile")))
        cfg = loader(path)
        _assert_preserved(cfg)
        assert path.read_bytes() == original
        assert "running from the migrated payload in memory" in caplog.text
    # Active saves retain their error contract; they do not use the best-effort wrapper.
    with monkeypatch.context() as patch:
        patch.setattr(config_migration.os, "replace", Mock(side_effect=OSError("save failed")))
        cfg.naming.enabled = False
        with pytest.raises(OSError, match="save failed"):
            persist_config(cfg, path=path, backup=False)
    assert path.read_bytes() == original
    persist_config(cfg, path=path)
    assert "self_learning" not in path.read_text()
    _assert_preserved(loader(path))


@pytest.mark.parametrize("loader", LOADERS)
def test_invalid_current_settings_are_not_migrated_on_disk(loader, tmp_path) -> None:
    payload = _legacy_payload()
    payload["memory"]["dream"]["interval_h"] = 0
    path = tmp_path / "config.toml"
    original = _write(path, payload)
    with pytest.raises(ValidationError):
        loader(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("config.toml.backup.*"))


@pytest.mark.parametrize("loader", LOADERS)
def test_unreadable_config_still_raises(loader, tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    _write(path, _legacy_payload())

    def guarded_open(original):
        def wrapped(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and native_io_path(file) == native_io_path(
                path
            ):
                raise PermissionError("synthetic unreadable profile")
            return original(file, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", guarded_open(builtins.open))
    monkeypatch.setattr(io, "open", guarded_open(io.open))
    with pytest.raises(PermissionError, match="unreadable profile"):
        loader(path)


def test_strip_is_unversioned_and_does_not_expand_validation() -> None:
    payload = _legacy_payload()
    result = config_migration.migrate_config_payload(payload, emit_diagnostics=False)
    assert "squilla_router.self_learning" in result.removed_fields
    assert "self_learning" in payload["squilla_router"]  # caller data is untouched
    assert not config_migration.migrate_config_payload(result.payload).changed
    assert "self_learning" not in str(GatewayConfig.model_json_schema())
    with pytest.raises(ValidationError):
        GatewayConfig(memory={"dream": {"unknown_active_setting": True}})


def test_retired_environment_values_are_not_validated(monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_SQUILLA_ROUTER_SELF_LEARNING", "not json")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SQUILLA_ROUTER__SELF_LEARNING", "not json")
    assert not hasattr(GatewayConfig().squilla_router, "self_learning")


def test_explicit_base_model_path_is_used_even_when_named_learned(tmp_path, monkeypatch):
    path = tmp_path / "router" / "learned" / "explicit-base"
    cfg = GatewayConfig(squilla_router={"v4_bundle_dir": str(path)})
    constructor = Mock()
    monkeypatch.setattr("opensquilla.squilla_router.v4_phase3.V4Phase3Strategy", constructor)
    monkeypatch.setattr(router_step, "_strategy", None)
    monkeypatch.setattr(router_step, "_strategy_key", None)
    assert router_step.preload_strategy(cfg.squilla_router) is constructor.return_value
    assert constructor.call_args.kwargs["bundle_dir"] == str(path)


@pytest.mark.parametrize(
    "state", ["missing", "untrained", "trained", "rolled_back", "corrupt", "denied", "dangling"]
)
async def test_first_route_never_accesses_historical_learning_state(
    state,
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "profile"
    retired = home / "router"
    home.mkdir()
    if state != "missing":
        retired.mkdir()
        if state == "dangling":
            try:
                (retired / "active").symlink_to(retired / "absent")
            except OSError:
                pytest.skip("symlink creation unavailable on this platform")
        else:
            (retired / "active").write_text(
                {
                    "trained": "learned/synthetic-v1",
                    "corrupt": "not-a-pointer",
                }.get(state, "baseline")
            )
        data = retired / "data" / "main"
        data.mkdir(parents=True)
        (data / "samples-synthetic.jsonl").write_text("corrupt historical samples")
        (data / ".train_state.json").write_text("not json")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    config_path = home / "config.toml"
    payload = _legacy_payload()
    payload["squilla_router"].pop("v4_bundle_dir")
    _write(config_path, payload)

    accessed = []
    retired_io = native_io_path(retired)

    def guard(original):
        def wrapped(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and native_io_path(path).is_relative_to(
                retired_io
            ):
                accessed.append(str(path))
                # Includes existence checks and directory scans, not just file reads.
                if state == "denied":
                    raise PermissionError("retired state must be irrelevant")
                pytest.fail(f"routing accessed retired state: {path}")
            return original(path, *args, **kwargs)

        return wrapped

    for owner, name in [
        (builtins, "open"),
        (io, "open"),
        (os, "stat"),
        (os, "lstat"),
        (os, "scandir"),
    ]:
        monkeypatch.setattr(owner, name, guard(getattr(owner, name)))

    class BaseStrategy:
        source = "v4_phase3"

        def __init__(self, *, bundle_dir, **kwargs):
            assert bundle_dir is None

        async def classify(self, message, valid_tiers, **kwargs):
            return "c1", 0.95, "v4_phase3", {"route_class": "R1"}

    monkeypatch.setattr("opensquilla.squilla_router.v4_phase3.V4Phase3Strategy", BaseStrategy)
    monkeypatch.setattr(router_step, "_strategy", None)
    monkeypatch.setattr(router_step, "_strategy_key", None)
    router_step._history_store.clear()
    cfg = GatewayConfig.load(config_path)
    ctx = TurnContext(
        message="Synthetic greeting",
        session_key=f"upgrade-{state}",
        config=cfg,
        provider=None,
        model=cfg.llm.model,
        tool_defs=[],
        system_prompt="Synthetic assistant",
    )
    try:
        await router_step.apply_squilla_router(ctx)
        assert not accessed
        assert ctx.model == "synthetic-base-model"
        assert not any("train" in key for key in ctx.metadata)
    finally:
        router_step._history_store.clear()
