"""Best-effort lifecycle for scoped telemetry recorders and uploaders.

The runtime opens each queue lazily under the unified upload preference.
Recorders and uploaders repeat the policy and environment-veto checks at their
durable and network boundaries; a paused stream retains its existing state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from opensquilla.telemetry.consent import (
    ConsentCheckpoint,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.consent_transition import telemetry_state_dir
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.desktop_ingress import drain_desktop_early_spool
from opensquilla.telemetry.desktop_state import desktop_early_spool_root
from opensquilla.telemetry.desktop_turn_counts import record_desktop_turn
from opensquilla.telemetry.outbox import OutboxPriority, TelemetryOutbox
from opensquilla.telemetry.recorder import RecordResult, RecordStatus, TelemetryRecorder
from opensquilla.telemetry.uploader import TelemetryUploader

log = logging.getLogger(__name__)

DEFAULT_TELEMETRY_V2_BASE_URL = "https://telemetry.opensquilla.ai"
DEFAULT_UPLOAD_INTERVAL_SECONDS = 30.0
SHUTDOWN_UPLOAD_TIMEOUT_SECONDS = 2.0


@dataclass
class _ScopeRuntime:
    outbox: TelemetryOutbox
    recorder: TelemetryRecorder
    uploader: TelemetryUploader

    async def close(self) -> None:
        await self.uploader.close()
        await self.outbox.close()


class ScopedTelemetryRuntime:
    """Own both isolated telemetry queues without affecting application work."""

    def __init__(
        self,
        *,
        config: object,
        base_url: str | None = None,
        state_dir: str | Path | None = None,
        upload_interval_seconds: float = DEFAULT_UPLOAD_INTERVAL_SECONDS,
        env: Mapping[str, str | None] | None = None,
    ) -> None:
        if not isinstance(upload_interval_seconds, (int, float)):
            raise TypeError("upload_interval_seconds must be numeric")
        if upload_interval_seconds <= 0:
            raise ValueError("upload_interval_seconds must be positive")
        self._config = config
        self._coordinator = scope_consent_coordinator_for(config)
        process_env = os.environ if env is None else env
        override_url = process_env.get("OPENSQUILLA_TELEMETRY_BASE_URL")
        self._base_url = (
            base_url
            if base_url is not None
            else override_url
            if override_url is not None
            else DEFAULT_TELEMETRY_V2_BASE_URL
        )
        self._state_dir = Path(state_dir or telemetry_state_dir(config))
        self._upload_interval_seconds = float(upload_interval_seconds)
        self._env = env
        self._scopes: dict[TelemetryScope, _ScopeRuntime] = {}
        self._scope_locks = {scope: asyncio.Lock() for scope in TelemetryScope}
        self._record_tasks: set[asyncio.Task[object]] = set()
        self._upload_task: asyncio.Task[None] | None = None
        self._upload_stop = asyncio.Event()
        self._shutdown_deadline: float | None = None
        self._shutdown_upload_guard: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._closed = False

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def opened_scopes(self) -> frozenset[TelemetryScope]:
        return frozenset(self._scopes)

    async def start(self) -> None:
        """Start the wake-up loop without creating files or making requests."""

        self._ensure_open()
        if self._closing or self._upload_stop.is_set():
            return
        self._bind_owner_loop()
        await self._drain_desktop_spool()
        if self._closing or self._closed or self._upload_stop.is_set():
            return
        if self._upload_task is None or self._upload_task.done():
            if self._upload_task is not None and not self._upload_task.cancelled():
                self._upload_task.exception()
            self._upload_task = asyncio.create_task(
                self._upload_loop(),
                name="opensquilla-telemetry-v2-uploader",
            )

    async def record(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None = None,
        expected_consent_revision: int | None = None,
    ) -> RecordResult:
        """Record one event after a lazy, fail-closed scope initialization."""

        self._ensure_open()
        try:
            scope = TelemetryScope(str(getattr(event, "consent_scope", "")))
        except ValueError as exc:
            raise ValueError("event has an invalid telemetry scope") from exc

        scoped = await self._scope_runtime(scope)
        if scoped is None:
            return RecordResult(RecordStatus.CONSENT_BLOCKED)
        result = await scoped.recorder.record(
            event,
            priority=priority,
            expected_consent_revision=expected_consent_revision,
        )
        if (
            result.status is RecordStatus.RECORDED
            and getattr(event, "event_name", "") == "turn_result"
        ):
            try:
                async with self._coordinator.authorized(
                    scope,
                    checkpoint=ConsentCheckpoint.ENQUEUE,
                    notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE[scope.value],
                ) as permit:
                    if permit is not None:
                        record_desktop_turn(self._state_dir, event)
            except Exception:
                log.debug("desktop turn counter checkpoint failed", exc_info=True)
        return result

    def record_background(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None = None,
    ) -> None:
        """Schedule a best-effort record without exposing failures to callers."""

        if self._closed or self._closing:
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            owner_loop = self._owner_loop
            if owner_loop is not None and owner_loop.is_running():
                owner_loop.call_soon_threadsafe(
                    partial(self.record_background, event, priority=priority),
                )
            return
        if self._owner_loop is not None and running_loop is not self._owner_loop:
            if self._owner_loop.is_running():
                self._owner_loop.call_soon_threadsafe(
                    partial(self.record_background, event, priority=priority),
                )
            return
        self._owner_loop = running_loop
        try:
            task = running_loop.create_task(
                self._record_safely(event, priority=priority),
                name=f"telemetry-record-{getattr(event, 'event_name', 'event')}",
            )
        except RuntimeError:
            return
        self._record_tasks.add(task)
        task.add_done_callback(self._record_tasks.discard)

    async def upload_once(self, scope: TelemetryScope | str) -> None:
        """Attempt one scope upload; all operational failures remain local."""

        self._ensure_open()
        normalized = TelemetryScope(scope)
        try:
            scoped = await self._scope_runtime(normalized)
            if scoped is None:
                return
            await scoped.uploader.upload_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("telemetry upload attempt failed", exc_info=True)

    def prepare_shutdown(self) -> None:
        """Bound active sends before producers drain, while still accepting records."""

        if self._closed or self._shutdown_deadline is not None:
            return
        self._bind_owner_loop()
        self._upload_stop.set()
        self._shutdown_deadline = (
            asyncio.get_running_loop().time() + SHUTDOWN_UPLOAD_TIMEOUT_SECONDS
        )
        if self._upload_task is not None:
            self._shutdown_upload_guard = asyncio.create_task(
                self._finish_upload_loop(self._upload_task, self._shutdown_deadline)
            )

    async def close(self, *, flush: bool = True) -> None:
        """Persist accepted records and attempt a bounded final batch per scope."""

        if self._closed or self._closing:
            return
        self._closing = True
        self.prepare_shutdown()
        upload_task = self._upload_task
        self._upload_task = None
        upload_guard = self._shutdown_upload_guard
        self._shutdown_upload_guard = None
        loop = asyncio.get_running_loop()
        assert self._shutdown_deadline is not None
        deadline = self._shutdown_deadline
        if not flush:
            deadline = loop.time()
            if upload_task is not None:
                upload_task.cancel()
            if upload_guard is not None:
                upload_guard.cancel()
        pending = asyncio.gather(*tuple(self._record_tasks), return_exceptions=True)
        try:
            # A send holds the scope's consent lock, which pending records also
            # need. The guard releases that lock at the network deadline, while
            # shielding local writes lets them finish durably after cancellation.
            await asyncio.shield(pending)
            # Share one deadline across scopes so a stalled endpoint does not
            # delay the other stream or make short commands wait for an interval.
            # upload_once retains consent, backoff, and receipt checks; cancelled
            # requests retain their durable lease for recovery and deduplication.
            if loop.time() < deadline:
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(
                        *(() if upload_guard is None else (upload_guard,)),
                        *(self.upload_once(scope) for scope in self._scopes),
                        return_exceptions=True,
                    )
        except TimeoutError:
            log.debug("telemetry final upload deadline reached")
        finally:
            if upload_guard is not None:
                upload_guard.cancel()
                await asyncio.gather(upload_guard, return_exceptions=True)
            if upload_task is not None and not upload_task.done():
                upload_task.cancel()
                await asyncio.gather(upload_task, return_exceptions=True)
            await pending
            self._record_tasks.clear()
            self._closed = True
            # A cycle already initializing a lazy scope may publish it while
            # close waits. Capture resources only after that task has stopped.
            scoped_runtimes = tuple(self._scopes.values())
            self._scopes.clear()
            for scoped in scoped_runtimes:
                try:
                    await scoped.close()
                except Exception:
                    log.debug("telemetry scope close failed", exc_info=True)
            self._owner_loop = None

    async def _finish_upload_loop(self, task: asyncio.Task[None], deadline: float) -> None:
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
        except TimeoutError:
            log.debug("telemetry in-flight upload deadline reached")
        except Exception:
            log.debug("telemetry upload loop close failed", exc_info=True)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _scope_runtime(self, scope: TelemetryScope) -> _ScopeRuntime | None:
        existing = self._scopes.get(scope)
        if existing is not None:
            return existing

        async with self._scope_locks[scope]:
            existing = self._scopes.get(scope)
            if existing is not None:
                return existing
            notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[scope.value]
            async with self._coordinator.authorized(
                scope,
                checkpoint=ConsentCheckpoint.ENQUEUE,
                notice_version=notice_version,
            ) as permit:
                if permit is None or self._closed:
                    return None

                outbox: TelemetryOutbox | None = None
                uploader: TelemetryUploader | None = None
                try:
                    outbox = await TelemetryOutbox.open(self._state_dir, scope)
                    recorder = TelemetryRecorder(outbox, config=self._config)
                    uploader = TelemetryUploader(
                        outbox,
                        base_url=self._base_url,
                        config=self._config,
                    )
                    scoped = _ScopeRuntime(
                        outbox=outbox,
                        recorder=recorder,
                        uploader=uploader,
                    )
                    self._scopes[scope] = scoped
                    return scoped
                except asyncio.CancelledError:
                    if uploader is not None:
                        await _ignore_close(uploader.close())
                    if outbox is not None:
                        await _ignore_close(outbox.close())
                    raise
                except Exception:
                    log.debug("telemetry scope initialization failed", exc_info=True)
                    if uploader is not None:
                        await _ignore_close(uploader.close())
                    if outbox is not None:
                        await _ignore_close(outbox.close())
                    return None

    async def _record_safely(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None,
    ) -> None:
        try:
            result = await self.record(event, priority=priority)
            if not self._closing and result.status in {
                RecordStatus.RECORDED,
                RecordStatus.DUPLICATE,
                RecordStatus.EVICTED,
            }:
                await self.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("telemetry event record failed", exc_info=True)

    async def _run_upload_cycle(self) -> None:
        await self._drain_desktop_spool()
        for scope in TelemetryScope:
            await self.upload_once(scope)

    async def _upload_loop(self) -> None:
        while not self._upload_stop.is_set():
            await self._run_upload_cycle()
            await self._wait_for_upload_interval()

    async def _wait_for_upload_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._upload_stop.wait(), timeout=self._upload_interval_seconds
            )
        except TimeoutError:
            pass

    async def _drain_desktop_spool(self) -> None:
        """Import Electron events without opening an unconsented scope."""

        root = desktop_early_spool_root(self._state_dir)
        try:
            root_metadata = root.lstat()
        except OSError:
            return
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            return

        recorders: dict[TelemetryScope, TelemetryRecorder] = {}
        for scope in TelemetryScope:
            scope_directory = root / scope.value
            try:
                scope_metadata = scope_directory.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(scope_metadata.st_mode) or not stat.S_ISDIR(scope_metadata.st_mode):
                continue
            try:
                if not resolve_scope_consent(
                    scope,
                    config=self._config,
                    env=self._env,
                ).enabled:
                    continue
                scoped = await self._scope_runtime(scope)
                if scoped is not None:
                    recorders[scope] = scoped.recorder
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("desktop telemetry scope initialization failed", exc_info=True)
        if not recorders:
            return
        try:
            await drain_desktop_early_spool(
                root,
                config=self._config,
                recorders=recorders,
                env=self._env,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("desktop telemetry spool drain failed", exc_info=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("telemetry runtime is closed")

    def _bind_owner_loop(self) -> None:
        running_loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = running_loop
            return
        if self._owner_loop is not running_loop:
            raise RuntimeError("telemetry runtime belongs to another event loop")


async def _ignore_close(operation: Coroutine[Any, Any, object]) -> None:
    try:
        await operation
    except Exception:
        pass


__all__ = [
    "DEFAULT_TELEMETRY_V2_BASE_URL",
    "DEFAULT_UPLOAD_INTERVAL_SECONDS",
    "ScopedTelemetryRuntime",
]
