from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path

import pytest

from opensquilla.skills.hub import management
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.scanner import ScanResult
from opensquilla.skills.io_worker import check_staging_cancelled, run_staging_worker
from tests.test_skills.test_hub_management_service import FakeImmutableSource


# Keep this real-worker integration contract outside the xdist pool. Install
# preparation has a separate guard from the scanner responsiveness assertions.
@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_scan_keeps_status_responsive_and_cancel_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    scan_ready = asyncio.Event()
    release_scan = asyncio.Event()
    entered = asyncio.Event()
    exited = threading.Event()
    main_thread = threading.get_ident()
    last_progress_requested = "admission"
    report_progress = management.report_install_progress_async

    async def observed_progress(phase: str, **counts) -> None:
        nonlocal last_progress_requested
        last_progress_requested = phase
        await report_progress(phase, **counts)
        if phase == "scanning":
            # Cold SQLite receipts and staging preparation are setup costs.
            # Start the scanner-entry guard only after that real work settles.
            scan_ready.set()
            await release_scan.wait()

    monkeypatch.setattr(management, "report_install_progress_async", observed_progress)

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

    def task_state(current: asyncio.Task) -> dict[str, object]:
        state: dict[str, object] = {"done": current.done(), "cancelled": current.cancelled()}
        if current.done() and not current.cancelled():
            error = current.exception()
            state["outcome"] = repr(error if error is not None else current.result())[:2000]
        awaited = current.get_coro()
        stack = []
        for _ in range(20):
            frame = getattr(awaited, "cr_frame", None) or getattr(awaited, "gi_frame", None)
            if frame is not None:
                stack.append(
                    f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}",
                )
                if frame.f_code.co_name in {"run_operation_worker", "run_staging_worker"}:
                    function = frame.f_locals.get("function")
                    state["worker_function"] = getattr(function, "__qualname__", "unknown")
            awaited = getattr(awaited, "cr_await", None) or getattr(awaited, "gi_yieldfrom", None)
            if awaited is None:
                break
        state["await_stack"] = stack
        return state

    task = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    bootstrap_waiter = asyncio.create_task(scan_ready.wait())
    entry_waiter = asyncio.create_task(entered.wait())
    failure: BaseException | None = None
    try:
        await asyncio.wait(
            {bootstrap_waiter, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED,
        )
        if scan_ready.is_set():
            release_scan.set()
            await asyncio.wait(
                {entry_waiter, task}, timeout=3, return_when=asyncio.FIRST_COMPLETED,
            )
        if not entered.is_set():
            install_finished = task.done()
            reason = (
                "install finished before scanning" if install_finished else (
                    "scan entry exceeded 3s" if scan_ready.is_set()
                    else "install preparation exceeded 30s"
                )
            )
            details: dict[str, object] = {
                "install": task_state(task),
                "last_progress_requested": last_progress_requested,
                "scan_ready": scan_ready.is_set(),
                "install_waiter_done": install_finished,
                "scanner_exited": exited.is_set(),
            }
            owned = operations.tasks.get(("owner", operation_id))
            if owned is not None:
                details["owned_install"] = task_state(owned)
            details["claim"] = [
                task_state(pending) for pending in asyncio.all_tasks()
                if pending.get_name() == f"skill-install-claim-{operation_id}"
            ]
            try:
                # Failure-only observation neither initializes nor writes the store.
                details["receipt"] = await asyncio.wait_for(
                    asyncio.to_thread(operations.store.read_existing, "owner", operation_id),
                    timeout=0.5,
                )
            except Exception as exc:
                details["receipt_error"] = repr(exc)
            if install_finished:
                pytest.fail(f"{reason}: {details}")
            error = TimeoutError(reason)
            error.add_note(f"install startup diagnostics: {details}")
            raise error
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
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        try:
            release_scan.set()
            bootstrap_waiter.cancel()
            entry_waiter.cancel()
            await asyncio.gather(bootstrap_waiter, entry_waiter, return_exceptions=True)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            # Cancelling the outer waiter alone leaves the shielded install running.
            assert await service.close(timeout=5), "install owners did not settle during cleanup"
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            primary = failure if failure is not None else cleanup_errors.pop(0)
            for cleanup_error in cleanup_errors:
                primary.add_note(f"install cleanup failed: {cleanup_error!r}")
            if failure is None:
                raise primary


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
