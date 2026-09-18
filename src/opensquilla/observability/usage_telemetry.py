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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from opensquilla import __version__
from opensquilla.observability import install_telemetry

log = logging.getLogger(__name__)

DAILY_TELEMETRY_SCHEMA_VERSION = 1
DAILY_USAGE_UPLOAD_INTERVAL_SECONDS = 60 * 60
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
    if os.environ.get("OPENSQUILLA_CODETASK_CHILD") == "1":
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
    rows = await storage.list_pending_daily_usage(before_day=current_day.isoformat())
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
        await _upload_pending_sources(storage, config=config)
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
    """Keep anonymous usage durable independently of ephemeral CLI transcripts."""

    def __init__(self, *, config: Any, legacy_storage: DailyUsageStorage | None) -> None:
        self._config = config
        self._legacy_storage = legacy_storage
        self._storage: Any = None
        self._storage_lock = asyncio.Lock()
        self._upload_task: asyncio.Task[None] | None = None
        self._install_thread: Any = None
        self._closed = False

    def start(self) -> None:
        if (
            self._closed or self._upload_task is not None
            or os.environ.get("OPENSQUILLA_CODETASK_CHILD") == "1"
        ):
            return
        self._install_thread = install_telemetry.start_background_install_telemetry(
            config=self._config,
        )
        self._upload_task = asyncio.create_task(
            run_daily_usage_upload_loop(self._legacy_storage, config=self._config),
            name="standalone-daily-usage-upload",
        )

    async def record_turn(
        self, *, run_kind: str, done_event: CompletedTurnUsage | None,
    ) -> None:
        if (
            self._closed
            or install_telemetry._telemetry_skip_reason(config=self._config) is not None
            or os.environ.get("OPENSQUILLA_CODETASK_CHILD") == "1"
            or run_kind not in INTERACTIVE_RUN_KINDS
            or done_event is None
        ):
            return
        from opensquilla.observability.daily_usage_store import DailyUsageStore

        async with self._storage_lock:
            if self._storage is None:
                self._storage = await DailyUsageStore.open(
                    standalone_daily_usage_path(self._config),
                )
            await record_completed_turn(
                self._storage, config=self._config, run_kind=run_kind, done_event=done_event,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task, self._upload_task = self._upload_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        async def finish_usage() -> None:
            if os.environ.get("OPENSQUILLA_CODETASK_CHILD") != "1":
                await _upload_pending_sources(self._legacy_storage, config=self._config)

        async def finish_install() -> None:
            thread, self._install_thread = self._install_thread, None
            if thread is not None:
                await asyncio.to_thread(thread.join, STANDALONE_CLOSE_TIMEOUT_SECONDS)

        try:
            async with asyncio.timeout(STANDALONE_CLOSE_TIMEOUT_SECONDS):
                await asyncio.gather(finish_usage(), finish_install(), return_exceptions=True)
        except Exception:
            log.debug("Standalone telemetry shutdown upload incomplete", exc_info=True)
        finally:
            async with self._storage_lock:
                storage, self._storage = self._storage, None
                if storage is not None:
                    await storage.close()


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
        import httpx

        async with httpx.AsyncClient(timeout=install_telemetry.DEFAULT_TIMEOUT_SECONDS) as client:
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
