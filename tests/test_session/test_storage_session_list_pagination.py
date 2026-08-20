from __future__ import annotations

import pytest

from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionListCursor, SessionStorage


@pytest.fixture
async def storage(tmp_path):
    store = SessionStorage(str(tmp_path / "sessions.db"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _seed(
    storage: SessionStorage,
    keys: list[str],
    *,
    updated_at: int = 1000,
) -> None:
    for index, key in enumerate(keys):
        key_parts = key.split(":", 2)
        agent_id = key_parts[1] if len(key_parts) == 3 and key_parts[0] == "agent" else "main"
        await storage.upsert_session(
            SessionNode(
                session_key=key,
                session_id=f"session-{index}-{key[-8:]}",
                agent_id=agent_id,
                status="idle",
                created_at=updated_at,
                updated_at=updated_at,
            )
        )


async def _collect(
    storage: SessionStorage,
    *,
    limit: int,
    cursor: SessionListCursor | None = None,
    guest_owner_id: str | None = None,
) -> tuple[list[str], list[int]]:
    keys: list[str] = []
    sizes: list[int] = []
    while True:
        page = await storage.list_sessions_page(
            limit=limit,
            cursor=cursor,
            guest_owner_id=guest_owner_id,
        )
        page_keys = [session.session_key for session in page.sessions]
        keys.extend(page_keys)
        sizes.append(len(page_keys))
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    return keys, sizes


async def test_keyset_pages_all_401_sessions_with_tied_timestamps(
    storage: SessionStorage,
) -> None:
    seeded = [f"agent:main:webchat:session-{index:03d}" for index in range(401)]
    await _seed(storage, seeded)

    keys, sizes = await _collect(storage, limit=200)

    assert sizes == [200, 200, 1]
    assert keys == sorted(seeded, reverse=True)
    assert len(keys) == len(set(keys)) == 401


async def test_keyset_traversal_handles_concurrent_insert_and_delete(
    storage: SessionStorage,
) -> None:
    seeded = [f"agent:main:webchat:session-{index:03d}" for index in range(205)]
    await _seed(storage, seeded)
    first = await storage.list_sessions_page(limit=100)
    assert first.next_cursor is not None

    inserted = "agent:main:webchat:newer"
    await _seed(storage, [inserted], updated_at=2000)
    deleted = "agent:main:webchat:session-050"
    await storage.delete_session(deleted)

    remaining, _sizes = await _collect(
        storage,
        limit=100,
        cursor=first.next_cursor,
    )
    traversed = [session.session_key for session in first.sessions] + remaining

    assert inserted not in traversed
    assert deleted not in traversed
    assert len(traversed) == len(set(traversed))
    assert set(traversed) == set(seeded) - {deleted}

    refreshed, _sizes = await _collect(storage, limit=100)
    assert set(refreshed) == (set(seeded) - {deleted}) | {inserted}


async def test_keyset_page_preserves_guest_owner_filter(storage: SessionStorage) -> None:
    owner_id = "a" * 64
    other_owner_id = "b" * 64
    visible = [
        guest_owned_session_key(owner_id, "visible-main", agent_id="main"),
        guest_owned_session_key(owner_id, "visible-research", agent_id="research"),
        guest_owned_session_key(owner_id, "visible-ops", agent_id="ops"),
    ]
    await _seed(
        storage,
        [
            *visible,
            guest_owned_session_key(other_owner_id, "other"),
            "agent:main:webchat:host",
        ],
    )

    keys, sizes = await _collect(storage, limit=2, guest_owner_id=owner_id)

    assert sizes == [2, 1]
    assert keys == sorted(visible, reverse=True)


async def test_keyset_page_returns_terminal_empty_page(storage: SessionStorage) -> None:
    page = await storage.list_sessions_page(limit=200)

    assert page.sessions == []
    assert page.has_more is False
    assert page.next_cursor is None
