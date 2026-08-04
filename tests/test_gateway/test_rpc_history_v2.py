from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from opensquilla.gateway import rpc_chat as rpc_chat_module
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionSummary
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope


def _wire_bytes(frame: object) -> int:
    return len(frame.model_dump_json().encode("utf-8"))  # type: ignore[attr-defined]


def test_history_v2_methods_are_read_scoped() -> None:
    assert METHOD_SCOPES["chat.history.v2"] == READ_SCOPE
    assert METHOD_SCOPES["chat.history.entry.v1"] == READ_SCOPE


@pytest.mark.asyncio
async def test_history_v2_keeps_latest_whole_messages_within_byte_budget(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-budget.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-budget"
    contents = [f"row-{index}:" + (chr(97 + index) * 22_000) for index in range(6)]
    try:
        await manager.create(session_key)
        awaitables = [
            await manager.append_message(session_key, "user", content)
            for content in contents
        ]
        assert len(awaitables) == len(contents)
        entries = await manager.get_canonical_transcript(session_key)

        response = await get_dispatcher().dispatch(
            "history-v2-budget",
            "chat.history.v2",
            {
                "sessionKey": session_key,
                "limit": 200,
                "includeSummaries": False,
                "maxResponseBytes": 64 * 1024,
            },
            RpcContext(conn_id="history-v2-budget", session_manager=manager),
        )

        assert response.ok is True
        payload = response.payload
        assert payload["wire_bytes"] == _wire_bytes(response)
        assert payload["wire_bytes"] <= 64 * 1024
        assert payload["byte_budget"] == 64 * 1024
        assert payload["truncated_by_bytes"] is True
        assert payload["has_more"] is True
        returned = payload["messages"]
        assert 0 < len(returned) < len(contents)
        assert [message["text"] for message in returned] == contents[-len(returned) :]
        assert all(len(message["text"]) == len(contents[-1]) for message in returned)
        first_retained = entries[-len(returned)]
        assert payload["oldest_cursor"] == (
            f"{first_retained.created_at}|{first_retained.id}"
        )
        assert payload["newest_cursor"] == f"{entries[-1].created_at}|{entries[-1].id}"
        assert payload["loaded_count"] == len(returned)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_giant_entry_preview_reassembles_through_detail_chunks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-detail.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-detail"
    content = "begin-🦐-" + ("z" * 96_000) + "-end"
    try:
        await manager.create(session_key)
        appended = await manager.append_message(session_key, "assistant", content)
        entry = (await manager.get_canonical_transcript(session_key))[0]
        assert entry.message_id == appended.message_id
        dispatcher = get_dispatcher()
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        original_getter = storage.get_canonical_transcript_entry_by_cursor
        detail_projection_reads = 0

        async def _counting_getter(*args, **kwargs):
            nonlocal detail_projection_reads
            detail_projection_reads += 1
            return await original_getter(*args, **kwargs)

        monkeypatch.setattr(
            storage,
            "get_canonical_transcript_entry_by_cursor",
            _counting_getter,
        )

        response = await dispatcher.dispatch(
            "history-v2-detail",
            "chat.history.v2",
            {
                "sessionKey": session_key,
                "limit": 1,
                "includeSummaries": False,
                "maxResponseBytes": 64 * 1024,
            },
            RpcContext(conn_id="history-v2-detail", session_manager=manager),
        )

        assert response.ok is True
        payload = response.payload
        assert payload["wire_bytes"] == _wire_bytes(response)
        assert payload["wire_bytes"] <= 64 * 1024
        assert payload["truncated_by_bytes"] is True
        assert payload["loaded_count"] == 1
        preview = payload["messages"][0]
        cursor = f"{entry.created_at}|{entry.id}"
        assert preview["preview"] == ""
        assert len(preview["preview"].encode("utf-8")) <= 4 * 1024
        assert preview["detail_ref"] == {
            "method": "chat.history.entry.v1",
            "sessionKey": session_key,
            "cursor": cursor,
        }
        assert preview["original_bytes"] is None

        chunks: list[bytes] = []
        offset = 0
        expected_digest = None
        expected_total = None
        while True:
            detail = await dispatcher.dispatch(
                f"history-v2-entry-{offset}",
                "chat.history.entry.v1",
                {
                    "sessionKey": session_key,
                    "cursor": cursor,
                    "offset": offset,
                    "chunkBytes": 8 * 1024,
                },
                RpcContext(conn_id="history-v2-entry", session_manager=manager),
            )
            assert detail.ok is True
            chunk = detail.payload
            assert chunk["offset"] == offset
            chunks.append(base64.b64decode(chunk["chunk_base64"], validate=True))
            expected_digest = expected_digest or chunk["sha256"]
            expected_total = expected_total or chunk["total"]
            assert chunk["sha256"] == expected_digest
            assert chunk["total"] == expected_total
            if chunk["next"] is None:
                break
            offset = chunk["next"]

        reconstructed = b"".join(chunks)
        assert len(reconstructed) == expected_total
        assert hashlib.sha256(reconstructed).hexdigest() == expected_digest
        projected = json.loads(reconstructed)
        assert projected["text"] == content
        assert projected["message_id"] == entry.message_id

        text_chunks: list[bytes] = []
        offset = 0
        while True:
            detail = await dispatcher.dispatch(
                f"history-v2-text-{offset}",
                "chat.history.entry.v1",
                {
                    "sessionKey": session_key,
                    "cursor": cursor,
                    "offset": offset,
                    "chunkBytes": 8 * 1024,
                    "field": "text",
                },
                RpcContext(conn_id="history-v2-text", session_manager=manager),
            )
            assert detail.ok is True
            chunk = detail.payload
            assert chunk["field"] == "text"
            assert chunk["content_type"] == "text/plain; charset=utf-8"
            text_chunks.append(base64.b64decode(chunk["chunk_base64"], validate=True))
            if chunk["next"] is None:
                break
            offset = chunk["next"]
        assert b"".join(text_chunks).decode("utf-8") == content
        assert detail_projection_reads == 2

        legacy = await dispatcher.dispatch(
            "history-legacy-unchanged",
            "chat.history",
            {
                "sessionKey": session_key,
                "limit": 1,
                "includeSummaries": False,
            },
            RpcContext(conn_id="history-legacy", session_manager=manager),
        )
        assert legacy.ok is True
        assert legacy.payload["messages"][0]["text"] == content
        assert "wire_bytes" not in legacy.payload
        assert "truncated_by_bytes" not in legacy.payload
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_detail_streams_entries_above_the_exact_projection_limit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-detail-limit.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-detail-limit"
    try:
        await manager.create(session_key)
        content = "oversized-" + ("界" * 700_000)
        await manager.append_message(session_key, "assistant", content)
        entry = (await manager.get_canonical_transcript(session_key))[0]
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001

        async def _must_not_materialize(*args, **kwargs):
            raise AssertionError("giant plain detail must use incremental field reads")

        monkeypatch.setattr(
            storage,
            "get_canonical_transcript_entry_by_cursor",
            _must_not_materialize,
        )

        chunks: list[bytes] = []
        offset = 0
        while True:
            response = await get_dispatcher().dispatch(
                f"history-v2-detail-limit-{offset}",
                "chat.history.entry.v1",
                {
                    "sessionKey": session_key,
                    "cursor": f"{entry.created_at}|{entry.id}",
                    "chunkBytes": 256 * 1024,
                    "offset": offset,
                    "field": "text",
                },
                RpcContext(conn_id="history-v2-detail-limit", session_manager=manager),
            )
            assert response.ok is True
            chunks.append(base64.b64decode(response.payload["chunk_base64"], validate=True))
            if response.payload["next"] is None:
                break
            offset = response.payload["next"]

        assert b"".join(chunks).decode("utf-8") == content
        stats = await rpc_chat_module._HISTORY_ENTRY_SPOOL.stats()  # noqa: SLF001
        assert stats.disk_bytes == len(content.encode("utf-8"))
        assert stats.memory_bytes == 0
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_streams_mixed_utf8_message_json_across_chunk_boundaries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-streamed-json.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-streamed-json"
    content = "begin:" + ('中文🦐 "quote" \\ path\n' * 70_000) + ":end"
    try:
        await manager.create(session_key)
        await manager.append_message(session_key, "assistant", content)
        entry = (await manager.get_canonical_transcript(session_key))[0]
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001

        async def _must_not_materialize(*args, **kwargs):
            raise AssertionError("large plain message must use incremental field reads")

        monkeypatch.setattr(
            storage,
            "get_canonical_transcript_entry_by_cursor",
            _must_not_materialize,
        )
        cursor = f"{entry.created_at}|{entry.id}"
        chunks: list[bytes] = []
        offset = 0
        digest = ""
        total = 0
        while True:
            response = await get_dispatcher().dispatch(
                f"history-v2-streamed-json-{offset}",
                "chat.history.entry.v1",
                {
                    "sessionKey": session_key,
                    "cursor": cursor,
                    # Deliberately odd so UTF-8 sequences and JSON escapes cross
                    # both storage and transport chunk boundaries.
                    "chunkBytes": 65_537,
                    "offset": offset,
                    "field": "message",
                },
                RpcContext(conn_id="history-v2-streamed-json", session_manager=manager),
            )
            assert response.ok is True
            chunk = response.payload
            chunks.append(base64.b64decode(chunk["chunk_base64"], validate=True))
            digest = digest or chunk["sha256"]
            total = total or chunk["total"]
            assert chunk["sha256"] == digest
            assert chunk["total"] == total
            if chunk["next"] is None:
                break
            offset = chunk["next"]

        reconstructed = b"".join(chunks)
        assert len(reconstructed) == total
        assert hashlib.sha256(reconstructed).hexdigest() == digest
        assert json.loads(reconstructed)["text"] == content
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_long_whitespace_content_block_uses_legacy_projection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-content-block-prefix.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-content-block-prefix"
    visible_text = "visible legacy block"
    content = (
        (" " * 10_000)
        + f"[ContentBlockText(type='text', text='{visible_text}'),"
        + (" padding" * 150_000)
        + "]"
    )
    try:
        await manager.create(session_key)
        await manager.append_message(session_key, "assistant", content)
        entry = (await manager.get_canonical_transcript(session_key))[0]
        cursor = f"{entry.created_at}|{entry.id}"
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001

        async def _must_not_stream_plain(*args, **kwargs):
            raise AssertionError("ambiguous whitespace prefixes require exact normalization")

        monkeypatch.setattr(
            rpc_chat_module,
            "_build_streamed_plain_history_message",
            _must_not_stream_plain,
        )
        dispatcher = get_dispatcher()
        ctx = RpcContext(
            conn_id="history-v2-content-block-prefix",
            session_manager=manager,
        )

        legacy = await dispatcher.dispatch(
            "history-content-block-prefix-legacy",
            "chat.history",
            {"sessionKey": session_key, "includeSummaries": False},
            ctx,
        )
        assert legacy.ok is True
        assert legacy.payload["messages"][0]["text"] == visible_text

        detail = await dispatcher.dispatch(
            "history-content-block-prefix-detail",
            "chat.history.entry.v1",
            {"sessionKey": session_key, "cursor": cursor, "field": "message"},
            ctx,
        )
        assert detail.ok is True
        assert detail.payload["next"] is None
        projected = json.loads(base64.b64decode(detail.payload["chunk_base64"], validate=True))
        assert projected["text"] == visible_text
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["user", "json", "tool_calls"])
async def test_history_v2_large_legacy_projection_fails_closed_and_connection_survives(
    tmp_path,
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / f"history-v2-fail-closed-{shape}.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = f"agent:main:webchat:history-v2-fail-closed-{shape}"
    try:
        monkeypatch.setattr(
            rpc_chat_module,
            "_CHAT_HISTORY_DETAIL_EXACT_ROW_BYTES",
            64 * 1024,
        )
        await manager.create(session_key)
        if shape == "user":
            await manager.append_message(session_key, "user", "u" * 1_100_000)
        elif shape == "json":
            await manager.append_message(
                session_key,
                "assistant",
                json.dumps({"text": "j" * 1_100_000}),
            )
        else:
            await manager.append_message(
                session_key,
                "assistant",
                "tool metadata",
                tool_calls=[{"payload": "t" * 1_100_000}],
            )
        entry = (await manager.get_canonical_transcript(session_key))[0]
        cursor = f"{entry.created_at}|{entry.id}"
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001

        response = await get_dispatcher().dispatch(
            f"history-v2-fail-closed-{shape}",
            "chat.history.entry.v1",
            {"sessionKey": session_key, "cursor": cursor},
            RpcContext(conn_id=f"history-v2-fail-closed-{shape}", session_manager=manager),
        )
        assert response.ok is False
        assert response.error.code == "HISTORY_DETAIL_PROJECTION_TOO_LARGE"

        health = await get_dispatcher().dispatch(
            f"history-v2-fail-closed-health-{shape}",
            "health",
            {},
            RpcContext(conn_id=f"history-v2-fail-closed-{shape}", session_manager=manager),
        )
        assert health.ok is True
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_sends_summary_metadata_without_summary_text(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-summary.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-summary"
    summary_text = "earlier context " * 10_000
    try:
        session = await manager.create(session_key)
        await storage.save_summary(
            SessionSummary(
                session_id=session.session_id,
                session_key=session_key,
                compaction_id="compaction-v2",
                trigger_reason="manual",
                summary_text=summary_text,
                removed_count=12,
                kept_count=2,
                covered_through_id=42,
            )
        )
        dispatcher = get_dispatcher()

        response = await dispatcher.dispatch(
            "history-v2-summary",
            "chat.history.v2",
            {"sessionKey": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(conn_id="history-v2-summary", session_manager=manager),
        )
        assert response.ok is True
        metadata = response.payload["compaction_summaries"][0]
        assert "summary_text" not in metadata
        assert metadata["summary_bytes"] == len(summary_text.encode("utf-8"))
        assert metadata["covered_through_id"] == 42

        legacy = await dispatcher.dispatch(
            "history-legacy-summary",
            "chat.history",
            {"sessionKey": session_key},
            RpcContext(conn_id="history-legacy-summary", session_manager=manager),
        )
        assert legacy.ok is True
        assert legacy.payload["compaction_summaries"][0]["summary_text"] == summary_text
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_trims_summary_metadata_to_the_response_budget(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-summary-budget.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-summary-budget"
    wide_metadata = "😀" * 256
    try:
        session = await manager.create(session_key)
        for index in range(40):
            await storage.save_summary(
                SessionSummary(
                    session_id=session.session_id,
                    session_key=session_key,
                    compaction_id=f"compaction-{index}-{wide_metadata}",
                    trigger_reason=wide_metadata,
                    summary_text="body is intentionally not transported",
                    summary_format=wide_metadata,
                    coverage_status=wide_metadata,
                    removed_count=1,
                    kept_count=1,
                    covered_through_id=index,
                    created_at=index,
                )
            )

        response = await get_dispatcher().dispatch(
            "history-v2-summary-budget",
            "chat.history.v2",
            {"sessionKey": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(conn_id="history-v2-summary-budget", session_manager=manager),
        )

        assert response.ok is True
        metadata = response.payload["compaction_summaries"]
        assert 0 < len(metadata) < 40
        assert response.payload["compaction_summaries_has_more"] is True
        assert response.payload["compaction_summary_count"] == 40
        assert response.payload["truncated_by_bytes"] is True
        assert response.payload["history_scope"] == "compacted"
        assert _wire_bytes(response) <= 64 * 1024
        assert [item["compaction_index"] for item in metadata] == list(
            range(40 - len(metadata), 40)
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_prioritizes_a_pageable_message_over_summary_metadata(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-summary-message-priority.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-summary-message-priority"
    wide_metadata = "😀" * 256
    try:
        session = await manager.create(session_key)
        await manager.append_message(session_key, "assistant", "body-" + "x" * 100_000)
        for index in range(200):
            await storage.save_summary(
                SessionSummary(
                    session_id=session.session_id,
                    session_key=session_key,
                    compaction_id=f"compaction-{index}-{wide_metadata}",
                    trigger_reason=wide_metadata,
                    summary_text="not transported",
                    summary_format=wide_metadata,
                    coverage_status=wide_metadata,
                    removed_count=1,
                    kept_count=1,
                    covered_through_id=index,
                    created_at=index,
                )
            )

        response = await get_dispatcher().dispatch(
            "history-v2-summary-message-priority",
            "chat.history.v2",
            {"sessionKey": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="history-v2-summary-message-priority",
                session_manager=manager,
            ),
        )

        assert response.ok is True
        assert response.payload["loaded_count"] == 1
        assert len(response.payload["messages"]) == 1
        assert isinstance(response.payload["oldest_cursor"], str)
        assert isinstance(response.payload["newest_cursor"], str)
        assert response.payload["has_more"] is False
        assert response.payload["compaction_summaries_has_more"] is True
        assert _wire_bytes(response) <= 64 * 1024
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_outcome_yields_to_pageable_message_and_summary_metadata(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-outcome-priority.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-outcome-priority"
    turn_id = "turn-outcome-priority"
    wide_metadata = "😀" * 256
    try:
        session = await manager.create(session_key)
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "body-" + ("x" * 100_000),
            )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id=turn_id,
                session_key=session_key,
                status=AgentTaskStatus.CANCELLED,
                details={
                    "turn_id": turn_id,
                    "turn_outcome": {
                        "kind": "interrupted",
                        "reason": "r" * 7_000,
                    },
                },
            )
        )
        for index in range(200):
            await storage.save_summary(
                SessionSummary(
                    session_id=session.session_id,
                    session_key=session_key,
                    compaction_id=f"compaction-{index}-{wide_metadata}",
                    trigger_reason=wide_metadata,
                    summary_text="not transported",
                    summary_format=wide_metadata,
                    coverage_status=wide_metadata,
                    removed_count=1,
                    kept_count=1,
                    covered_through_id=index,
                    created_at=index,
                )
            )

        response = await get_dispatcher().dispatch(
            "history-v2-outcome-priority",
            "chat.history.v2",
            {"sessionKey": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="history-v2-outcome-priority",
                session_manager=manager,
            ),
        )

        assert response.ok is True
        assert response.payload["loaded_count"] == 1
        assert len(response.payload["messages"]) == 1
        assert isinstance(response.payload["oldest_cursor"], str)
        assert isinstance(response.payload["newest_cursor"], str)
        assert response.payload["turn_outcomes"] == []
        assert response.payload["truncated_by_bytes"] is True
        assert response.payload["compaction_summaries_has_more"] is True
        assert _wire_bytes(response) <= 64 * 1024
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_count_and_byte_cursors_cover_every_message_once(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-pages.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-pages"
    contents = [f"page-{index}:" + (chr(97 + index) * 30_000) for index in range(8)]
    try:
        await manager.create(session_key)
        for content in contents:
            await manager.append_message(session_key, "user", content)

        dispatcher = get_dispatcher()
        ctx = RpcContext(conn_id="history-v2-pages", session_manager=manager)
        count_limited = await dispatcher.dispatch(
            "history-v2-count",
            "chat.history.v2",
            {
                "sessionKey": session_key,
                "limit": 3,
                "includeSummaries": False,
                "maxResponseBytes": 4 * 1024 * 1024,
            },
            ctx,
        )
        assert count_limited.ok is True
        assert count_limited.payload["loaded_count"] == 3
        assert [row["text"] for row in count_limited.payload["messages"]] == contents[-3:]

        before = None
        pages: list[list[str]] = []
        cursors: list[str] = []
        while True:
            response = await dispatcher.dispatch(
                f"history-v2-page-{len(pages)}",
                "chat.history.v2",
                {
                    "sessionKey": session_key,
                    "limit": 4,
                    "before": before,
                    "includeSummaries": False,
                    "maxResponseBytes": 64 * 1024,
                },
                ctx,
            )
            assert response.ok is True
            assert _wire_bytes(response) <= 64 * 1024
            payload = response.payload
            rows = [row["text"] for row in payload["messages"]]
            assert 0 < len(rows) < 4
            pages.append(rows)
            if not payload["has_more"]:
                break
            next_before = payload["oldest_cursor"]
            assert isinstance(next_before, str)
            assert next_before not in cursors
            cursors.append(next_before)
            before = next_before

        assert [row for page in reversed(pages) for row in page] == contents
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_preserves_bounded_turn_outcomes_without_full_task_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-outcomes.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-outcomes"
    try:
        await manager.create(session_key)
        for turn_id in ("turn-small", "turn-large"):
            with turn_context_scope({"turn_id": turn_id}):
                await manager.append_message(session_key, "assistant", turn_id)
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="turn-small",
                session_key=session_key,
                status=AgentTaskStatus.CANCELLED,
                started_at=10,
                finished_at=20,
                details={
                    "turn_id": "turn-small",
                    "turn_outcome": {
                        "kind": "interrupted",
                        "reason": "operator_stop",
                    },
                },
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="turn-large",
                session_key=session_key,
                status=AgentTaskStatus.FAILED,
                started_at=30,
                finished_at=40,
                details={"turn_id": "turn-large", "opaque": "x" * 1_100_000},
            )
        )

        async def _must_not_load_full_tasks(*args, **kwargs):
            raise AssertionError("v2 outcomes must use bounded task metadata")

        monkeypatch.setattr(storage, "get_agent_tasks_by_ids", _must_not_load_full_tasks)
        response = await get_dispatcher().dispatch(
            "history-v2-outcomes",
            "chat.history.v2",
            {
                "sessionKey": session_key,
                "includeSummaries": False,
                "maxResponseBytes": 64 * 1024,
            },
            RpcContext(conn_id="history-v2-outcomes", session_manager=manager),
        )

        assert response.ok is True
        assert response.payload["turn_outcomes"] == [
            {
                "turn_id": "turn-small",
                "task_id": "turn-small",
                "status": "cancelled",
                "started_at": 10,
                "finished_at": 20,
                "outcome": {"kind": "interrupted", "reason": "operator_stop"},
            },
            {
                "turn_id": "turn-large",
                "task_id": "turn-large",
                "status": "failed",
                "started_at": 30,
                "finished_at": 40,
                "outcome": {"kind": "failed", "reason": "failed"},
            },
        ]
        assert _wire_bytes(response) <= 64 * 1024
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_after_pages_keep_the_earliest_fitting_prefix(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-after-pages.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-after-pages"
    contents = [f"after-{index}:" + (chr(97 + index) * 30_000) for index in range(8)]
    try:
        await manager.create(session_key)
        for content in contents:
            await manager.append_message(session_key, "user", content)
        entries = await manager.get_canonical_transcript(session_key)

        dispatcher = get_dispatcher()
        ctx = RpcContext(conn_id="history-v2-after-pages", session_manager=manager)
        after = f"{entries[0].created_at}|{entries[0].id}"
        returned: list[str] = []
        seen_cursors: set[str] = set()
        while True:
            response = await dispatcher.dispatch(
                f"history-v2-after-{len(returned)}",
                "chat.history.v2",
                {
                    "sessionKey": session_key,
                    "limit": 4,
                    "after": after,
                    "includeSummaries": False,
                    "maxResponseBytes": 64 * 1024,
                },
                ctx,
            )
            assert response.ok is True
            assert _wire_bytes(response) <= 64 * 1024
            payload = response.payload
            rows = [row["text"] for row in payload["messages"]]
            assert 0 < len(rows) < 4
            returned.extend(rows)
            if not payload["has_more"]:
                break
            next_after = payload["newest_cursor"]
            assert isinstance(next_after, str)
            assert next_after not in seen_cursors
            seen_cursors.add(next_after)
            after = next_after

        assert returned == contents[1:]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_validates_response_budget_bounds_and_stays_usable(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-budget-bounds.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-budget-bounds"
    try:
        await manager.create(session_key)
        await manager.append_message(session_key, "user", '中文🙂 "quoted" \\ path')
        dispatcher = get_dispatcher()
        ctx = RpcContext(conn_id="history-v2-budget-bounds", session_manager=manager)

        for invalid in (True, 64 * 1024 - 1, 4 * 1024 * 1024 + 1, "not-a-size"):
            response = await dispatcher.dispatch(
                f"history-v2-invalid-{invalid}",
                "chat.history.v2",
                {"sessionKey": session_key, "maxResponseBytes": invalid},
                ctx,
            )
            assert response.ok is False
            assert response.error.code == "INVALID_REQUEST"

        for cursor_field in ("before", "after"):
            for invalid_cursor in (
                "",
                "not-a-cursor",
                "1|not-an-id",
                "-1|1",
                "1|0",
                f"{1 << 63}|1",
                f"1|{1 << 63}",
            ):
                response = await dispatcher.dispatch(
                    f"history-v2-invalid-{cursor_field}-{invalid_cursor}",
                    "chat.history.v2",
                    {
                        "sessionKey": session_key,
                        cursor_field: invalid_cursor,
                    },
                    ctx,
                )
                assert response.ok is False
                assert response.error.code == "INVALID_REQUEST"

        for valid in (64 * 1024, 4 * 1024 * 1024):
            response = await dispatcher.dispatch(
                f"history-v2-valid-{valid}",
                "chat.history.v2",
                {"sessionKey": session_key, "maxResponseBytes": valid},
                ctx,
            )
            assert response.ok is True
            assert response.payload["byte_budget"] == valid
            assert response.payload["wire_bytes"] == _wire_bytes(response)
            assert _wire_bytes(response) <= valid

        follow_up = await dispatcher.dispatch(
            "history-v2-follow-up",
            "health",
            {},
            ctx,
        )
        assert follow_up.ok is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_holds_the_session_lock_only_for_the_cursor_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-lock-snapshot.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-lock-snapshot"
    mutation_lock = asyncio.Lock()

    class _LockingTurnRunner:
        def get_session_lock(self, key: str) -> asyncio.Lock:
            assert key == session_key
            return mutation_lock

    try:
        await manager.create(session_key)
        await manager.append_message(session_key, "assistant", "bounded message")
        original_cursor = manager.get_canonical_transcript_cursor_page
        original_exact = manager.get_canonical_transcript_entry_by_cursor
        original_summaries = manager.get_summary_metadata

        async def _cursor(*args, **kwargs):
            assert mutation_lock.locked()
            return await original_cursor(*args, **kwargs)

        async def _exact(*args, **kwargs):
            assert mutation_lock.locked() is False
            return await original_exact(*args, **kwargs)

        async def _summaries(*args, **kwargs):
            assert mutation_lock.locked() is False
            return await original_summaries(*args, **kwargs)

        monkeypatch.setattr(manager, "get_canonical_transcript_cursor_page", _cursor)
        monkeypatch.setattr(manager, "get_canonical_transcript_entry_by_cursor", _exact)
        monkeypatch.setattr(manager, "get_summary_metadata", _summaries)

        response = await get_dispatcher().dispatch(
            "history-v2-lock-snapshot",
            "chat.history.v2",
            {"sessionKey": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="history-v2-lock-snapshot",
                session_manager=manager,
                turn_runner=_LockingTurnRunner(),
            ),
        )

        assert response.ok is True
        assert response.payload["messages"][0]["text"] == "bounded message"
        assert mutation_lock.locked() is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_history_detail_same_key_is_single_flight_and_error_keeps_rpc_usable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-detail-single-flight.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-detail-single-flight"
    await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
    try:
        await manager.create(session_key)
        await manager.append_message(session_key, "assistant", "detail-" + "x" * 100_000)
        entry = (await manager.get_canonical_transcript(session_key))[0]
        cursor = f"{entry.created_at}|{entry.id}"
        original_getter = storage.get_canonical_transcript_entry_by_cursor
        getter_calls = 0
        build_started = asyncio.Event()
        release_build = asyncio.Event()

        async def _slow_getter(*args, **kwargs):
            nonlocal getter_calls
            getter_calls += 1
            build_started.set()
            await release_build.wait()
            return await original_getter(*args, **kwargs)

        monkeypatch.setattr(
            storage,
            "get_canonical_transcript_entry_by_cursor",
            _slow_getter,
        )
        dispatcher = get_dispatcher()
        ctx = RpcContext(conn_id="history-v2-detail-single-flight", session_manager=manager)
        calls = [
            asyncio.create_task(
                dispatcher.dispatch(
                    f"detail-single-flight-{index}",
                    "chat.history.entry.v1",
                    {
                        "sessionKey": session_key,
                        "cursor": cursor,
                        "chunkBytes": 8 * 1024,
                    },
                    ctx,
                )
            )
            for index in range(2)
        ]
        await build_started.wait()
        release_build.set()
        first, second = await asyncio.gather(*calls)

        assert first.ok is True
        assert second.ok is True
        assert first.payload["chunk_base64"] == second.payload["chunk_base64"]
        assert getter_calls == 1

        invalid = await dispatcher.dispatch(
            "detail-invalid-offset",
            "chat.history.entry.v1",
            {
                "sessionKey": session_key,
                "cursor": cursor,
                "offset": first.payload["total"] + 1,
            },
            ctx,
        )
        assert invalid.ok is False
        assert invalid.error.code == "INVALID_REQUEST"

        health = await dispatcher.dispatch("detail-health", "health", {}, ctx)
        assert health.ok is True
    finally:
        await rpc_chat_module._HISTORY_ENTRY_SPOOL.clear()  # noqa: SLF001
        await storage.close()


@pytest.mark.asyncio
async def test_history_v2_marks_legacy_archive_rows_without_stable_ids_incomplete(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-v2-incomplete-archive.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:history-v2-incomplete-archive"
    try:
        node = await manager.create(session_key)
        await manager.append_message(session_key, "assistant", "active")
        await storage.conn.execute(
            """
            INSERT INTO compacted_transcript_entries (
                session_id,
                session_key,
                original_entry_id,
                message_id,
                role,
                content,
                created_at,
                archived_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (node.session_id, session_key, "legacy-null-id", "user", "legacy", 1, 2),
        )
        await storage.conn.commit()

        response = await get_dispatcher().dispatch(
            "history-v2-incomplete-archive",
            "chat.history.v2",
            {
                "sessionKey": session_key,
                "includeSummaries": False,
                "maxResponseBytes": 64 * 1024,
            },
            RpcContext(conn_id="history-v2-incomplete-archive", session_manager=manager),
        )

        assert response.ok is True
        assert response.payload["canonical_complete"] is False
        assert response.payload["canonical_incomplete_reason"] == (
            "canonical_archive_coverage_unverified"
        )
        assert [message["text"] for message in response.payload["messages"]] == ["active"]
    finally:
        await storage.close()
