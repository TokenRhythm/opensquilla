from __future__ import annotations

# fmt: off
# ruff: noqa: E501
import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, Protocol

import structlog

from opensquilla.channels.types import IncomingMessage

log = structlog.get_logger(__name__)


class DebounceCoordinator(Protocol):
    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any) -> None: ...


class _DefaultDebounceCoordinator:
    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any) -> None:
        async with self._lock:
            if state := self._pending.get(session_key):
                state.buffer.append(message)
                return
            task = asyncio.create_task(self._fire(session_key, window_s), name=f"channel-debounce:{session_key}")
            self._pending[session_key] = SimpleNamespace(buffer=[message], on_fire=on_fire, task=task)

    async def cancel(self, session_key: str) -> None:
        async with self._lock:
            state = self._pending.pop(session_key, None)
        if state is None or state.task.done():
            return
        log.info("channel.debounce_cancelled", session_key=session_key)
        state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.task

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(k) for k in list(self._pending)), return_exceptions=True)

    async def _fire(self, session_key: str, window_s: float) -> None:
        try:
            await asyncio.sleep(window_s)
            async with self._lock:
                state = self._pending.pop(session_key, None)
            if state is None:
                return
            first = state.buffer[0]
            content = "\n".join(m.content for m in state.buffer)
            attachments = [a for m in state.buffer for a in (m.attachments or [])]
            metadata = dict(first.metadata or {})
            native_ids: list[str] = []
            aliases = ("native_message_id", "message_id", "msg_id", "event_id", "activity_id", "update_id", "ts")
            for buffered in state.buffer:
                buffered_metadata = dict(buffered.metadata or {})
                for alias in aliases:
                    value = buffered_metadata.get(alias)
                    if value is not None and str(value).strip():
                        native_ids.append(f"{alias}:{str(value).strip()}")
                        break
            if len(native_ids) == len(state.buffer):
                metadata["_opensquilla_debounce_native_message_ids"] = native_ids
            else:
                # A partial aggregate cannot be identified by its known subset:
                # a later batch could reuse those ids with different no-id
                # messages and collide. Preserve the first message's native
                # routing metadata, but force the whole batch onto one generated
                # fallback identity instead of falling through to that alias.
                metadata["_opensquilla_debounce_native_ids_incomplete"] = True
            msg = IncomingMessage(sender_id=first.sender_id, channel_id=first.channel_id, content=content, attachments=attachments, metadata=metadata, provenance=first.provenance)
            combined = SimpleNamespace(content=content, attachments=attachments, message=msg, source_messages=tuple(state.buffer), coalesced_count=len(state.buffer))
            log.info("channel.debounce_coalesced", session_key=session_key, coalesced_count=combined.coalesced_count)
            await state.on_fire(combined)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel_dispatch.debounce_enqueue_failed", reason="unexpected")
