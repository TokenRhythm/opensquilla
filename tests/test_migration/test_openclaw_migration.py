from __future__ import annotations

import json
import shutil
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from opensquilla.engine.context import load_context_files
from opensquilla.gateway.config_migration import LATEST_CONFIG_VERSION
from opensquilla.migration.openclaw import (
    MigrationOptions,
    OpenClawMigrator,
    _ApplyRollback,
)
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.provider.selector import build_provider
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer


def _make_source(root: Path) -> Path:
    source = root / ".openclaw"
    workspace = source / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "skills" / "demo").mkdir(parents=True)
    (workspace / "tts").mkdir(parents=True)
    (source / "credentials").mkdir(parents=True)

    (workspace / "SOUL.md").write_text("openclaw soul\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("agent rules\n", encoding="utf-8")
    (workspace / "USER.md").write_text("user profile\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("core memory\n", encoding="utf-8")
    (workspace / "memory" / "2026-05-10.md").write_text("daily note\n", encoding="utf-8")
    (workspace / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nDemo body\n",
        encoding="utf-8",
    )
    (workspace / "tts" / "voice.txt").write_text("voice asset\n", encoding="utf-8")
    (source / "exec-approvals.json").write_text(
        json.dumps({"allow": ["^git status$", "^pytest "]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (source / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "deepseek-chat",
                        "timeoutSeconds": 500,
                    }
                },
                "mcp": {
                    "servers": {
                        "demo-mcp": {
                            "command": "node",
                            "args": ["server.js"],
                            "env": {"DEMO": "1"},
                        }
                    }
                },
                "tools": {"exec": {"timeoutSec": 45}},
                "messages": {
                    "telegram": {"defaultChatId": "12345"},
                    "discord": {"defaultChannelId": "C999"},
                    "tts": {"provider": "elevenlabs", "elevenlabs": {"voice": "voice-1"}},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def test_failed_rollback_retains_before_images_and_reports_recovery_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    rollback = _ApplyRollback()
    backup_root = rollback._backup_root

    def fail_restore(cls, image) -> None:
        del cls, image
        raise OSError("forced restore failure")

    monkeypatch.setattr(_ApplyRollback, "_restore", classmethod(fail_restore))

    try:
        with pytest.raises(OSError, match="before-image backup retained") as caught:
            with rollback:
                rollback.capture(target)
                target.write_text("after\n", encoding="utf-8")
                raise RuntimeError("forced apply failure")

        assert str(backup_root) in str(caught.value)
        assert backup_root.is_dir()
        assert (backup_root / "0").read_text(encoding="utf-8") == "before\n"
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def test_late_failure_preserves_concurrent_settings_save(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    config_path.write_text("debug = false\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report_started = Event()
    settings_saved = Event()

    def fail_report_write(self) -> None:
        del self
        report_started.set()
        if not settings_saved.wait(timeout=5):
            raise AssertionError("settings save did not complete")
        raise RuntimeError("forced late failure")

    monkeypatch.setattr(OpenClawMigrator, "_write_report_files", fail_report_write)
    migrator = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        migration = pool.submit(migrator.migrate)
        assert report_started.wait(timeout=5)
        try:
            settings = load_config(config_path, persist_migrations=False)
            settings.debug = True
            persist_config(settings, path=config_path, backup=False)
        finally:
            settings_saved.set()

        with pytest.raises(RuntimeError, match="forced late failure"):
            migration.result(timeout=5)

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["debug"] is True
    assert "llm" not in persisted


def test_dry_run_does_not_write_targets(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=False)
    ).migrate()

    assert report["apply"] is False
    assert any(item["status"] == "planned" for item in report["items"])
    assert not config_path.exists()
    assert not (home / "workspace" / "SOUL.md").exists()
    assert not (home / "skills" / "openclaw-imports").exists()


def test_dry_run_does_not_upgrade_legacy_target_config(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "homes"
        / "cli-0.1"
        / "config.toml"
    )
    shutil.copy2(fixture, config_path)
    original = config_path.read_bytes()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=False)
    ).migrate()

    assert config_path.read_bytes() == original
    assert list(tmp_path.glob("config.toml.backup.*")) == []


def test_apply_defers_legacy_target_config_upgrade_to_final_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    original = (
        f"workspace_dir = {json.dumps(str(home / 'workspace'))}\n"
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        "\n[llm_ensemble]\n"
        "proposer_timeout_seconds = 120.0\n"
        "aggregator_timeout_seconds = 120.0\n"
    )
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            preset="user-data",
            # Skill import is the only selected user-data operation that
            # intentionally changes config; exclude it so this test isolates
            # the deferred target-schema persist.
            exclude=("skills", "shared-skills"),
        )
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["config_version"] == LATEST_CONFIG_VERSION
    assert persisted["llm_ensemble"]["proposer_timeout_seconds"] == 3600.0
    assert persisted["llm_ensemble"]["aggregator_timeout_seconds"] == 3600.0
    backups = list(tmp_path.glob("config.toml.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_apply_migrates_workspace_skills_config_and_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    workspace = home / "workspace"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "opensquilla soul\n"
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "agent rules\n"
    memory = (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "core memory" in memory
    assert "daily note" in memory

    skill_file = home / "skills" / "openclaw-imports" / "demo" / "SKILL.md"
    assert skill_file.read_text(encoding="utf-8").startswith("---\nname: demo")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["llm"]["model"] == "deepseek-chat"
    assert config["mcp"]["enabled"] is True
    assert config["mcp"]["servers"][0]["name"] == "demo-mcp"
    assert str(home / "skills" / "openclaw-imports") in config["skills"]["extra_dirs"]
    assert config["agent_runtime_timeout_seconds"] == 500

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "OPENSQUILLA_SAFE_BIN_ALLOW=^git status$,^pytest " in env_text
    assert (Path(report["output_dir"]) / "report.json").is_file()
    assert (Path(report["output_dir"]) / "summary.md").is_file()


def test_explicit_state_dir_normalizes_default_workspace_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    config_path.write_text(
        'workspace_dir = "'
        + str(Path.home() / ".opensquilla" / "workspace").replace("\\", "\\\\")
        + '"\nstate_dir = "'
        + str(Path.home() / ".opensquilla" / "state").replace("\\", "\\\\")
        + '"\n',
        encoding="utf-8",
    )

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    workspace = home / "workspace"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "opensquilla soul\n"
    assert not any("target exists" in item["reason"] for item in report["items"])
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["workspace_dir"] == str(home / "workspace")
    assert config["state_dir"] == str(home / "state")


def test_secrets_are_opt_in_and_reports_are_redacted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    (source / ".env").write_text("DEEPSEEK_API_KEY=sk-secret-value\n", encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    no_secret_report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "no-secret.toml", apply=True)
    ).migrate()
    assert "sk-secret-value" not in json.dumps(no_secret_report)
    assert "DEEPSEEK_API_KEY=sk-secret-value" not in (home / ".env").read_text(
        encoding="utf-8"
    )

    secret_report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "with-secret.toml",
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    assert "DEEPSEEK_API_KEY=sk-secret-value" in (home / ".env").read_text(encoding="utf-8")
    encoded = json.dumps(secret_report)
    assert "sk-secret-value" not in encoded
    assert "[redacted]" in encoded


def test_migrate_secrets_can_create_supported_channels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    (source / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=tg-secret\nDISCORD_BOT_TOKEN=discord-secret\n",
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    channels = {entry["type"]: entry for entry in config["channels"]["channels"]}
    assert channels["telegram"]["token"] == "tg-secret"
    assert channels["telegram"]["default_chat_id"] == "12345"
    assert channels["discord"]["token"] == "discord-secret"
    assert channels["discord"]["default_channel_id"] == "C999"
    assert "tg-secret" not in json.dumps(report)
    assert "discord-secret" not in json.dumps(report)


def test_tts_assets_are_copied_and_tts_config_is_archived(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=True)
    ).migrate()

    assert (home / "tts" / "voice.txt").read_text(encoding="utf-8") == "voice asset\n"
    archive = Path(report["output_dir"]) / "archive" / "tts-config.json"
    assert archive.is_file()
    assert any(
        item["kind"] == "tts-config" and item["status"] == "archived"
        for item in report["items"]
    )


def test_unmapped_openclaw_config_is_archived(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["plugins"] = {"enabled": True, "entries": [{"id": "custom"}]}
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=True)
    ).migrate()

    archive = Path(report["output_dir"]) / "archive" / "plugins-config.json"
    assert archive.is_file()
    assert any(
        item["kind"] == "plugins-config" and item["status"] == "archived"
        for item in report["items"]
    )


def test_archive_redacts_secret_values_by_key_name(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["plugins"] = {
        "entries": [
            {
                "id": "custom",
                "config": {
                    "apiKey": "opaque-value-123",
                    "nested": {"webhookToken": "opaque-value-456"},
                },
            }
        ]
    }
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=True)
    ).migrate()

    archive_text = (
        Path(report["output_dir"]) / "archive" / "plugins-config.json"
    ).read_text(encoding="utf-8")
    assert "opaque-value-123" not in archive_text
    assert "opaque-value-456" not in archive_text
    assert archive_text.count("[redacted]") == 2


def test_user_data_preset_skips_runtime_config_and_archives(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["plugins"] = {"entries": [{"id": "custom"}]}
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            preset="user-data",
        )
    ).migrate()

    assert (home / "workspace" / "SOUL.md").is_file()
    assert (home / "skills" / "openclaw-imports" / "demo" / "SKILL.md").is_file()
    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert str(home / "skills" / "openclaw-imports") in persisted["skills"]["extra_dirs"]
    # The user-data preset skips model config entirely; with sparse
    # persistence the llm section is not written at all.
    assert persisted.get("llm", {}).get("model") != "deepseek-chat"
    assert not (Path(report["output_dir"]) / "archive" / "plugins-config.json").exists()
    assert not any(item["kind"] == "model-config" for item in report["items"])


def test_granular_archive_option_archives_only_selected_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["plugins"] = {"entries": [{"id": "custom"}]}
    config["cron"] = {"jobs": [{"id": "nightly"}]}
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            preset="user-data",
            include=("plugins-config",),
        )
    ).migrate()

    archive_dir = Path(report["output_dir"]) / "archive"
    assert (archive_dir / "plugins-config.json").is_file()
    assert not (archive_dir / "cron-config.json").exists()
    assert any(
        item["kind"] == "plugins-config" and item["status"] == "archived"
        for item in report["items"]
    )
    assert not any(item["kind"] == "cron-jobs" for item in report["items"])


def test_imported_skill_without_frontmatter_is_reported_not_loadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    workspace = source / "workspace"
    bad_skill = workspace / "skills" / "no-frontmatter"
    bad_skill.mkdir(parents=True)
    (bad_skill / "SKILL.md").write_text("Missing frontmatter body\n", encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            preset="user-data",
        )
    ).migrate()

    item = next(
        item
        for item in report["items"]
        if item["source"] and item["source"].endswith("no-frontmatter")
    )
    assert item["status"] == "migrated"
    assert item["details"]["opensquilla_loadable"] is False
    assert "missing YAML frontmatter" in item["details"]["compatibility_issues"]


def test_crlf_skill_frontmatter_is_reported_loadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    skill_file = source / "workspace" / "skills" / "demo" / "SKILL.md"
    skill_file.write_text(
        "---\r\nname: demo\r\ndescription: Demo skill\r\n---\r\nDemo body\r\n",
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            preset="user-data",
        )
    ).migrate()

    item = next(item for item in report["items"] if item["kind"] == "skills")
    assert item["details"]["opensquilla_loadable"] is True
    assert item["details"]["compatibility"] == "loadable"


def test_bom_encoded_openclaw_files_are_parsed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "claude-3-5-sonnet-latest"
    config["models"] = {"providers": {"anthropic": {"baseUrl": "https://example.test/v1"}}}
    (source / "openclaw.json").write_text(
        json.dumps(config),
        encoding="utf-8-sig",
    )
    (source / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-bom-secret\n",
        encoding="utf-8-sig",
    )
    (source / "workspace" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nDemo body\n",
        encoding="utf-8-sig",
    )
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["llm"]["provider"] == "anthropic"
    assert config["llm"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert config["llm"]["base_url"] == "https://example.test/v1"
    assert "ANTHROPIC_API_KEY=sk-bom-secret" in (home / ".env").read_text(
        encoding="utf-8"
    )
    item = next(item for item in report["items"] if item["kind"] == "skills")
    assert item["details"]["opensquilla_loadable"] is True


def test_model_config_resolves_object_and_alias_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = {"primary": "Claude Opus 4.6"}
    config["agents"]["defaults"]["models"] = {
        "anthropic/claude-opus-4-6": {"alias": "Claude Opus 4.6"}
    }
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm"]["model"] == "anthropic/claude-opus-4-6"
    item = next(item for item in report["items"] if item["kind"] == "model-config")
    assert item["details"]["requested_model"] == "Claude Opus 4.6"
    assert item["details"]["resolved_from_alias"] is True
    assert item["details"]["source_format"] == "object"


def test_provider_keys_can_migrate_from_provider_config_when_opted_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "openrouter/deepseek/deepseek-v3.1"
    config["models"] = {
        "providers": {
            "openrouter": {
                "apiKey": "sk-or-from-config",
                "baseUrl": "https://openrouter.example.test/api/v1",
            }
        }
    }
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    no_secret_report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "no-secret.toml", apply=True)
    ).migrate()
    no_secret_env = (home / ".env").read_text(encoding="utf-8") if (home / ".env").exists() else ""
    assert "OPENROUTER_API_KEY" not in no_secret_env
    assert "sk-or-from-config" not in json.dumps(no_secret_report)

    secret_report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-from-config" in env_text
    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    # Sparse persistence omits values equal to the built-in default
    # (provider "openrouter"); the effective provider must still resolve
    # to the migrated value.
    assert persisted["llm"].get("provider", "openrouter") == "openrouter"
    assert load_config(config_path).llm.provider == "openrouter"
    assert persisted["llm"]["model"] == "deepseek/deepseek-v3.1"
    assert persisted["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert persisted["llm"]["base_url"] == "https://openrouter.example.test/api/v1"
    item = next(item for item in secret_report["items"] if item["kind"] == "model-config")
    assert item["details"]["source_model"] == "openrouter/deepseek/deepseek-v3.1"
    assert item["details"]["normalized_provider_prefix"] == "openrouter"
    assert "sk-or-from-config" not in json.dumps(secret_report)


def test_zai_and_glm_models_migrate_to_zhipu_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "zai/glm-4.5"
    config["models"] = {
        "providers": {
            "zai": {
                "apiKey": "sk-zai-secret",
                "baseUrl": "https://zhipu.example.test/api/paas/v4",
            }
        }
    }
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm"]["provider"] == "zhipu"
    assert persisted["llm"]["model"] == "glm-4.5"
    assert persisted["llm"]["api_key_env"] == "ZAI_API_KEY"
    assert persisted["llm"]["base_url"] == "https://zhipu.example.test/api/paas/v4"
    build_provider(persisted["llm"]["provider"], persisted["llm"]["model"])


def test_model_provider_conflict_with_existing_tier_profile_is_reported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "anthropic/claude-3-5-sonnet"
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                "",
                "[squilla_router]",
                "enabled = true",
                'tier_profile = "openrouter"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm"]["provider"] == "openrouter"
    assert persisted["llm"]["model"] == "deepseek/deepseek-v4-flash"
    assert persisted["squilla_router"]["tier_profile"] == "openrouter"
    item = next(item for item in report["items"] if item["kind"] == "model-config")
    assert item["details"]["tier_profile_conflict"] == "openrouter"
    assert item["details"]["llm_provider_left_unchanged"] == "openrouter"
    assert item["details"]["llm_model_left_unchanged"] == "deepseek/deepseek-v4-flash"
    assert item["details"]["skipped_model"] == "anthropic/claude-3-5-sonnet"
    assert item["details"]["manual_steps"]


def test_model_provider_conflict_with_direct_provider_preserves_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "anthropic/claude-3-5-sonnet"
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "deepseek"',
                'model = "deepseek-v4-flash"',
                "",
                "[squilla_router]",
                "enabled = true",
                'tier_profile = "deepseek"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm"]["provider"] == "deepseek"
    assert persisted["llm"]["model"] == "deepseek-v4-flash"
    item = next(item for item in report["items"] if item["kind"] == "model-config")
    assert item["details"]["tier_profile_conflict"] == "deepseek"
    assert item["details"]["llm_model_left_unchanged"] == "deepseek-v4-flash"
    assert item["details"]["skipped_model"] == "anthropic/claude-3-5-sonnet"


def test_env_secret_migration_preserves_existing_lines_and_dedupes_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"]["model"] = "openrouter/deepseek/deepseek-v3.1"
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    (source / ".env").write_text("OPENROUTER_API_KEY=sk-new\n", encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "\n".join(
            [
                "# existing operator note",
                "",
                "UNRELATED=value",
                "export OPENROUTER_API_KEY=sk-old",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    env_text = env_path.read_text(encoding="utf-8")
    assert "# existing operator note" in env_text
    assert "UNRELATED=value" in env_text
    assert "OPENROUTER_API_KEY=sk-new" in env_text
    assert "sk-old" not in env_text
    assert env_text.count("OPENROUTER_API_KEY=") == 1


def test_memory_migration_deduplicates_and_archives_overflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    workspace = source / "workspace"
    (workspace / "MEMORY.md").write_text("shared memory\n", encoding="utf-8")
    (workspace / "memory" / "2026-05-10.md").write_text("shared memory\n", encoding="utf-8")
    (workspace / "memory" / "2026-05-11.md").write_text("x" * 90_000, encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    migrated = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert migrated.count("shared memory") == 1
    assert "Migration overflow" in migrated
    overflow = Path(report["output_dir"]) / "archive" / "memory-overflow" / "MEMORY.overflow.md"
    assert overflow.is_file()
    assert "x" in overflow.read_text(encoding="utf-8")
    item = next(item for item in report["items"] if item["kind"] == "memory")
    assert item["details"]["deduplicated_blocks"] == 1
    assert "overflow" not in item["details"]


def test_workspace_text_is_rebranded_and_original_is_archived(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    workspace = source / "workspace"
    (workspace / "SOUL.md").write_text(
        "OpenClaw should read .openclaw paths. ClawdBot helps MoltBot.\n",
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()

    migrated = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert "OpenSquilla should read .opensquilla paths" in migrated
    assert "ClawdBot" not in migrated
    original = Path(report["output_dir"]) / "archive" / "files" / "workspace-original" / "SOUL.md"
    assert original.read_text(encoding="utf-8").startswith("OpenClaw should read .openclaw")
    item = next(item for item in report["items"] if item["kind"] == "soul")
    assert item["details"]["semantic_conversions"] == ["openclaw-branding"]


def test_rebrand_preserves_openclaw_source_references(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    workspace = source / "workspace"
    (workspace / "SOUL.md").write_text(
        "\n".join(
            [
                "你是 OpenClaw 迁移后的助手。",
                "保留 OpenClaw 来源归档用于人工复核。",
                "不要只迁移 openclaw.json。",
                "今天通过真实 OpenClaw CLI onboarding 和 gateway 验证流程。",
                "OpenClaw 的 branding 文本需要语义转换。",
            ]
        ),
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=True)
    ).migrate()

    migrated = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert "你是 OpenSquilla 迁移后的助手。" in migrated
    assert "OpenClaw 来源归档" in migrated
    assert "openclaw.json" in migrated
    assert "OpenClaw CLI onboarding" in migrated
    assert "OpenClaw 的 branding 文本" in migrated


def test_agent_search_and_channel_semantics_are_mapped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["agents"]["defaults"].update(
        {
            "thinkingDefault": "high",
            "compaction": {"mode": "truncate", "model": "legacy-compact-model"},
            "verboseDefault": True,
            "humanDelay": {"minMs": 10, "maxMs": 20},
            "userTimezone": "Asia/Shanghai",
        }
    )
    config["messages"]["telegram"]["allowFrom"] = ["1001", "1002"]
    config["messages"]["discord"]["allowedUsers"] = ["discord-user"]
    config["messages"]["slack"] = {"adminUsers": ["slack-admin"]}
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    (source / ".env").write_text("BRAVE_API_KEY=brave-secret\n", encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=config_path,
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm"]["thinking"] == "high"
    assert persisted["context_overflow_policy"] == "hard_truncate"
    assert persisted["search_provider"] == "brave"
    assert persisted["search_api_key_env"] == "BRAVE_API_KEY"
    assert persisted["channel_admin_senders"] == {"slack": ["slack-admin"]}
    assert "BRAVE_API_KEY=brave-secret" in (home / ".env").read_text(encoding="utf-8")
    notes = Path(report["output_dir"]) / "MIGRATION_NOTES.md"
    assert notes.is_file()
    notes_text = notes.read_text(encoding="utf-8")
    assert "verboseDefault" in notes_text
    assert "humanDelay" in notes_text
    assert "legacy-compact-model" in notes_text
    assert "allowFrom" in notes_text
    assert "allowedUsers" in notes_text


def test_dry_run_report_includes_semantic_notes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    config = json.loads((source / "openclaw.json").read_text(encoding="utf-8"))
    config["messages"]["telegram"]["allowFrom"] = ["1001"]
    (source / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=False)
    ).migrate()

    assert report["apply"] is False
    assert any("allowFrom" in note for note in report["notes"])
    assert not Path(report["output_dir"], "MIGRATION_NOTES.md").exists()


def test_rebranded_daily_memory_original_is_archived(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    workspace = source / "workspace"
    (workspace / "MEMORY.md").write_text("stable memory\n", encoding="utf-8")
    (workspace / "memory" / "2026-05-12.md").write_text(
        "OpenClaw daily memory\n",
        encoding="utf-8",
    )
    home = tmp_path / "opensquilla-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "config.toml", apply=True)
    ).migrate()

    migrated = (home / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    assert "OpenSquilla daily memory" in migrated
    original = (
        Path(report["output_dir"])
        / "archive"
        / "files"
        / "workspace-original"
        / "memory"
        / "2026-05-12.md"
    )
    assert original.read_text(encoding="utf-8") == "OpenClaw daily memory\n"


def test_migrated_workspace_and_loadable_skills_are_consumed_by_opensquilla(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            preset="user-data",
        )
    ).migrate()

    context = load_context_files(str(home / "workspace"))
    assert context.soul == "opensquilla soul\n"
    assert context.agents == "agent rules\n"
    assert context.user == "user profile\n"
    assert "core memory" in (context.memory or "")
    assert "daily note" in (context.memory or "")

    loader = SkillLoader(
        bundled_dir=None,
        workspace_dir=None,
        managed_dir=None,
        personal_agents_dir=None,
        project_agents_dir=None,
        extra_dirs=[home / "skills" / "openclaw-imports"],
        snapshot_path=tmp_path / "skills_snapshot.json",
    )
    skills = loader.load_all()
    assert [(skill.name, skill.layer) for skill in skills] == [("demo", SkillLayer.EXTRA)]
    assert skills[0].description == "Demo skill"
