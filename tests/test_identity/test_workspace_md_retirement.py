from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from opensquilla.gateway import workspace_template_upgrade as upgrade
from opensquilla.identity.bootstrap import RETIRED_WORKSPACE_FILENAMES, ensure_agent_workspace
from opensquilla.identity.workspace import (
    filter_workspace_filenames_for_session,
    load_workspace_files,
    load_workspace_files_budgeted_with_report,
)
from opensquilla.profile_operation_lock import ProfileOperationLock

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workspace_md_retirement"


def old_default(name: str) -> bytes:
    return (FIXTURES / name.replace(".md", ".txt")).read_bytes()


@pytest.fixture
def seeded(tmp_path):
    root = tmp_path / "workspace"
    ensure_agent_workspace(root)
    for name in ("AGENTS.md", "SOUL.md"):
        (root / name).write_bytes(old_default(name))
    return root, tmp_path / "profile"


def run_upgrade(root, home):
    with ProfileOperationLock(home):
        return upgrade.upgrade_workspace_defaults(root, profile_home=home)


def test_old_files_and_onboarding_state_are_never_changed(tmp_path):
    state = tmp_path / ".opensquilla" / "workspace-state.json"
    state.parent.mkdir()
    state.write_bytes(b'{"bootstrap_seeded_at":"old","custom":true}')
    for name in RETIRED_WORKSPACE_FILENAMES:
        (tmp_path / name).write_bytes(b"old implicit instructions")
    for _ in range(2):
        ensure_agent_workspace(tmp_path)
        assert state.read_bytes() == b'{"bootstrap_seeded_at":"old","custom":true}'
        assert not RETIRED_WORKSPACE_FILENAMES.intersection(load_workspace_files(tmp_path))
        for name in RETIRED_WORKSPACE_FILENAMES:
            assert (tmp_path / name).read_bytes() == b"old implicit instructions"


@pytest.mark.parametrize("filenames", [(), [], tuple(RETIRED_WORKSPACE_FILENAMES)])
def test_empty_or_retired_selection_never_reads_files(tmp_path, monkeypatch, filenames):
    ensure_agent_workspace(tmp_path)

    def forbidden(_path):
        pytest.fail("empty/retired selection must not read a file")

    monkeypatch.setattr("opensquilla.identity.workspace._read_file_sync", forbidden)
    assert load_workspace_files(tmp_path, filenames=filenames) == {}
    assert load_workspace_files_budgeted_with_report(tmp_path, filenames=filenames) == ({}, [])
    assert filter_workspace_filenames_for_session(filenames, None) == ()


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (None, ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md")),
        ("cron:job", ("AGENTS.md",)),
        ("agent:main:subagent:one", ("AGENTS.md", "SOUL.md")),
        ("agent:main:group:one", ("AGENTS.md", "SOUL.md")),
    ],
)
def test_retained_session_file_contract(session, expected):
    assert filter_workspace_filenames_for_session(None, session) == expected


@pytest.mark.parametrize("variant", ["plain", "bom", "crlf", "bom-crlf"])
def test_known_defaults_are_backed_up_and_updated_once(seeded, variant):
    root, home = seeded
    originals = {}
    for name in ("AGENTS.md", "SOUL.md"):
        data = old_default(name)
        if "crlf" in variant:
            data = data.replace(b"\n", b"\r\n")
        if "bom" in variant:
            data = b"\xef\xbb\xbf" + data
        (root / name).write_bytes(data)
        originals[name] = data
    results = run_upgrade(root, home)
    assert [r.status for r in results] == ["updated", "updated"]
    assert (root / "AGENTS.md").read_bytes() == b""
    assert b"`TOOLS.md`" not in (root / "SOUL.md").read_bytes()
    assert b"`AGENTS.md`" in (root / "SOUL.md").read_bytes()
    for result in results:
        assert result.backup_path.read_bytes() == originals[result.filename]
        assert result.backup_path.parent == root / ".opensquilla/template-backups/md-retirement-v1"
        if os.name != "nt":
            assert result.backup_path.stat().st_mode & 0o777 == 0o600
    before = sorted(root.rglob("*.bak"))
    assert all(r.status == "unchanged" for r in run_upgrade(root, home))
    assert sorted(root.rglob("*.bak")) == before
    assert not (root / ".opensquilla/workspace-state.json").exists()


@pytest.mark.parametrize("addition", [b"\n", b" ", b"\nMy rule\n"])
def test_even_small_custom_edits_are_not_upgraded(seeded, addition):
    root, home = seeded
    custom = old_default("AGENTS.md") + addition
    (root / "AGENTS.md").write_bytes(custom)
    result = run_upgrade(root, home)[0]
    assert result.status == "unchanged"
    assert (root / "AGENTS.md").read_bytes() == custom


def test_without_profile_lease_does_not_write_or_backup(seeded):
    root, home = seeded
    results = upgrade.upgrade_workspace_defaults(root, profile_home=home)
    assert all(r.reason == "profile-lease-required" for r in results)
    assert (root / "AGENTS.md").read_bytes() == old_default("AGENTS.md")
    assert not (root / ".opensquilla").exists()


def test_backup_failure_keeps_original_and_other_file_can_upgrade(seeded, monkeypatch):
    root, home = seeded
    original = upgrade._write_backup

    def fail_agents(path, data):
        if path.name.startswith("AGENTS.md"):
            raise PermissionError("backup denied")
        return original(path, data)

    monkeypatch.setattr(upgrade, "_write_backup", fail_agents)
    results = run_upgrade(root, home)
    assert [r.status for r in results] == ["skipped", "updated"]
    assert (root / "AGENTS.md").read_bytes() == old_default("AGENTS.md")


def test_concurrent_edit_is_detected_before_publication(seeded, monkeypatch):
    root, home = seeded
    original = upgrade._write_backup

    def edit_after_backup(path, data):
        original(path, data)
        if path.name.startswith("AGENTS.md"):
            (root / "AGENTS.md").write_bytes(b"concurrent custom rules")

    monkeypatch.setattr(upgrade, "_write_backup", edit_after_backup)
    result = run_upgrade(root, home)[0]
    assert result.status == "skipped"
    assert result.reason == "ConfigChangedError"
    assert (root / "AGENTS.md").read_bytes() == b"concurrent custom rules"
    assert result.backup_path.read_bytes() == old_default("AGENTS.md")


def test_readonly_file_is_not_replaced(seeded):
    root, home = seeded
    target = root / "AGENTS.md"
    target.chmod(0o400)
    try:
        assert run_upgrade(root, home)[0].reason == "read-only"
        assert target.read_bytes() == old_default("AGENTS.md")
    finally:
        target.chmod(0o600)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "backup-directory", "workspace"])
def test_redirected_or_linked_files_are_not_upgraded(seeded, kind):
    root, home = seeded
    outside = root.parent / "outside"
    outside.mkdir()
    original = outside / "AGENTS.md"
    original.write_bytes(old_default("AGENTS.md"))
    target = root / "AGENTS.md"
    try:
        if kind == "symlink":
            target.unlink()
            target.symlink_to(original)
        elif kind == "hardlink":
            target.unlink()
            os.link(original, target)
        elif kind == "backup-directory":
            (root / ".opensquilla").symlink_to(outside, target_is_directory=True)
        else:
            alias = root.parent / "alias"
            alias.symlink_to(root, target_is_directory=True)
            root = alias
    except OSError as exc:
        pytest.skip(f"filesystem link capability unavailable: {exc}")
    assert run_upgrade(root, home)[0].status == "skipped"
    assert target.read_bytes() == old_default("AGENTS.md")
    assert original.read_bytes() == old_default("AGENTS.md")
    assert sorted(p.name for p in outside.iterdir()) == ["AGENTS.md"]


def test_conflicting_existing_backup_is_never_overwritten(seeded):
    root, home = seeded
    data = old_default("AGENTS.md")
    backup = root / ".opensquilla/template-backups/md-retirement-v1"
    backup.mkdir(parents=True)
    target = backup / f"AGENTS.md.{hashlib.sha256(data).hexdigest()}.bak"
    target.write_bytes(b"unrelated existing backup")
    assert run_upgrade(root, home)[0].status == "skipped"
    assert (root / "AGENTS.md").read_bytes() == data
    assert target.read_bytes() == b"unrelated existing backup"


def test_backup_reused_after_interrupted_preparation(seeded, monkeypatch):
    root, home = seeded
    with monkeypatch.context() as patch:
        patch.setattr(upgrade.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError()))
        results = run_upgrade(root, home)
    assert all(r.status == "skipped" for r in results)
    backups = sorted(root.rglob("*.bak"))
    assert len(backups) == 2
    assert all(r.status == "updated" for r in run_upgrade(root, home))
    assert sorted(root.rglob("*.bak")) == backups
