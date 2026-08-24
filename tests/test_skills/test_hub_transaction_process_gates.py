from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    recover_pending_skill_transaction,
    rollback_root,
    staging_root,
)

_CRASH_EXIT_CODE = 73
_LOCK_BUSY_EXIT_CODE = 75
_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
_CRASH_WORKER = _FIXTURE_ROOT / "skill_transaction_crash_worker.py"
_LEASE_WORKER = _FIXTURE_ROOT / "skill_profile_lease_worker.py"


def _write_skill(path: Path, marker: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        (
            "---\n"
            "name: example\n"
            f"description: {marker} process fixture\n"
            "---\n"
            f"{marker}\n"
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "crash_phase",
    ["prepared", "old_moved", "new_moved", "lock_written"],
)
def test_real_process_crash_recovers_previous_tree_and_lock(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    lockfile_path = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    old_tree_digest = compute_tree_sha256(target)
    lockfile = Lockfile()
    lockfile.add(
        "example",
        LockEntry(
            source="crash-source",
            identifier="example",
            version="old",
            path=str(target),
            requested_identifier="example",
            resolved_revision="old-revision",
            tree_sha256=old_tree_digest,
            source_package_id="crash-source:example",
        ),
    )
    lockfile.save(lockfile_path)
    old_lock_bytes = lockfile_path.read_bytes()

    crashed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WORKER),
            str(managed),
            str(lockfile_path),
            str(journal_path),
            crash_phase,
        ],
        check=False,
        timeout=20,
    )

    assert crashed.returncode == _CRASH_EXIT_CODE
    assert journal_path.is_file()
    journal = SkillTransactionJournal.load(journal_path)
    assert journal is not None
    assert journal.phase == crash_phase
    staging = Path(journal.staging)
    rollback = Path(journal.rollback)
    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile_path,
        journal_path=journal_path,
    )
    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert diagnostics[0].details["phase"] == crash_phase
    assert compute_tree_sha256(target) == old_tree_digest
    assert "old" in (target / "SKILL.md").read_text(encoding="utf-8")
    assert lockfile_path.read_bytes() == old_lock_bytes
    assert not staging.exists()
    assert not rollback.exists()
    assert not staging.parent.exists()
    assert not rollback.parent.exists()
    assert not journal_path.exists()


def test_real_process_crash_before_journal_sweeps_orphan_reservation(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    lockfile_path = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    old_tree_digest = compute_tree_sha256(target)
    lockfile = Lockfile()
    lockfile.add(
        "example",
        LockEntry(
            source="crash-source",
            identifier="example",
            version="old",
            path=str(target),
            requested_identifier="example",
            resolved_revision="old-revision",
            tree_sha256=old_tree_digest,
            source_package_id="crash-source:example",
        ),
    )
    lockfile.save(lockfile_path)
    old_lock_bytes = lockfile_path.read_bytes()

    crashed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WORKER),
            str(managed),
            str(lockfile_path),
            str(journal_path),
            "pre_journal",
        ],
        check=False,
        timeout=20,
    )

    assert crashed.returncode == _CRASH_EXIT_CODE
    assert not journal_path.exists()
    assert len(list((managed / ".opensquilla-staging").iterdir())) == 1
    assert len(list((managed / ".opensquilla-rollback").iterdir())) == 1

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile_path,
        journal_path=journal_path,
        sweep_orphan_staging=True,
    )

    assert [item.code for item in diagnostics] == ["ORPHAN_STAGING_RECOVERED"]
    assert compute_tree_sha256(target) == old_tree_digest
    assert lockfile_path.read_bytes() == old_lock_bytes
    assert list((managed / ".opensquilla-staging").iterdir()) == []
    assert list((managed / ".opensquilla-rollback").iterdir()) == []


def _worker_environment(state_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["OPENSQUILLA_USER_STATE_DIR"] = str(state_root)
    environment["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    return environment


def _wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10.0
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError(f"lease holder exited early with {process.returncode}")
        if time.monotonic() >= deadline:
            raise AssertionError("lease holder did not publish its ready marker")
        time.sleep(0.01)


def _release_lease_holder(process: subprocess.Popen[bytes]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_gateway_profile_lease_blocks_offline_process_then_allows_install(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    state_root = tmp_path / "user-state"
    managed = profile_home / "skills"
    lockfile = profile_home / "skills-lock.json"
    journal = profile_home / "state" / "skill-transaction.json"
    ready = tmp_path / "lease-ready"
    installed = tmp_path / "installed"
    environment = _worker_environment(state_root)
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_LEASE_WORKER),
            "hold",
            str(profile_home),
            str(ready),
        ],
        env=environment,
        stdin=subprocess.PIPE,
    )
    try:
        _wait_for_file(ready, holder)
        blocked = subprocess.run(
            [
                sys.executable,
                str(_LEASE_WORKER),
                "install",
                str(profile_home),
                str(managed),
                str(lockfile),
                str(journal),
                str(installed),
            ],
            check=False,
            env=environment,
            timeout=20,
        )
        assert blocked.returncode == _LOCK_BUSY_EXIT_CODE
        assert not installed.exists()
        assert not lockfile.exists()
        assert not journal.exists()
        assert not (managed / "process-skill").exists()
    finally:
        _release_lease_holder(holder)
    assert holder.returncode == 0

    succeeded = subprocess.run(
        [
            sys.executable,
            str(_LEASE_WORKER),
            "install",
            str(profile_home),
            str(managed),
            str(lockfile),
            str(journal),
            str(installed),
        ],
        check=False,
        env=environment,
        timeout=20,
    )

    assert succeeded.returncode == 0
    assert installed.read_text(encoding="utf-8") == "process-skill"
    assert (managed / "process-skill" / "SKILL.md").is_file()
    assert Lockfile.load(lockfile).get("process-skill") is not None
    assert not journal.exists()


def test_unleased_build_services_does_not_sweep_another_process_reservation(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    state_root = tmp_path / "user-state"
    managed = profile_home / "skills"
    transaction_id = "a" * 32
    orphan_reservation = staging_root(managed) / transaction_id
    rollback_reservation = rollback_root(managed) / transaction_id
    (orphan_reservation / "_candidate").mkdir(parents=True)
    (orphan_reservation / "_candidate" / "SKILL.md").write_text(
        "---\nname: lease-probe\ndescription: lease probe\n---\nprobe\n",
        encoding="utf-8",
    )
    rollback_reservation.mkdir(parents=True)

    ready = tmp_path / "lease-ready"
    probe = tmp_path / "boot-probe.json"
    environment = _worker_environment(state_root)
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_LEASE_WORKER),
            "hold",
            str(profile_home),
            str(ready),
        ],
        env=environment,
        stdin=subprocess.PIPE,
    )
    try:
        _wait_for_file(ready, holder)
        unleased_builder = subprocess.run(
            [
                sys.executable,
                str(_LEASE_WORKER),
                "boot_probe",
                str(profile_home),
                str(managed),
                str(orphan_reservation),
                str(probe),
            ],
            check=False,
            env=environment,
            timeout=30,
        )
        assert unleased_builder.returncode == 0
        assert orphan_reservation.is_dir()
        assert rollback_reservation.is_dir()
        assert "PROFILE_LEASE_REQUIRED" in probe.read_text(encoding="utf-8")
    finally:
        _release_lease_holder(holder)
    assert holder.returncode == 0


def test_two_spawn_offline_writers_allow_exactly_one_transaction(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    state_root = tmp_path / "user-state"
    managed = profile_home / "skills"
    lockfile = profile_home / "skills-lock.json"
    journal = profile_home / "state" / "skill-transaction.json"
    first_marker = tmp_path / "first-installed"
    second_marker = tmp_path / "second-installed"
    first_ready = tmp_path / "first-ready"
    first_release = tmp_path / "first-release"
    environment = _worker_environment(state_root)
    first = subprocess.Popen(
        [
            sys.executable,
            str(_LEASE_WORKER),
            "install_wait",
            str(profile_home),
            str(managed),
            str(lockfile),
            str(journal),
            str(first_marker),
            str(first_ready),
            str(first_release),
        ],
        env=environment,
    )
    try:
        _wait_for_file(first_ready, first)
        second = subprocess.run(
            [
                sys.executable,
                str(_LEASE_WORKER),
                "install",
                str(profile_home),
                str(managed),
                str(lockfile),
                str(journal),
                str(second_marker),
            ],
            check=False,
            env=environment,
            timeout=20,
        )
        assert second.returncode == _LOCK_BUSY_EXIT_CODE
        assert not second_marker.exists()
    finally:
        first_release.write_text("release", encoding="utf-8")
        try:
            first.wait(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=5)

    assert first.returncode == 0
    assert first_marker.read_text(encoding="utf-8") == "process-skill"
    entry = Lockfile.load(lockfile).get("process-skill")
    assert entry is not None
    assert compute_tree_sha256(managed / "process-skill") == entry.tree_sha256
    assert not journal.exists()
