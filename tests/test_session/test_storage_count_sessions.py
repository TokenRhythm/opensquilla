"""Exact stored-session counting contracts."""

from __future__ import annotations

import pytest

from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def storage(tmp_path):
    store = SessionStorage(str(tmp_path / "sessions.db"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _seed(storage: SessionStorage, key: str, session_id: str) -> None:
    await storage.upsert_session(
        SessionNode(
            session_key=key,
            session_id=session_id,
            agent_id="main",
            status="idle",
            created_at=1,
            updated_at=1,
        )
    )


async def test_count_sessions_can_scope_to_one_guest_owner(storage: SessionStorage) -> None:
    owner_id = "a" * 64
    other_owner_id = "b" * 64
    await _seed(storage, guest_owned_session_key(owner_id, "one"), "guest-one")
    await _seed(storage, guest_owned_session_key(owner_id, "two"), "guest-two")
    await _seed(storage, guest_owned_session_key(other_owner_id, "other"), "guest-other")
    await _seed(storage, "agent:main:webchat:host", "host")

    assert await storage.count_sessions() == 4
    assert await storage.count_sessions(guest_owner_id=owner_id) == 2
    assert await storage.count_sessions(guest_owner_id="invalid") == 0
