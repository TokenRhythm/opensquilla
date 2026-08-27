from __future__ import annotations

import inspect
import json
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

import opensquilla.sandbox.upgrade_migration as upgrade_migration
from opensquilla.sandbox.upgrade_migration import SandboxUpgradeCoordinator


@pytest.mark.parametrize(
    ("legacy_mode", "canonical", "expected_status"),
    [
        ("standard", "safe", "committed"),
        ("trusted", "safe", "committed"),
        ("managed", "safe", "committed"),
        ("full", "full", "not_required"),
    ],
)
def test_direct_update_preserves_comments_and_unknown_fields(
    tmp_path: Path,
    legacy_mode: str,
    canonical: str,
    expected_status: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        (
            "# retained comment\n"
            'unknown_top = "keep"\n\n'
            "[sandbox]\n"
            f'run_mode = "{legacy_mode}" # retained inline\n'
            "mystery = 42\n"
        ),
        encoding="utf-8",
    )
    preferences = tmp_path / "desktop-preferences.json"
    preferences.write_text(
        json.dumps({"runMode": legacy_mode, "unknown": {"keep": True}}),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == expected_status
    assert report.canonical_mode == canonical
    text = config.read_text(encoding="utf-8")
    assert "# retained comment" in text
    assert "# retained inline" in text
    parsed = tomllib.loads(text)
    assert parsed["sandbox"]["run_mode"] == canonical
    assert parsed["sandbox"]["mystery"] == 42
    assert parsed["unknown_top"] == "keep"
    assert json.loads(preferences.read_text(encoding="utf-8")) == {
        "runMode": canonical,
        "unknown": {"keep": True},
    }


def test_already_canonical_profile_performs_no_disk_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "safe"\n', encoding="utf-8")
    preferences = tmp_path / "preferences.json"
    preferences.write_text('{"runMode":"full"}', encoding="utf-8")
    before = {path: path.read_bytes() for path in (config, preferences)}

    def unexpected_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("canonical migration must not write")

    monkeypatch.setattr(upgrade_migration, "_atomic_write", unexpected_write)
    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "not_required"
    assert {path: path.read_bytes() for path in before} == before
    assert not (tmp_path / upgrade_migration.SNAPSHOT_NAME).exists()
    assert not (tmp_path / upgrade_migration.JOURNAL_NAME).exists()


def test_missing_profile_is_not_created_by_optional_migration(tmp_path: Path) -> None:
    missing_home = tmp_path / "not-installed"

    report = SandboxUpgradeCoordinator(missing_home).run()

    assert report.ok is True
    assert report.status == "not_required"
    assert not missing_home.exists()


def test_unrelated_config_is_not_given_a_sandbox_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    original = b'agents = [{ id = "qa", name = "QA" }]\n'
    config.write_bytes(original)
    monkeypatch.setattr(
        upgrade_migration,
        "_atomic_write",
        lambda *_args, **_kwargs: pytest.fail("unrelated config was rewritten"),
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.status == "not_required"
    assert config.read_bytes() == original


def test_only_changed_config_files_are_replaced_and_sessions_db_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "trusted"\n', encoding="utf-8")
    preferences = tmp_path / "preferences.json"
    preferences.write_text('{"runMode":"safe"}', encoding="utf-8")
    database = tmp_path / "state" / "sessions.db"
    database.parent.mkdir()
    database.write_bytes(b"in-use-session-data")
    writes: list[str] = []
    real_atomic_write = upgrade_migration._atomic_write

    def record_write(path: Path, payload: bytes) -> None:
        writes.append(path.name)
        real_atomic_write(path, payload)

    monkeypatch.setattr(upgrade_migration, "_atomic_write", record_write)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert writes == ["config.toml"]
    assert database.read_bytes() == b"in-use-session-data"
    assert database not in upgrade_migration.inventory_sandbox_stores(tmp_path)
    assert not (tmp_path / upgrade_migration.SNAPSHOT_NAME).exists()


def test_atomic_replace_failure_leaves_target_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    original = b'[sandbox]\nrun_mode = "trusted"\n'
    target.write_bytes(original)

    def fail_replace(_source: object, _destination: object) -> None:
        raise PermissionError("file is held by a residual process")

    monkeypatch.setattr(upgrade_migration.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="residual process"):
        upgrade_migration._atomic_write(target, b'[sandbox]\nrun_mode = "safe"\n')

    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".config.toml.*"))


def test_coordinator_reports_retry_without_corrupting_locked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    original = b'[sandbox]\nrun_mode = "standard"\n'
    config.write_bytes(original)
    monkeypatch.setattr(
        upgrade_migration,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("sharing violation")),
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "retry_required"
    assert "sharing violation" in str(report.error)
    assert config.read_bytes() == original


def test_busy_profile_lock_returns_immediately_for_a_later_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    original = b'[sandbox]\nrun_mode = "trusted"\n'
    config.write_bytes(original)
    observed_timeouts: list[float] = []

    @contextmanager
    def busy_lock(_home: Path, *, timeout: float) -> Iterator[None]:
        observed_timeouts.append(timeout)
        raise TimeoutError("old gateway still owns the profile lock")
        yield

    monkeypatch.setattr("opensquilla.recovery.locking.acquire_profile_locks", busy_lock)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert observed_timeouts == [0.0]
    assert report.ok is False
    assert report.status == "retry_required"
    assert config.read_bytes() == original


def test_prepared_legacy_artifacts_resume_idempotently_then_cleanup(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "standard"\n', encoding="utf-8")
    snapshot = tmp_path / upgrade_migration.SNAPSHOT_NAME
    snapshot.mkdir()
    (snapshot / "config.toml").write_bytes(config.read_bytes())
    journal = tmp_path / upgrade_migration.JOURNAL_NAME
    journal.write_text(
        json.dumps({"migrationVersion": 2, "status": "prepared"}),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "committed"
    assert 'run_mode = "safe"' in config.read_text(encoding="utf-8")
    assert not snapshot.exists()
    assert not journal.exists()


def test_committed_journal_does_not_require_snapshot_or_acl(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "safe"\n', encoding="utf-8")
    journal = tmp_path / upgrade_migration.JOURNAL_NAME
    journal.write_text(
        json.dumps({"migrationVersion": 2, "status": "committed"}),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "not_required"
    assert not journal.exists()
    assert report.snapshot_path is None


def test_cleanup_failure_is_non_blocking_and_retried_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "safe"\n',
        encoding="utf-8",
    )
    snapshot = tmp_path / upgrade_migration.SNAPSHOT_NAME
    snapshot.mkdir()

    def fail_cleanup(path: Path) -> None:
        raise PermissionError(f"busy: {path.name}")

    monkeypatch.setattr(upgrade_migration, "_remove_legacy_path", fail_cleanup)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "cleanup_pending"
    assert "PermissionError" in str(report.error)
    assert snapshot.exists()


def test_invalid_legacy_journal_is_not_manual_recovery(tmp_path: Path) -> None:
    journal = tmp_path / upgrade_migration.JOURNAL_NAME
    journal.write_text('{"migrationVersion":999}', encoding="utf-8")

    report = upgrade_migration.inspect_sandbox_upgrade(tmp_path)

    assert report.ok is True
    assert report.status == "legacy_artifacts_present"
    assert journal.exists()


def test_repeated_run_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "managed"\n', encoding="utf-8")

    first = SandboxUpgradeCoordinator(tmp_path).run()
    after_first = config.read_bytes()
    second = SandboxUpgradeCoordinator(tmp_path).run()

    assert first.status == "committed"
    assert second.status == "not_required"
    assert config.read_bytes() == after_first


def test_concurrent_migrations_serialize_and_converge(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: SandboxUpgradeCoordinator(tmp_path).run(), range(2)))

    assert {report.status for report in reports} <= {
        "committed",
        "not_required",
        "retry_required",
    }
    assert any(report.ok for report in reports)
    assert 'run_mode = "safe"' in (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_migrator_has_no_platform_acl_or_powershell_dependency() -> None:
    source = inspect.getsource(upgrade_migration)

    assert "setsecurityinfo" not in source.casefold()
    assert "dacl" not in source.casefold()
    assert "subprocess" not in source
    assert "sessions.db" not in source
    assert "os.name" not in source
    assert ".chmod" not in source
    assert "fchmod" not in source


def test_gateway_boot_does_not_raise_for_sandbox_normalization_failure() -> None:
    root = Path(__file__).parents[2]
    source = (root / "src" / "opensquilla" / "gateway" / "boot.py").read_text(
        encoding="utf-8"
    )
    block_start = source.index("# Best-effort normalization of released sandbox spellings")
    block_end = source.index("# ── Sandbox runtime", block_start)
    migration_block = source[block_start:block_end]

    assert "migration_failed_manual_recovery_required" not in migration_block
    assert "raise RuntimeError" not in migration_block
    assert "log.warning" in migration_block
    assert "upgrade_report.ok and not upgrade_report.error" in migration_block


def test_atomic_writer_is_platform_neutral() -> None:
    source = inspect.getsource(upgrade_migration._atomic_write)

    assert "tempfile.mkstemp" in source
    assert "os.fsync" in source
    assert "os.replace" in source
    assert "os.name" not in source
