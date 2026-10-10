from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import opensquilla.skills.loader as loader_module
import opensquilla.skills.watcher as watcher_module
from opensquilla.engine.runtime import TurnRunner


def _write_skill(root: Path, name: str = "demo", body: str = "body") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _loader(tmp_path: Path) -> tuple[loader_module.SkillLoader, Path]:
    root = tmp_path / "skills"
    root.mkdir()
    loader = loader_module.SkillLoader(
        bundled_dir=root,
        snapshot_path=tmp_path / "snapshot.json",
        lockfile_path=tmp_path / "skills-lock.json",
    )
    return loader, root


def test_clean_turn_snapshot_does_not_probe_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    loader.snapshot_for_turn()

    calls = 0

    def fail_probe(_skill_dir: Path) -> dict[str, int | str]:
        nonlocal calls
        calls += 1
        raise AssertionError("clean turn probed the Skill tree")

    monkeypatch.setattr("opensquilla.skills.loader.compute_tree_state", fail_probe)
    snapshot = loader.snapshot_for_turn()

    assert snapshot.generation == 1
    assert calls == 0


def test_dirty_turn_refreshes_and_publishes_new_generation(tmp_path: Path) -> None:
    loader, root = _loader(tmp_path)
    skill_dir = _write_skill(root)
    first = loader.snapshot_for_turn()

    (skill_dir / "nested").mkdir()
    (skill_dir / "nested" / ".hidden").write_text("changed", encoding="utf-8")
    loader.mark_dirty("watch")
    second = loader.snapshot_for_turn()

    assert second.generation == first.generation + 1
    assert second.manifest != first.manifest


def test_explicit_reload_keeps_full_scan_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    loader.snapshot_for_turn()
    calls = 0
    real_tree_state = loader_module.compute_tree_state

    def counted(path: Path) -> dict[str, int | str]:
        nonlocal calls
        calls += 1
        return real_tree_state(path)

    monkeypatch.setattr(loader_module, "compute_tree_state", counted)
    loader.reload(force=True, reason="test.reload")

    assert calls > 0


def test_publication_barrier_returns_visible_lkg_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    baseline = loader.snapshot_for_turn()
    monkeypatch.setattr(
        loader_module,
        "compute_tree_state",
        lambda _path: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )

    with loader.catalog_publication_barrier():
        loader.mark_dirty("management")
        assert loader.snapshot_for_turn() is baseline


def test_slow_probe_records_completion_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    real_manifest = loader._build_manifest

    def slow_manifest() -> dict[str, dict[str, int | str]]:
        import time

        time.sleep(0.03)
        return real_manifest()

    monkeypatch.setattr(loader, "_build_manifest", slow_manifest)
    started = time.monotonic()
    loader.load_all()
    finished = time.monotonic()

    assert started <= loader._last_probe_at <= finished


def test_turn_runner_prefers_snapshot_for_turn() -> None:
    snapshot = object()
    loader = SimpleNamespace(snapshot_for_turn=Mock(return_value=snapshot))
    runner = TurnRunner(provider_selector=None, skill_loader=loader)

    assert runner._resolve_skill_catalog() is snapshot
    loader.snapshot_for_turn.assert_called_once_with(reason="turn")


def test_turn_runner_keeps_legacy_loader_fallback() -> None:
    snapshot = object()
    loader = SimpleNamespace(
        refresh_if_changed=Mock(),
        snapshot=Mock(return_value=snapshot),
    )
    runner = TurnRunner(provider_selector=None, skill_loader=loader)

    assert runner._resolve_skill_catalog() is snapshot
    loader.refresh_if_changed.assert_called_once_with(reason="turn")


@pytest.mark.asyncio
async def test_polling_fallback_invalidates_nested_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(watcher_module, "_awatch", None)
    loader, root = _loader(tmp_path)
    skill_dir = _write_skill(root)
    loader.snapshot_for_turn()
    changes: list[str] = []
    original_mark_dirty = loader.mark_dirty
    monkeypatch.setattr(
        loader,
        "mark_dirty",
        lambda reason="mutation": (changes.append(reason), original_mark_dirty(reason))[1],
    )
    watcher = watcher_module.SkillCatalogWatcher(loader, poll_interval=0.01)
    await watcher.start()
    try:
        # start() schedules the initial scan; wait for its baseline before
        # mutating the tree so a slow worker cannot absorb the change into it.
        async with asyncio.timeout(5):
            while watcher._poll_signature is None:
                await asyncio.sleep(0.01)
        (skill_dir / "nested").mkdir()
        (skill_dir / "nested" / ".hidden").write_text("changed", encoding="utf-8")
        async with asyncio.timeout(5):
            while "poll" not in changes:
                await asyncio.sleep(0.01)
    finally:
        await watcher.stop()

    assert watcher.task is None
    assert "poll" in changes


@pytest.mark.asyncio
async def test_native_watcher_debounces_to_one_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    invalidations: list[str] = []
    monkeypatch.setattr(loader, "mark_dirty", invalidations.append)

    async def fake_awatch(*_paths: Path, **kwargs: object):
        yield {("modified", str(root / "demo" / "SKILL.md"))}
        await asyncio.sleep(3600)

    monkeypatch.setattr(watcher_module, "_awatch", fake_awatch)
    watcher = watcher_module.SkillCatalogWatcher(loader, debounce_ms=1)
    await watcher.start()
    for _ in range(20):
        if invalidations:
            break
        await asyncio.sleep(0.01)
    await watcher.stop()

    assert invalidations == ["watch"]


@pytest.mark.asyncio
async def test_native_watcher_filesystem_probes_leave_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Root existence checks must run off the Gateway event-loop thread."""
    loader, root = _loader(tmp_path)
    _write_skill(root)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    real_is_dir = Path.is_dir

    def guarded_is_dir(path: Path) -> bool:
        probe_threads.append(threading.get_ident())
        if threading.get_ident() == main_thread:
            raise AssertionError("watcher performed a filesystem probe on the event loop")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    async def fake_awatch(*_paths: Path, **kwargs: object):
        yield {("modified", str(root / "demo" / "SKILL.md"))}
        stop_event = kwargs["stop_event"]
        assert isinstance(stop_event, asyncio.Event)
        await stop_event.wait()

    monkeypatch.setattr(watcher_module, "_awatch", fake_awatch)
    watcher = watcher_module.SkillCatalogWatcher(loader, debounce_ms=1)
    stop_event = asyncio.Event()
    task = asyncio.create_task(watcher._watch_loop(stop_event))
    try:
        async with asyncio.timeout(5):
            while len(probe_threads) < 3:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
    finally:
        stop_event.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5)

    assert len(probe_threads) >= 3  # roots + current presence + initial presence
    assert all(thread_id != main_thread for thread_id in probe_threads)


@pytest.mark.asyncio
async def test_native_watcher_loader_callbacks_do_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A catalog lock held by a loader callback must not stall control tasks."""
    root = tmp_path / "skills"
    root.mkdir()
    main_thread = threading.get_ident()
    roots_release = threading.Event()
    dirty_entered = threading.Event()
    dirty_release = threading.Event()
    dirty_done = threading.Event()
    callback_threads: list[tuple[str, int]] = []
    invalidations: list[str] = []

    class BlockingLoader:
        def watch_roots(self) -> tuple[Path, ...]:
            callback_threads.append(("roots", threading.get_ident()))
            if threading.get_ident() == main_thread:
                raise AssertionError("watch_roots ran on the event loop")
            assert roots_release.wait(timeout=5)
            return (root,)

        def mark_dirty(self, reason: str) -> None:
            callback_threads.append(("dirty", threading.get_ident()))
            if threading.get_ident() == main_thread:
                raise AssertionError("mark_dirty ran on the event loop")
            invalidations.append(reason)
            dirty_entered.set()
            assert dirty_release.wait(timeout=5)
            dirty_done.set()

    async def fake_awatch(*_paths: Path, **kwargs: object):
        yield {("modified", str(root / "SKILL.md"))}
        stop_event = kwargs["stop_event"]
        assert isinstance(stop_event, asyncio.Event)
        await stop_event.wait()

    monkeypatch.setattr(watcher_module, "_awatch", fake_awatch)
    watcher = watcher_module.SkillCatalogWatcher(BlockingLoader())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    gaps: list[float] = []
    heartbeat_stop = asyncio.Event()

    async def heartbeat() -> None:
        previous = loop.time()
        while not heartbeat_stop.is_set():
            await asyncio.sleep(0.01)
            current = loop.time()
            gaps.append(current - previous)
            previous = current

    async def release_dirty_after_callback() -> None:
        await asyncio.to_thread(dirty_entered.wait, 5)
        await asyncio.sleep(0.25)
        dirty_release.set()

    # Hold the equivalent of the loader refresh lock in the callback.  A
    # timer releases it from another thread so the pre-fix synchronous path
    # cannot deadlock the test; it still produces a visible event-loop gap.
    threading.Timer(0.25, roots_release.set).start()
    task = asyncio.create_task(watcher._watch_loop(stop_event))
    heartbeat_task = asyncio.create_task(heartbeat())
    release_task = asyncio.create_task(release_dirty_after_callback())
    try:
        async with asyncio.timeout(5):
            await asyncio.to_thread(dirty_done.wait, 5)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        heartbeat_stop.set()
        await heartbeat_task
        await release_task
        if not task.done():
            task.cancel()
            await task

    assert invalidations == ["watch"]
    assert callback_threads
    assert all(thread_id != main_thread for _kind, thread_id in callback_threads)
    assert gaps and max(gaps) < 0.2


@pytest.mark.asyncio
async def test_watcher_shutdown_is_idempotent(tmp_path: Path) -> None:
    loader, root = _loader(tmp_path)
    _write_skill(root)
    watcher = watcher_module.SkillCatalogWatcher(loader, poll_interval=0.01)
    await watcher.start()
    await watcher.stop()
    await watcher.stop()

    assert watcher.task is None
