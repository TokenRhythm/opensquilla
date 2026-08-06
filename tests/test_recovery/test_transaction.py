from __future__ import annotations

import json
import multiprocessing
import os
import sys
import uuid
from pathlib import Path

import pytest

from opensquilla.recovery import inspect_profile
from opensquilla.recovery.errors import RecoveryError, StaleRecoveryTransactionError
from opensquilla.recovery.restore import _identity_payload
from opensquilla.recovery.transaction import recover_profile_transaction


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve())))


def _contend_for_transaction_gateway(
    state_dir: str,
    queue: multiprocessing.Queue,
) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    lock = GatewayPidLock(state_dir)
    try:
        lock.acquire()
    except SystemExit:
        queue.put("busy")
    else:
        queue.put("acquired")
        lock.release()


def _profile(home: Path, marker: str) -> Path:
    workspace = home / "workspace"
    state = home / "state"
    workspace.mkdir(parents=True)
    state.mkdir()
    (workspace / "SOUL.md").write_text(marker + "\n", encoding="utf-8")
    (home / "config.toml").write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n',
        encoding="utf-8",
    )
    return home


def _write_journal(home: Path, payload: dict[str, object]) -> Path:
    path = home.parent / f".{home.name}.profile-replace.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_typed_import_target_parked_can_be_rolled_back_without_reimport(
    tmp_path: Path,
) -> None:
    import opensquilla.migration.opensquilla_home as migration_module

    home = tmp_path / "opensquilla"
    source = _profile(tmp_path / "source", "source must remain untouched")
    transaction_id = str(uuid.uuid4())
    backup = _profile(home.with_name(f"{home.name}.backup.{transaction_id}"), "original")
    staging = _profile(
        home.parent / f".{home.name}.profile-staging.{transaction_id}",
        "unpublished candidate",
    )
    payload = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(home),
        "backup": _normalized_path(backup),
        "staging": _normalized_path(staging),
        "phase": "target_parked",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": _identity_payload(source),
            "original_target": _identity_payload(backup),
            "staging": _identity_payload(staging),
            "backup": _identity_payload(backup),
            "candidate": None,
        },
    }
    journal = _write_journal(home, payload)
    before = inspect_profile(home, profile_kind="desktop-primary")

    assert before.stable_code == "transaction_incomplete"
    assert "recover-transaction" in before.allowed_actions
    with pytest.raises(RecoveryError) as missing_adapter:
        recover_profile_transaction(
            home,
            transaction_id=before.transaction_id,
            expected_revision=before.revision,
        )
    assert missing_adapter.value.stable_code == "transaction_recovery_unsafe"
    assert not home.exists()
    assert backup.exists()
    assert staging.exists()
    assert journal.exists()

    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        import_recoverer=migration_module.recover_interrupted_profile_import,
    )

    assert result.outcome == "ready"
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "original\n"
    assert (source / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "source must remain untouched\n"
    )
    assert not staging.exists()
    assert not backup.exists()
    assert not journal.exists()


def test_import_recovery_handles_candidate_move_before_phase_update(
    tmp_path: Path,
) -> None:
    import opensquilla.migration.opensquilla_home as migration_module

    home = _profile(tmp_path / "opensquilla", "published candidate")
    source = _profile(tmp_path / "source", "source")
    transaction_id = str(uuid.uuid4())
    backup = _profile(home.with_name(f"{home.name}.backup.{transaction_id}"), "original")
    staging = home.parent / f".{home.name}.profile-staging.{transaction_id}"
    candidate_identity = _identity_payload(home)
    payload = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(home),
        "backup": _normalized_path(backup),
        "staging": _normalized_path(staging),
        "phase": "target_parked",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": _identity_payload(source),
            "original_target": _identity_payload(backup),
            "staging": candidate_identity,
            "backup": _identity_payload(backup),
            "candidate": None,
        },
    }
    _write_journal(home, payload)
    before = inspect_profile(home, profile_kind="desktop-primary")

    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        import_recoverer=migration_module.recover_interrupted_profile_import,
    )

    assert result.outcome == "ready"
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "original\n"
    assert not backup.exists()
    assert not staging.exists()


def test_import_recovery_is_idempotent_after_paths_were_already_rolled_back(
    tmp_path: Path,
) -> None:
    import opensquilla.migration.opensquilla_home as migration_module

    home = _profile(tmp_path / "opensquilla", "original")
    source = _profile(tmp_path / "source", "source")
    candidate = _profile(tmp_path / "candidate-probe", "candidate")
    candidate_identity = _identity_payload(candidate)
    transaction_id = str(uuid.uuid4())
    backup = home.with_name(f"{home.name}.backup.{transaction_id}")
    staging = home.parent / f".{home.name}.profile-staging.{transaction_id}"
    payload = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(home),
        "backup": _normalized_path(backup),
        "staging": _normalized_path(staging),
        "phase": "candidate_published_unvalidated",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": _identity_payload(source),
            "original_target": _identity_payload(home),
            "staging": candidate_identity,
            "backup": _identity_payload(home),
            "candidate": candidate_identity,
        },
    }
    _write_journal(home, payload)
    # The candidate directory is transaction-owned scratch; simulate the
    # recovery process having already removed it before crashing at journal unlink.
    import shutil

    shutil.rmtree(candidate)
    before = inspect_profile(home, profile_kind="desktop-primary")

    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        import_recoverer=migration_module.recover_interrupted_profile_import,
    )

    assert result.outcome == "ready"
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "original\n"


def test_transaction_recovery_locks_parked_backup_before_restoring_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.migration.opensquilla_home as migration_module

    home = tmp_path / "opensquilla"
    source = _profile(tmp_path / "source", "source")
    transaction_id = str(uuid.uuid4())
    backup = _profile(home.with_name(f"{home.name}.backup.{transaction_id}"), "original")
    staging = _profile(
        home.parent / f".{home.name}.profile-staging.{transaction_id}",
        "candidate",
    )
    payload = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(home),
        "backup": _normalized_path(backup),
        "staging": _normalized_path(staging),
        "phase": "target_parked",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": _identity_payload(source),
            "original_target": _identity_payload(backup),
            "staging": _identity_payload(staging),
            "backup": _identity_payload(backup),
            "candidate": None,
        },
    }
    _write_journal(home, payload)
    before = inspect_profile(home, profile_kind="desktop-primary")
    original_recover = migration_module.recover_interrupted_profile_import
    observations: list[str] = []

    def recover_while_old_gateway_contends(candidate_home: Path, journal: dict) -> None:
        context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
        queue = context.Queue()
        process = context.Process(
            target=_contend_for_transaction_gateway,
            args=(str(backup / "state"), queue),
        )
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        observations.append(queue.get(timeout=1))
        original_recover(candidate_home, journal)

    monkeypatch.setattr(
        migration_module,
        "recover_interrupted_profile_import",
        recover_while_old_gateway_contends,
    )

    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        import_recoverer=migration_module.recover_interrupted_profile_import,
    )

    assert result.outcome == "ready"
    assert observations == ["busy"]


def test_typed_restore_target_parked_restores_current_target_and_keeps_selection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    selected = _profile(tmp_path / "opensquilla.backup.selected", "selected")
    current_backup = _profile(
        home.with_name(f"{home.name}.backup.{transaction_id}"),
        "current",
    )
    payload = {
        "schema_version": 1,
        "operation": "restore-profile",
        "transaction_id": transaction_id,
        "source": _normalized_path(selected),
        "target": _normalized_path(home),
        "backup": _normalized_path(current_backup),
        "staging": "",
        "phase": "target_parked",
        "target_existed": True,
        "identities": {
            "source": _identity_payload(selected),
            "original_target": _identity_payload(current_backup),
            "staging": None,
            "backup": _identity_payload(current_backup),
            "candidate": _identity_payload(selected),
        },
    }
    journal = _write_journal(home, payload)
    before = inspect_profile(home, profile_kind="desktop-primary")

    assert "recover-transaction" in before.allowed_actions
    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
    )

    assert result.outcome == "ready"
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (selected / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "selected\n"
    assert not current_backup.exists()
    assert not journal.exists()


def test_restore_recovery_is_idempotent_after_both_moves_completed(
    tmp_path: Path,
) -> None:
    home = _profile(tmp_path / "opensquilla", "current")
    transaction_id = str(uuid.uuid4())
    selected = _profile(tmp_path / "opensquilla.backup.selected", "selected")
    current_backup = home.with_name(f"{home.name}.backup.{transaction_id}")
    original_identity = _identity_payload(home)
    payload = {
        "schema_version": 1,
        "operation": "restore-profile",
        "transaction_id": transaction_id,
        "source": _normalized_path(selected),
        "target": _normalized_path(home),
        "backup": _normalized_path(current_backup),
        "staging": "",
        "phase": "candidate_published_unvalidated",
        "target_existed": True,
        "identities": {
            "source": _identity_payload(selected),
            "original_target": original_identity,
            "staging": None,
            "backup": original_identity,
            "candidate": _identity_payload(selected),
        },
    }
    journal = _write_journal(home, payload)
    before = inspect_profile(home, profile_kind="desktop-primary")

    result = recover_profile_transaction(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
    )

    assert result.outcome == "ready"
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (selected / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "selected\n"
    assert not current_backup.exists()
    assert not journal.exists()


def test_untyped_or_tampered_journal_has_no_automatic_recovery_action(
    tmp_path: Path,
) -> None:
    home = _profile(tmp_path / "opensquilla", "current")
    _write_journal(
        home,
        {
            "schema_version": 1,
            "phase": "prepared",
            "transaction_id": str(uuid.uuid4()),
        },
    )

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "transaction_incomplete"
    assert "recover-transaction" not in report.allowed_actions


@pytest.mark.parametrize("tamper", ["extra", "empty-identities", "missing-flags"])
def test_almost_typed_import_journal_is_read_only_and_not_recoverable(
    tmp_path: Path,
    tamper: str,
) -> None:
    home = _profile(tmp_path / "opensquilla", "current")
    source = _profile(tmp_path / "source", "source")
    transaction_id = str(uuid.uuid4())
    staging = _profile(
        home.parent / f".{home.name}.profile-staging.{transaction_id}",
        "candidate",
    )
    backup = home.with_name(f"{home.name}.backup.{transaction_id}")
    home_identity = _identity_payload(home)
    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(home),
        "backup": _normalized_path(backup),
        "staging": _normalized_path(staging),
        "phase": "prepared",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": _identity_payload(source),
            "original_target": home_identity,
            "staging": _identity_payload(staging),
            "backup": home_identity,
            "candidate": None,
        },
    }
    if tamper == "extra":
        payload["unexpected"] = "future"
    elif tamper == "empty-identities":
        payload["identities"] = {}
    else:
        payload.pop("target_had_real_data")
        payload.pop("target_was_empty")
    journal = _write_journal(home, payload)
    journal_before = journal.read_bytes()
    home_before = (home / "workspace" / "SOUL.md").read_bytes()
    source_before = (source / "workspace" / "SOUL.md").read_bytes()
    staging_before = (staging / "workspace" / "SOUL.md").read_bytes()

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "transaction_incomplete"
    assert "recover-transaction" not in report.allowed_actions
    assert journal.read_bytes() == journal_before
    assert (home / "workspace" / "SOUL.md").read_bytes() == home_before
    assert (source / "workspace" / "SOUL.md").read_bytes() == source_before
    assert (staging / "workspace" / "SOUL.md").read_bytes() == staging_before
    assert not backup.exists()


def test_recovery_revision_rejects_a_typed_journal_replaced_after_inspection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first_source = _profile(tmp_path / "source-first", "first source")
    second_source = _profile(tmp_path / "source-second", "second source")
    first_backup = _profile(
        home.with_name(f"{home.name}.backup.{first_id}"),
        "first original",
    )
    second_backup = _profile(
        home.with_name(f"{home.name}.backup.{second_id}"),
        "second original",
    )
    first_staging = _profile(
        home.parent / f".{home.name}.profile-staging.{first_id}",
        "first candidate",
    )
    second_staging = _profile(
        home.parent / f".{home.name}.profile-staging.{second_id}",
        "second candidate",
    )

    def payload_for(
        transaction_id: str,
        source: Path,
        backup: Path,
        staging: Path,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation": "profile-import",
            "source_kind": "cli-home",
            "transaction_id": transaction_id,
            "source": _normalized_path(source),
            "target": _normalized_path(home),
            "backup": _normalized_path(backup),
            "staging": _normalized_path(staging),
            "phase": "target_parked",
            "target_existed": True,
            "target_had_real_data": True,
            "target_was_empty": False,
            "identities": {
                "source": _identity_payload(source),
                "original_target": _identity_payload(backup),
                "staging": _identity_payload(staging),
                "backup": _identity_payload(backup),
                "candidate": None,
            },
        }

    journal = _write_journal(
        home,
        payload_for(first_id, first_source, first_backup, first_staging),
    )
    inspected = inspect_profile(home, profile_kind="desktop-primary")
    journal.write_text(
        json.dumps(
            payload_for(second_id, second_source, second_backup, second_staging),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StaleRecoveryTransactionError):
        recover_profile_transaction(
            home,
            transaction_id=inspected.transaction_id,
            expected_revision=inspected.revision,
        )

    assert not home.exists()
    assert first_backup.exists()
    assert second_backup.exists()
    assert first_staging.exists()
    assert second_staging.exists()
