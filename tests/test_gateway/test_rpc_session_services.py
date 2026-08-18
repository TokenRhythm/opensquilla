from __future__ import annotations

import asyncio
from types import SimpleNamespace

from opensquilla.gateway.session_services import (
    clear_session_epoch,
    get_session_epoch,
    get_session_lock,
    get_session_storage,
    set_session_epoch,
)


class _PublicSessionManager:
    def __init__(self, storage: object) -> None:
        self.storage = storage
        self.epochs: dict[str, int] = {}

    def get_cached_epoch(self, session_key: str) -> int | None:
        return self.epochs.get(session_key)

    def set_cached_epoch(self, session_key: str, epoch: int) -> None:
        self.epochs[session_key] = epoch

    def clear_cached_epoch(self, session_key: str) -> None:
        self.epochs.pop(session_key, None)


def test_session_services_prefer_public_session_manager_surface() -> None:
    storage = object()
    manager = _PublicSessionManager(storage)

    assert get_session_storage(manager) is storage
    assert get_session_epoch(manager, "agent:main:main") is None

    set_session_epoch(manager, "agent:main:main", 7)

    assert manager.epochs == {"agent:main:main": 7}
    assert get_session_epoch(manager, "agent:main:main") == 7

    clear_session_epoch(manager, "agent:main:main")
    assert manager.epochs == {}


def test_session_services_keep_private_fallback_for_older_test_doubles() -> None:
    storage = object()
    manager = SimpleNamespace(_storage=storage, _epoch_cache={})

    assert get_session_storage(manager) is storage
    assert get_session_epoch(manager, "agent:main:main") is None

    set_session_epoch(manager, "agent:main:main", 3)

    assert manager._epoch_cache == {"agent:main:main": 3}
    assert get_session_epoch(manager, "agent:main:main") == 3
    clear_session_epoch(manager, "agent:main:main")
    assert manager._epoch_cache == {}


def test_get_session_lock_prefers_public_runtime_method() -> None:
    lock = asyncio.Lock()
    runner = SimpleNamespace(get_session_lock=lambda _key: lock)

    assert get_session_lock(runner, "agent:main:main") is lock
