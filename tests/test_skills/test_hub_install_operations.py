from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from opensquilla.skills.hub.operations import (
    InstallOperations,
    InstallOperationStore,
    current_install_operation,
    operation_database,
)
from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    managed_root_identity,
    recover_pending_skill_transaction,
    rollback_root,
    staging_root,
)


@pytest.mark.asyncio
async def test_disconnected_waiter_does_not_cancel_or_replay_install(tmp_path: Path) -> None:
    store = InstallOperationStore(tmp_path / "operations.db", root_id="root")
    operations = InstallOperations(store)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    operation_id = str(uuid.uuid4())

    async def install():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"success": True, "installed": True, "effectiveFrom": "next_turn"}

    waiter = asyncio.create_task(
        operations.run("owner", operation_id, {"identifier": "demo"}, install)
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert store.read("owner", operation_id)["state"] == "running"
    assert store.read("another-owner", operation_id)["state"] == "unknown"
    release.set()
    result = await operations.run("owner", operation_id, {"identifier": "demo"}, install)
    assert result["success"]
    assert (await operations.run("owner", operation_id, {"identifier": "demo"}, install)) == result
    assert calls == 1
    assert store.read("owner", operation_id)["state"] == "succeeded"
    with pytest.raises(ValueError, match="another install"):
        await operations.run("owner", operation_id, {"identifier": "different"}, install)


@pytest.mark.asyncio
async def test_cancel_after_durable_commit_returns_success(tmp_path: Path) -> None:
    store = InstallOperationStore(tmp_path / "operations.db", root_id="root")
    operations = InstallOperations(store)
    committed = asyncio.Event()
    operation_id = str(uuid.uuid4())

    async def install():
        context = current_install_operation()
        context.store.finish(context.owner, context.id, {"success": True, "installed": True})
        committed.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    await committed.wait()
    result = await operations.cancel("owner", operation_id)
    assert result["success"]
    assert not result["cancelled"]
    assert (await waiter)["success"]


@pytest.mark.parametrize(
    "phase", ["prepared", "old_moved", "new_moved", "lock_written", "committed"]
)
def test_restart_reconciles_operation_after_transaction_recovery(
    tmp_path: Path, phase: str
) -> None:
    managed = tmp_path / "managed"
    journal_path = tmp_path / "state" / "transaction.json"
    lockfile = tmp_path / "lock.json"
    operation_id = str(uuid.uuid4())
    root_id = managed_root_identity(managed)
    store = InstallOperationStore(
        operation_database(journal_path), root_id=root_id, process_id="old"
    )
    store.create("owner", operation_id, "signature")
    staging = staging_root(managed) / "tx" / "demo"
    rollback = rollback_root(managed) / "tx" / "demo"
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_text("synthetic fixture")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="demo",
        target=managed / "demo",
        staging=staging,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.install_operation_id = operation_id
    journal.install_operation_owner = "owner"
    if phase == "committed":
        journal.install_receipt = {"success": True, "installed": True, "name": "demo"}
    journal.phase = phase
    journal.write(journal_path)
    result = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )
    assert not any(item.blocking for item in result)
    restarted = InstallOperationStore(operation_database(journal_path), root_id=root_id)
    receipt = restarted.read("owner", operation_id)
    assert receipt["state"] == ("succeeded" if phase == "committed" else "failed")
    assert receipt["result"]["success"] == (phase == "committed")
    assert not journal_path.exists()


def test_restart_without_proof_requires_recovery_and_retention_keeps_active(tmp_path: Path) -> None:
    path = tmp_path / "operations.db"
    old = InstallOperationStore(path, root_id="root", process_id="old")
    unknown_id = str(uuid.uuid4())
    old.create("owner", unknown_id, "signature")
    current = InstallOperationStore(path, root_id="root")
    assert current.read("owner", unknown_id)["state"] == "recovery_required"
    active_id = str(uuid.uuid4())
    current.create("owner", active_id, "signature")
    with current.connect() as db:
        db.execute("UPDATE skill_installs SET updated=0 WHERE id=?", (active_id,))
        for i in range(1005):
            db.execute(
                "INSERT INTO skill_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "root",
                    "owner",
                    str(uuid.uuid4()),
                    "sig",
                    "old",
                    "failed",
                    "complete",
                    "{}",
                    '{"success":false}',
                    0,
                    i + 9999999999,
                ),
            )
    current.prune()
    assert current.read("owner", active_id)["state"] == "running"
    with current.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM skill_installs WHERE state!='running'").fetchone()[0]
            == 1000
        )


@pytest.mark.asyncio
async def test_cancel_before_install_reserves_a_terminal_receipt(tmp_path: Path) -> None:
    store = InstallOperationStore(tmp_path / "operations.db", root_id="root")
    operations = InstallOperations(store)
    operation_id = str(uuid.uuid4())
    result = await operations.cancel("owner", operation_id)
    assert result["cancelled"]

    async def forbidden():
        raise AssertionError("cancelled operation must never begin installation")

    result = await operations.run("owner", operation_id, {"identifier": "demo"}, forbidden)
    assert result["cancelled"]


@pytest.mark.asyncio
async def test_expired_receipt_never_replays_the_same_operation_id(tmp_path: Path) -> None:
    store = InstallOperationStore(tmp_path / "operations.db", root_id="root")
    operations = InstallOperations(store)
    operation_id = str(uuid.uuid4())
    calls = 0

    async def install():
        nonlocal calls
        calls += 1
        return {"success": True}

    await operations.run("owner", operation_id, {"identifier": "demo"}, install)
    with store.connect() as db:
        db.execute("UPDATE skill_installs SET updated=0 WHERE id=?", (operation_id,))
    store.prune()
    assert store.read("owner", operation_id)["state"] == "unknown"
    with pytest.raises(ValueError, match="receipt expired"):
        await operations.run("owner", operation_id, {"identifier": "demo"}, install)
    assert calls == 1
