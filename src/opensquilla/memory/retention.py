"""Memory TTL retention helper.

Single source of truth for "delete memory files older than ``ttl_days`` and
clean up the SQLite index".

Used by:
- ``MemorySyncManager._ttl_sweep_loop`` — periodic background sweep.
- ``tools/builtin/memory_tools.py:_maybe_prune`` — in-line on ``memory_save``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.identity.workspace import BOOTSTRAP_FILENAMES

logger = structlog.get_logger(__name__)


# Curated workspace files that must NEVER be deleted by TTL even if they
# accidentally end up under ``memory/`` (mirrors
# ``identity/workspace.py:BOOTSTRAP_FILENAMES`` plus ``MEMORY.md`` /
# ``memory.md`` aliases).
DEFAULT_EXEMPT_FILES: frozenset[str] = frozenset(
    {"MEMORY.md", *BOOTSTRAP_FILENAMES}
)


@dataclass(frozen=True)
class PruneResult:
    """Outcome of one ``prune_expired_memory_files`` call. Emitted to logs
    so operators can correlate sweep cadence with disk pressure.

    ``failed_store_removals`` carries paths whose disk file was unlinked
    but whose ``store.remove_file`` raised something other than
    ``FileNotFoundError``. The caller (``MemorySyncManager._do_ttl_sweep``)
    must enqueue them for the next watcher pass — without that, an
    initial-sweep failure would leave orphan SQLite chunks forever
    because ``_mtimes`` is empty at startup and the watcher diff cannot
    discover a file it never saw.
    """

    files_examined: int
    files_pruned: int
    chunks_removed: int
    duration_ms: int
    capped: bool = False
    failed_store_removals: tuple[str, ...] = ()
    error: str | None = None


def _is_under_dot_prefix_dir(rel_to_memory: Path) -> bool:
    """True if any parent directory part starts with '.'.

    Mirrors ``MemorySyncManager._scan_files`` (``sync_manager.py:163-165``)
    so private dot-prefix subdirs stay invisible to both auto-indexing AND
    TTL deletion.
    """
    return any(part.startswith(".") for part in rel_to_memory.parts[:-1])


async def prune_expired_memory_files(
    *,
    memory_dir: Path | str,
    store: Any,
    ttl_days: int,
    workspace_dir: Path | str | None = None,
    exempt_files: frozenset[str] = DEFAULT_EXEMPT_FILES,
    exempt_dot_prefix_dirs: bool = True,
    cap_per_sweep: int = 200,
    debounce_seconds: float = 2.0,
) -> PruneResult:
    """Remove memory files older than ``ttl_days``. Returns counts.

    Args:
        memory_dir: Per-agent memory root (e.g. ``<workspace>/memory``).
        store: ``LongTermMemoryStore`` (anything with ``async remove_file``).
        ttl_days: 0 disables the sweep. Files with mtime older than
            ``now - ttl_days * 86400 - debounce_seconds`` are pruned.
        workspace_dir: Per-agent workspace root. Store keys are computed
            as ``path.relative_to(workspace_dir)`` to match the
            ``MemorySyncManager._scan_files`` convention. Defaults to
            ``memory_dir.parent`` since manager.py keeps the parent /
            child relationship in both ``source="workspace"`` and
            ``source="state"`` modes.
        exempt_files: Names that survive regardless of mtime. Default
            covers ``MEMORY.md`` + bootstrap files; pass a smaller set
            only if you know what you are doing.
        exempt_dot_prefix_dirs: If True (default), files under any
            dot-prefix subdirectory survive. Mirrors ``sync_manager._scan_files`` rule.
        cap_per_sweep: Max files to delete in one call. Bounds the
            blast radius of the first restart sweep when files have
            accumulated. Subsequent sweeps pick up the rest.
        debounce_seconds: Margin subtracted from the cutoff so files
            written within the last ``debounce_seconds`` are never
            pruned. Matches ``MemorySyncManager`` watcher debounce so a
            file just written by ``TurnCaptureService`` cannot race
            into the sweep.

    Returns:
        ``PruneResult`` with examined / pruned / chunks_removed counts
        and a ``capped`` flag if the cap was hit.

    Concurrency:
        - Tolerates ``FileNotFoundError`` on both ``stat()`` and
          ``unlink()`` so a parallel sweeper unlink does not crash this call.
        - ``store.remove_file`` failures are logged and swallowed; the
          watcher's next tick will re-discover and clean any stale
          chunk left behind.
    """
    started = time.monotonic()
    if ttl_days <= 0:
        return PruneResult(0, 0, 0, int((time.monotonic() - started) * 1000))

    mem_root = Path(memory_dir)
    if not mem_root.is_dir():
        return PruneResult(0, 0, 0, int((time.monotonic() - started) * 1000))

    workspace_root = Path(workspace_dir) if workspace_dir is not None else mem_root.parent
    cutoff = time.time() - (ttl_days * 86400) - max(0.0, debounce_seconds)

    examined = 0
    pruned = 0
    chunks_removed = 0
    capped = False
    failed_store_removals: list[str] = []

    for path in mem_root.rglob("*.md"):
        if pruned >= cap_per_sweep:
            capped = True
            break

        # Concurrent delete (another sweeper, OS) -> skip.
        try:
            if not path.is_file():
                continue
        except FileNotFoundError:
            continue

        if path.name in exempt_files:
            continue

        # TTL only applies to DATED notes (memory/YYYY-MM-DD.md and
        # YYYY-MM-DD-<slug>.md). Named evergreen notes are treated as
        # non-expiring by retrieval (_is_evergreen), so the sweep must not
        # delete them. Keep this in lockstep with retrieval's matcher.
        from opensquilla.memory.retrieval import _match_dated_basename

        if _match_dated_basename(path.name) is None:
            continue

        try:
            rel_to_memory = path.relative_to(mem_root)
        except ValueError:
            continue

        if exempt_dot_prefix_dirs and _is_under_dot_prefix_dir(rel_to_memory):
            continue

        examined += 1

        # Re-stat immediately before unlink to catch mtime updates that
        # happened between rglob and now (mitigation D.2).
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue

        if mtime >= cutoff:
            continue

        # Store keys are workspace-relative, NOT memory-relative — must
        # match MemorySyncManager._scan_files.
        try:
            store_key = path.relative_to(workspace_root).as_posix()
        except ValueError:
            # Symlink or odd path that escapes workspace — skip.
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            # Another writer raced ahead. Skip — they own the cleanup.
            continue
        except OSError as exc:
            logger.warning("retention.unlink_failed", path=store_key, error=str(exc))
            continue

        store_removed = True
        try:
            await store.remove_file(store_key)
        except FileNotFoundError:
            # Already gone from index; that's fine.
            pass
        except Exception as exc:  # noqa: BLE001
            # Disk file is already unlinked, but the SQLite chunks
            # survived. Track the path so the caller can retry on the
            # next watcher tick — at startup ``_mtimes`` is empty and
            # the watcher diff alone cannot rediscover the file.
            store_removed = False
            failed_store_removals.append(store_key)
            logger.warning(
                "retention.remove_file_failed", path=store_key, error=str(exc)
            )

        pruned += 1
        chunks_removed += 1 if store_removed else 0
        logger.info("retention.pruned", path=store_key)

    duration_ms = int((time.monotonic() - started) * 1000)
    return PruneResult(
        files_examined=examined,
        files_pruned=pruned,
        chunks_removed=chunks_removed,
        duration_ms=duration_ms,
        capped=capped,
        failed_store_removals=tuple(failed_store_removals),
    )
