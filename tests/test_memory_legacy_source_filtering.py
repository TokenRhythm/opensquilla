from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.memory.retrieval import MemoryRetriever
from opensquilla.memory.source_paths import (
    is_legacy_memory_capture,
    is_legacy_memory_capture_file,
    is_legacy_raw_fallback,
    is_legacy_turn_capture,
)
from opensquilla.memory.store import LongTermMemoryStore
from opensquilla.memory.sync_manager import MemorySyncManager
from opensquilla.memory.types import MemorySearchOpts, MemorySearchResult, MemorySource

RAW_PATH = "memory/2026-01-01-reset-1700000000.md"
TURN_PATH = "memory/archive/2026-01-01/turn.md"
NOTE_PATH = "memory/archive/design.md"
RAW = "# Raw flush (reset)\n\nprivate raw history\n"
TURN = (
    "# Turn Capture\n- source_kind: turn_capture\n"
    "- schema: turn-capture-v1\n\nprivate turn history\n"
)
NOTE = "# Archived design\nThis is an ordinary user-maintained note.\n"


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        (RAW_PATH, RAW, True),
        (RAW_PATH, "User's reset checklist", False),
        ("memory/my-notes.md", RAW, False),
        (f"memory/nested/{Path(RAW_PATH).name}", RAW, False),
        (TURN_PATH, TURN, True),
        (TURN_PATH, "# Turn Capture\nA user's ordinary note", False),
        (NOTE_PATH, NOTE, False),
        ("memory/topic.md", TURN, False),
        ("memory/archive/turn.md", "x" * 1000 + TURN, False),
        ("../memory/archive/turn.md", TURN, False),
    ],
)
def test_legacy_classification_requires_generated_markers_and_historical_location(
    path, content, expected,
):
    assert is_legacy_memory_capture(path, content) is expected


def test_classifier_helpers_are_bounded_read_only(tmp_path):
    assert is_legacy_raw_fallback(Path(RAW_PATH), RAW)
    assert is_legacy_turn_capture(TURN)
    path = tmp_path / "turn.md"
    path.write_text(TURN + "x" * 100_000, encoding="utf-8")
    before = path.read_bytes()

    assert is_legacy_memory_capture_file(path, relative_path=TURN_PATH)

    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["turn.md"]


def _write_sources(workspace: Path) -> dict[str, bytes]:
    for relative_path, content in ((RAW_PATH, RAW), (TURN_PATH, TURN), (NOTE_PATH, NOTE)):
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*.md")}


@pytest.mark.asyncio
async def test_restart_reconciles_existing_index_without_moving_sources(tmp_path):
    workspace = tmp_path / "workspace"
    before = _write_sources(workspace)
    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    try:
        for path, content in ((RAW_PATH, RAW), (TURN_PATH, TURN), (NOTE_PATH, NOTE)):
            await store.index_file(path=path, content=content, source=MemorySource.memory)
        await store.index_file(
            path="sessions/main/old.md", content="session history", source=MemorySource.sessions,
        )
        manager = MemorySyncManager(store, workspace, workspace / "memory")
        assert not manager._mtimes

        await manager.sync(reason="manual")

        assert await store.list_paths(source=MemorySource.memory) == [NOTE_PATH]
        assert await store.list_paths(source=MemorySource.sessions) == ["sessions/main/old.md"]
        assert {
            p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*.md")
        } == before
        assert not (workspace / "memory" / ".raw_fallbacks").exists()
    finally:
        await store.close()


class _FailingCleanupStore:
    def __init__(self) -> None:
        self.paths = {RAW_PATH, TURN_PATH, NOTE_PATH}
        self.fail_remove = True
        self.fail_list = False

    async def list_paths(self, source):
        if self.fail_list:
            raise OSError("index unavailable")
        assert source is MemorySource.memory
        return sorted(self.paths)

    async def index_file(self, *, path, content, source):
        self.paths.add(path)
        return 1

    async def remove_file(self, path):
        if self.fail_remove:
            raise OSError("index locked")
        self.paths.discard(path)

    async def search(self, **kwargs):
        # Simulate chunks whose identifying header was in an earlier chunk.
        return [
            MemorySearchResult(
                chunk_id=path, path=path, source=MemorySource.memory,
                start_line=50, end_line=51, snippet="history", score=0.9,
            )
            for path in sorted(self.paths)
        ], "fts_only"


@pytest.mark.asyncio
async def test_failed_cleanup_cannot_return_capture_chunks_and_retries(tmp_path):
    workspace = tmp_path / "workspace"
    before = _write_sources(workspace)
    store = _FailingCleanupStore()
    manager = MemorySyncManager(store, workspace, workspace / "memory")
    retriever = MemoryRetriever(store, sync_manager=manager, workspace_dir=workspace)
    await manager.sync(reason="manual")

    results = await retriever.search("history", MemorySearchOpts(min_score=0.0))

    assert [r.path for r in results] == [NOTE_PATH]
    assert manager._pending_deletes == {RAW_PATH, TURN_PATH}
    store.fail_remove = False
    await manager.sync(reason="search:tool")
    assert store.paths == {NOTE_PATH}
    assert not manager._pending_deletes
    assert {
        p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*.md")
    } == before


@pytest.mark.asyncio
async def test_index_listing_failure_remains_dirty_until_reconciled(tmp_path):
    workspace = tmp_path / "workspace"
    _write_sources(workspace)
    store = _FailingCleanupStore()
    store.fail_list = True
    store.fail_remove = False
    manager = MemorySyncManager(store, workspace, workspace / "memory")

    await manager.sync(reason="manual")

    assert manager._dirty
    assert not manager._index_paths_reconciled
    store.fail_list = False
    await manager.sync(reason="search:tool")
    assert manager._index_paths_reconciled
    assert not manager._dirty
    assert store.paths == {NOTE_PATH}


@pytest.mark.asyncio
async def test_archive_note_quoting_capture_schema_after_prefix_stays_searchable(tmp_path):
    workspace = tmp_path / "workspace"
    _write_sources(workspace)
    (workspace / NOTE_PATH).write_text(NOTE + "x" * 1000 + TURN, encoding="utf-8")
    store = _FailingCleanupStore()
    store.paths = {NOTE_PATH}

    async def search(**kwargs):
        return [MemorySearchResult(
            chunk_id="quoted-schema", path=NOTE_PATH, source=MemorySource.memory,
            start_line=50, end_line=54, snippet=TURN, text=TURN, score=0.9,
        )], "fts_only"

    store.search = search
    retriever = MemoryRetriever(store, workspace_dir=workspace)

    results = await retriever.search("capture", MemorySearchOpts(min_score=0.0))

    assert [r.path for r in results] == [NOTE_PATH]


@pytest.mark.asyncio
async def test_ttl_rechecks_dynamic_maintenance_permission(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    target = memory / "2020-01-01.md"
    target.write_text("old user note", encoding="utf-8")
    os.utime(target, (1, 1))
    allowed = False
    store = _FailingCleanupStore()
    store.paths.clear()
    store.fail_remove = False
    manager = MemorySyncManager(
        store, workspace, memory, ttl_days=1, ttl_sweep_interval_minutes=60,
        maintenance_allowed=lambda: allowed,
    )

    await manager._do_ttl_sweep(initial=True)
    assert target.read_text(encoding="utf-8") == "old user note"

    allowed = True
    await manager._do_ttl_sweep()
    assert not target.exists()

    target.write_text("later protected note", encoding="utf-8")
    os.utime(target, (1, 1))
    allowed = False
    await manager._do_ttl_sweep()
    assert target.read_text(encoding="utf-8") == "later protected note"
