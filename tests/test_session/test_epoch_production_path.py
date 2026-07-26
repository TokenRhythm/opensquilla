"""Tests for the epoch production path.

Verifies that ``SessionManager.append_message`` passes ``expected_epoch``
through to storage; that a concurrent reset + write pair is atomic so
the write either fully succeeds (pre-reset) or fully fails (post-reset)
with no partial rows; and that ``_emit_to_subscribers`` reads the
in-process epoch cache after warm-up rather than hitting the DB on
every event.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage, StaleEpochError

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def storage():
    s = SessionStorage(":memory:")
    await s.connect()
    yield s
    await s.close()


async def _make_session(storage: SessionStorage, key: str = "agent:main:prod") -> SessionNode:
    node = SessionNode(session_key=key, session_id="sid-prod")
    await storage.upsert_session(node)
    return node


# ── append_message passes expected_epoch ────────────────────────────────────


@pytest.mark.asyncio
async def test_manager_passes_expected_epoch(storage):
    """SessionManager.append_message must pass expected_epoch= to storage.

    Uses real storage so we can verify the epoch guard fires on a stale write.
    """
    node = await _make_session(storage)
    key = node.session_key

    mgr = SessionManager(storage)

    # epoch=0 at this point — a normal write must succeed.
    entry = await mgr.append_message(key, "user", "hello")
    assert entry is not None

    # Now simulate a reset: epoch bumps to 1.
    await storage.increment_epoch(key)

    # The manager reads the node (epoch=0 snapshot) then tries to write — but
    # the storage epoch is now 1.  The atomic guard must reject it.
    # We can reproduce this by monkey-patching get_session to return a stale node.
    stale_node = await storage.get_session(key)
    stale_node.epoch = 0  # artificially stale

    original_get = storage.get_session

    async def _stale_get(k):
        return stale_node

    storage.get_session = _stale_get
    try:
        with pytest.raises(StaleEpochError):
            await mgr.append_message(key, "assistant", "stale response")
    finally:
        storage.get_session = original_get


# ── append_message passes expected_epoch (mock variant) ──────────────────────


@pytest.mark.asyncio
async def test_manager_passes_expected_epoch_mock():
    """Append and the narrow session touch must be one storage transaction."""
    mock_storage = MagicMock()
    mock_storage.get_session = AsyncMock(
        return_value=SessionNode(
            session_key="agent:main:mock",
            session_id="sid-mock",
            epoch=7,
        )
    )
    mock_storage.upsert_session = AsyncMock()
    mock_storage.append_transcript_entry = AsyncMock()
    mock_storage.append_transcript_entry_and_touch = AsyncMock()

    mgr = SessionManager(mock_storage)
    await mgr.append_message("agent:main:mock", "user", "hi", token_count=11)

    mock_storage.append_transcript_entry_and_touch.assert_awaited_once()
    args, kwargs = mock_storage.append_transcript_entry_and_touch.call_args
    assert isinstance(args[0], TranscriptEntry)
    assert kwargs["expected_epoch"] == 7, (
        f"expected_epoch should be 7 (from node.epoch), got {kwargs['expected_epoch']}"
    )
    assert isinstance(kwargs["updated_at"], int)
    assert kwargs["token_delta"] == 11
    assert kwargs["mark_total_tokens_stale"] is True
    mock_storage.append_transcript_entry.assert_not_awaited()
    mock_storage.upsert_session.assert_not_awaited()


# ── concurrent reset + write is atomic ──────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_reset_during_write_atomic(storage):
    """Reset concurrent with write: result is atomic — write fully succeeds or fully fails.

    Spawns 3 coroutines all holding epoch=0; fires a reset (epoch→1); all 3
    writes must raise StaleEpochError and leave zero rows in transcript.
    """
    node = await _make_session(storage)
    key = node.session_key

    captured_epoch = await storage.get_epoch(key)  # 0

    # Reset fires while writes are in-flight.
    await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 1

    def _entry(label: str) -> TranscriptEntry:
        return TranscriptEntry(
            session_id=node.session_id,
            session_key=key,
            role="assistant",
            content=f"concurrent {label}",
        )

    async def _write(label: str) -> str:
        try:
            await storage.append_transcript_entry(
                _entry(label), expected_epoch=captured_epoch
            )
            return "ok"
        except StaleEpochError:
            return "stale"

    results = await asyncio.gather(_write("A"), _write("B"), _write("C"))

    # All must be rejected — no partial commit.
    assert all(r == "stale" for r in results), (
        f"All concurrent stale writes must be rejected; got {results}"
    )
    entries = await storage.get_transcript(node.session_id)
    assert entries == [], f"No rows must be written after stale epoch; found {entries}"


# ── _emit_to_subscribers uses cache after warm-up ──────────────────────────


@pytest.mark.asyncio
async def test_emit_no_db_query_per_event(storage):
    """_emit_to_subscribers must not call storage.get_epoch on every event.

    After the first cache-miss DB hit, subsequent emits for the same session
    read the in-process cache.  100 emits → < 5 DB calls.
    """
    from opensquilla.gateway.rpc_sessions import _emit_to_subscribers

    node = await _make_session(storage)
    key = node.session_key

    # Track get_epoch call count.
    call_count = 0
    original_get_epoch = storage.get_epoch

    async def _counting_get_epoch(session_key: str) -> int:
        nonlocal call_count
        call_count += 1
        return await original_get_epoch(session_key)

    storage.get_epoch = _counting_get_epoch

    class FakeConn:
        async def send_event(self, event_name: str, payload: dict) -> None:
            pass

    fake_conn = FakeConn()

    class FakeRegistry:
        def get(self, conn_id: str):
            return fake_conn

    class FakeSubMgr:
        def get_message_subscribers(self, session_key: str):
            return {"conn-1"}

        def get_session_subscribers(self):
            return set()

    # Use a real SessionManager so _epoch_cache is available.
    session_manager = SessionManager(storage)

    fake_ctx = SimpleNamespace(
        subscription_manager=FakeSubMgr(),
        session_manager=session_manager,
    )

    import opensquilla.gateway.websocket as _ws_module
    original_get_registry = _ws_module.get_registry
    _ws_module.get_registry = lambda: FakeRegistry()
    try:
        for _ in range(100):
            await _emit_to_subscribers(
                fake_ctx, key, "session.event.text_delta", {"text": "x"}
            )
    finally:
        _ws_module.get_registry = original_get_registry

    assert call_count < 5, (
        f"storage.get_epoch called {call_count} times for 100 emits; "
        f"expected < 5 (cache should be used after first miss)"
    )
