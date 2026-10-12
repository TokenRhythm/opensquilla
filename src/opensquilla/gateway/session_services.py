"""Typed gateway accessors for session/runtime services.

The gateway still accepts older test doubles that expose historical private
attributes. Production objects should expose the public methods/properties
documented by these helpers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast


class SessionStorageProvider(Protocol):
    @property
    def storage(self) -> Any: ...


class SessionEpochCache(Protocol):
    def get_cached_epoch(self, session_key: str) -> int | None: ...

    def set_cached_epoch(self, session_key: str, epoch: int) -> None: ...


class SessionLockProvider(Protocol):
    def get_session_lock(self, session_key: str) -> asyncio.Lock: ...


def get_session_storage(session_manager: object | None) -> Any | None:
    """Return the session storage surface exposed by a manager-like object."""
    if session_manager is None:
        return None
    storage = getattr(session_manager, "storage", None)
    if storage is not None:
        return storage
    return getattr(session_manager, "_storage", None)


def get_session_epoch(session_manager: object | None, session_key: str) -> int | None:
    """Read the in-process epoch cache through a public surface when available."""
    if session_manager is None:
        return None
    getter = getattr(session_manager, "get_cached_epoch", None)
    if callable(getter):
        value = getter(session_key)
        return int(value) if value is not None else None
    cache = getattr(session_manager, "_epoch_cache", None)
    if not isinstance(cache, dict):
        return None
    value = cache.get(session_key)
    return int(value) if value is not None else None


def set_session_epoch(session_manager: object | None, session_key: str, epoch: int) -> None:
    """Update the in-process epoch cache through a public surface when available."""
    if session_manager is None:
        return
    setter = getattr(session_manager, "set_cached_epoch", None)
    if callable(setter):
        setter(session_key, epoch)
        return
    cache = getattr(session_manager, "_epoch_cache", None)
    if isinstance(cache, dict):
        cache[session_key] = epoch


def get_session_lock(
    turn_runner: object | None,
    session_key: str,
) -> asyncio.Lock | None:
    """Return a per-session runtime lock without coupling RPC to private fields."""
    if turn_runner is None:
        return None
    public_getter = getattr(turn_runner, "get_session_lock", None)
    if callable(public_getter):
        return cast(asyncio.Lock, public_getter(session_key))
    private_getter = getattr(turn_runner, "_get_session_lock", None)
    if callable(private_getter):
        return cast(asyncio.Lock, private_getter(session_key))
    return None
