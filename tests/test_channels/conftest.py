"""Real owned channel stores for adapter/runtime integration tests."""

from __future__ import annotations

import pytest

from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore


@pytest.fixture
async def channel_store():
    stores = []

    def factory(path, **kwargs):
        store = ChannelDeliveryStore(path, **kwargs)
        # Fault tests hold an external writer lock; only tests shorten the
        # busy wait. The production worker retains SQLite's 30 second budget.
        store._conn.execute("PRAGMA busy_timeout=100")
        return store

    async def create(path, **kwargs):
        store = AsyncChannelDeliveryStore(
            path, store_factory=lambda value: factory(value, **kwargs)
        )
        stores.append(store)
        await store.open()
        return store

    yield create
    for store in stores:
        await store.close()
