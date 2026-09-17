"""Same-id assistant materialization tests for the Step 1 foundation."""

from __future__ import annotations

import pytest
import pytest_asyncio

from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture
async def manager() -> SessionManager:
    storage = SessionStorage(":memory:")
    await storage.connect()
    value = SessionManager(storage, inject_time_prefix=False)
    await value.create("agent:main:identity")
    yield value
    await storage.close()


@pytest.mark.asyncio
async def test_reservation_is_ledger_only_and_same_id_upserts_one_row(
    manager: SessionManager,
) -> None:
    key = "agent:main:identity"
    node = await manager.get_session(key)
    assert node is not None

    reservation = manager.reserve_assistant_message(
        "turn-identity",
        "assistant-caller-id",
        key,
        "web",
    )
    assert reservation.assistant_message_id == "assistant-caller-id"
    assert await manager._storage.count_transcript_entries(node.session_id) == 0

    first = await manager.append_message(
        key,
        "assistant",
        "draft",
        message_id=reservation.assistant_message_id,
        token_count=3,
    )
    second = await manager.append_message(
        key,
        "assistant",
        "final",
        message_id=reservation.assistant_message_id,
        token_count=5,
    )
    replay = await manager.append_message(
        key,
        "assistant",
        "final",
        message_id=reservation.assistant_message_id,
        token_count=5,
    )

    entries = await manager.get_transcript(key)
    assert len(entries) == 1
    assert entries[0].message_id == reservation.assistant_message_id
    assert entries[0].content == "final"
    assert second.id == replay.id == entries[0].id
    assert first.message_id == second.message_id == replay.message_id


@pytest.mark.asyncio
async def test_reservation_without_visible_output_creates_no_row(
    manager: SessionManager,
) -> None:
    key = "agent:main:identity"
    node = await manager.get_session(key)
    assert node is not None

    manager.reserve_assistant_message(
        "turn-no-output",
        "assistant-never-materialized",
        key,
    )

    assert await manager._storage.count_transcript_entries(node.session_id) == 0
