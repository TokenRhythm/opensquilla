from __future__ import annotations

# fmt: off
# ruff: noqa: E501
import asyncio
import contextlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol, cast

import structlog

from opensquilla.channels.types import IncomingMessage

log = structlog.get_logger(__name__)


class DebounceCoordinator(Protocol):
    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any, on_settled: Any = None) -> None:
        pass


@dataclass
class _DebounceState:
    buffer: list[IncomingMessage]
    on_fire: Any
    on_settled: list[Any] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    settled: bool = False
    settle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _DefaultDebounceCoordinator:
    def __init__(self) -> None:
        self._pending: dict[str, _DebounceState] = {}
        self._lock = asyncio.Lock()

    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any, on_settled: Any = None) -> None:
        async with self._lock:
            existing = self._pending.get(session_key)
            if existing is not None:
                existing.buffer.append(message)
                if on_settled is not None:
                    existing.on_settled.append(on_settled)
                return
            state = _DebounceState(
                buffer=[message],
                on_fire=on_fire,
                on_settled=([on_settled] if on_settled is not None else []),
            )
            self._pending[session_key] = state
            state.task = asyncio.create_task(
                self._fire(session_key, window_s, state),
                name=f"channel-debounce:{session_key}",
            )

    async def cancel(self, session_key: str) -> None:
        async with self._lock:
            state = self._pending.pop(session_key, None)
        if state is None:
            return
        task = state.task
        if task is not None and not task.done():
            log.info("channel.debounce_cancelled", session_key=session_key)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await cast(Any, task)
        # A task cancelled before its coroutine takes the first step does not
        # execute ``finally``. Settle here as the exactly-once fallback.
        await self._settle(session_key, state)

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(k) for k in list(self._pending)), return_exceptions=True)

    async def _fire(
        self,
        session_key: str,
        window_s: float,
        state: _DebounceState,
    ) -> None:
        try:
            await asyncio.sleep(window_s)
            async with self._lock:
                if self._pending.get(session_key) is not state:
                    return
                self._pending.pop(session_key, None)
            if not state.buffer:
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
        finally:
            async with self._lock:
                if self._pending.get(session_key) is state:
                    self._pending.pop(session_key, None)
            await self._settle(session_key, state)

    async def _settle(self, session_key: str, state: _DebounceState) -> None:
        async with state.settle_lock:
            if state.settled:
                return
            state.settled = True
            # Each scheduled user message contributes one intent lease.  The
            # batch owns them until its durable acceptance completes, or until
            # cancellation/error proves no task will be accepted.
            for settle in state.on_settled:
                try:
                    await settle()
                except Exception:
                    log.exception(
                        "channel_dispatch.debounce_intent_release_failed",
                        session_key=session_key,
                    )
