"""Renewable owner leases: a dropped connection gets grace, orphan work is cancelled."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from opensquilla.session.sidechat import set_state, sidechat_metadata
from opensquilla.session.storage import StorageBusyError, _is_sqlite_busy

_leases: dict[tuple[int, str], asyncio.Task[None]] = {}
log = structlog.get_logger(__name__)


async def renew_sidechat_lease(
    storage: Any,
    key: str,
    close: Callable[[], Awaitable[object]],
) -> None:
    await set_state(storage, key, "ready", renew=True)
    watch_sidechat_lease(storage, key, close)


def watch_sidechat_lease(
    storage: Any,
    key: str,
    close: Callable[[], Awaitable[object]],
) -> None:
    identity = (id(storage), key)
    current = _leases.get(identity)
    if current is not None and not current.done():
        return

    async def watch() -> None:
        delay = 0.25
        next_warning = 0.0
        try:
            while True:
                try:
                    node = await storage.get_session(key)
                    metadata = sidechat_metadata(node)
                    if metadata is None or metadata.get("state") == "closed":
                        return
                    parent = await storage.get_session(metadata["parentKey"])
                except (StorageBusyError, sqlite3.OperationalError) as exc:
                    if not isinstance(exc, StorageBusyError) and not _is_sqlite_busy(exc):
                        raise
                    now = time.monotonic()
                    if now >= next_warning:
                        log.warning("sidechat.lease_read_deferred", session_key=key)
                        next_warning = now + 30
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
                    continue
                delay = 0.25
                next_warning = 0.0
                parent_changed = (
                    parent is None
                    or parent.session_id != metadata["parentSessionId"]
                    or parent.epoch != metadata["parentEpoch"]
                )
                remaining = (
                    int(metadata.get("leaseExpiresAt", 0)) - int(time.time() * 1000)
                ) / 1000
                if remaining <= 0 or parent_changed or metadata.get("state") == "closing":
                    try:
                        await close()
                        return
                    except Exception:
                        # Stay fenced and retry cleanup; never silently reopen a
                        # child because a provider or subprocess drains slowly.
                        log.warning("sidechat.lease_cleanup_deferred", session_key=key)
                        await asyncio.sleep(5)
                        continue
                await asyncio.sleep(min(remaining, 60))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sidechat.lease_watch_failed", session_key=key)
        finally:
            if _leases.get(identity) is asyncio.current_task():
                _leases.pop(identity, None)

    _leases[identity] = asyncio.create_task(watch(), name=f"sidechat-lease:{key}")


async def restore_sidechat_leases(
    ctx: Any, close_child: Callable[[str], Awaitable[object]],
) -> None:
    """Restore persisted deadlines before restart recovery may resume any queued turn."""
    from opensquilla.gateway.session_services import get_session_storage

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return
    reader = getattr(storage, "list_open_sidechats", None)
    if not callable(reader):
        return
    for child in await reader():
        key = child.session_key
        metadata = sidechat_metadata(child) or {}

        async def close(key: str = key) -> object:
            return await close_child(key)

        if int(metadata.get("leaseExpiresAt", 0)) <= int(time.time() * 1000):
            # Expiry already fences admission. A provider/process may take
            # longer to drain than one close attempt; it must not prevent the
            # entire Gateway from starting or lose its cleanup owner.
            await set_state(storage, key, "closing")
            try:
                await close()
            except Exception:
                log.warning("sidechat.restart_cleanup_deferred", session_key=key)
                watch_sidechat_lease(storage, key, close)
        else:
            watch_sidechat_lease(storage, key, close)
