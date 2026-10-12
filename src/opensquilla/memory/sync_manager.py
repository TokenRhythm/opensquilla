"""Unified memory sync trigger manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from .source_paths import is_memory_source_path
from .store import LongTermMemoryStore
from .types import MemorySource

logger = structlog.get_logger(__name__)


@dataclass
class SessionDeltaTracker:
    """Track accumulated session changes to avoid over-indexing."""

    delta_bytes_threshold: int = 100_000  # 100KB
    delta_messages_threshold: int = 50
    _pending_bytes: int = field(default=0, init=False, repr=False)
    _pending_messages: int = field(default=0, init=False, repr=False)

    def record(self, byte_count: int, message_count: int = 1) -> None:
        self._pending_bytes += byte_count
        self._pending_messages += message_count

    def should_sync(self) -> bool:
        return (
            self._pending_bytes >= self.delta_bytes_threshold
            or self._pending_messages >= self.delta_messages_threshold
        )

    def has_pending(self) -> bool:
        return self._pending_bytes > 0 or self._pending_messages > 0

    def reset(self) -> None:
        self._pending_bytes = 0
        self._pending_messages = 0


class MemorySyncManager:
    """Manages all memory sync triggers through a unified sync() entry point.

    Trigger points:
      1. session-start  — first time a session key is seen
      2. search         — before search, if dirty
      3. watch          — file changes detected by polling
      4. timer          — periodic interval (optional, default off)
      5. session-delta  — accumulated byte/message threshold
      6. post-compaction — after context compaction

    Background TTL sweep runs as an independent loop (NOT routed through
    ``sync()``) so retention semantics stay separate from index sync.
    """

    def __init__(
        self,
        store: LongTermMemoryStore,
        workspace_dir: str | Path,
        memory_dir: str | Path,
        poll_interval: float = 2.0,
        debounce_seconds: float = 1.5,
        interval_minutes: float = 0.0,
        ttl_days: int = 0,
        ttl_sweep_interval_minutes: float = 0.0,
        session_indexer: Any | None = None,
    ) -> None:
        self._store = store
        self._workspace_dir = Path(workspace_dir).expanduser().resolve()
        self._memory_dir = Path(memory_dir).expanduser().resolve()
        self._poll_interval = poll_interval
        self._debounce_seconds = debounce_seconds
        self._interval_minutes = interval_minutes
        self._ttl_days = ttl_days
        self._ttl_sweep_interval_minutes = ttl_sweep_interval_minutes
        self._session_indexer = session_indexer

        self._dirty = False
        self._warmed_sessions: set[str] = set()
        self._delta = SessionDeltaTracker()
        self._mtimes: dict[str, float] = {}
        self._pending_changes: set[str] = set()
        self._pending_deletes: set[str] = set()
        self._last_change_time: float = 0.0

        self._poll_task: asyncio.Task[None] | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._ttl_sweep_task: asyncio.Task[None] | None = None
        self._running = False

    # --- Public API ---

    def _ttl_enabled(self) -> bool:
        return self._ttl_days > 0 and self._ttl_sweep_interval_minutes > 0

    async def start(self) -> None:
        """Start the file watcher, optional timer, and optional TTL sweep.

        Critical ordering: TTL sweep runs **before** the initial
        ``_do_file_sync()`` so we don't waste an embed/index pass on files
        we are about to delete.

        Initial sync still runs with empty _mtimes so every surviving
        disk file is seen as "new". index_file() skips unchanged files
        via hash check, so normal restarts add no overhead.
        """
        if self._running:
            return
        self._running = True
        # 1. TTL sweep FIRST — drop expired files before we waste cycles
        #    embedding content we are about to throw away.
        if self._ttl_enabled():
            await self._do_ttl_sweep(initial=True)
        # 2. THEN initial sync — surviving files get indexed.
        await self._do_file_sync()
        await self._do_session_sync(reason="initial")
        # 3. Now start background loops.
        self._poll_task = asyncio.create_task(self._poll_loop())
        if self._interval_minutes > 0:
            self._timer_task = asyncio.create_task(self._timer_loop())
        if self._ttl_enabled():
            self._ttl_sweep_task = asyncio.create_task(self._ttl_sweep_loop())
        logger.info("sync_manager.started")

    async def stop(self) -> None:
        """Stop all background tasks."""
        self._running = False
        for task in (self._poll_task, self._timer_task, self._ttl_sweep_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._timer_task = None
        self._ttl_sweep_task = None
        logger.info("sync_manager.stopped")

    async def sync(self, reason: str, *, force: bool = False) -> None:
        """Unified sync entry point.

        Re-enqueues ``store.remove_file`` failures into
        ``_pending_deletes`` so a transient SQLite lock does not lose
        the path forever. Without this the first watcher tick would clear
        the queue regardless of outcome and orphan SQLite chunks for any
        path whose retry also failed.
        """
        is_search_reason = reason == "search" or reason.startswith("search:")
        session_delta_pending = self._delta.has_pending()
        if is_search_reason and not self._dirty and not force and not session_delta_pending:
            return
        if reason == "session-delta" and not self._delta.should_sync() and not force:
            return

        logger.info("sync_manager.sync", reason=reason, force=force)
        if reason == "watch" and (self._pending_changes or self._pending_deletes):
            # Snapshot AND clear before sync so that re-enqueued failures
            # land in a clean set (they survive into the next tick rather
            # than getting wiped by a post-sync .clear()).
            changes = set(self._pending_changes)
            deletes = set(self._pending_deletes)
            self._pending_changes.clear()
            self._pending_deletes.clear()
            failed_deletes = await self._do_file_sync(
                changes=changes, deletes=deletes
            )
        else:
            failed_deletes = await self._do_file_sync(force=force)

        session_sync_failed = await self._do_session_sync(reason=reason, force=force)

        if failed_deletes:
            self._pending_deletes.update(failed_deletes)
            self._dirty = True
            logger.warning(
                "sync_manager.deletes_requeued",
                reason=reason,
                paths=sorted(failed_deletes),
            )
        else:
            # Don't clobber _dirty=True set by a concurrent _do_ttl_sweep
            # (separate background task). If anything is still queued,
            # leave _dirty truthy so the next search-time sync retries
            # without waiting for poll.
            self._dirty = bool(
                self._pending_changes or self._pending_deletes or session_sync_failed
            )

        if reason == "session-delta" or (
            is_search_reason
            and session_delta_pending
            and self._session_indexer is not None
            and not session_sync_failed
        ):
            self._delta.reset()

    async def warm_session(self, session_key: str) -> None:
        """Trigger 1: sync on first session access."""
        if session_key in self._warmed_sessions:
            return
        self._warmed_sessions.add(session_key)
        await self.sync(reason="session-start")

    def mark_dirty(self) -> None:
        """Trigger 6: flag that a sync is needed (e.g. after compaction)."""
        self._dirty = True

    def notify_message(self, byte_count: int) -> None:
        """Trigger 5: accumulate session delta."""
        self._delta.record(byte_count=byte_count)

    # --- Internal ---

    def _scan_files(self) -> dict[str, float]:
        """Scan watched paths and return {relative_path: mtime}."""
        result: dict[str, float] = {}
        memory_md = self._workspace_dir / "MEMORY.md"
        if memory_md.is_file():
            result["MEMORY.md"] = memory_md.stat().st_mtime
        if self._memory_dir.is_dir():
            for path in self._memory_dir.rglob("*.md"):
                if path.is_file():
                    rel_to_memory = path.relative_to(self._memory_dir)
                    if any(part.startswith(".") for part in rel_to_memory.parts[:-1]):
                        continue
                    rel = path.relative_to(self._workspace_dir).as_posix()
                    if not is_memory_source_path(rel):
                        continue
                    result[rel] = path.stat().st_mtime
        return result

    async def _do_file_sync(
        self,
        *,
        changes: set[str] | None = None,
        deletes: set[str] | None = None,
        force: bool = False,
    ) -> set[str]:
        """Re-index changed and deleted files from disk.

        Returns the set of delete paths whose ``store.remove_file`` raised
        (anything other than success). Caller is expected to re-enqueue
        them so a transient SQLite lock does not orphan chunks. Index
        failures stay log-only because the file is still on disk and the
        next watcher tick will rediscover it via mtime.
        """
        if changes is None or deletes is None:
            current = self._scan_files()

            changes = set()
            deletes = set()

            for path, mtime in current.items():
                old_mtime = self._mtimes.get(path)
                if force or old_mtime is None or mtime > old_mtime:
                    changes.add(path)

            for path in set(self._mtimes) - set(current):
                deletes.add(path)

            self._mtimes = current

        failed_deletes: set[str] = set()
        for rel_path in deletes:
            try:
                await self._store.remove_file(rel_path)
                logger.info("sync_manager.removed", path=rel_path)
            except Exception:
                failed_deletes.add(rel_path)
                logger.warning("sync_manager.remove_failed", path=rel_path)

        for rel_path in changes:
            abs_path = self._workspace_dir / rel_path
            if not abs_path.is_file():
                continue
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                n = await self._store.index_file(
                    path=rel_path,
                    content=content,
                    source=MemorySource.memory,
                )
                if n > 0:
                    logger.info("sync_manager.indexed", path=rel_path, chunks=n)
            except Exception:
                logger.warning("sync_manager.index_failed", path=rel_path)

        return failed_deletes

    async def _do_session_sync(self, *, reason: str, force: bool = False) -> bool:
        """Sync the derived sessions source when the current trigger can affect it.

        Returns True when sync failed, allowing callers to keep the manager dirty
        for a later search/manual retry.
        """
        if self._session_indexer is None:
            return False
        should_sync = force or reason in {
            "initial",
            "manual",
            "session-start",
            "session-delta",
        } or reason.startswith("search:")
        if not should_sync:
            return False
        try:
            result = await self._session_indexer.sync(force=force)
            logger.info(
                "sync_manager.sessions_synced",
                reason=reason,
                force=force,
                indexed=getattr(result, "indexed", 0),
                removed=getattr(result, "removed", 0),
                skipped=getattr(result, "skipped", 0),
            )
            return False
        except Exception:
            logger.exception("sync_manager.sessions_sync_failed", reason=reason)
            return True

    async def _poll_loop(self) -> None:
        """Trigger 3: file watcher polling loop with debounce."""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._running:
                    break

                current = self._scan_files()
                now = asyncio.get_event_loop().time()

                for path, mtime in current.items():
                    old = self._mtimes.get(path)
                    if old is None or mtime > old:
                        self._pending_changes.add(path)
                        self._last_change_time = now

                for path in set(self._mtimes) - set(current):
                    self._pending_deletes.add(path)
                    self._last_change_time = now

                self._mtimes = current

                if (self._pending_changes or self._pending_deletes) and (
                    now - self._last_change_time >= self._debounce_seconds
                ):
                    # sync(reason="watch") atomically snapshots, clears,
                    # and re-enqueues failed store removals. Do NOT clear
                    # _pending_* here — that would wipe the re-enqueued
                    # failures and orphan SQLite chunks on transient
                    # errors.
                    await self.sync(reason="watch")

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_manager.poll_error")

    async def _timer_loop(self) -> None:
        """Trigger 4: periodic sync timer."""
        interval = self._interval_minutes * 60
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._running:
                    await self.sync(reason="timer")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_manager.timer_error")

    async def _do_ttl_sweep(self, *, initial: bool = False) -> None:
        """Run one TTL prune pass via the retention helper.

        ``initial=True`` is used by ``start()`` for the synchronous first
        sweep BEFORE the index sync. Subsequent sweeps from the
        background loop pass ``initial=False`` for log differentiation.

        Failed ``store.remove_file`` calls (e.g. transient SQLite lock
        contention) leave the disk file gone but the SQLite chunks
        present. We enqueue the path into ``_pending_deletes`` so the
        next watcher tick retries — without this, the initial sweep at
        startup would leak orphan chunks because ``_mtimes`` is empty
        and the watcher diff cannot rediscover an unseen file.
        """
        from opensquilla.memory.retention import prune_expired_memory_files

        try:
            result = await prune_expired_memory_files(
                memory_dir=self._memory_dir,
                store=self._store,
                ttl_days=self._ttl_days,
                workspace_dir=self._workspace_dir,
                debounce_seconds=self._debounce_seconds + 0.5,
            )
        except Exception:
            logger.exception("sync_manager.ttl_sweep_failed", initial=initial)
            return

        if result.failed_store_removals:
            self._pending_deletes.update(result.failed_store_removals)
            self._dirty = True
            logger.warning(
                "sync_manager.ttl_sweep_store_removals_pending",
                initial=initial,
                paths=list(result.failed_store_removals),
            )

        if result.files_pruned > 0 or result.error:
            logger.info(
                "sync_manager.ttl_sweep",
                initial=initial,
                examined=result.files_examined,
                pruned=result.files_pruned,
                chunks_removed=result.chunks_removed,
                duration_ms=result.duration_ms,
                capped=result.capped,
                failed_store_removals=len(result.failed_store_removals),
                error=result.error,
            )

    async def _ttl_sweep_loop(self) -> None:
        """Periodic TTL sweep, separate from sync to keep retention semantics
        distinct from index sync."""
        interval = self._ttl_sweep_interval_minutes * 60
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._running and self._ttl_enabled():
                    await self._do_ttl_sweep(initial=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_manager.ttl_sweep_loop_error")
