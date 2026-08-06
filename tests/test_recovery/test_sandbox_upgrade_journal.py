from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from opensquilla.sandbox.upgrade_migration import (
    SandboxUpgradeCoordinator,
    inspect_sandbox_upgrade,
)


def test_interrupted_prepared_journal_retries_to_commit(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "standard"\n',
        encoding="utf-8",
    )
    coordinator = SandboxUpgradeCoordinator(tmp_path)
    coordinator.snapshot_path.mkdir()
    (coordinator.snapshot_path / "manifest.json").write_text(
        '{"stores":[]}',
        encoding="utf-8",
    )
    coordinator.journal_path.write_text(
        json.dumps(
            {
                "migrationVersion": 2,
                "status": "prepared",
                "stores": ["config.toml"],
                "snapshot": str(coordinator.snapshot_path),
            }
        ),
        encoding="utf-8",
    )

    report = coordinator.run()

    assert report.ok is True
    assert report.status == "committed"
    assert inspect_sandbox_upgrade(tmp_path).ok is True


def test_invalid_journal_requires_manual_recovery_without_rollback(
    tmp_path: Path,
) -> None:
    journal = tmp_path / ".sandbox-upgrade-v2.json"
    journal.write_text('{"migrationVersion":999}', encoding="utf-8")

    report = inspect_sandbox_upgrade(tmp_path)

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert journal.exists()


def test_concurrent_direct_update_migrations_serialize_on_profile_lock(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(
            pool.map(
                lambda _: SandboxUpgradeCoordinator(tmp_path).run(),
                range(2),
            )
        )

    assert all(report.ok and report.status == "committed" for report in reports)
    assert 'run_mode = "safe"' in (tmp_path / "config.toml").read_text(
        encoding="utf-8"
    )
