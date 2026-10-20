"""Best-effort lifecycle for scoped event recorders and uploaders.

The runtime opens each queue lazily under the unified upload preference.
Recorders and uploaders repeat the policy and environment-veto checks at their
durable and network boundaries; a paused stream retains its existing state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import threading
from collections import deque
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
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
from opensquilla.telemetry.device_identity import get_device_id
from opensquilla.telemetry.outbox import OutboxPriority, TelemetryOutbox
from opensquilla.telemetry.recorder import RecordResult, RecordStatus, TelemetryRecorder
from opensquilla.telemetry.state_io import run_state_io
from opensquilla.telemetry.uploader import TelemetryUploader

log = logging.getLogger(__name__)

DEFAULT_TELEMETRY_V2_BASE_URL = "https://telemetry.opensquilla.ai"
DEFAULT_UPLOAD_INTERVAL_SECONDS = 30.0
SHUTDOWN_UPLOAD_TIMEOUT_SECONDS = 2.0
BACKGROUND_RECORD_QUEUE_LIMIT = 256
BACKGROUND_RECORD_BATCH_SIZE = 32
BACKGROUND_RECORD_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class _PendingRecord:
    event: StrictTelemetryModel
    scope: TelemetryScope
    priority: OutboxPriority
    consent_revision: int


@dataclass
class _ScopeRuntime:
    outbox: TelemetryOutbox
    recorder: TelemetryRecorder
    uploader: TelemetryUploader

    async def close(self) -> None:
        await self.uploader.close()
        await self.outbox.close()


class ScopedTelemetryRuntime:
    """Own both isolated event queues without affecting application work."""

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
        self._record_queue: deque[_PendingRecord] = deque()
        self._record_queue_lock = threading.Lock()
        self._record_drain_scheduled = False
        self._record_drain_task: asyncio.Task[object] | None = None
        self._record_flush = asyncio.Event()
        self._device_id: str | None = None
        self._device_identity_resolved = False
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

    def device_id_for(self, scope: TelemetryScope) -> str | None:
        """Return cached identity without OS or filesystem work in observers."""

        if self._closed or not resolve_scope_consent(
            scope, config=self._config, env=self._env
        ).enqueue_allowed:
            return None
        return self._device_id

    async def prepare_device_identity(self, scope: TelemetryScope) -> str | None:
        """Resolve an explicitly awaited startup observation outside the loop."""

        if self._closed or not resolve_scope_consent(
            scope, config=self._config, env=self._env,
        ).enqueue_allowed:
            return None
        async with self._coordinator.authorized(
            scope, checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE[scope.value],
        ) as permit:
            if permit is None:
                return None
            return await self._resolve_device_identity()

    async def _resolve_device_identity(self) -> str | None:
        if not self._device_identity_resolved:
            self._device_id = await run_state_io(get_device_id)
            self._device_identity_resolved = True
        return self._device_id

    async def start(self) -> None:
        """Start the wake-up loop without creating files or making requests."""

        self._ensure_open()
        if self._closing or self._upload_stop.is_set():
            return
        self._bind_owner_loop()
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
            raise ValueError("event has an invalid statistics scope") from exc

        scoped = await self._scope_runtime(scope)
        if scoped is None:
            return RecordResult(RecordStatus.CONSENT_BLOCKED)
        result = await scoped.recorder.record(
            event,
            priority=priority,
            expected_consent_revision=expected_consent_revision,
        )
        await self._checkpoint_desktop_turns(scope, ((event, result),))
        return result

    def record_background(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None = None,
    ) -> None:
        """Admit optional work without creating one task or transaction per event.

        Saturation drops observations before durable acceptance. Higher priority
        observations may replace a lower priority queued observation; persisted
        outbox records and direct ``record`` calls are unaffected.
        """

        if self._closed or self._closing:
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        owner_loop = self._owner_loop or running_loop
        if owner_loop is None or not owner_loop.is_running():
            return
        try:
            scope = TelemetryScope(str(getattr(event, "consent_scope", "")))
            pending = _PendingRecord(
                event, scope, _background_priority(event, priority),
                self._coordinator.revision(scope),
            )
        except (TypeError, ValueError):
            return
        self._owner_loop = owner_loop
        with self._record_queue_lock:
            if self._closed or self._closing:
                return
            if len(self._record_queue) >= BACKGROUND_RECORD_QUEUE_LIMIT:
                lowest = min(self._record_queue, key=lambda item: item.priority)
                if pending.priority <= lowest.priority:
                    return
                self._record_queue.remove(lowest)
            self._record_queue.append(pending)
            if self._record_drain_scheduled:
                if (
                    running_loop is owner_loop
                    and len(self._record_queue) >= BACKGROUND_RECORD_BATCH_SIZE
                ):
                    self._record_flush.set()
                return
            self._record_drain_scheduled = True
        if running_loop is owner_loop:
            self._ensure_record_drain()
        else:
            try:
                owner_loop.call_soon_threadsafe(self._ensure_record_drain)
            except RuntimeError:
                with self._record_queue_lock:
                    self._record_queue.clear()
                    self._record_drain_scheduled = False

    def _ensure_record_drain(self) -> None:
        if self._closed:
            return
        if self._record_drain_task is not None and not self._record_drain_task.done():
            return
        task = asyncio.create_task(
            self._drain_background_records(), name="telemetry-record-drain",
        )
        self._record_drain_task = task
        self._record_tasks.add(task)
        task.add_done_callback(self._record_tasks.discard)

    async def _drain_background_records(self) -> None:
        while True:
            with self._record_queue_lock:
                queue_size = len(self._record_queue)
            if 0 < queue_size < BACKGROUND_RECORD_BATCH_SIZE and not self._closing:
                try:
                    await asyncio.wait_for(
                        self._record_flush.wait(), timeout=BACKGROUND_RECORD_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    pass
            self._record_flush.clear()
            with self._record_queue_lock:
                batch = tuple(
                    self._record_queue.popleft()
                    for _ in range(min(len(self._record_queue), BACKGROUND_RECORD_BATCH_SIZE))
                )
                if not batch:
                    self._record_drain_scheduled = False
                    return
            groups: dict[tuple[TelemetryScope, int], list[_PendingRecord]] = {}
            for pending in batch:
                groups.setdefault((pending.scope, pending.consent_revision), []).append(pending)
            recorded = False
            for group in groups.values():
                recorded = await self._record_batch_safely(group) or recorded
            if (
                recorded and not self._closing
                and (self._upload_task is None or self._upload_task.done())
            ):
                try:
                    await self.start()
                except Exception:
                    log.debug("statistics upload loop start failed", exc_info=True)
            # Let application callbacks run between bounded persistence groups.
            await asyncio.sleep(0)

    async def _record_batch_safely(self, batch: list[_PendingRecord]) -> bool:
        scope = batch[0].scope
        try:
            batch = await self._prepare_background_identity(batch)
            if len(batch) == 1:
                return await self._record_safely(batch[0])
            scoped = await self._scope_runtime(scope)
            if scoped is None:
                return False
            results = await scoped.recorder.record_many(
                [(pending.event, pending.priority) for pending in batch],
                expected_consent_revision=batch[0].consent_revision,
            )
            await self._checkpoint_desktop_turns(
                scope, tuple((pending.event, result) for pending, result in zip(batch, results)),
            )
            return any(_record_was_accepted(result) for result in results)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A malformed or conflicting optional event must not lose its peers.
            log.debug("statistics batch record failed", exc_info=True)
            recorded = False
            for pending in batch:
                recorded = await self._record_safely(pending) or recorded
            return recorded

    async def _prepare_background_identity(
        self, batch: list[_PendingRecord],
    ) -> list[_PendingRecord]:
        async with self._coordinator.authorized(
            batch[0].scope,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE[batch[0].scope.value],
        ) as permit:
            if permit is None or permit.revision != batch[0].consent_revision:
                return batch
            await self._resolve_device_identity()
            if self._device_id is None:
                return batch
            return [
                _PendingRecord(
                    pending.event.model_copy(update={"device_id": self._device_id}),
                    pending.scope, pending.priority, pending.consent_revision,
                ) if getattr(pending.event, "device_id", None) is None else pending
                for pending in batch
            ]

    async def _checkpoint_desktop_turns(
        self,
        scope: TelemetryScope,
        records: tuple[tuple[StrictTelemetryModel, RecordResult], ...],
    ) -> None:
        turns = tuple(event for event, result in records if (
            result.status is RecordStatus.RECORDED
            and getattr(event, "event_name", "") == "turn_result"
        ))
        if not turns:
            return
        try:
            async with self._coordinator.authorized(
                scope,
                checkpoint=ConsentCheckpoint.ENQUEUE,
                notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE[scope.value],
            ) as permit:
                if permit is not None:
                    def checkpoint() -> None:
                        for event in turns:
                            try:
                                record_desktop_turn(self._state_dir, event)
                            except Exception:
                                log.debug("desktop turn counter checkpoint failed", exc_info=True)

                    await run_state_io(checkpoint)
        except Exception:
            log.debug("desktop turn counter checkpoint failed", exc_info=True)

    async def recover_contract_rejection_once(
        self,
        event: StrictTelemetryModel,
        *,
        expected_consent_revision: int,
    ) -> bool:
        """Recover an existing Growth observation without creating a new fact."""

        self._ensure_open()
        if str(getattr(event, "consent_scope", "")) != TelemetryScope.GROWTH.value:
            return False
        scoped = await self._scope_runtime(TelemetryScope.GROWTH)
        if scoped is None:
            return False
        return await scoped.recorder.recover_contract_rejection_once(
            event, expected_consent_revision=expected_consent_revision,
        )

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
            log.debug("statistics upload attempt failed", exc_info=True)

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
        self._record_flush.set()
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
        # A worker may have admitted records before its wake-up callback runs.
        with self._record_queue_lock:
            queued = bool(self._record_queue)
        if queued:
            self._ensure_record_drain()
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
            log.debug("statistics final upload deadline reached")
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
                    log.debug("statistics scope close failed", exc_info=True)
            self._owner_loop = None

    async def _finish_upload_loop(self, task: asyncio.Task[None], deadline: float) -> None:
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
        except TimeoutError:
            log.debug("statistics in-flight upload deadline reached")
        except Exception:
            log.debug("statistics upload loop close failed", exc_info=True)
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
                    log.debug("statistics scope initialization failed", exc_info=True)
                    if uploader is not None:
                        await _ignore_close(uploader.close())
                    if outbox is not None:
                        await _ignore_close(outbox.close())
                    return None

    async def _record_safely(self, pending: _PendingRecord) -> bool:
        try:
            result = await self.record(
                pending.event, priority=pending.priority,
                expected_consent_revision=pending.consent_revision,
            )
            return _record_was_accepted(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("statistics event record failed", exc_info=True)
            return False

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

        def existing_spool_scopes() -> list[TelemetryScope]:
            try:
                root_metadata = root.lstat()
            except OSError:
                return []
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                return []
            scopes = []
            for scope in TelemetryScope:
                try:
                    scope_metadata = (root / scope.value).lstat()
                except OSError:
                    continue
                if stat.S_ISDIR(scope_metadata.st_mode) and not stat.S_ISLNK(
                    scope_metadata.st_mode
                ):
                    scopes.append(scope)
            return scopes

        recorders: dict[TelemetryScope, TelemetryRecorder] = {}
        for scope in await run_state_io(existing_spool_scopes):
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
                log.debug("desktop statistics scope initialization failed", exc_info=True)
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
            log.debug("desktop statistics spool drain failed", exc_info=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("statistics runtime is closed")

    def _bind_owner_loop(self) -> None:
        running_loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = running_loop
            return
        if self._owner_loop is not running_loop:
            raise RuntimeError("statistics runtime belongs to another event loop")


async def _ignore_close(operation: Coroutine[Any, Any, object]) -> None:
    try:
        await operation
    except Exception:
        pass


def _record_was_accepted(result: RecordResult) -> bool:
    return result.status in {RecordStatus.RECORDED, RecordStatus.DUPLICATE, RecordStatus.EVICTED}


def _background_priority(
    event: StrictTelemetryModel, priority: OutboxPriority | int | None,
) -> OutboxPriority:
    if priority is not None:
        return OutboxPriority(priority)
    if getattr(event, "event_name", None) == "app_crash_detected":
        return OutboxPriority.CRITICAL
    if getattr(event, "outcome", None) in {"fail", "timeout"}:
        return OutboxPriority.HIGH
    if getattr(event, "event_name", None) == "performance_summary":
        return OutboxPriority.LOW
    return OutboxPriority.NORMAL


__all__ = [
    "DEFAULT_TELEMETRY_V2_BASE_URL",
    "DEFAULT_UPLOAD_INTERVAL_SECONDS",
    "ScopedTelemetryRuntime",
]
