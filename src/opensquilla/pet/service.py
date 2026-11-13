"""PetReminderService — in-process reminder + dispatch lifecycle (PRD F2/F3).

Subscribes to the live session-event funnel, maps terminal/approval/plan events
to native ``/notify`` calls (identity-deduped, pause-aware, redacted), spools
offline, and polls the native dispatch queue. Started only by the gateway boot
path when ``[pet].enabled``; importing this module never starts anything.

Notification text is the configured template, redacted and truncated — raw
session content is never echoed to the pet (PRD risk R6).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.pet import events
from opensquilla.pet.bridge_client import BridgeClient
from opensquilla.pet.dispatch import DispatchProcessor
from opensquilla.pet.ports import RpcInvoker, SessionEventSource, SessionMetadataResolver
from opensquilla.pet.spool import Spool, is_paused, resolve_pause_file, resolve_spool_dir
from opensquilla.redaction import redact_error_text

if TYPE_CHECKING:
    from opensquilla.gateway.config import PetConfig

log = structlog.get_logger(__name__)

_MESSAGE_MAX_LEN = 120
_QUEUE_MAX = 1000


class PetReminderService:
    """Own the reminder/dispatch loops for one gateway process."""

    def __init__(
        self,
        config: PetConfig,
        *,
        event_source: SessionEventSource,
        rpc_invoke: RpcInvoker,
        bridge: BridgeClient | None = None,
        metadata_resolver: SessionMetadataResolver | None = None,
    ) -> None:
        self._config = config
        self._event_source = event_source
        self._metadata_resolver = metadata_resolver
        self._bridge = bridge or BridgeClient(
            config.bridge_url, timeout_ms=config.request_timeout_ms
        )
        self._ledger = events.DedupeLedger(config.dedupe_window_ms)
        self._spool = Spool(
            resolve_spool_dir(config.spool_dir), cap_bytes=config.spool_cap_bytes
        )
        self._pause_file = resolve_pause_file(config.pause_file)
        self._dispatch = (
            DispatchProcessor(self._bridge, rpc_invoke, exe=config.exe)
            if config.dispatch_enabled
            else None
        )
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue(
            maxsize=_QUEUE_MAX
        )
        self._remove_observer: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> str:
        if self._running:
            return "ready"
        self._running = True
        self._remove_observer = self._event_source.add_local_observer(self._on_event)
        self._tasks.append(asyncio.create_task(self._notify_loop()))
        if self._dispatch is not None:
            self._tasks.append(asyncio.create_task(self._dispatch_loop()))
        log.info("pet.service_started", bridge=self._config.bridge_url)
        return "ready"

    async def stop(self) -> None:
        self._running = False
        if self._remove_observer is not None:
            try:
                self._remove_observer()
            except Exception:  # noqa: BLE001
                pass
            self._remove_observer = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._bridge.aclose()
        log.info("pet.service_stopped")

    @property
    def running(self) -> bool:
        return self._running

    # -- event intake (cheap, non-blocking; runs inside record()) ----------

    def _on_event(self, session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        if events.map_event(event_name) is None:
            # Clarify/choice cards are not a dedicated event: they ride a
            # tool_result whose result is a paused user-input form. Only
            # enqueue those, not every tool result.
            if event_name != events.TOOL_RESULT_EVENT:
                return
            if events.pending_clarify_request(payload) is None:
                return
        if not self._config.include_cron and session_key.startswith("cron:"):
            return
        try:
            self._queue.put_nowait((session_key, event_name, dict(payload)))
        except asyncio.QueueFull:
            log.debug("pet.queue_full", session_key=session_key)

    # -- workers -----------------------------------------------------------

    async def _notify_loop(self) -> None:
        while self._running:
            session_key, event_name, payload = await self._queue.get()
            try:
                await self._handle_event(session_key, event_name, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.debug("pet.notify_handle_failed", error=str(exc))

    async def _dispatch_loop(self) -> None:
        interval = max(0.25, self._config.poll_interval_ms / 1000.0)
        while self._running:
            try:
                if self._dispatch is not None:
                    await self._dispatch.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("pet.dispatch_loop_failed", error=str(exc))
            await asyncio.sleep(interval)

    # -- notification logic ------------------------------------------------

    def _message_for(self, kind: str) -> str:
        cfg = self._config
        if kind == events.KIND_STOP:
            return cfg.msg_stop
        if kind == events.KIND_PERMISSION:
            return cfg.msg_permission
        if kind == events.KIND_QUESTION:
            return cfg.msg_plan
        return cfg.msg_turn_start

    async def _handle_event(
        self, session_key: str, event_name: str, payload: dict[str, Any]
    ) -> None:
        if event_name == events.TOOL_RESULT_EVENT:
            # A pending clarify/choice card → speak a question prompt and raise
            # the window, deduped by the form's request id.
            request_id = events.pending_clarify_request(payload)
            if request_id is None:
                return
            kind = events.KIND_QUESTION
            identity = request_id
            message = self._config.msg_clarify
            dedup_payload: dict[str, Any] = {"request_id": request_id}
        else:
            kind = events.map_event(event_name)
            if kind is None:
                return
            identity = events.event_identity(payload)
            message = self._message_for(kind)
            dedup_payload = payload

        if kind == events.KIND_TURN_START:
            # New turn: void the previous turn's ledger, then archive silently.
            self._ledger.reset_session(session_key)
            _title, project = await self._resolve_metadata(session_key)
            await self._send_or_spool(
                kind, self._config.msg_turn_start, session_key, identity,
                raise_window="", project=project,
            )
            return

        if not self._ledger.allow(session_key, kind, dedup_payload):
            return
        if is_paused(self._pause_file):
            log.debug("pet.paused", kind=kind)
            return

        title, project = await self._resolve_metadata(session_key)
        if "{session}" in message:
            message = message.replace("{session}", events.session_label(title, project))
        text = redact_error_text(message, max_len=_MESSAGE_MAX_LEN)
        raise_window = (
            self._config.raise_window if events.raises_foreground(kind) else ""
        )
        await self._send_or_spool(
            kind, text, session_key, identity, raise_window, project=project
        )

    async def _resolve_metadata(self, session_key: str) -> tuple[str, str]:
        if self._metadata_resolver is None:
            return ("", "")
        try:
            title, project = await self._metadata_resolver(session_key)
            return (str(title or ""), str(project or ""))
        except Exception:  # noqa: BLE001 - labeling is best-effort
            return ("", "")

    async def _send_or_spool(
        self,
        kind: str,
        message: str,
        session_key: str,
        identity: str,
        raise_window: str,
        *,
        project: str = "",
    ) -> None:
        body = events.build_notify_payload(
            kind=kind,
            message=message,
            title=self._config.title,
            session_key=session_key,
            event_id=identity,
            raise_window=raise_window,
            project=project,
        )
        if await self._bridge.notify(body):
            log.info("pet.notify", kind=kind, session_key=session_key)
            return
        if self._spool.append(body):
            log.info("pet.notify_spooled", kind=kind, session_key=session_key)
        else:
            log.warning("pet.notify_lost", kind=kind, session_key=session_key)

    # -- tool surface ------------------------------------------------------

    async def speak(self, message: str) -> bool:
        """Speak arbitrary (redacted, truncated) text via the pet ``/speak``."""
        text = redact_error_text(message, max_len=_MESSAGE_MAX_LEN)
        if not text:
            return False
        return await self._bridge.speak(text, title=self._config.title)

    async def notify_text(self, message: str) -> bool:
        """Send a free-form ``message`` notification (redacted, truncated)."""
        text = redact_error_text(message, max_len=_MESSAGE_MAX_LEN)
        if not text:
            return False
        body = events.build_notify_payload(
            kind="message", message=text, title=self._config.title
        )
        if await self._bridge.notify(body):
            return True
        return self._spool.append(body)

    async def probe_state(self) -> dict[str, Any] | None:
        return await self._bridge.state()


__all__ = ["PetReminderService"]
