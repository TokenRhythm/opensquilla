from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from opensquilla.config_version import LATEST_CONFIG_VERSION
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
        f"config_version = {LATEST_CONFIG_VERSION}\n"
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
    assert (home / "config.toml").read_text(encoding="utf-8") == (
        f"config_version = {LATEST_CONFIG_VERSION}\n"
    )
    corrupt = _corrupt_configs(home)
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "state_dir = 7\n"


def test_recover_config_ignores_link_shaped_backups(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    good = _valid_config(home)
    target = tmp_path / "outside.toml"
    target.write_text(good, encoding="utf-8")
    try:
        (home / "config.toml.backup.20260301000000000000").symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise
    (home / "config.toml").write_text("workspace_dir = [\n", encoding="utf-8")

    report = recover_config(home)

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == (
        f"config_version = {LATEST_CONFIG_VERSION}\n"
    )


def test_recover_config_refuses_schema_too_new(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    (home / "config.toml.backup.20260101000000000000").write_text(
        _valid_config(home), encoding="utf-8"
    )
    future_config = f"config_version = {LATEST_CONFIG_VERSION + 1}\n"
    (home / "config.toml").write_text(future_config, encoding="utf-8")

    before = inspect_profile(home)
    assert before.stable_code == "config_schema_too_new"
    assert "recover-config" not in before.allowed_actions

    with pytest.raises(RecoveryError) as excinfo:
        recover_config(home)
    assert excinfo.value.stable_code == "config_recovery_not_applicable"
    assert (home / "config.toml").read_text(encoding="utf-8") == future_config
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


def test_interrupted_recovery_remains_repairable(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.recovery import config_recovery

    home = _profile(tmp_path)
    original = b"workspace_dir = [\n"
    (home / "config.toml").write_bytes(original)
    (home / "state" / "chat.json").write_bytes(b"synthetic history")
    good = _valid_config(home)
    (home / "config.toml.backup.20260101000000000000").write_text(good, encoding="utf-8")
    park = config_recovery._park_corrupt_config

    def interrupt(path: Path) -> Path:
        park(path)
        raise RuntimeError("synthetic crash after preserving corrupt config")

    with monkeypatch.context() as patch:
        patch.setattr(config_recovery, "_park_corrupt_config", interrupt)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            recover_config(home)
    assert not (home / "config.toml").exists()
    assert _corrupt_configs(home)[0].read_bytes() == original
    assert "recover-config" in inspect_profile(home).allowed_actions
    for _ in range(3):
        assert recover_config(home).outcome == "ready"
        assert (home / "config.toml").read_text(encoding="utf-8") == good
        assert (home / "state" / "chat.json").read_bytes() == b"synthetic history"
        assert (home / "workspace" / "SOUL.md").read_text() == "synthetic identity\n"
    assert len(_corrupt_configs(home)) == 1


def test_old_interrupted_recovery_without_backup_can_resume(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    evidence = home / "config.toml.corrupt.20261116010203000000"
    evidence.write_bytes(b"broken = [\n")
    assert "recover-config" in inspect_profile(home).allowed_actions
    assert recover_config(home).outcome == "ready"
    assert evidence.read_bytes() == b"broken = [\n"


def test_unknown_missing_config_is_not_automatically_repaired(tmp_path: Path) -> None:
    home = tmp_path / "unknown"
    home.mkdir()
    (home / "unrecognized-data").write_bytes(b"preserve me")
    (home / "config.toml.corrupt.not-a-recovery-stamp").write_bytes(b"broken = [\n")
    assert "recover-config" not in inspect_profile(home).allowed_actions
    recover_config(home)
    assert not (home / "config.toml").exists()


def test_replacement_write_failure_does_not_park_original(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.recovery import config_recovery

    home = _profile(tmp_path)
    original = b"broken = [\n"
    (home / "config.toml").write_bytes(original)

    def fail(_path: Path, _data: bytes) -> None:
        raise OSError("synthetic disk full")

    monkeypatch.setattr(config_recovery, "_write_config_no_replace", fail)
    with pytest.raises(OSError, match="synthetic disk full"):
        recover_config(home)
    assert (home / "config.toml").read_bytes() == original
    assert _corrupt_configs(home) == []
    assert not list(home.glob(".config.toml.recovery-*.tmp"))


def test_missing_config_recovery_does_not_follow_hardlinked_evidence(tmp_path: Path) -> None:
    home = _profile(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"broken = [\n")
    os.link(outside, home / "config.toml.corrupt.20261116010203000000")
    assert "recover-config" not in inspect_profile(home).allowed_actions
    recover_config(home)
    assert not (home / "config.toml").exists()
    assert outside.read_bytes() == b"broken = [\n"


def test_recovery_never_replaces_competing_config(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.recovery import config_recovery

    home = _profile(tmp_path)
    (home / "config.toml").write_bytes(b"broken = [\n")
    park = config_recovery._park_corrupt_config
    competing = _valid_config(home).encode()

    def race(path: Path) -> Path:
        parked = park(path)
        path.write_bytes(competing)
        return parked

    monkeypatch.setattr(config_recovery, "_park_corrupt_config", race)
    with pytest.raises(RecoveryError):
        recover_config(home)
    assert (home / "config.toml").read_bytes() == competing
    assert _corrupt_configs(home)[0].read_bytes() == b"broken = [\n"
    assert not list(home.glob(".config.toml.recovery-*.tmp"))
