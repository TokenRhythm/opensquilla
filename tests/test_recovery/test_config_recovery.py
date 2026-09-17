from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.recovery import RecoveryError, inspect_profile, recover_config


@pytest.fixture(autouse=True)
def _isolated_profile_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))


def _profile(tmp_path: Path) -> Path:
    home = tmp_path / "profile"
    workspace = home / "workspace"
    state = home / "state"
    workspace.mkdir(parents=True)
    state.mkdir()
    (workspace / "SOUL.md").write_text("synthetic identity\n", encoding="utf-8")
    return home


def _valid_config(home: Path) -> str:
    return (
        "config_version = 1\n"
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(home / 'workspace'))}\n"
    )


def _corrupt_configs(home: Path) -> list[Path]:
    return sorted(home.glob("config.toml.corrupt.*"))


def test_recover_config_restores_newest_valid_backup_and_preserves_corrupt_file(
    tmp_path: Path,
) -> None:
    home = _profile(tmp_path)
    good = _valid_config(home)
    (home / "config.toml.backup.20260101000000000000").write_text(
        'search_provider = "stale"\n' + good, encoding="utf-8"
    )
    (home / "config.toml.backup.20260201000000000000").write_text(good, encoding="utf-8")
    (home / "config.toml").write_text("workspace_dir = [\n", encoding="utf-8")

    before = inspect_profile(home)
    assert before.outcome == "attention"
    assert before.stable_code == "config_invalid"
    assert "recover-config" in before.allowed_actions

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == good
    corrupt = _corrupt_configs(home)
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "workspace_dir = [\n"


def test_recover_config_skips_newer_unparseable_backup(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    good = _valid_config(home)
    (home / "config.toml.backup.20260101000000000000").write_text(good, encoding="utf-8")
    (home / "config.toml.backup.20260301000000000000").write_text(
        "also = corrupt [\n", encoding="utf-8"
    )
    (home / "config.toml").write_text("\x00broken", encoding="utf-8")

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == good


def test_recover_config_without_backup_keeps_evidence_and_writes_defaults(
    tmp_path: Path,
) -> None:
    home = _profile(tmp_path)
    (home / "config.toml").write_text("state_dir = 7\n", encoding="utf-8")

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == "config_version = 1\n"
    corrupt = _corrupt_configs(home)
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "state_dir = 7\n"


def test_recover_config_ignores_link_shaped_backups(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    good = _valid_config(home)
    target = tmp_path / "outside.toml"
    target.write_text(good, encoding="utf-8")
    (home / "config.toml.backup.20260301000000000000").symlink_to(target)
    (home / "config.toml").write_text("workspace_dir = [\n", encoding="utf-8")

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == "config_version = 1\n"


def test_recover_config_refuses_schema_too_new(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    (home / "config.toml.backup.20260101000000000000").write_text(
        _valid_config(home), encoding="utf-8"
    )
    (home / "config.toml").write_text("config_version = 99\n", encoding="utf-8")

    before = inspect_profile(home)
    assert before.stable_code == "config_schema_too_new"
    assert "recover-config" not in before.allowed_actions

    with pytest.raises(RecoveryError) as excinfo:
        recover_config(home)
    assert excinfo.value.stable_code == "config_recovery_not_applicable"
    assert (home / "config.toml").read_text(encoding="utf-8") == "config_version = 99\n"
    assert _corrupt_configs(home) == []


def test_recover_config_is_a_no_op_on_a_healthy_profile(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    good = _valid_config(home)
    (home / "config.toml").write_text(good, encoding="utf-8")

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == good
    assert _corrupt_configs(home) == []


def test_config_unreadable_report_offers_recover_config(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    (home / "config.toml").write_text("workspace_dir = [\n", encoding="utf-8")

    report = inspect_profile(home)

    assert report.stable_code == "config_invalid"
    assert report.allowed_actions[0] == "recover-config"
