from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.session_services import session_id_for_key


@pytest.mark.asyncio
async def test_session_id_for_key_prefers_manager_lookup() -> None:
    class Manager:
        async def get_session(self, key: str):
            assert key == "agent:main:main"
            return SimpleNamespace(session_id="session-1")

    assert await session_id_for_key(Manager(), "agent:main:main") == "session-1"


@pytest.mark.asyncio
async def test_session_id_for_key_treats_missing_or_invalid_rows_as_absent() -> None:
    class MissingManager:
        async def get_session(self, _key: str):
            raise KeyError("missing")

    class InvalidManager:
        async def get_session(self, _key: str):
            return SimpleNamespace(session_id="")

    assert await session_id_for_key(MissingManager(), "agent:main:main") is None
    assert await session_id_for_key(InvalidManager(), "agent:main:main") is None
