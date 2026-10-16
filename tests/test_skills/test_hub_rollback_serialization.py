"""A failed publisher owns the managed store until disk and catalog agree."""

from __future__ import annotations

import asyncio
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.skills.hub import management, transaction
from opensquilla.skills.hub.lockfile import Lockfile
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.source import SkillBundle, SourceResolution
from opensquilla.skills.hub.transaction import SkillTransactionJournal, staging_root
from opensquilla.skills.loader import SkillLoader
from tests.test_skills.test_hub_management_service import (
    CompatibilityRollbackLoader,
    FakeImmutableSource,
    _service,
)

pytestmark = pytest.mark.asyncio


class _Source(FakeImmutableSource):
    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
        name = resolution.requested_identifier
        text = "Old instructions." if self.revision == "a" * 40 else "New instructions."
        return SkillBundle(
            name=name,
            files={"SKILL.md": f"---\nname: {name}\ndescription: Fixture\n---\n{text}\n"},
            meta=resolution.meta,
            resolution=resolution,
        )


async def _result(task: asyncio.Task, timeout: float = 5):
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, "operation did not settle"
    return task.result()


async def _drain(*tasks: asyncio.Task) -> None:
    done, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel()
    if pending:
        settled, pending = await asyncio.wait(pending, timeout=5)
        done |= settled
    assert not pending, "cleanup did not settle after releasing its thread gates"
    await asyncio.gather(*done, return_exceptions=True)


async def _thread_started(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 5)


async def _setup(tmp_path: Path, operation: str, *, fallback: bool = False):
    loader = SkillLoader(
        managed_dir=tmp_path / "managed", snapshot_path=tmp_path / "snapshot.json",
    )
    loader.reload(force=True, reason="test.rollback-serialization")
    source = _Source({})
    service = _service(tmp_path, source, loader=loader)
    if operation != "install":
        assert (await service.install("first", "fake")).success
    if operation == "update":
        source.revision = "b" * 40
    if fallback:
        service = _service(tmp_path, source, loader=CompatibilityRollbackLoader(loader))
    await service.prepare_install_operations()
    return service, loader


def _start(service: SkillManagementService, operation: str, name: str = "first"):
    operation_id = str(uuid.uuid4())

    async def mutate():
        if operation == "update":
            result = (await service.update(name))[0]
        elif operation == "uninstall":
            result = await service.uninstall(name)
        else:
            result = await service.install(name, "fake")
        return result.to_dict()

    task = asyncio.create_task(service.install_operations.run(
        "test-owner", operation_id, {"operation": operation, "name": name}, mutate,
    ))
    return operation_id, task


def _assert_previous(service, loader, operation):
    target = service.managed_dir / "first"
    entry = Lockfile.load(service.lockfile_path).get("first")
    skill = loader.get_by_name("first")
    if operation == "install":
        assert not target.exists()
        assert entry is None and skill is None
    else:
        assert "Old instructions." in (target / "SKILL.md").read_text(encoding="utf-8")
        assert entry is not None and entry.resolved_revision == "a" * 40
        assert skill is not None and "Old instructions." in skill.content


# Exercise real receipt, journal and catalog writes with concurrent owners while
# keeping the bounded thread gates isolated from unrelated CI filesystem load.
@pytest.mark.ci_serial
@pytest.mark.parametrize("operation", ["install", "update", "uninstall"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_rollback_serializes_next_writer_and_committed_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    operation: str, cancelled: bool, fallback: bool,
) -> None:
    service, loader = await _setup(tmp_path, operation, fallback=fallback)
    loop = asyncio.get_running_loop()
    postflight, rollback = asyncio.Event(), asyncio.Event()
    release_postflight, release_rollback = threading.Event(), threading.Event()
    actual_reload = service._loader.reload_verified
    actual_recover = management.recover_pending_skill_transaction
    failed = False
    gated = False

    def reject_first(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            loop.call_soon_threadsafe(postflight.set)
            assert release_postflight.wait(5)
            raise RuntimeError("first postflight failed")
        return actual_reload(*args, **kwargs)

    def gated_recovery(**kwargs):
        nonlocal gated
        if not gated and SkillTransactionJournal.load(kwargs["journal_path"]) is not None:
            gated = True
            loop.call_soon_threadsafe(rollback.set)
            assert release_rollback.wait(5)
        return actual_recover(**kwargs)

    monkeypatch.setattr(service._loader, "reload_verified", reject_first)
    monkeypatch.setattr(management, "recover_pending_skill_transaction", gated_recovery)
    first_waiting, writer_waiting, reader_waiting = (
        asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    acquire = service._mutation_lock.acquire
    first_id = second_id = ""
    acquired: list[str] = []

    async def observe_acquire():
        task = asyncio.current_task()
        assert task is not None
        name = task.get_name()
        if name == f"skill-install-{first_id}":
            first_waiting.set()
        elif name == f"skill-install-{second_id}":
            writer_waiting.set()
        elif name == "committed-reader":
            reader_waiting.set()
        result = await acquire()
        acquired.append(name)
        return result

    monkeypatch.setattr(service._mutation_lock, "acquire", observe_acquire)
    pending = []
    setup_lock_held = False
    try:
        # Queue both real writers before starting the rollback gate's clock.
        # Candidate/receipt I/O is preparation, not the contention under test.
        await acquire()
        setup_lock_held = True
        first_id, first = _start(service, operation)
        pending.append(first)
        await asyncio.wait_for(first_waiting.wait(), 5)
        second_id, second = _start(service, "install", "second")
        pending.append(second)
        await asyncio.wait_for(writer_waiting.wait(), 5)
        assert acquired == []
        assert not first.done() and not second.done()
        service._mutation_lock.release()
        setup_lock_held = False

        await _thread_started(postflight)
        assert acquired == [f"skill-install-{first_id}"]
        owner = service.install_operations.tasks[("test-owner", first_id)]
        if cancelled:
            owner.cancel("original cancellation")
        release_postflight.set()
        await _thread_started(rollback)
        if cancelled:
            owner.cancel("repeated cancellation")
            await asyncio.sleep(0)
            assert not owner.done()
        async def read_committed():
            async with service.committed_store_read():
                _assert_previous(service, loader, operation)
                second_entry = Lockfile.load(service.lockfile_path).get("second")
                assert (loader.get_by_name("second") is not None) == (second_entry is not None)

        reader = asyncio.create_task(read_committed(), name="committed-reader")
        pending.append(reader)
        await asyncio.wait_for(reader_waiting.wait(), 5)
        assert not reader.done(), "committed reads must wait for disk and catalog rollback"
        assert not second.done()
        assert service._mutation_lock.locked()
        assert acquired == [f"skill-install-{first_id}"]
        assert loader._publication_barrier_depth == 1
        journal = SkillTransactionJournal.load(service.journal_path)
        assert journal is not None and journal.install_operation_id == first_id
        release_rollback.set()
        first_result = await _result(first)
        second_result = await _result(second)
        await _result(reader)
        assert not first_result["success"]
        # Uninstall propagates cancellation; unlike install/update it does not
        # finish an install receipt itself. Check its restored files below.
        if not (cancelled and operation == "uninstall"):
            assert first_result["rollbackPerformed"]
        assert bool(first_result.get("cancelled")) is cancelled
        assert second_result["success"], second_result
        _assert_previous(service, loader, operation)
        assert (service.managed_dir / "second" / "SKILL.md").is_file()
        assert Lockfile.load(service.lockfile_path).get("second") is not None
        assert loader.get_by_name("second") is not None
        assert acquired == [
            f"skill-install-{first_id}", f"skill-install-{second_id}", "committed-reader",
        ]
        first_receipt = await service.install_operations.store.read_async("test-owner", first_id)
        second_receipt = await service.install_operations.store.read_async("test-owner", second_id)
        assert first_receipt["state"] == ("cancelled" if cancelled else "failed")
        assert second_receipt["state"] == "succeeded"
        assert not service.journal_path.exists()
        assert not service._mutation_lock.locked()
        assert loader._publication_barrier_depth == 0
    finally:
        if setup_lock_held:
            service._mutation_lock.release()
        release_postflight.set()
        release_rollback.set()
        if pending:
            await _drain(*pending)
        assert await service.close(timeout=5)


async def test_cancelled_lock_waiter_cleans_only_its_staging_and_keeps_owner_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, loader = await _setup(tmp_path, "install")
    loop = asyncio.get_running_loop()
    postflight, release = asyncio.Event(), threading.Event()
    real_reload = loader.reload_verified

    def paused_reload(*args, **kwargs):
        loop.call_soon_threadsafe(postflight.set)
        assert release.wait(5)
        return real_reload(*args, **kwargs)

    monkeypatch.setattr(loader, "reload_verified", paused_reload)
    _, first = _start(service, "install")
    pending = [first]
    try:
        await _thread_started(postflight)
        journal_before = service.journal_path.read_bytes()
        existing_staging = set(staging_root(service.managed_dir).iterdir())
        waiting = asyncio.Event()
        acquire = service._mutation_lock.acquire

        async def observe_acquire():
            waiting.set()
            return await acquire()

        monkeypatch.setattr(service._mutation_lock, "acquire", observe_acquire)
        second_id, second = _start(service, "install", "second")
        pending.append(second)
        await asyncio.wait_for(waiting.wait(), 5)
        assert set(staging_root(service.managed_dir).iterdir()) > existing_staging
        service.install_operations.tasks[("test-owner", second_id)].cancel()
        result = await _result(second)
        assert result["cancelled"] and not result["success"]
        assert service.journal_path.read_bytes() == journal_before
        assert set(staging_root(service.managed_dir).iterdir()) == existing_staging
        assert service._mutation_lock.locked()
        assert loader._publication_barrier_depth == 1
        assert not (service.managed_dir / "second").exists()
        release.set()
        assert (await _result(first))["success"]
        assert loader.get_by_name("first") is not None
        assert Lockfile.load(service.lockfile_path).get("first") is not None
    finally:
        release.set()
        await _drain(*pending)
        assert await service.close(timeout=5)


@pytest.mark.parametrize("failure", ["restore", "barrier_close"])
async def test_catalog_cleanup_exception_releases_lock_after_closing_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    service, loader = await _setup(tmp_path, "install")
    reload = loader.reload_verified
    restore = loader.restore_snapshot
    barrier = loader.catalog_publication_barrier
    failed_reload = False
    failed_cleanup = False
    closed_while_owned = []

    def reject_once(*args, **kwargs):
        nonlocal failed_reload
        if not failed_reload:
            failed_reload = True
            raise RuntimeError("postflight failed")
        return reload(*args, **kwargs)

    def failed_restore(*args, **kwargs):
        nonlocal failed_cleanup
        if not failed_cleanup:
            failed_cleanup = True
            assert service._mutation_lock.locked()
            raise RuntimeError("snapshot restoration failed")
        return restore(*args, **kwargs)

    @contextmanager
    def observed_barrier(*args, **kwargs):
        nonlocal failed_cleanup
        try:
            with barrier(*args, **kwargs) as publication:
                yield publication
        finally:
            closed_while_owned.append(service._mutation_lock.locked())
            if failure == "barrier_close" and not failed_cleanup:
                failed_cleanup = True
                raise RuntimeError("barrier close failed")

    monkeypatch.setattr(loader, "reload_verified", reject_once)
    monkeypatch.setattr(loader, "catalog_publication_barrier", observed_barrier)
    if failure == "restore":
        monkeypatch.setattr(loader, "restore_snapshot", failed_restore)
    _, first = _start(service, "install")
    pending = [first]
    try:
        result = await _result(first)
        assert not result["success"]
        assert result["message"] == (
            "snapshot restoration failed" if failure == "restore" else "barrier close failed"
        )
        assert closed_while_owned == [True]
        assert not service._mutation_lock.locked()
        assert loader._publication_barrier_depth == 0
        _assert_previous(service, loader, "install")
        _, second = _start(service, "install", "second")
        pending.append(second)
        assert (await _result(second))["success"]
        assert loader.get_by_name("second") is not None
    finally:
        await _drain(*pending)
        assert await service.close(timeout=5)


async def test_failed_disk_recovery_blocks_queued_writer_without_replacing_its_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, loader = await _setup(tmp_path, "install")
    loop = asyncio.get_running_loop()
    restoring, release = asyncio.Event(), threading.Event()

    def failed_postflight(*args, **kwargs):
        raise RuntimeError("postflight failed")

    def failed_restore(*args, **kwargs):
        loop.call_soon_threadsafe(restoring.set)
        assert release.wait(5)
        raise OSError("disk recovery unavailable")

    monkeypatch.setattr(loader, "reload_verified", failed_postflight)
    monkeypatch.setattr(transaction, "_restore_lockfile", failed_restore)
    first_id, first = _start(service, "install")
    pending = [first]
    try:
        await _thread_started(restoring)
        journal_before = service.journal_path.read_bytes()
        waiting = asyncio.Event()
        acquire = service._mutation_lock.acquire

        async def observe_acquire():
            waiting.set()
            return await acquire()

        monkeypatch.setattr(service._mutation_lock, "acquire", observe_acquire)
        second_id, second = _start(service, "install", "second")
        pending.append(second)
        await asyncio.wait_for(waiting.wait(), 5)
        assert service._mutation_lock.locked()
        release.set()
        assert (await _result(first))["recoveryRequired"]
        assert (await _result(second))["recoveryRequired"]
        assert service.journal_path.read_bytes() == journal_before
        assert loader.get_by_name("first") is None
        assert loader.get_by_name("second") is None
        assert not (service.managed_dir / "second").exists()
        assert not service._mutation_lock.locked()
        assert loader._publication_barrier_depth == 0
        for operation_id in (first_id, second_id):
            receipt = await service.install_operations.store.read_async("test-owner", operation_id)
            assert receipt["state"] == "recovery_required"
    finally:
        release.set()
        await _drain(*pending)
        assert await service.close(timeout=5)
