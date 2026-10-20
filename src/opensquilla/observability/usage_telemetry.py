"""Daily aggregation of conversation and token counters.

No prompts, responses, model/provider names, session identifiers, costs, tools,
or local paths enter this module's durable rows or network payloads. Collection
uses the existing telemetry service's dedicated usage endpoint and unified
network privacy switch.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
from collections.abc import Awaitable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from opensquilla import __version__
from opensquilla.observability import install_telemetry
from opensquilla.observability.daily_usage_store import (
    DailyUsageDelta,
    DailyUsageStore,
    daily_usage_finalizes_at,
)

log = logging.getLogger(__name__)

DAILY_TELEMETRY_SCHEMA_VERSION = 1
DAILY_USAGE_UPLOAD_INTERVAL_SECONDS = 60 * 60
DAILY_USAGE_FLUSH_INTERVAL_SECONDS = 5.0
DAILY_USAGE_MAX_BUFFER_DAYS = 32
STANDALONE_CLOSE_TIMEOUT_SECONDS = 2.0
USAGE_TELEMETRY_ENDPOINT_ENV = "OPENSQUILLA_USAGE_TELEMETRY_ENDPOINT"
DEFAULT_USAGE_TELEMETRY_ENDPOINT = "https://telemetry.opensquilla.ai/v1/usage"
INTERACTIVE_RUN_KINDS = frozenset(
    {"default", "session_turn", "web_turn", "channel_turn", "interactive"}
)


class DailyUsageStorage(Protocol):
    async def ensure_daily_usage_store_id(self) -> str: ...

    async def record_daily_usage(
        self,
        *,
        day: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        updated_at: int,
    ) -> None: ...

    async def list_pending_daily_usage(self, *, before_day: str) -> list[dict[str, Any]]: ...

    async def mark_daily_usage_uploaded(
        self,
        *,
        day: str,
        uploaded_at: int,
        expected_conversation_turns: int,
    ) -> bool: ...


class CompletedTurnUsage(Protocol):
    """The content-free subset of a completed turn used for aggregation.

    Keeping this structural avoids coupling the observability package back to
    the engine package (and therefore avoids an import cycle).
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int


async def record_completed_turn(
    storage: DailyUsageStorage,
    *,
    config: Any,
    run_kind: str,
    done_event: CompletedTurnUsage | None,
    now: datetime | None = None,
) -> bool:
    """Add an eligible completed turn to today's local aggregate."""
    if install_telemetry._telemetry_skip_reason(config=config) is not None:
        return False
    if run_kind not in INTERACTIVE_RUN_KINDS or done_event is None:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    await storage.record_daily_usage(
        day=current.date().isoformat(),
        input_tokens=max(0, int(done_event.input_tokens or 0)),
        output_tokens=max(0, int(done_event.output_tokens or 0)),
        cached_tokens=max(0, int(done_event.cached_tokens or 0)),
        cache_write_tokens=max(0, int(done_event.cache_write_tokens or 0)),
        updated_at=int(current.timestamp() * 1000),
    )
    return True


async def upload_pending_daily_usage(
    storage: DailyUsageStorage,
    *,
    config: Any,
    today: date | None = None,
) -> int:
    """Upload pending aggregates for completed UTC days; failures stay retryable.

    The current (still-accumulating) day is deliberately excluded: the
    event ID — sent as the ``Idempotency-Key`` — is stable per (database,
    day), so an intraday snapshot would freeze the day at its first upload
    on any endpoint that honors idempotency semantics. Waiting until the
    UTC day has closed means each day is uploaded exactly once with its
    final totals, and retries legitimately replay the same content.
    """
    if install_telemetry._telemetry_skip_reason(config=config) is not None:
        return 0
    endpoint = _endpoint()
    if not endpoint:
        return 0
    current_day = today or datetime.now(UTC).date()
    # Capture the admission boundary before reading: a snapshot taken just
    # before finality must not become uploadable merely because resuming its
    # caller crosses midnight's grace deadline.
    now = datetime.now(UTC).timestamp()
    rows = await storage.list_pending_daily_usage(before_day=current_day.isoformat())
    rows = [row for row in rows if now >= daily_usage_finalizes_at(str(row["day"]))]
    if not rows:
        return 0

    install_id = await asyncio.to_thread(
        install_telemetry.ensure_install_telemetry_id,
        config=config,
    )
    if install_telemetry._telemetry_skip_reason(config=config) is not None:
        return 0

    store_id = await storage.ensure_daily_usage_store_id()
    uploaded = 0
    for row in rows:
        payload = _daily_payload(
            row,
            install_id=install_id,
            store_id=store_id,
            sent_at=_utc_now(),
        )
        # The privacy flag is hot-reloadable. Do not start another request
        # after an operator opts out while a multi-day batch is in flight.
        if install_telemetry._telemetry_skip_reason(config=config) is not None:
            break
        ok, error = await _post_payload(endpoint, payload)
        if not ok:
            log.debug("Daily usage telemetry upload failed: %s", error)
            continue
        await storage.mark_daily_usage_uploaded(
            day=str(row["day"]),
            uploaded_at=int(datetime.now(UTC).timestamp() * 1000),
            expected_conversation_turns=int(row["conversation_turns"]),
        )
        uploaded += 1
    return uploaded


async def run_daily_usage_upload_loop(
    storage: DailyUsageStorage | None,
    *,
    config: Any,
    interval_seconds: float = DAILY_USAGE_UPLOAD_INTERVAL_SECONDS,
) -> None:
    """Upload immediately on startup, then retry pending aggregates hourly."""
    interval = max(float(interval_seconds), 0.01)
    while True:
        try:
            await _upload_pending_sources(storage, config=config)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Daily usage telemetry upload incomplete", exc_info=True)
        await asyncio.sleep(interval)


async def _upload_pending_sources(storage: DailyUsageStorage | None, *, config: Any) -> None:
    # A damaged legacy database must not starve independently durable CLI rows.
    if storage is not None:
        try:
            await upload_pending_daily_usage(storage, config=config)
        except Exception:
            log.debug("Legacy daily usage telemetry upload failed", exc_info=True)
    try:
        await upload_pending_standalone_daily_usage(config=config)
    except Exception:
        log.debug("Standalone daily usage telemetry upload failed", exc_info=True)


def standalone_daily_usage_path(config: Any) -> Path:
    from opensquilla.paths import default_opensquilla_home, native_io_path

    root = Path(getattr(config, "state_dir", None) or default_opensquilla_home() / "state")
    return native_io_path(root / "telemetry" / "standalone-usage.sqlite3")


async def upload_pending_standalone_daily_usage(*, config: Any) -> int:
    """Drain standalone counters from either a CLI process or a later Gateway."""
    if install_telemetry._telemetry_skip_reason(config=config) is not None:
        return 0
    path = standalone_daily_usage_path(config)
    if not path.is_file():
        return 0
    from opensquilla.observability.daily_usage_store import DailyUsageStore

    storage = await DailyUsageStore.open(path)
    try:
        return await upload_pending_daily_usage(storage, config=config)
    finally:
        await storage.close()


class StandaloneUsageTelemetry:
    """Batch anonymous usage without involving a turn's storage or network path.

    Accepted counters stay in memory until the next five-second flush or orderly
    shutdown. An abrupt process exit can lose that short, bounded interval.
    Historical session-database counters retain their independent upload identity.
    """

    def __init__(
        self,
        *,
        config: Any,
        legacy_storage: DailyUsageStorage | None,
        flush_interval_seconds: float = DAILY_USAGE_FLUSH_INTERVAL_SECONDS,
        max_buffer_days: int = DAILY_USAGE_MAX_BUFFER_DAYS,
        upload_on_close: bool = True,
    ) -> None:
        if flush_interval_seconds <= 0 or max_buffer_days < 1:
            raise ValueError("Daily usage buffer limits must be positive")
        self._config = config
        self._legacy_storage = legacy_storage
        self._storage: DailyUsageStore | None = None
        self._flush_lock = asyncio.Lock()
        self._pending: dict[str, DailyUsageDelta] = {}
        self._flush_interval = flush_interval_seconds
        self._max_buffer_days = max_buffer_days
        self._flush_worker: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._upload_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._install_thread: Any = None
        self._closed = False
        self._overflow_logged = False
        self._upload_on_close = upload_on_close

    def enable_uploads(self) -> None:
        """Allow a lifecycle owner to upload after its readiness edge."""
        self._upload_on_close = True

    def start(self) -> None:
        if (
            self._closed or self._upload_task is not None
        ):
            return
        self._install_thread = install_telemetry.start_background_install_telemetry(
            config=self._config,
        )
        self.enable_uploads()
        self._upload_task = asyncio.create_task(
            self._run_upload_loop(),
            name="standalone-daily-usage-upload",
        )

    async def _run_upload_loop(self) -> None:
        # Finalized durable rows are independent of the local flush worker.
        # Buffered additions expire before they can change an eligible upload.
        await run_daily_usage_upload_loop(
            self._legacy_storage, config=self._config,
        )

    async def record_turn(
        self, *, run_kind: str, done_event: CompletedTurnUsage | None,
    ) -> None:
        if (
            self._closed
            or install_telemetry._telemetry_skip_reason(config=self._config) is not None
            or run_kind not in INTERACTIVE_RUN_KINDS
            or done_event is None
        ):
            return
        current = datetime.now(UTC)
        day = current.date().isoformat()
        previous = self._pending.get(day)
        if previous is None and len(self._pending) >= self._max_buffer_days:
            if not self._overflow_logged:
                log.debug("Daily usage telemetry buffer full")
                self._overflow_logged = True
            return
        self._pending[day] = DailyUsageDelta(
            day=day,
            conversation_turns=(previous.conversation_turns if previous else 0) + 1,
            input_tokens=(previous.input_tokens if previous else 0)
            + max(0, int(done_event.input_tokens or 0)),
            output_tokens=(previous.output_tokens if previous else 0)
            + max(0, int(done_event.output_tokens or 0)),
            cached_tokens=(previous.cached_tokens if previous else 0)
            + max(0, int(done_event.cached_tokens or 0)),
            cache_write_tokens=(previous.cache_write_tokens if previous else 0)
            + max(0, int(done_event.cache_write_tokens or 0)),
            updated_at=int(current.timestamp() * 1000),
        )
        if self._flush_worker is None:
            self._flush_worker = asyncio.create_task(
                self._run_flush_loop(), name="daily-usage-counter-flush",
            )

    async def _run_flush_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Daily usage telemetry flush incomplete", exc_info=True)

    async def flush(self) -> None:
        """Persist a snapshot while newer turns keep accumulating in memory."""
        async with self._flush_lock:
            task = self._flush_task
            if task is not None and not task.done():
                await asyncio.shield(task)
            if self._pending:
                task = asyncio.create_task(
                    self._persist_pending(), name="daily-usage-counter-write",
                )
                task.add_done_callback(_observe_task_result)
                self._flush_task = task
            else:
                return
            # A cancelled uploader or close deadline must not replay a SQLite
            # write already committing in its worker thread.
            await asyncio.shield(task)

    async def _persist_pending(self) -> None:
        snapshot = tuple(self._pending.values())
        if self._storage is None:
            self._storage = await DailyUsageStore.open(standalone_daily_usage_path(self._config))
        await self._storage.record_daily_usage_batch(snapshot, discard_expired=True)
        for saved in snapshot:
            current = self._pending[saved.day]
            remaining_turns = current.conversation_turns - saved.conversation_turns
            if remaining_turns == 0:
                del self._pending[saved.day]
            else:
                self._pending[saved.day] = DailyUsageDelta(
                    day=saved.day,
                    conversation_turns=remaining_turns,
                    input_tokens=current.input_tokens - saved.input_tokens,
                    output_tokens=current.output_tokens - saved.output_tokens,
                    cached_tokens=current.cached_tokens - saved.cached_tokens,
                    cache_write_tokens=current.cache_write_tokens - saved.cache_write_tokens,
                    updated_at=current.updated_at,
                )
        self._overflow_logged = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._upload_task, self._flush_worker) if task is not None]
        self._upload_task = self._flush_worker = None
        for task in tasks:
            task.cancel()

        async def finish_install() -> None:
            thread, self._install_thread = self._install_thread, None
            if thread is not None:
                await asyncio.to_thread(thread.join, STANDALONE_CLOSE_TIMEOUT_SECONDS)

        async def finish_counters() -> None:
            try:
                # An older in-flight snapshot can leave one newer snapshot to
                # persist. No further turns are accepted once close begins.
                while self._pending:
                    await self.flush()
            except Exception:
                log.debug("Daily usage telemetry flush incomplete", exc_info=True)

        async def finish_close() -> None:
            try:
                closing: list[Awaitable[Any]] = [
                    finish_counters(), asyncio.gather(*tasks, return_exceptions=True),
                ]
                if self._upload_on_close:
                    closing.extend((
                        _upload_pending_sources(self._legacy_storage, config=self._config),
                        finish_install(),
                    ))
                # Already durable sources remain uploadable even when the
                # independent counter store fails or is still waiting to write.
                await asyncio.gather(*closing, return_exceptions=True)
            finally:
                try:
                    flush_task = self._flush_task
                    if flush_task is not None and not flush_task.done():
                        await asyncio.shield(flush_task)
                    if self._pending:
                        await self.flush()
                finally:
                    storage, self._storage = self._storage, None
                    if storage is not None:
                        await storage.close()

        self._close_task = asyncio.create_task(finish_close(), name="daily-usage-telemetry-close")
        self._close_task.add_done_callback(_observe_task_result)
        try:
            done, _ = await asyncio.wait(
                {self._close_task}, timeout=STANDALONE_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self._close_task.cancel()
            raise
        if not done:
            self._close_task.cancel()
            log.debug("Standalone telemetry shutdown incomplete")
        else:
            try:
                self._close_task.result()
            except Exception:
                log.debug("Standalone telemetry shutdown incomplete", exc_info=True)


def _observe_task_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


def _daily_payload(
    row: dict[str, Any],
    *,
    install_id: str,
    store_id: str,
    sent_at: str,
) -> dict[str, Any]:
    day = str(row["day"])
    return {
        "schema_version": DAILY_TELEMETRY_SCHEMA_VERSION,
        "event": "daily_usage",
        "event_id": _daily_event_id(store_id, day),
        "install_id": install_id,
        "opensquilla_version": __version__,
        "day": day,
        "sent_at": sent_at,
        "conversation_turns": int(row["conversation_turns"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cached_tokens": int(row["cached_tokens"]),
        "cache_write_tokens": int(row["cache_write_tokens"]),
    }


def _daily_event_id(store_id: str, day: str) -> str:
    """Give independent profile databases distinct, retry-stable daily keys.

    The random local discriminator is never uploaded. Unlike installation
    identity it belongs to the aggregate store, not the machine, and survives
    database moves and changes to the separate installation state.
    """
    digest = hmac.new(
        store_id.encode("utf-8"),
        f"daily-usage:store-v1:{day}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _endpoint() -> str:
    return os.environ.get(
        USAGE_TELEMETRY_ENDPOINT_ENV,
        DEFAULT_USAGE_TELEMETRY_ENDPOINT,
    ).strip()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _post_payload(endpoint: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        from opensquilla.observability.http_client import create_observability_async_client

        client = await create_observability_async_client(
            timeout=install_telemetry.DEFAULT_TIMEOUT_SECONDS
        )
        async with client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Idempotency-Key": str(payload["event_id"])},
            )
        if response.status_code in install_telemetry._SUCCESS_STATUS_CODES:
            return True, None
        return False, f"http_status_{response.status_code}"
    except Exception as exc:
        return False, str(exc)
