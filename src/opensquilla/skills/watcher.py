"""Filesystem invalidation for the published Skill catalog."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import structlog

_Awatch = Callable[..., Any]
_awatch: _Awatch | None
try:
    from watchfiles import awatch as _imported_awatch
except Exception:
    _awatch = None
else:
    _awatch = _imported_awatch

log = structlog.get_logger(__name__)

_DEBOUNCE_MS = 250
_WATCH_STEP_MS = 500
_POLL_INTERVAL_SECONDS = 5.0


class SkillCatalogWatcher:
    """Invalidate a loader when any configured Skill root changes."""

    def __init__(
        self,
        loader: Any,
        *,
        debounce_ms: int = _DEBOUNCE_MS,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._loader = loader
        self._debounce_ms = max(1, int(debounce_ms))
        self._poll_interval = max(0.01, float(poll_interval))
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._polling = _awatch is None
        self._poll_signature: tuple[tuple[str, int, int, int], ...] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def using_fallback(self) -> bool:
        return self._polling

    async def start(self) -> None:
        """Start one watcher task; failures never block gateway startup."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="skills:catalog-watcher")

    async def stop(self) -> None:
        """Stop the watcher and await its task without leaking background work."""
        task = self._task
        self._task = None
        stop_event = self._stop_event
        self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Cancellation is expected after task.cancel() during normal shutdown.
            pass
        except Exception:
            log.debug("skills.catalog_watcher_stop_failed", exc_info=True)

    async def _run(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        if _awatch is None:
            await self._poll_loop(stop_event)
            return
        try:
            await self._watch_loop(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._polling = True
            log.warning("skills.catalog_watcher_failed_using_polling", exc_info=True)
            await self._poll_loop(stop_event)

    async def _watch_loop(self, stop_event: asyncio.Event) -> None:
        awatch = _awatch
        if awatch is None:
            return
        while not stop_event.is_set():
            roots = self._roots()
            existing_roots = tuple(root for root in roots if root.is_dir())
            if not existing_roots:
                self._polling = True
                await self._poll_once(stop_event)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    # The timeout lets polling re-check roots while waiting to stop.
                    pass
                continue
            try:
                async for changes in awatch(
                    *existing_roots,
                    debounce=self._debounce_ms,
                    step=_WATCH_STEP_MS,
                    stop_event=stop_event,
                    yield_on_timeout=True,
                    recursive=True,
                ):
                    if stop_event.is_set():
                        return
                    current_roots = self._roots()
                    roots_changed = current_roots != roots
                    presence_changed = _root_presence(current_roots) != _root_presence(roots)
                    if roots_changed or presence_changed:
                        self._loader.mark_dirty("watch.roots")
                        break
                    if changes:
                        self._loader.mark_dirty("watch")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._polling = True
                raise

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._poll_once(stop_event)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def _poll_once(self, stop_event: asyncio.Event) -> None:
        roots = self._roots()
        signature = await asyncio.to_thread(_root_signature, roots)
        if self._poll_signature is None:
            self._poll_signature = signature
        elif signature != self._poll_signature:
            self._poll_signature = signature
            self._loader.mark_dirty("poll")
        if roots != self._roots():
            self._loader.mark_dirty("poll.roots")
        await asyncio.sleep(0)
        if stop_event.is_set():
            return

    def _roots(self) -> tuple[Path, ...]:
        roots = getattr(self._loader, "watch_roots", None)
        if callable(roots):
            try:
                return tuple(Path(root) for root in roots())
            except Exception:
                log.debug("skills.catalog_watcher_roots_failed", exc_info=True)
        return ()


def _root_signature(roots: Iterable[Path]) -> tuple[tuple[str, int, int, int], ...]:
    """Build a bounded polling signature without following directory symlinks."""
    entries: list[tuple[str, int, int, int]] = []
    for root in roots:
        root_path = Path(root)
        try:
            root_stat = root_path.lstat()
        except OSError:
            entries.append((os.fspath(root_path), 0, 0, 0))
            continue
        entries.append(
            (
                os.fspath(root_path),
                int(root_stat.st_mtime_ns),
                int(root_stat.st_size),
                int(root_stat.st_mode),
            )
        )
        if not root_path.is_dir():
            continue
        for current, dirs, files in os.walk(root_path, followlinks=False):
            dirs[:] = sorted(dirs)
            files.sort()
            for name in (*dirs, *files):
                path = Path(current) / name
                try:
                    stat = path.lstat()
                except OSError:
                    continue
                entries.append(
                    (
                        os.fspath(path),
                        int(stat.st_mtime_ns),
                        int(stat.st_size),
                        int(stat.st_mode),
                    )
                )
    return tuple(entries)


def _root_presence(roots: Iterable[Path]) -> tuple[bool, ...]:
    return tuple(Path(root).is_dir() for root in roots)
