from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import uuid
from pathlib import Path

import pytest

from opensquilla.skills.hub import management as hub_management
from opensquilla.skills.hub import transaction as hub_transaction
from opensquilla.skills.hub.lockfile import Lockfile
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
from opensquilla.skills.loader import SkillLoader
from tests.test_skills.test_hub_management_service import FakeImmutableSource, _service


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["failure", "cancellation"])
@pytest.mark.parametrize("rollback_fails", [False, True], ids=["rolled-back", "needs-recovery"])
async def test_postflight_settlement_preserves_result_and_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool, rollback_fails: bool,
) -> None:
    managed = tmp_path / "managed"
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.install-operation-baseline")
    source = FakeImmutableSource({
        "SKILL.md": "---\nname: demo\ndescription: Synthetic install fixture.\n---\n# Demo\n",
    })
    service = _service(tmp_path, source, loader=loader)
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def fail_postflight(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release postflight")
            raise RuntimeError("synthetic postflight rejection")
        finally:
            finished.set()

    monkeypatch.setattr(loader, "reload_verified", fail_postflight)
    real_restore = hub_transaction._restore_lockfile
    if rollback_fails:
        def fail_rollback(*args, **kwargs):
            raise OSError("synthetic rollback failure")

        monkeypatch.setattr(hub_transaction, "_restore_lockfile", fail_rollback)

    async def install():
        return (await service.install("demo", "fake")).to_dict()

    request = {"identifier": "demo", "source": "fake"}
    waiter = asyncio.create_task(operations.run("owner", operation_id, request, install))
    cancellation = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert (managed / "demo" / "SKILL.md").exists()
        assert operations.store.read("owner", operation_id)["state"] == "running"
        if cancelled:
            cancellation = asyncio.create_task(operations.cancel("owner", operation_id))
            await asyncio.sleep(0)
            assert not cancellation.done()
            assert not finished.is_set()
    finally:
        release.set()
        result = await asyncio.wait_for(waiter, timeout=5)
        cancel_result = await cancellation if cancellation else None

    receipt = operations.store.read("owner", operation_id)
    expected_state = (
        "recovery_required" if rollback_fails else "cancelled" if cancelled else "failed"
    )
    assert receipt["state"] == expected_state
    assert receipt["result"] == result
    assert result["success"] is False
    assert bool(result.get("cancelled")) is (cancelled and not rollback_fails)
    assert bool(result.get("recoveryRequired")) is rollback_fails
    assert result["rollbackPerformed"] is not rollback_fails
    assert result["diagnostics"]
    if cancel_result is not None:
        assert cancel_result == {
            **result, "cancelled": bool(result.get("cancelled")), "pending": False,
        }
    assert await operations.run("owner", operation_id, request, install) == result
    assert source.resolve_calls == 1
    assert finished.is_set()
    assert loader.get_by_name("demo") is None
    assert (managed / "demo").exists() is rollback_fails
    assert (Lockfile.load(service.lockfile_path).get("demo") is not None) is rollback_fails
    assert service.journal_path.exists() is rollback_fails
    if rollback_fails:
        monkeypatch.setattr(hub_transaction, "_restore_lockfile", real_restore)
        recovered = recover_pending_skill_transaction(
            managed_dir=managed, lockfile_path=service.lockfile_path,
            journal_path=service.journal_path,
        )
        assert not any(item.blocking for item in recovered)
        assert operations.store.read("owner", operation_id)["state"] == "failed"
        assert not (managed / "demo").exists()
        assert not service.journal_path.exists()


@pytest.mark.asyncio
async def test_committed_cleanup_warning_is_preserved_in_all_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    source = FakeImmutableSource({
        "SKILL.md": "---\nname: demo\ndescription: Synthetic install fixture.\n---\n# Demo\n",
    })
    service = _service(tmp_path, source, loader=loader)
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    real_validate = hub_management.validate_transaction_journal_paths

    def fail_cleanup(journal, **kwargs):
        if journal.phase == "committed":
            raise OSError("synthetic committed cleanup failure")
        return real_validate(journal, **kwargs)

    monkeypatch.setattr(hub_management, "validate_transaction_journal_paths", fail_cleanup)

    async def install():
        return (await service.install("demo", "fake")).to_dict()

    result = await operations.run("owner", operation_id, {}, install)
    assert result["success"] is True
    assert any(item["code"] == "TRANSACTION_CLEANUP_PENDING" for item in result["diagnostics"])
    assert operations.store.read("owner", operation_id)["result"] == result
    assert await operations.run("owner", operation_id, {}, install) == result
    assert (await operations.cancel("owner", operation_id))["success"] is True
    assert (managed / "demo" / "SKILL.md").exists()
    assert loader.get_by_name("demo") is not None
    recovered = recover_pending_skill_transaction(
        managed_dir=managed, lockfile_path=service.lockfile_path, journal_path=service.journal_path,
    )
    assert not any(item.blocking for item in recovered)
    assert operations.store.read("owner", operation_id)["result"] == result


@pytest.mark.parametrize("crash_point", ["before", "after"])
def test_restart_retains_rollback_proof_around_journal_removal(
    tmp_path: Path, crash_point: str,
) -> None:
    operation_id = str(uuid.uuid4())
    worker = textwrap.dedent("""\
        import asyncio
        import os
        import sys
        from pathlib import Path

        from opensquilla.skills.hub import transaction
        from tests.test_skills.test_hub_management_service import FakeImmutableSource, _service

        root = Path(sys.argv[1])
        operation_id = sys.argv[2]
        crash_point = sys.argv[3]
        source = FakeImmutableSource({
            "SKILL.md": "---\\nname: demo\\ndescription: Crash fixture.\\n---\\n# Demo\\n",
        })
        service = _service(root, source)

        async def reject_postflight(**kwargs):
            raise RuntimeError("synthetic postflight rejection")

        service._reload_and_verify = reject_postflight
        remove_journal = transaction.remove_transaction_journal

        def crash_at_removal(path):
            status = service.install_operations.store.read("owner", operation_id)
            assert status["state"] == "running" and not status["terminal"]
            assert "result" not in status
            if crash_point == "after":
                remove_journal(path)
            os._exit(73)

        transaction.remove_transaction_journal = crash_at_removal

        async def install():
            return (await service.install("demo", "fake")).to_dict()

        asyncio.run(service.install_operations.run("owner", operation_id, {}, install))
        raise AssertionError("worker missed its crash point")
    """)
    crashed = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path), operation_id, crash_point],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert crashed.returncode == 73, crashed.stderr
    managed = tmp_path / "managed"
    journal_path = tmp_path / "transaction.json"
    lockfile_path = tmp_path / "skills-lock.json"
    assert journal_path.exists() is (crash_point == "before")
    assert not (managed / "demo").exists()
    assert Lockfile.load(lockfile_path).get("demo") is None
    restarted = InstallOperationStore(
        operation_database(journal_path), root_id=managed_root_identity(managed),
    )
    receipt = restarted.read("owner", operation_id)
    assert receipt["state"] == "failed"
    assert receipt["result"]["rollbackPerformed"] is True
    recovered = recover_pending_skill_transaction(
        managed_dir=managed, lockfile_path=lockfile_path, journal_path=journal_path,
    )
    assert not any(item.blocking for item in recovered)
    restarted.recover_orphans(journal_path)
    assert restarted.read("owner", operation_id)["state"] == "failed"
    assert restarted.read("owner", operation_id)["result"]["rollbackPerformed"] is True
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_rollback_checkpoint_failure_keeps_journal_for_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeImmutableSource({
        "SKILL.md": "---\nname: demo\ndescription: Crash fixture.\n---\n# Demo\n",
    })
    service = _service(tmp_path, source)
    operations = service.install_operations
    operation_id = str(uuid.uuid4())

    async def fail_postflight(**kwargs):
        raise RuntimeError("synthetic postflight rejection")

    def fail_checkpoint(*args):
        raise OSError("synthetic checkpoint failure")

    monkeypatch.setattr(service, "_reload_and_verify", fail_postflight)
    monkeypatch.setattr(operations.store, "checkpoint_rollback", fail_checkpoint)

    async def install():
        return (await service.install("demo", "fake")).to_dict()

    result = await operations.run("owner", operation_id, {}, install)
    assert result["recoveryRequired"] is True
    assert operations.store.read("owner", operation_id)["state"] == "recovery_required"
    assert service.journal_path.exists()
    assert not (service.managed_dir / "demo").exists()
    recovered = recover_pending_skill_transaction(
        managed_dir=service.managed_dir, lockfile_path=service.lockfile_path,
        journal_path=service.journal_path,
    )
    assert not any(item.blocking for item in recovered)
    assert operations.store.read("owner", operation_id)["state"] == "failed"
    assert not service.journal_path.exists()


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
