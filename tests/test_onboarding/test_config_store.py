"""Tests for config_store persistence."""

from __future__ import annotations

import datetime as dt
import os
import stat
import tomllib

import pytest

import opensquilla.gateway.config_migration as migration_module
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.onboarding.config_store import (
    PersistResult,
    default_config_path,
    load_config,
    persist_config,
    resolve_config_path,
    validate_config_payload,
)


def _clear_path_env(monkeypatch):
    for key in (
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
        "OPENSQUILLA_STATE_DIR",
        "OPENSQUILLA_HOME",
        "OPENSQUILLA_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_config_path_under_home(monkeypatch, tmp_path):
    _clear_path_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no opensquilla.toml here
    p = default_config_path()
    assert p == tmp_path / ".opensquilla" / "config.toml"


def test_default_path_uses_env_when_set(tmp_path, monkeypatch):
    target = tmp_path / "explicit.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.chdir(tmp_path)
    assert default_config_path() == target


def test_default_path_prefers_cwd_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "opensquilla.toml").write_text("")
    monkeypatch.chdir(project)
    assert default_config_path() == project / "opensquilla.toml"


def test_resolve_config_path_ignores_cwd_directory(tmp_path, monkeypatch):
    _clear_path_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "opensquilla.toml").mkdir()
    monkeypatch.chdir(tmp_path)

    path, source = resolve_config_path(None)

    assert path == home / ".opensquilla" / "config.toml"
    assert source == "home"


def test_default_path_falls_back_to_home(tmp_path, monkeypatch):
    _clear_path_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # no opensquilla.toml in cwd
    assert default_config_path() == home / ".opensquilla" / "config.toml"


def test_resolve_config_path_returns_source(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    (project / "opensquilla.toml").write_text("")
    monkeypatch.chdir(project)
    path, source = resolve_config_path(None)
    assert path == project / "opensquilla.toml"
    assert source == "cwd"


def test_persist_creates_file_with_mode_0600(tmp_path):
    cfg = GatewayConfig()
    target = tmp_path / "config.toml"
    result = persist_config(cfg, path=target)
    assert isinstance(result, PersistResult)
    assert target.exists()
    mode = stat.S_IMODE(os.stat(target).st_mode)
    if os.name == "nt":
        assert mode & stat.S_IWRITE
    else:
        assert mode == 0o600


def test_persist_creates_backup_when_target_exists(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = GatewayConfig()
    result = persist_config(cfg, path=target)
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert result.backup_path.name.startswith("config.toml.backup.")


def test_persist_atomic_no_leftover_tmp(tmp_path):
    target = tmp_path / "config.toml"
    cfg = GatewayConfig()
    persist_config(cfg, path=target)
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_persist_validates_before_writing():
    with pytest.raises(Exception):
        validate_config_payload({"port": "not-a-port"})


def test_load_returns_gateway_config(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18792\n")
    cfg = load_config(target)
    assert cfg.port == 18792


def test_load_sets_config_path_for_existing_config(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18792\n")
    cfg = load_config(target)
    assert cfg.config_path == str(target)


def test_load_accepts_existing_operator_toggle_config(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        "\n".join(
            [
                "[skills]",
                "disabled = []",
                "coding_mode = false",
                "",
                "[meta_skill]",
                "auto_trigger = false",
                "",
                "[naming]",
                "enabled = true",
                'surfaces = ["webchat", "cli", "channel"]',
                "max_chars = 48",
                'language = "auto"',
                "",
            ]
        )
    )

    cfg = load_config(target)

    assert cfg.skills.disabled == []
    assert "coding_mode" not in cfg.skills.model_dump()
    assert "meta_skill" not in cfg.model_dump()
    assert cfg.naming.enabled is True
    assert cfg.naming.max_chars == 48
    assert cfg.config_path == str(target)


def test_load_migrates_legacy_memory_config_before_validation(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        "\n".join(
            [
                "[memory]",
                'capture_mode = "archive_turn_pair"',
                "index_captured_turns = false",
                "prefetch_enabled = true",
                "prefetch_max_results = 3",
                "prefetch_min_score = 0.3",
                "",
            ]
        )
    )

    cfg = load_config(target)

    assert cfg.memory.capture_mode == "turn_pair"
    assert cfg.config_path == str(target)
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    backup_text = backups[-1].read_text()
    assert 'capture_mode = "archive_turn_pair"' in backup_text
    assert "index_captured_turns = false" in backup_text
    data = tomllib.loads(target.read_text())
    assert data["memory"]["capture_mode"] == "turn_pair"
    assert "index_captured_turns" not in data["memory"]
    assert "prefetch_enabled" not in data["memory"]
    assert "prefetch_max_results" not in data["memory"]
    assert "prefetch_min_score" not in data["memory"]


def test_migration_then_persist_keeps_distinct_backups_on_name_collision(
    tmp_path, monkeypatch
):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 5, 19, 20, 40, 0)
            if tz is not None:
                return value.replace(tzinfo=tz)
            return value

    monkeypatch.setattr(migration_module.datetime, "datetime", FixedDateTime)
    target = tmp_path / "config.toml"
    target.write_text(
        "\n".join(
            [
                "[memory]",
                'capture_mode = "archive_turn_pair"',
                "index_captured_turns = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(target)
    cfg.port = 18793
    persist_config(cfg, path=target, backup=True)

    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert len(backups) == 2
    backup_texts = [path.read_text(encoding="utf-8") for path in backups]
    assert any('capture_mode = "archive_turn_pair"' in text for text in backup_texts)
    assert any('capture_mode = "turn_pair"' in text for text in backup_texts)


def test_validate_config_payload_does_not_migrate_legacy_memory_payload() -> None:
    with pytest.raises(Exception):
        validate_config_payload({"memory": {"capture_mode": "archive_turn_pair"}})

    with pytest.raises(Exception):
        validate_config_payload({"memory": {"index_captured_turns": False}})

    with pytest.raises(Exception):
        validate_config_payload({"memory": {"prefetch_enabled": True}})

    with pytest.raises(Exception):
        validate_config_payload({"memory": {"cost": {"embedding_cache": True}}})


def test_persist_round_trip_preserves_unrelated(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'base_url = "https://openrouter.ai/api/v1"\n'
    )
    cfg = load_config(target)
    cfg.port = 18793
    persist_config(cfg, path=target)
    text = target.read_text()
    assert "openrouter" in text
    assert "18793" in text


def test_persist_omits_runtime_secret_paths(tmp_path):
    cfg = GatewayConfig()
    cfg.llm.api_key = "from-env"
    cfg.mark_runtime_secret("llm.api_key")

    target = tmp_path / "config.toml"
    persist_config(cfg, path=target)

    text = target.read_text()
    data = tomllib.loads(text)
    assert "api_key" not in data.get("llm", {})
    assert "from-env" not in text


def test_env_sourced_llm_key_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "",
            "api_key_env": "OPENROUTER_API_KEY",
        }
    )

    runtime = resolve_llm_runtime_config(cfg)
    assert runtime.api_key == "sk-from-env"

    target = tmp_path / "config.toml"
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert "api_key" not in data["llm"]
    assert "sk-from-env" not in target.read_text()


def test_load_nonexistent_returns_default(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert isinstance(cfg, GatewayConfig)
    assert cfg.port == 18791  # default


def test_persist_omits_empty_agents_table(tmp_path):
    target = tmp_path / "config.toml"

    persist_config(GatewayConfig(), path=target)

    data = tomllib.loads(target.read_text())
    assert "agents" not in data


def test_persist_round_trips_agents_list(tmp_path):
    target = tmp_path / "config.toml"
    cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="openai/test")])

    persist_config(cfg, path=target)
    loaded = load_config(target)

    assert len(loaded.agents) == 1
    assert loaded.agents[0].id == "ops"
    assert loaded.agents[0].model == "openai/test"
