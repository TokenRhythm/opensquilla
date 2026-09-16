from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path

import pytest

from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.scanner import ScanResult
from opensquilla.skills.io_worker import check_staging_cancelled, run_staging_worker
from tests.test_skills.test_hub_management_service import FakeImmutableSource


@pytest.mark.asyncio
async def test_scan_keeps_status_responsive_and_cancel_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    exited = threading.Event()
    main_thread = threading.get_ident()

    def scanning(directory: Path) -> ScanResult:
        assert threading.get_ident() != main_thread
        loop.call_soon_threadsafe(entered.set)
        end = time.monotonic() + 5
        try:
            while time.monotonic() < end:
                check_staging_cancelled()
                time.sleep(0.002)
            raise AssertionError("scan cancellation did not reach its worker")
        finally:
            exited.set()

    monkeypatch.setattr("opensquilla.skills.hub.management.scan_skill_tree", scanning)
    managed = tmp_path / "skills"
    service = SkillManagementService(
        router=SourceRouter([FakeImmutableSource({
            "SKILL.md": "---\nname: demo\ndescription: A synthetic sample.\n---\n# Demo\n",
        })]), managed_dir=managed,
        lockfile_path=tmp_path / "lock.json", journal_path=tmp_path / "state" / "journal.json",
    )
    operations = service.install_operations
    operation_id = str(uuid.uuid4())

    async def install():
        return (await service.install("demo", "fake")).to_dict()

    task = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    await asyncio.wait_for(entered.wait(), timeout=3)
    status = operations.store.read("owner", operation_id)
    assert status["state"] == "running"
    assert status["phase"] == "scanning"
    assert not exited.is_set()
    result = await operations.cancel("owner", operation_id)
    assert result["cancelled"]
    assert (await task)["cancelled"]
    assert exited.is_set()
    assert not (managed / "demo").exists()
    assert not list((managed / ".opensquilla-staging").glob("*"))


@pytest.mark.asyncio
async def test_repeated_cancel_joins_worker_before_returning() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = threading.Event()
    exited = threading.Event()

    def working() -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            for _ in range(2500):
                check_staging_cancelled()
                time.sleep(0.002)
        finally:
            loop.call_soon_threadsafe(cleanup_started.set)
            release_cleanup.wait(timeout=3)
            exited.set()

    task = asyncio.create_task(run_staging_worker(working))
    await entered.wait()
    task.cancel("original cancellation")
    await cleanup_started.wait()
    task.cancel("repeated cancellation")
    await asyncio.sleep(0)
    assert not task.done()
    assert not exited.is_set()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert exited.is_set()
    # Cancellation is scoped to that worker, never to subsequent ordinary reads.
    check_staging_cancelled()
    assert await run_staging_worker(lambda: "next operation") == "next operation"
