"""Slow filesystem calls must not stall Gateway or outlive memory ownership."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.memory.sync_manager import MemorySyncManager
from opensquilla.memory.turn_capture import TurnCaptureService


class BlockedIO:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.thread: int | None = None

    def wait(self) -> None:
        self.thread = threading.get_ident()
        self.entered.set()
        try:
            assert self.release.wait(10), "Test did not release the file operation"
        finally:
            self.finished.set()

    async def wait_entered(self) -> None:
        assert await asyncio.wait_for(asyncio.to_thread(self.entered.wait, 10), 12)
        assert not self.finished.is_set(), "File I/O blocked the event loop until its own timeout"


class RecordingStore:
    def __init__(self) -> None:
        self.loop_thread = threading.get_ident()
        self.files: dict[str, str] = {}
        self.indexed: list[tuple[str, str]] = []
        self.removed: list[str] = []

    async def index_file(self, *, path: str, content: str, source: object) -> int:
        assert threading.get_ident() == self.loop_thread
        self.files[path] = content
        self.indexed.append((path, content))
        return 1

    async def remove_file(self, path: str) -> None:
        assert threading.get_ident() == self.loop_thread
        self.files.pop(path, None)
        self.removed.append(path)


async def finish_within_deadline(operation):
    # wait_for cancels its task and can itself wait indefinitely for settled
    # application cancellation. Bound the observation without cancelling it.
    task = asyncio.ensure_future(operation)
    done, _ = await asyncio.wait({task}, timeout=10)
    assert task in done, "Memory operation did not finish within the test deadline"
    return task.result()


def make_manager(tmp_path, *, names=("a.md",), poll_interval=10):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    for name in names:
        (memory / name).write_text(f"initial {name}", encoding="utf-8")
    store = RecordingStore()
    manager = MemorySyncManager(store, workspace, memory, poll_interval=poll_interval)
    return manager, store, memory


@pytest_asyncio.fixture
async def health_client(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile))
    config = GatewayConfig(state_dir=str(profile), auth=AuthConfig(mode="none"))
    config.control_ui.enabled = False
    app = create_gateway_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://isolated.invalid",
    ) as client:
        assert (await client.get("/health")).status_code == 200
        yield client


@pytest.mark.parametrize("phase", ["initial_scan", "background_scan", "manual_read"])
async def test_memory_io_keeps_real_gateway_health_responsive(
    tmp_path, monkeypatch, health_client, phase,
):
    manager, store, memory = make_manager(tmp_path, poll_interval=0.01)
    block = BlockedIO()
    loop_thread = threading.get_ident()
    if phase == "background_scan":
        await finish_within_deadline(manager.start())
    if phase in {"initial_scan", "background_scan"}:
        original = Path.rglob

        def blocked_rglob(path, *args, **kwargs):
            if path == memory and not block.entered.is_set():
                block.wait()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", blocked_rglob)
    else:
        original = Path.read_text

        def blocked_read(path, *args, **kwargs):
            if path == memory / "a.md" and not block.entered.is_set():
                block.wait()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", blocked_read)

    if phase == "background_scan":
        operation = manager._poll_task
    else:
        operation = asyncio.create_task(
            manager.start() if phase == "initial_scan" else manager.sync("manual"),
        )
    try:
        await block.wait_entered()
        assert block.thread != loop_thread
        assert not operation.done()
        # The I/O remains blocked until finally. A timer threshold alone
        # could hide a request that only began after a stalled loop resumed.
        response = await asyncio.wait_for(health_client.get("/health"), 3)
        assert response.status_code == 200
        assert response.json() == {"ok": True, "status": "live"}
        assert not block.release.is_set() and not block.finished.is_set()
    finally:
        block.release.set()
        if phase != "background_scan":
            await finish_within_deadline(operation)
        await finish_within_deadline(manager.stop())
    assert store.indexed


async def test_concurrent_manual_syncs_serialize_scan_and_snapshot_application(
    tmp_path, monkeypatch,
):
    manager, store, memory = make_manager(tmp_path)
    block = BlockedIO()
    original = manager._scan_files
    scans = []

    def blocked_scan():
        snapshot = original()
        scans.append(snapshot)
        if len(scans) == 1:
            block.wait()
        return snapshot

    monkeypatch.setattr(manager, "_scan_files", blocked_scan)
    first = asyncio.create_task(manager.sync("manual"))
    second = None
    try:
        await block.wait_entered()
        (memory / "a.md").write_text("updated", encoding="utf-8")
        first_mtime = scans[0]["memory/a.md"]
        os.utime(memory / "a.md", (first_mtime + 5, first_mtime + 5))
        second = asyncio.create_task(manager.sync("manual"))
        await asyncio.sleep(0)
        assert len(scans) == 1 and not second.done()
    finally:
        block.release.set()
        await finish_within_deadline(first)
        if second is not None:
            await finish_within_deadline(second)
        await finish_within_deadline(manager.stop())
    assert len(scans) == 2
    assert manager._mtimes == scans[-1]
    assert scans[-1]["memory/a.md"] > scans[0]["memory/a.md"]
    assert store.files["memory/a.md"] == "updated"


async def test_repeated_sync_cancellation_drains_read_and_preserves_retry(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path)
    block = BlockedIO()
    original = Path.read_text
    reads = []

    def blocked_read(path, *args, **kwargs):
        if path == memory / "a.md":
            reads.append(threading.get_ident())
            if len(reads) == 1:
                block.wait()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    operation = asyncio.create_task(manager.sync("manual"))
    retry = None
    try:
        await block.wait_entered()
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and manager._operation_lock.locked()
        retry = asyncio.create_task(manager.sync("manual"))
        await asyncio.sleep(0)
        assert len(reads) == 1 and not retry.done()
    finally:
        block.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finish_within_deadline(operation)
        if retry is not None:
            await finish_within_deadline(retry)
        await finish_within_deadline(manager.stop())
    assert len(reads) == 2
    assert store.indexed == [("memory/a.md", "initial a.md")]
    assert all(thread != store.loop_thread for thread in reads)


async def test_stop_during_manual_read_preserves_unindexed_file_for_restart(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path)
    await finish_within_deadline(manager.start())
    (memory / "a.md").write_text("updated", encoding="utf-8")
    block = BlockedIO()
    original = Path.read_text

    def blocked_read(path, *args, **kwargs):
        if path == memory / "a.md" and not block.entered.is_set():
            block.wait()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    operation = asyncio.create_task(manager.sync("manual", force=True))
    stopping = None
    try:
        await block.wait_entered()
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert manager._stopped and not stopping.done()
        assert store.files["memory/a.md"] == "initial a.md"
    finally:
        block.release.set()
        await finish_within_deadline(operation)
        if stopping is not None:
            await finish_within_deadline(stopping)
        else:
            await finish_within_deadline(manager.stop())
    assert "memory/a.md" in manager._pending_changes
    assert store.files["memory/a.md"] == "initial a.md"
    await finish_within_deadline(manager.start())
    try:
        assert store.files["memory/a.md"] == "updated"
        assert not manager._pending_changes
    finally:
        await finish_within_deadline(manager.stop())


async def test_stop_during_delete_retains_not_yet_removed_keys_for_restart(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path, names=("a.md", "b.md"))
    await finish_within_deadline(manager.start())
    for path in memory.iterdir():
        path.unlink()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store.remove_file
    first_key = []

    async def blocked_remove(path):
        if not entered.is_set():
            first_key.append(path)
            entered.set()
            await release.wait()
        await original(path)

    monkeypatch.setattr(store, "remove_file", blocked_remove)
    operation = asyncio.create_task(manager.sync("manual"))
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert manager._stopped and not stopping.done()
    finally:
        release.set()
        await finish_within_deadline(operation)
        if stopping is not None:
            await finish_within_deadline(stopping)
        else:
            await finish_within_deadline(manager.stop())
    assert manager._pending_deletes == {"memory/a.md", "memory/b.md"} - set(first_key)
    await finish_within_deadline(manager.start())
    try:
        assert not store.files and not manager._pending_deletes
        assert set(store.removed) == {"memory/a.md", "memory/b.md"}
    finally:
        await finish_within_deadline(manager.stop())


async def test_stop_during_scan_keeps_previous_deletion_snapshot_for_restart(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path)
    await finish_within_deadline(manager.start())
    (memory / "a.md").unlink()
    block = BlockedIO()
    original = manager._scan_files

    def blocked_scan():
        snapshot = original()
        if not block.entered.is_set():
            block.wait()
        return snapshot

    monkeypatch.setattr(manager, "_scan_files", blocked_scan)
    operation = asyncio.create_task(manager.sync("manual"))
    stopping = None
    try:
        await block.wait_entered()
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert manager._stopped and not stopping.done()
    finally:
        block.release.set()
        await finish_within_deadline(operation)
        if stopping is not None:
            await finish_within_deadline(stopping)
        else:
            await finish_within_deadline(manager.stop())
    assert "memory/a.md" in manager._mtimes and store.files
    await finish_within_deadline(manager.start())
    try:
        assert not store.files and store.removed == ["memory/a.md"]
    finally:
        await finish_within_deadline(manager.stop())


async def test_repeated_stop_cancellation_waits_for_background_scan(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path, poll_interval=0.01)
    await finish_within_deadline(manager.start())
    block = BlockedIO()
    original = manager._scan_files

    def blocked_scan():
        if not block.entered.is_set():
            block.wait()
        return original()

    monkeypatch.setattr(manager, "_scan_files", blocked_scan)
    stopping = None
    try:
        await block.wait_entered()
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done() and manager._operation_lock.locked()
    finally:
        block.release.set()
        if stopping is not None:
            with pytest.raises(asyncio.CancelledError):
                await finish_within_deadline(stopping)
        else:
            await finish_within_deadline(manager.stop())
    assert manager._poll_task is None
    assert not manager._operation_lock.locked()
    assert manager._stop_operation.done()


async def test_stop_fences_start_already_queued_behind_initial_scan(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path)
    block = BlockedIO()
    original = manager._scan_files
    scans = []

    def blocked_scan():
        scans.append(threading.get_ident())
        if len(scans) == 1:
            block.wait()
        return original()

    monkeypatch.setattr(manager, "_scan_files", blocked_scan)
    first = asyncio.create_task(manager.start())
    queued_start = None
    stopping = None
    try:
        await block.wait_entered()
        queued_start = asyncio.create_task(manager.start())
        await asyncio.sleep(0)
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert manager._stopped and not queued_start.done()
    finally:
        block.release.set()
        await finish_within_deadline(first)
        if queued_start is not None:
            await finish_within_deadline(queued_start)
        if stopping is not None:
            await finish_within_deadline(stopping)
        else:
            await finish_within_deadline(manager.stop())
    assert len(scans) == 1
    assert not manager._running and manager._stopped
    assert manager._poll_task is None
    assert manager._stop_operation.done()
    await finish_within_deadline(manager.start())
    try:
        assert manager._running and manager._poll_task is not None
        assert store.files["memory/a.md"] == "initial a.md"
    finally:
        await finish_within_deadline(manager.stop())


async def test_start_requested_after_stop_waits_for_cancelled_initial_scan(tmp_path, monkeypatch):
    manager, store, memory = make_manager(tmp_path)
    block = BlockedIO()
    original = manager._scan_files

    def blocked_scan():
        if not block.entered.is_set():
            block.wait()
        return original()

    monkeypatch.setattr(manager, "_scan_files", blocked_scan)
    first = asyncio.create_task(manager.start())
    stopping = None
    restarting = None
    try:
        await block.wait_entered()
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        first.cancel()
        restarting = asyncio.create_task(manager.start())
        await asyncio.sleep(0)
        assert not stopping.done() and not restarting.done()
    finally:
        block.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finish_within_deadline(first)
        if stopping is not None:
            await finish_within_deadline(stopping)
        if restarting is not None:
            await finish_within_deadline(restarting)
    try:
        assert manager._running and not manager._stopped
        assert manager._poll_task is not None
        assert store.files["memory/a.md"] == "initial a.md"
    finally:
        await finish_within_deadline(manager.stop())


async def test_capture_cancellation_retains_append_lock_until_worker_finishes(
    tmp_path, monkeypatch,
):
    turns = tmp_path / "state" / "turns"
    service = TurnCaptureService(workspace_dir=tmp_path / "workspace", turns_dir=turns)
    block = BlockedIO()
    original = Path.write_text
    target = turns / "agent-main-main" / "2026-10-12.md"
    writes = []

    def blocked_write(path, *args, **kwargs):
        if path == target:
            writes.append(threading.get_ident())
            if len(writes) == 1:
                block.wait()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", blocked_write)

    async def capture(text):
        return await service.capture_turn(
            session_key="agent:main:main", session_id=text,
            user_text=text, assistant_text="", captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        )

    first = asyncio.create_task(capture("first"))
    second = None
    try:
        await block.wait_entered()
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(capture("second"))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert service._write_lock.locked() and len(writes) == 1
    finally:
        block.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finish_within_deadline(first)
        if second is not None:
            await finish_within_deadline(second)
    content = target.read_text(encoding="utf-8")
    assert content.count("### User\nfirst") == 1
    assert content.count("### User\nsecond") == 1
    assert len(writes) == 2 and all(thread != threading.get_ident() for thread in writes)


async def test_cancelled_capture_finishes_rollback_before_next_append(tmp_path, monkeypatch):
    turns = tmp_path / "state" / "turns"
    service = TurnCaptureService(workspace_dir=tmp_path / "workspace", turns_dir=turns)
    captured_at = datetime(2026, 10, 12, tzinfo=UTC)

    async def capture(text):
        return await service.capture_turn(
            session_key="agent:main:main", session_id=text,
            user_text=text, assistant_text="", captured_at=captured_at,
        )

    await finish_within_deadline(capture("seed"))
    target = turns / "agent-main-main" / "2026-10-12.md"
    original = Path.write_text
    block = BlockedIO()
    writes = []

    def partial_failed_write(path, data, *args, **kwargs):
        if path == target:
            writes.append(threading.get_ident())
            if len(writes) == 1:
                original(path, "partial failed write", *args, **kwargs)
                block.wait()
                raise OSError("Synthetic partial write failure")
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", partial_failed_write)
    first = asyncio.create_task(capture("failed"))
    second = None
    try:
        await block.wait_entered()
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(capture("next"))
        await asyncio.sleep(0)
        assert not first.done() and not second.done() and len(writes) == 1
    finally:
        block.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finish_within_deadline(first)
        if second is not None:
            await finish_within_deadline(second)
    content = target.read_text(encoding="utf-8")
    assert "### User\nseed" in content and "### User\nnext" in content
    assert "### User\nfailed" not in content and "partial failed write" not in content
    assert len(writes) == 3


@pytest.mark.parametrize("cutoff", ["stop", "cancel"])
async def test_initial_retention_preserves_failed_index_cleanup_across_restart(
    tmp_path, monkeypatch, cutoff,
):
    manager, store, memory = make_manager(tmp_path, names=("2020-01-01.md",))
    manager._ttl_days = 1
    manager._ttl_sweep_interval_minutes = 60
    target = memory / "2020-01-01.md"
    os.utime(target, (1, 1))
    store.files["memory/2020-01-01.md"] = "old"
    entered = asyncio.Event()
    release = asyncio.Event()
    original_remove = store.remove_file
    fails = True

    async def failing_remove(path):
        if fails:
            entered.set()
            await release.wait()
            raise RuntimeError("Synthetic index cleanup failure")
        await original_remove(path)

    monkeypatch.setattr(store, "remove_file", failing_remove)
    operation = asyncio.create_task(manager.start())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if cutoff == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
        else:
            stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
        if cutoff == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await finish_within_deadline(operation)
        else:
            await finish_within_deadline(operation)
            if stopping is not None:
                await finish_within_deadline(stopping)
    assert not target.exists()
    assert manager._pending_deletes == {"memory/2020-01-01.md"}
    assert store.files
    fails = False
    await finish_within_deadline(manager.start())
    try:
        assert not store.files and not manager._pending_deletes
    finally:
        await finish_within_deadline(manager.stop())
