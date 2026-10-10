from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.operations import (
    InstallOperation,
    InstallOperationStore,
    operation_database,
)
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.transaction import managed_root_identity
from opensquilla.skills.loader import SkillLoader
from tests.test_skills.test_hub_management_service import FakeImmutableSource
from tests.test_skills.test_hub_management_service import _service as _install_service


def _service(tmp_path: Path, *, unleased: bool = False) -> SkillManagementService:
    diagnostics = (
        SkillDiagnostic(
            code="PROFILE_LEASE_REQUIRED", severity=DiagnosticSeverity.ERROR,
            phase=DiagnosticPhase.STORE, message="test requires writer lease", blocking=True,
        ),
    ) if unleased else ()
    return SkillManagementService(
        router=SourceRouter([]), managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "lock.json", journal_path=tmp_path / "state/journal.json",
        startup_recovery_diagnostics=diagnostics,
    )


async def _status(service: SkillManagementService, operation_id: str):
    principal = Principal(
        role="operator", scopes=frozenset({"operator.admin"}), is_owner=True,
        authenticated=True,
    )
    return await get_dispatcher().dispatch(
        "status", "skills.install.status", {"operationId": operation_id},
        RpcContext(conn_id="test", principal=principal, skill_management_service=service),
    )


@pytest.mark.asyncio
async def test_lazy_status_does_not_create_an_unleased_database(tmp_path: Path) -> None:
    service = _service(tmp_path, unleased=True)
    operations = service.install_operations
    assert operations is service.install_operations
    assert not operations.store.path.exists()
    result = await _status(service, str(uuid.uuid4()))
    assert result.ok and result.payload["state"] == "unknown"
    assert result.payload["scope"]
    assert not operations.store.path.parent.exists()


@pytest.mark.asyncio
async def test_status_does_not_create_schema_in_existing_empty_database(tmp_path: Path) -> None:
    service = _service(tmp_path, unleased=True)
    path = operation_database(service.journal_path)
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()
    result = await _status(service, str(uuid.uuid4()))
    assert result.ok and result.payload["state"] == "unknown"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


@pytest.mark.asyncio
async def test_unleased_status_never_backfills_or_recovers_foreign_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, unleased=True)
    path = operation_database(service.journal_path)
    root = managed_root_identity(service.managed_dir)
    old = InstallOperationStore(path, root_id=root, process_id="old-process")
    operation_id = str(uuid.uuid4())
    old.create("local-owner", operation_id, "request")
    with old.connect() as db:
        db.execute("DELETE FROM skill_install_keys")
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    for _ in range(2):
        result = await _status(service, operation_id)
        assert result.ok and result.payload["state"] == "recovery_required"
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    with real_connect(path) as db:
        assert db.execute("SELECT state, process FROM skill_installs").fetchone() == (
            "running", "old-process",
        )
        assert db.execute("SELECT count(*) FROM skill_install_keys").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_concurrent_preparation_is_once_and_journal_defers_orphan_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    path = operation_database(service.journal_path)
    old = InstallOperationStore(
        path, root_id=managed_root_identity(service.managed_dir), process_id="old",
    )
    operation_id = str(uuid.uuid4())
    old.create("local-owner", operation_id, "request")
    service.journal_path.write_text("pending journal", encoding="utf-8")
    store = service.install_operations.store
    initializations = 0
    recoveries = 0
    real_initialize = store._initialize
    real_recovery = store.recover_orphans

    def counted_initialize():
        nonlocal initializations
        initializations += 1
        real_initialize()

    monkeypatch.setattr(store, "_initialize", counted_initialize)

    def counted_recovery(path):
        nonlocal recoveries
        recoveries += 1
        real_recovery(path)

    monkeypatch.setattr(store, "recover_orphans", counted_recovery)
    await asyncio.gather(*(service.prepare_install_operations(recover_orphans=True)
                           for _ in range(3)))
    assert initializations == 1
    assert recoveries == 0
    with old.connect() as db:
        assert db.execute("SELECT state FROM skill_installs").fetchone()[0] == "running"
    service.journal_path.unlink()
    await service.prepare_install_operations(recover_orphans=True)
    await service.prepare_install_operations(recover_orphans=True)
    assert initializations == 1 and recoveries == 1
    with old.connect() as db:
        assert db.execute("SELECT state FROM skill_installs").fetchone()[0] == "recovery_required"


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_status_waits_for_exclusive_sqlite_lock_without_stopping_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    operation_id = str(uuid.uuid4())
    store = service.install_operations.store
    store.create("local-owner", operation_id, "request")
    held, release = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    query_started = asyncio.Event()
    real_connect = sqlite3.connect

    def hold_lock():
        with real_connect(store.path) as db:
            db.execute("BEGIN EXCLUSIVE")
            held.set()
            release.wait(timeout=5)
            db.rollback()

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        # Signal entry before SQLite can wait on the lock during compilation.
        loop.call_soon_threadsafe(query_started.set)
        return db

    holder = threading.Thread(target=hold_lock)
    holder.start()
    await asyncio.to_thread(held.wait, 5)
    assert held.is_set()
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    task = asyncio.create_task(_status(service, operation_id))
    try:
        await asyncio.wait_for(query_started.wait(), timeout=5)
        for _ in range(3):
            await asyncio.sleep(0.01)
        assert not task.done(), "SQLite wait must allow unrelated loop work"
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
    result = await task
    assert result.ok and result.payload["state"] == "running"


@pytest.mark.asyncio
async def test_disconnect_during_claim_does_not_strand_running_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    claiming, installing = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    real_create = operations.store.create
    calls = 0

    def blocked_create(*args):
        loop.call_soon_threadsafe(claiming.set)
        assert release.wait(timeout=5)
        return real_create(*args)

    async def install():
        nonlocal calls
        calls += 1
        installing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(operations.store, "create", blocked_create)
    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    try:
        await asyncio.wait_for(claiming.wait(), timeout=5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
    await asyncio.wait_for(installing.wait(), timeout=5)
    cancelled = await operations.cancel("owner", operation_id)
    assert cancelled["cancelled"]
    assert calls == 1
    assert (await operations.store.read_async("owner", operation_id))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_cancel_request_drains_its_reserved_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    finishing = asyncio.Event()
    release = threading.Event()
    real_finish = operations.store.finish

    def blocked_finish(*args, **kwargs):
        loop.call_soon_threadsafe(finishing.set)
        assert release.wait(timeout=5)
        real_finish(*args, **kwargs)

    monkeypatch.setattr(operations.store, "finish", blocked_finish)
    cancellation = asyncio.create_task(operations.cancel("owner", operation_id))
    try:
        await asyncio.wait_for(finishing.wait(), timeout=5)
        cancellation.cancel()
        await asyncio.sleep(0)
        assert not cancellation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancellation
    assert (await operations.store.read_async("owner", operation_id))["state"] == "cancelled"

    async def forbidden():
        raise AssertionError("cancelled operation must not run")

    assert (await operations.run("owner", operation_id, {}, forbidden))["cancelled"]


@pytest.mark.ci_serial
@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_repeated_cancel_during_rollback_keeps_proof_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool,
) -> None:
    managed = tmp_path / "managed"
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.repeated-cancel")
    source = FakeImmutableSource({
        "SKILL.md": "---\nname: demo\ndescription: Cancel fixture.\n---\n# Demo\n",
    })
    service = _install_service(tmp_path, source, loader=loader)
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    postflight, checkpoint = asyncio.Event(), asyncio.Event()
    release_postflight, release_checkpoint = threading.Event(), threading.Event()
    real_checkpoint = operations.store.checkpoint_rollback

    def blocked_postflight(*args, **kwargs):
        loop.call_soon_threadsafe(postflight.set)
        assert release_postflight.wait(timeout=5)
        raise RuntimeError("synthetic postflight rejection")

    def blocked_checkpoint(*args):
        loop.call_soon_threadsafe(checkpoint.set)
        assert release_checkpoint.wait(timeout=5)
        real_checkpoint(*args)

    def blocked_failed_restore(*args, **kwargs):
        loop.call_soon_threadsafe(checkpoint.set)
        assert release_checkpoint.wait(timeout=5)
        raise OSError("synthetic rollback failure")

    monkeypatch.setattr(loader, "reload_verified", blocked_postflight)
    monkeypatch.setattr(operations.store, "checkpoint_rollback", blocked_checkpoint)
    if rollback_fails:
        monkeypatch.setattr(
            "opensquilla.skills.hub.transaction._restore_lockfile", blocked_failed_restore,
        )

    async def install():
        return (await service.install("demo", "fake")).to_dict()

    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    first_cancel = second_cancel = None
    try:
        await asyncio.wait_for(postflight.wait(), timeout=10)
        first_cancel = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.sleep(0)
        release_postflight.set()
        await asyncio.wait_for(checkpoint.wait(), timeout=10)
        second_cancel = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.sleep(0)
        assert not second_cancel.done()
        assert service.journal_path.exists()
    finally:
        release_postflight.set()
        release_checkpoint.set()
    result = await waiter
    assert bool(result.get("cancelled")) is not rollback_fails
    assert result["rollbackPerformed"] is not rollback_fails
    assert bool(result.get("recoveryRequired")) is rollback_fails
    assert result["diagnostics"]
    assert service.journal_path.exists() is rollback_fails
    assert await first_cancel == {**result, "cancelled": not rollback_fails, "pending": False}
    assert await second_cancel == {**result, "cancelled": not rollback_fails, "pending": False}


@pytest.mark.asyncio
async def test_progress_throttles_before_submitting_database_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.hub import operations as module

    store = InstallOperationStore(tmp_path / "operations.db", root_id="root")
    operation_id = str(uuid.uuid4())
    store.create("owner", operation_id, "request")
    operation = InstallOperation(store, "owner", operation_id)
    clock = 10.0
    workers = 0
    real_worker = module.run_operation_worker

    async def counted_worker(function, *args, **kwargs):
        nonlocal workers
        workers += 1
        return await real_worker(function, *args, **kwargs)

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock, time=time.time))
    monkeypatch.setattr(module, "run_operation_worker", counted_worker)
    for size in range(100):
        await operation.progress_async("downloading", downloadedBytes=size)
    assert workers == 1
    clock += 0.25
    await operation.progress_async("downloading", totalFiles=4)
    assert workers == 2
    assert store.read("owner", operation_id)["progress"] == {
        "downloadedBytes": 99, "totalFiles": 4,
    }
    await operation.progress_async("scanning")
    assert workers == 3


@pytest.mark.asyncio
async def test_repeated_cancel_during_terminal_finish_keeps_committed_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    committed = asyncio.Event()
    release = threading.Event()
    real_finish = operations.store.finish

    def blocked_finish(owner, operation_id, result, **kwargs):
        real_finish(owner, operation_id, result, **kwargs)
        if result.get("success"):
            loop.call_soon_threadsafe(committed.set)
            assert release.wait(timeout=5)

    monkeypatch.setattr(operations.store, "finish", blocked_finish)

    async def install():
        return {"success": True, "installed": True, "message": "proven success"}

    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        first = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.sleep(0)
        second = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
    finally:
        release.set()
    result = await waiter
    assert result["success"] and not result.get("cancelled")
    assert (await first)["success"] and (await second)["success"]
    receipt = await operations.store.read_async("owner", operation_id)
    assert receipt["state"] == "succeeded" and receipt["result"] == result


@pytest.mark.asyncio
async def test_repeated_cancel_and_disconnected_cancel_waiters_still_settle_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    operations = service.install_operations
    operation_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    installing, reading = asyncio.Event(), asyncio.Event()
    release_read = threading.Event()
    real_read = operations.store.read_existing

    def blocked_read(*args):
        loop.call_soon_threadsafe(reading.set)
        assert release_read.wait(timeout=5)
        return real_read(*args)

    async def install():
        installing.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    await asyncio.wait_for(installing.wait(), timeout=5)
    monkeypatch.setattr(operations.store, "read_existing", blocked_read)
    first = second = None
    try:
        first = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.wait_for(reading.wait(), timeout=5)
        second = asyncio.create_task(operations.cancel("owner", operation_id))
        await asyncio.sleep(0)
        first.cancel()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(asyncio.CancelledError):
            await second
        assert operations.tasks
    finally:
        release_read.set()
    result = await waiter
    assert result["cancelled"]
    await asyncio.sleep(0)
    assert not operations.tasks
    assert real_read("owner", operation_id)["state"] == "cancelled"


@pytest.mark.asyncio
async def test_status_retention_is_read_only_and_internal_receipts_stay_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.hub import operations as module

    service = _service(tmp_path)
    await service.prepare_install_operations()
    store = service.install_operations.store
    now = 2_000_000_000.0
    cutoff = now - 7 * 86400
    expired, boundary, active = (str(uuid.uuid4()) for _ in range(3))
    with store.connect() as db:
        for operation_id, state, updated in (
            (expired, "succeeded", cutoff - .001),
            (boundary, "succeeded", cutoff),
            (active, "running", 0),
        ):
            db.execute(
                "INSERT INTO skill_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store.root_id, "local-owner", operation_id, "request", store.process_id,
                 state, "complete", "{}", '{"success":true}', updated, updated),
            )
            db.execute("INSERT INTO skill_install_keys VALUES (?, ?, ?)",
                       (store.root_id, "local-owner", operation_id))
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: now))
    result = await _status(service, expired)
    assert result.ok and result.payload["state"] == "unknown"
    assert (await _status(service, boundary)).payload["state"] == "succeeded"
    assert (await _status(service, active)).payload["state"] == "running"
    assert (await store.read_async("local-owner", expired))["state"] == "succeeded"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM skill_installs").fetchone()[0] == 3
    service.journal_path.write_text("retained journal", encoding="utf-8")
    assert (await _status(service, expired)).payload["state"] == "succeeded"
    service.journal_path.unlink()
    store.prune()
    with pytest.raises(ValueError, match="receipt expired"):
        store.create("local-owner", expired, "request")


@pytest.mark.asyncio
async def test_status_terminal_cap_uses_root_scope_and_rowid_tie_order(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.prepare_install_operations()
    store = service.install_operations.store
    oldest, newest = str(uuid.uuid4()), str(uuid.uuid4())
    updated = time.time()
    ids = [oldest] + [str(uuid.uuid4()) for _ in range(999)] + [newest]
    with store.connect() as db:
        for position, operation_id in enumerate(ids):
            # The retention cap spans caller identities, as physical pruning did.
            owner = "local-owner" if position in (0, 1000) else "other-owner"
            db.execute(
                "INSERT INTO skill_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store.root_id, owner, operation_id, "request", store.process_id, "succeeded",
                 "complete", "{}", '{"success":true}', updated, updated),
            )
    assert (await _status(service, oldest)).payload["state"] == "unknown"
    assert (await _status(service, newest)).payload["state"] == "succeeded"
    assert (await store.read_async("local-owner", oldest))["state"] == "succeeded"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM skill_installs").fetchone()[0] == 1001
    service.journal_path.write_text("retained journal", encoding="utf-8")
    assert (await _status(service, oldest)).payload["state"] == "succeeded"
