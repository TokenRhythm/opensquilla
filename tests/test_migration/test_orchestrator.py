"""Tests for shared migration orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.migration import orchestrator


def test_run_migration_batch_uses_canonical_source_order(monkeypatch, tmp_path):
    detected = [
        orchestrator.DetectedMigrationSource("openclaw", tmp_path / ".openclaw"),
        orchestrator.DetectedMigrationSource("hermes", tmp_path / ".hermes"),
    ]
    calls: list[tuple[str, Path]] = []

    def fake_run_one_migration(name, source_path, options, **_kwargs):
        calls.append((name, source_path))
        return {
            "output_dir": str(tmp_path / "reports" / name),
            "items": [{"kind": "config", "status": "planned"}],
        }

    monkeypatch.setattr(orchestrator, "run_one_migration", fake_run_one_migration)

    result = orchestrator.run_migration_batch(
        detected,
        ["hermes", "openclaw"],
        orchestrator.MigrationBatchOptions(apply=False),
    )

    assert result.selected == ("openclaw", "hermes")
    assert [name for name, _path in calls] == ["openclaw", "hermes"]
    assert result.has_error is False


def test_run_migration_batch_validates_all_sources_before_running(monkeypatch, tmp_path):
    detected = [
        orchestrator.DetectedMigrationSource("openclaw", tmp_path / ".openclaw"),
        orchestrator.DetectedMigrationSource("hermes", tmp_path / ".hermes"),
    ]

    def fake_run_one_migration(*_args, **_kwargs):
        raise AssertionError("should validate before running any migrator")

    monkeypatch.setattr(orchestrator, "run_one_migration", fake_run_one_migration)

    with pytest.raises(orchestrator.MigrationOptionError):
        orchestrator.run_migration_batch(
            detected,
            ["openclaw", "hermes"],
            orchestrator.MigrationBatchOptions(persona_conflict="bogus"),
        )


def test_opensquilla_batch_rejects_custom_config_path(tmp_path: Path) -> None:
    with pytest.raises(orchestrator.MigrationOptionError, match="--config"):
        orchestrator.validate_batch_options(
            ("opensquilla",),
            orchestrator.MigrationBatchOptions(config=tmp_path / "custom.toml"),
        )


def test_detected_portable_kind_reaches_opensquilla_migrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    portable_base = tmp_path / "local-app-data"
    portable = portable_base / "OpenSquilla" / "portable" / "dummy-release"
    portable.mkdir(parents=True)
    (portable / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("LOCALAPPDATA", str(portable_base))
    monkeypatch.delenv("TEMP", raising=False)
    target = tmp_path / "target-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))

    detected = orchestrator.detect_default_sources()

    assert detected == [
        orchestrator.DetectedMigrationSource(
            "opensquilla", portable, source_kind="windows-portable"
        )
    ]
    result = orchestrator.run_migration_batch(
        detected,
        ["opensquilla"],
        orchestrator.MigrationBatchOptions(
            config=target / "config.toml",
            apply=False,
        ),
    )
    assert result.reports["opensquilla"]["source_kind"] == "windows-portable"


def test_detected_desktop_kind_reaches_opensquilla_migrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    desktop = tmp_path / "desktop-home"
    desktop.mkdir()
    (desktop / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    target = tmp_path / "target-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))
    monkeypatch.setattr(
        "opensquilla.migration.opensquilla_home.detect_desktop_home",
        lambda _target=None: desktop,
    )

    detected = orchestrator.detect_default_sources()

    assert detected == [
        orchestrator.DetectedMigrationSource("opensquilla", desktop, source_kind="desktop-home")
    ]
    result = orchestrator.run_migration_batch(
        detected,
        ["opensquilla"],
        orchestrator.MigrationBatchOptions(
            config=target / "config.toml",
            apply=False,
        ),
    )
    assert result.reports["opensquilla"]["source_kind"] == "desktop-home"
