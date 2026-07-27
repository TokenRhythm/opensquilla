from __future__ import annotations

import pytest

from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_runtime_preference_persists_across_storage_restart(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        assert await storage.get_runtime_preference("sandbox.run_mode") is None
        assert (
            await storage.set_runtime_preference("sandbox.run_mode", "standard")
            == "standard"
        )
    finally:
        await storage.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        assert (
            await restarted.get_runtime_preference("sandbox.run_mode")
            == "standard"
        )
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_runtime_preference_upsert_replaces_confirmed_value() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        await storage.set_runtime_preference("sandbox.run_mode", "standard")

        confirmed = await storage.set_runtime_preference(
            "sandbox.run_mode",
            "trusted",
        )

        assert confirmed == "trusted"
        assert (
            await storage.get_runtime_preference("sandbox.run_mode")
            == "trusted"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", "trusted"),
        ("sandbox.run_mode", ""),
    ],
)
async def test_runtime_preference_rejects_empty_keys_and_values(
    key: str,
    value: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        with pytest.raises(ValueError):
            await storage.set_runtime_preference(key, value)
    finally:
        await storage.close()
