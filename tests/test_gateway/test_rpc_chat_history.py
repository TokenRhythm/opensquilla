import asyncio
import json
from types import SimpleNamespace

import pytest

import opensquilla.gateway.rpc_chat as rpc_chat_module
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_chat import _handle_chat_history
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage, StorageBusyError
from opensquilla.session.turn_context import turn_context_scope
from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventStart


class _FakeSessionManager:
    def __init__(
        self,
        entries,
        *,
        canonical_entries=None,
        summaries=None,
        canonical_exception=None,
        transcript_exception=None,
    ):
        self._entries = entries
        self._canonical_entries = canonical_entries
        self._summaries = summaries or []
        self._canonical_exception = canonical_exception
        self._transcript_exception = transcript_exception
        self.used_canonical = False

    async def get_transcript(self, session_key):
        if self._transcript_exception is not None:
            raise self._transcript_exception
        return self._entries

    async def get_canonical_transcript(self, session_key):
        self.used_canonical = True
        if self._canonical_exception is not None:
            raise self._canonical_exception
        if self._canonical_entries is None:
            raise RuntimeError("canonical unavailable")
        return self._canonical_entries

    async def get_summaries(self, session_key):
        return self._summaries


class _FakePagedSessionManager(_FakeSessionManager):
    def __init__(self, entries, *, page=None, page_exception=None):
        super().__init__(entries, canonical_entries=[_entry(99)])
        self._page = page
        self._page_exception = page_exception
        self.page_calls = []

    async def get_canonical_transcript_page(self, session_key, **kwargs):
        self.page_calls.append((session_key, kwargs))
        if self._page_exception is not None:
            raise self._page_exception
        return self._page


def _entry(idx: int, role: str = "user") -> TranscriptEntry:
    return TranscriptEntry(
        id=idx,
        session_id="parent",
        session_key="agent:main:webchat:test",
        role=role,
        content=f"message {idx}",
        created_at=idx,
        message_id=f"msg-{idx}",
    )


async def _record_finalized_usage(
    storage: SessionStorage,
    *,
    session_id: str,
    session_epoch: int = 0,
    turn_id: str,
    event_id: str,
    call_index: int = 0,
    input_tokens: int = 7,
    output_tokens: int = 3,
    cost_nanos: int = 30_000_000,
) -> None:
    await storage.start_usage_event(
        UsageEventStart(
            event_id=event_id,
            execution_id=turn_id,
            call_index=call_index,
            session_id=session_id,
            session_epoch=session_epoch,
            started_at_ms=10 + call_index,
            turn_id=turn_id,
            provider="ledger-provider",
            model="ledger-model",
        )
    )
    await storage.finalize_usage_event(
        event_id,
        UsageEventCompletion(
            completed_at_ms=20 + call_index,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_nanos=cost_nanos,
            billed_cost_nanos=cost_nanos,
            cost_source="provider_billed",
            provider="ledger-provider",
            model="ledger-model",
            coverage_status="complete",
        ),
    )


@pytest.mark.asyncio
async def test_chat_history_returns_pagination_metadata_with_legacy_messages() -> None:
    entries = [_entry(idx) for idx in range(1, 4)]

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 2},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager(entries, canonical_entries=entries),
        ),
    )

    assert [msg["text"] for msg in result["messages"]] == ["message 2", "message 3"]
    assert result["has_more"] is True
    assert result["oldest_cursor"] == "2|2"
    assert result["newest_cursor"] == "3|3"
    assert result["history_scope"] == "latest_window"
    assert result["loaded_count"] == 2
    assert result["page_size"] == 2
    assert result["canonical_available"] is True
    assert result["canonical_complete"] is True


@pytest.mark.asyncio
async def test_chat_history_projects_parallel_legacy_activity_on_incomplete_page() -> None:
    tool_entry = TranscriptEntry(
        id=10,
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="assistant",
        content=(
            "Inspect the source.\n"
            "[Used tool: read_file]\n"
            "Compare the directory.\n"
            "[Used tool: list_dir]"
        ),
        created_at=10,
        message_id="legacy-tools",
    )
    result_entry = TranscriptEntry(
        id=11,
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="user",
        content=(
            "[Tool result (call-read): source payload]\n"
            "[Tool result (call-list): directory payload]"
        ),
        created_at=11,
        message_id="legacy-results",
    )
    manager = _FakePagedSessionManager(
        [tool_entry, result_entry],
        page={
            "entries": [tool_entry, result_entry],
            "has_more": False,
            "canonical_complete": False,
        },
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 2},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=manager,
        ),
    )

    assert result["loaded_count"] == 2
    assert result["canonical_complete"] is False
    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_id"] == "legacy-tools"
    assert [segment.get("tool_use_id") for segment in result["messages"][0]["tool_calls"]] == [
        None,
        "call-read",
        None,
        "call-list",
        "call-read",
        "call-list",
    ]
    assert "[Used tool:" not in str(result["messages"])
    assert "[Tool result" not in str(result["messages"])


@pytest.mark.asyncio
async def test_chat_history_projects_legacy_tool_pair_split_by_page_boundary(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-legacy-boundary.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:legacy-boundary"
    await manager.create(session_key)
    try:
        await manager.append_message(session_key, "user", "earlier request")
        await manager.append_message(
            session_key,
            "assistant",
            "[Used tool: read_file]",
        )
        await manager.append_message(
            session_key,
            "user",
            "[Tool result (call-boundary): private payload]",
        )
        await manager.append_message(session_key, "user", "continue")

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 2},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        earlier = await _handle_chat_history(
            {
                "sessionKey": session_key,
                "limit": 2,
                "before": result["oldest_cursor"],
            },
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        forward = await _handle_chat_history(
            {
                "sessionKey": session_key,
                "limit": 2,
                "after": earlier["newest_cursor"],
            },
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        refreshed = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 2},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
    finally:
        await storage.close()

    assert result["loaded_count"] == 2
    assert result["has_more"] is True
    assert [message["role"] for message in result["messages"]] == [
        "assistant",
        "user",
    ]
    assert [message["text"] for message in result["messages"]] == ["", "continue"]
    assert result["messages"][0]["tool_calls"] == [
        {
            "type": "tool_use",
            "tool_use_id": "call-boundary",
            "name": "read_file",
            "input": {},
            "legacy_projection": True,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-boundary",
            "name": "read_file",
            "result": "private payload",
            "legacy_projection": True,
        },
    ]
    assert "[Tool result" not in str(result["messages"])
    assert [message["text"] for message in earlier["messages"]] == ["earlier request"]
    assert earlier["loaded_count"] == 2
    assert forward["messages"] == result["messages"]
    assert forward["loaded_count"] == 2
    assert refreshed["messages"] == result["messages"]
    assert refreshed["oldest_cursor"] == result["oldest_cursor"]
    assert refreshed["newest_cursor"] == result["newest_cursor"]


@pytest.mark.asyncio
async def test_chat_history_preserves_ambiguous_result_suffix_same_page_and_cross_page(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-legacy-ambiguous.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:legacy-ambiguous"
    await manager.create(session_key)
    ambiguous = "[Tool result (call-boundary): [\"a\"]\nsecond payload line]"
    try:
        await manager.append_message(session_key, "assistant", "[Used tool: read_file]")
        await manager.append_message(session_key, "user", ambiguous)
        await manager.append_message(session_key, "user", "continue")

        same_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 3},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        cross_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 2},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
    finally:
        await storage.close()

    same_activity = same_page["messages"][0]
    cross_activity = cross_page["messages"][0]
    assert same_activity["message_id"] == cross_activity["message_id"]
    assert same_activity["tool_calls"] == cross_activity["tool_calls"]
    assert cross_activity["tool_calls"][1]["result"] == '["a"]\nsecond payload line'
    assert cross_page["loaded_count"] == 2
    assert cross_page["messages"][1]["text"] == "continue"


@pytest.mark.asyncio
async def test_chat_history_fails_safe_for_result_line_with_untrusted_trailing_text(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-legacy-trailing-text.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:legacy-trailing-text"
    await manager.create(session_key)
    ambiguous = "[Tool result (call-boundary): ok]\nPlease also update README.md"
    try:
        await manager.append_message(session_key, "assistant", "[Used tool: read_file]")
        await manager.append_message(session_key, "user", ambiguous)
        await manager.append_message(session_key, "user", "continue")
        same_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 3},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        cross_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 2},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
    finally:
        await storage.close()

    assert [message["text"] for message in same_page["messages"]] == [
        "[Used tool: read_file]",
        ambiguous,
        "continue",
    ]
    assert [message["text"] for message in cross_page["messages"]] == [
        ambiguous,
        "continue",
    ]
    assert all("tool_calls" not in message for message in same_page["messages"])
    assert all("tool_calls" not in message for message in cross_page["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auxiliary_failure",
    [OSError("transient lookbehind failure"), TypeError("legacy manager signature")],
)
async def test_chat_history_auxiliary_lookbehind_failure_preserves_main_page(
    auxiliary_failure: Exception,
) -> None:
    result_entry = _entry(11)
    result_entry.content = "[Tool result (call-1): payload]"

    class _AuxiliaryFailureManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__([result_entry], canonical_entries=[result_entry])
            self.page_calls = 0

        async def get_canonical_transcript_page(self, session_key, **kwargs):
            self.page_calls += 1
            if self.page_calls == 1:
                return {
                    "entries": [result_entry],
                    "has_more": True,
                    "canonical_complete": False,
                }
            raise auxiliary_failure

    manager = _AuxiliaryFailureManager()
    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 1},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=manager,
        ),
    )

    assert [message["text"] for message in result["messages"]] == [result_entry.content]
    assert result["loaded_count"] == 1
    assert result["oldest_cursor"] == "11|11"
    assert result["newest_cursor"] == "11|11"
    assert result["has_more"] is True
    assert result["canonical_complete"] is False


@pytest.mark.asyncio
async def test_chat_history_malformed_auxiliary_lookahead_preserves_main_page() -> None:
    tool_entry = _entry(12, role="assistant")
    tool_entry.content = "[Used tool: read_file]"

    class _MalformedLookaheadManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__([tool_entry], canonical_entries=[tool_entry])
            self.page_calls = 0

        async def get_canonical_transcript_page(self, session_key, **kwargs):
            self.page_calls += 1
            if self.page_calls == 1:
                return {
                    "entries": [tool_entry],
                    "has_more": False,
                    "canonical_complete": True,
                }
            return {"has_more": False, "canonical_complete": True}

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 1},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_MalformedLookaheadManager(),
        ),
    )

    assert [message["text"] for message in result["messages"]] == [tool_entry.content]
    assert result["loaded_count"] == 1
    assert result["oldest_cursor"] == "12|12"
    assert result["newest_cursor"] == "12|12"


@pytest.mark.asyncio
async def test_chat_history_auxiliary_storage_busy_remains_retryable() -> None:
    result_entry = _entry(13)
    result_entry.content = "[Tool result (call-1): payload]"
    busy = StorageBusyError(
        "get_canonical_transcript_page",
        waited_ms=50,
        retry_after_ms=100,
    )

    class _BusyLookbehindManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__([result_entry], canonical_entries=[result_entry])
            self.page_calls = 0

        async def get_canonical_transcript_page(self, session_key, **kwargs):
            self.page_calls += 1
            if self.page_calls == 1:
                return {
                    "entries": [result_entry],
                    "has_more": True,
                    "canonical_complete": True,
                }
            raise busy

    with pytest.raises(StorageBusyError) as caught:
        await _handle_chat_history(
            {"sessionKey": "agent:main:webchat:test", "limit": 1},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=_BusyLookbehindManager(),
            ),
        )

    assert caught.value is busy


@pytest.mark.asyncio
async def test_chat_history_batch_projects_missing_turn_usage_from_ledger(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-projection.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-projection"
    session = await manager.create(session_key)
    try:
        with turn_context_scope({"turn_id": "legacy-cancelled-turn"}):
            await manager.append_message(session_key, "assistant", "partial answer")
        await storage.start_usage_event(
            UsageEventStart(
                event_id="usage-1",
                execution_id="legacy-cancelled-turn",
                call_index=0,
                session_id=session.session_id,
                started_at_ms=10,
                turn_id="legacy-cancelled-turn",
                provider="test-provider",
                model="test-model",
            )
        )
        await storage.finalize_usage_event(
            "usage-1",
            UsageEventCompletion(
                completed_at_ms=20,
                input_tokens=7,
                output_tokens=3,
                total_tokens=10,
                cost_source="none",
                provider="test-provider",
                model="test-model",
                coverage_status="complete",
            ),
        )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert len(result["messages"]) == 1
        message = result["messages"][0]
        assert message["usage"]["input_tokens"] == 7
        assert message["usage"]["output_tokens"] == 3
        assert message["usage"]["coverage_status"] == "complete"
        durable = await manager.get_transcript(session_key)
        assert durable[0].turn_usage is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_projects_ledger_totals_over_partial_duplicate_usage(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-partial.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-partial"
    session = await manager.create(session_key)
    turn_id = "partial-duplicate-turn"
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "partial answer",
                turn_usage={
                    "provider": "routed-provider",
                    "model": "routed-model",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cost_usd": 0.001,
                    "ensemble_trace": {"final_request_role": "aggregator"},
                    "model_usage_breakdown": [{"role": "proposer"}],
                    "routing_source": "squilla_router",
                },
            )
            await manager.append_message(
                session_key,
                "assistant",
                "final answer",
            )

        for event_id, call_index, input_tokens, output_tokens, cost_nanos in (
            ("usage-partial-1", 0, 7, 3, 10_000_000),
            ("usage-partial-2", 1, 5, 2, 20_000_000),
        ):
            await storage.start_usage_event(
                UsageEventStart(
                    event_id=event_id,
                    execution_id=turn_id,
                    call_index=call_index,
                    session_id=session.session_id,
                    session_epoch=session.epoch,
                    started_at_ms=10 + call_index,
                    turn_id=turn_id,
                    provider="test-provider",
                    model="test-model",
                )
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(
                    completed_at_ms=20 + call_index,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cost_nanos=cost_nanos,
                    billed_cost_nanos=cost_nanos,
                    cost_source="provider_billed",
                    provider="test-provider",
                    model="test-model",
                    coverage_status="complete",
                ),
            )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert len(result["messages"]) == 2
        assert "usage" not in result["messages"][0]
        usage = result["messages"][1]["usage"]
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 17
        assert usage["cost_usd"] == pytest.approx(0.03)
        assert usage["billed_cost"] == pytest.approx(0.03)
        assert usage["coverage_status"] == "complete"
        assert usage["missing_cost_entries"] == 0
        assert usage["provider"] == "routed-provider"
        assert usage["model"] == "routed-model"
        assert usage["ensemble_trace"] == {"final_request_role": "aggregator"}
        assert usage["model_usage_breakdown"] == [{"role": "proposer"}]
        assert usage["routing_source"] == "squilla_router"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_keeps_richer_ensemble_receipt_on_terminal_duplicate(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-richer-receipt.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-richer-receipt"
    session = await manager.create(session_key)
    turn_id = "richer-receipt-turn"
    richer_breakdown = [
        {
            "role": "proposer",
            "provider": "proposal-provider",
            "model": "proposal-model",
            "input_tokens": 4,
            "output_tokens": 2,
        },
        {
            "role": "aggregator",
            "provider": "aggregator-provider",
            "model": "aggregator-model",
            "input_tokens": 6,
            "output_tokens": 3,
        },
    ]
    richer_trace = {
        "profile": "custom_b5",
        "physical_request_count": 2,
        "final_request_role": "aggregator",
        "proposer_roles": ["proposer"],
    }
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "initial ensemble answer",
                turn_usage={
                    "provider": "aggregator-provider",
                    "model": "aggregator-model",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cost_usd": 0.001,
                    "model_usage_breakdown": richer_breakdown,
                    "ensemble_trace": richer_trace,
                    "routing_source": "squilla_router",
                },
            )
            await manager.append_message(
                session_key,
                "assistant",
                "continued ensemble answer",
                turn_usage={
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cost_usd": 0.002,
                    "model_usage_breakdown": [
                        {
                            "role": "aggregator",
                            "provider": "aggregator-provider",
                            "model": "aggregator-model",
                            "input_tokens": 2,
                            "output_tokens": 1,
                        }
                    ],
                    "ensemble_trace": {
                        "final_request_role": "aggregator",
                        "physical_request_count": 1,
                    },
                },
            )

        for event_id, call_index, input_tokens, output_tokens, cost_nanos in (
            ("usage-richer-1", 0, 7, 3, 10_000_000),
            ("usage-richer-2", 1, 5, 2, 20_000_000),
            ("usage-richer-3", 2, 4, 1, 30_000_000),
        ):
            await storage.start_usage_event(
                UsageEventStart(
                    event_id=event_id,
                    execution_id=turn_id,
                    call_index=call_index,
                    session_id=session.session_id,
                    session_epoch=session.epoch,
                    started_at_ms=10 + call_index,
                    turn_id=turn_id,
                    provider="ledger-provider",
                    model="ledger-model",
                )
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(
                    completed_at_ms=20 + call_index,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cost_nanos=cost_nanos,
                    billed_cost_nanos=cost_nanos,
                    cost_source="provider_billed",
                    provider="ledger-provider",
                    model="ledger-model",
                    coverage_status="complete",
                ),
            )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert len(result["messages"]) == 2
        assert sum("usage" in message for message in result["messages"]) == 1
        assert "usage" not in result["messages"][0]
        usage = result["messages"][1]["usage"]
        assert usage["input_tokens"] == 16
        assert usage["output_tokens"] == 6
        assert usage["total_tokens"] == 22
        assert usage["cost_usd"] == pytest.approx(0.06)
        assert usage["billed_cost"] == pytest.approx(0.06)
        assert usage["coverage_status"] == "complete"
        assert usage["missing_cost_entries"] == 0
        assert usage["model_usage_breakdown"] == richer_breakdown
        assert usage["ensemble_trace"] == richer_trace
        assert usage["routing_source"] == "squilla_router"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_usage_projection_is_terminal_across_limit_one_pages(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-terminal-page.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-terminal-page"
    session = await manager.create(session_key)
    turn_id = "terminal-page-turn"
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "tool call carrier",
                turn_usage={
                    "cost_usd": 0.001,
                    "model_usage_breakdown": [{"role": "proposer"}],
                },
            )
            await manager.append_message(session_key, "assistant", "final answer")
        transcript = await manager.get_transcript(session_key)
        terminal_cursor = f"{transcript[-1].created_at}|{transcript[-1].id}"
        await _record_finalized_usage(
            storage,
            session_id=session.session_id,
            session_epoch=session.epoch,
            turn_id=turn_id,
            event_id="usage-terminal-page",
        )

        latest = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        older = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1, "before": terminal_cursor},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert [message["text"] for message in latest["messages"]] == ["final answer"]
        assert latest["messages"][0]["usage"]["cost_usd"] == pytest.approx(0.03)
        assert [message["text"] for message in older["messages"]] == ["tool call carrier"]
        assert "usage" not in older["messages"][0]
        combined = [*older["messages"], *latest["messages"]]
        assert sum("usage" in message for message in combined) == 1
        assert sum(
            message.get("usage", {}).get("cost_usd", 0) for message in combined
        ) == pytest.approx(0.03)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_usage_projection_is_independent_of_page_load_order(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-load-order.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-load-order"
    session = await manager.create(session_key)
    turn_id = "load-order-turn"
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "first assistant",
                turn_usage={"cost_usd": 0.001},
            )
            await manager.append_message(session_key, "assistant", "second assistant")
        transcript = await manager.get_transcript(session_key)
        terminal_cursor = f"{transcript[-1].created_at}|{transcript[-1].id}"
        await _record_finalized_usage(
            storage,
            session_id=session.session_id,
            session_epoch=session.epoch,
            turn_id=turn_id,
            event_id="usage-load-order",
        )

        older_first = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1, "before": terminal_cursor},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        latest_second = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        latest_first = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        older_second = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1, "before": terminal_cursor},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert "usage" not in older_first["messages"][0]
        assert "usage" not in older_second["messages"][0]
        assert latest_first["messages"][0]["usage"]["cost_usd"] == pytest.approx(0.03)
        assert latest_second["messages"][0]["usage"]["cost_usd"] == pytest.approx(0.03)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_usage_projection_terminal_crosses_compacted_boundary(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-compacted-page.db"))
    await storage.connect()
    await storage.initialize_usage_ledger(1)
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-compacted-page"
    session = await manager.create(session_key)
    turn_id = "compacted-page-turn"
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(
                session_key,
                "assistant",
                "archived carrier",
                turn_usage={
                    "cost_usd": 0.001,
                    "model_usage_breakdown": [{"role": "proposer"}],
                },
            )
            await manager.append_message(session_key, "assistant", "active terminal")
        transcript = await manager.get_transcript(session_key)
        await storage.rewrite_compacted_session(
            node=session,
            summary=None,
            archived_entries=[transcript[0]],
            entries=[transcript[1]],
        )
        active = await manager.get_transcript(session_key)
        terminal_cursor = f"{active[-1].created_at}|{active[-1].id}"
        await _record_finalized_usage(
            storage,
            session_id=session.session_id,
            session_epoch=session.epoch,
            turn_id=turn_id,
            event_id="usage-compacted-page",
        )

        archived_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1, "before": terminal_cursor},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )
        active_page = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 1},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert [message["text"] for message in archived_page["messages"]] == ["archived carrier"]
        assert "usage" not in archived_page["messages"][0]
        assert [message["text"] for message in active_page["messages"]] == ["active terminal"]
        assert active_page["messages"][0]["usage"]["cost_usd"] == pytest.approx(0.03)
        # Structural detail stranded on an earlier page stays there. Placement
        # only guarantees the total is billed once; migrating a richer receipt
        # across pages would cost a full-history scan on every read.
        assert "model_usage_breakdown" not in active_page["messages"][0]["usage"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_turn_continuation_probe_only_reports_rows_after_the_cursor(
    tmp_path,
) -> None:
    """The probe answers 'does this turn continue past the page?' and no more.

    Placement depends on the answer being false for the newest page, which is
    what keeps that read from touching transcript rows at all.
    """
    storage = SessionStorage(str(tmp_path / "history-continuation-probe.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:continuation-probe"
    session = await manager.create(session_key)
    turn_id = "probe-turn"
    try:
        with turn_context_scope({"turn_id": turn_id}):
            await manager.append_message(session_key, "assistant", "carrier")
            await manager.append_message(session_key, "assistant", "terminal")
        transcript = await manager.get_transcript(session_key)
        carrier, terminal = transcript[0], transcript[1]

        # Cursor at the carrier: the terminal row still lies ahead.
        assert await storage.get_turn_ids_continuing_after_cursor(
            session_id=session.session_id,
            created_at=carrier.created_at,
            entry_id=carrier.id,
            turn_ids=[turn_id],
        ) == {turn_id}

        # Cursor at the terminal row: nothing follows, so the page owns it.
        assert await storage.get_turn_ids_continuing_after_cursor(
            session_id=session.session_id,
            created_at=terminal.created_at,
            entry_id=terminal.id,
            turn_ids=[turn_id],
        ) == set()

        # Turns that were never asked about are never reported.
        assert await storage.get_turn_ids_continuing_after_cursor(
            session_id=session.session_id,
            created_at=carrier.created_at,
            entry_id=carrier.id,
            turn_ids=["unrelated-turn"],
        ) == set()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_usage_projection_failure_does_not_hide_history(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-failure.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-projection-failure"
    await manager.create(session_key)
    try:
        with turn_context_scope({"turn_id": "legacy-turn"}):
            await manager.append_message(session_key, "assistant", "still visible")

        async def fail_projection(**_kwargs):
            raise RuntimeError("projection unavailable")

        monkeypatch.setattr(storage, "get_turn_usage_projections", fail_projection)
        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert [message["text"] for message in result["messages"]] == ["still visible"]
        assert "usage" not in result["messages"][0]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_returns_typed_outcomes_for_explicit_page_turns(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-turn-outcomes.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:typed-outcome"
    await manager.create(session_key)
    try:
        # The exact page lookup must not depend on list_agent_tasks' oldest-100
        # default window.
        for index in range(101):
            await storage.create_agent_task(
                AgentTaskRecord(
                    task_id=f"older-turn-{index}",
                    session_key=session_key,
                    agent_id="main",
                    source_kind="webui",
                    queue_mode="followup",
                    run_kind="session_turn",
                    status=AgentTaskStatus.SUCCEEDED,
                )
            )
        with turn_context_scope({"turn_id": "turn-stopped"}):
            await manager.append_message(session_key, "user", "stop this")
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="turn-stopped",
                session_key=session_key,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="session_turn",
                status=AgentTaskStatus.CANCELLED,
                started_at=110,
                finished_at=120,
                details={
                    "turn_id": "turn-stopped",
                    "accepted_model_routing": {
                        "effective_mode": "ensemble",
                    },
                    "turn_outcome": {
                        "kind": "interrupted",
                        "reason": "cancelled",
                        "cancellation_source": "webui_stop",
                        "retryable": True,
                    },
                },
            )
        )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert result["turn_outcomes"] == [
            {
                "turn_id": "turn-stopped",
                "task_id": "turn-stopped",
                "status": "cancelled",
                "started_at": 110,
                "finished_at": 120,
                "outcome": {
                    "kind": "interrupted",
                    "reason": "cancelled",
                    "cancellation_source": "webui_stop",
                    "retryable": True,
                },
                "accepted_routing_mode": "ensemble",
            }
        ]
        assert result["messages"][0]["turn_context"]["turn_id"] == "turn-stopped"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_projects_usage_barrier_retry_and_activity_snapshot(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-barrier.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-barrier"
    await manager.create(session_key)
    try:
        with turn_context_scope({"turn_id": "turn-usage"}):
            primary_user = await manager.append_message(
                session_key, "user", "retry this"
            )
            steer_user = await manager.append_message(
                session_key, "user", "same-turn steer"
            )
            await manager.append_message(
                session_key,
                "system",
                "Error: usage ledger temporarily unavailable; provider request was not sent",
            )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="turn-usage",
                session_key=session_key,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="session_turn",
                status=AgentTaskStatus.FAILED,
                terminal_reason="error",
                error_class="usage_accounting_busy",
                error_message="usage ledger temporarily unavailable; provider request was not sent",
                details={
                    "turn_id": "turn-usage",
                    "turn_outcome": {
                        "kind": "blocked",
                        "reason": "usage_accounting_busy",
                        "error_class": "usage_accounting_busy",
                        "retryable": True,
                        # The persisted primary id is authoritative over a
                        # stale nested projection that points at a Steer.
                        "user_message_id": steer_user.message_id,
                    },
                    "retry_after_ms": 100,
                    "persisted_user_message_id": primary_user.message_id,
                    "persisted_user_message_ids": [
                        primary_user.message_id,
                        steer_user.message_id,
                    ],
                    "usage_call_index": 1,
                    "no_prior_provider_dispatch": True,
                    "replay_safe": True,
                    "activity_snapshot": {
                        "version": 1,
                        "task_id": "turn-usage",
                        "turn_id": "turn-usage",
                        "phases": [
                            {"kind": "router", "phase": "decided", "at": 1_000},
                            {"kind": "state", "phase": "thinking", "at": 1_100},
                        ],
                    },
                },
            )
        )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        outcome = result["turn_outcomes"][0]
        assert outcome["code"] == outcome["error_class"] == "usage_accounting_busy"
        assert outcome["retryable"] is True
        assert outcome["retry_after_ms"] == 100
        assert outcome["usage_call_index"] == 1
        assert outcome["no_prior_provider_dispatch"] is True
        assert outcome["replay_safe"] is True
        assert outcome["user_message_id"] == primary_user.message_id
        assert outcome["outcome"]["user_message_id"] == primary_user.message_id
        assert outcome["activity_snapshot"]["phases"] == [
            {"kind": "router", "phase": "decided", "at": 1_000},
            {"kind": "state", "phase": "thinking", "at": 1_100},
        ]
        assert "safe to retry" in outcome["terminal_message"].lower()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_projects_later_usage_barrier_as_not_replay_safe(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "history-usage-barrier-later-call.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:usage-barrier-later-call"
    await manager.create(session_key)
    try:
        with turn_context_scope({"turn_id": "turn-usage"}):
            await manager.append_message(session_key, "user", "continue after tools")
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="turn-usage",
                session_key=session_key,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="session_turn",
                status=AgentTaskStatus.FAILED,
                terminal_reason="error",
                error_class="usage_accounting_busy",
                error_message=(
                    "usage ledger temporarily unavailable; provider request was not sent"
                ),
                details={
                    "turn_id": "turn-usage",
                    "turn_outcome": {
                        "kind": "blocked",
                        "reason": "usage_accounting_busy",
                        "error_class": "usage_accounting_busy",
                        "retryable": True,
                        "usage_call_index": 2,
                        "no_prior_provider_dispatch": False,
                        "replay_safe": False,
                    },
                    "usage_call_index": 2,
                    "no_prior_provider_dispatch": False,
                    "replay_safe": False,
                },
            )
        )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        outcome = result["turn_outcomes"][0]
        assert outcome["retryable"] is True
        assert outcome["usage_call_index"] == 2
        assert outcome["no_prior_provider_dispatch"] is False
        assert outcome["replay_safe"] is False
        assert "earlier work" in outcome["terminal_message"].lower()
        assert "safe to retry" not in outcome["terminal_message"].lower()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_derives_legacy_outcomes_only_from_explicit_task_status(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-legacy-turn-outcomes.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:legacy-outcomes"
    await manager.create(session_key)
    cases = [
        ("turn-succeeded", AgentTaskStatus.SUCCEEDED, "completed"),
        ("turn-cancelled", AgentTaskStatus.CANCELLED, "interrupted"),
        ("turn-timeout", AgentTaskStatus.TIMEOUT, "interrupted"),
        ("turn-failed", AgentTaskStatus.FAILED, "failed"),
        ("turn-abandoned", AgentTaskStatus.ABANDONED, "interrupted"),
    ]
    try:
        for index, (turn_id, status, _kind) in enumerate(cases, start=1):
            with turn_context_scope({"turn_id": turn_id}):
                await manager.append_message(session_key, "user", f"prompt {index}")
            await storage.create_agent_task(
                AgentTaskRecord(
                    task_id=turn_id,
                    session_key=session_key,
                    agent_id="main",
                    source_kind="webui",
                    queue_mode="followup",
                    run_kind="session_turn",
                    status=status,
                    started_at=index * 10,
                    finished_at=index * 10 + 5,
                    # No details.turn_outcome: this is an upgraded legacy row.
                    details={},
                )
            )

        result = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        )

        assert [
            (
                item["turn_id"],
                item["status"],
                item["outcome"],
            )
            for item in result["turn_outcomes"]
        ] == [
            (
                turn_id,
                status.value,
                {"kind": kind, "reason": status.value},
            )
            for turn_id, status, kind in cases
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chat_history_before_cursor_returns_older_page() -> None:
    entries = [_entry(idx) for idx in range(1, 6)]

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 2, "before": "4|4"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager(entries, canonical_entries=entries),
        ),
    )

    assert [msg["text"] for msg in result["messages"]] == ["message 2", "message 3"]
    assert result["has_more"] is True
    assert result["oldest_cursor"] == "2|2"
    assert result["newest_cursor"] == "3|3"


@pytest.mark.asyncio
async def test_chat_history_uses_canonical_transcript_when_available() -> None:
    active_entries = [_entry(3)]
    canonical_entries = [_entry(1), _entry(2), _entry(3)]
    mgr = _FakeSessionManager(active_entries, canonical_entries=canonical_entries)

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 10},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert mgr.used_canonical is True
    assert [msg["text"] for msg in result["messages"]] == [
        "message 1",
        "message 2",
        "message 3",
    ]
    assert result["canonical_available"] is True
    assert result["canonical_complete"] is True


@pytest.mark.asyncio
async def test_chat_history_prefers_bounded_canonical_page_when_available() -> None:
    mgr = _FakePagedSessionManager(
        [_entry(4)],
        page=SimpleNamespace(
            entries=[_entry(2), _entry(3)],
            has_more=True,
            canonical_complete=False,
        ),
    )

    result = await _handle_chat_history(
        {
            "sessionKey": "agent:main:webchat:test",
            "limit": 2,
            "before": "4|4",
            "includeSummaries": False,
        },
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert [msg["text"] for msg in result["messages"]] == ["message 2", "message 3"]
    assert result["has_more"] is True
    assert result["canonical_available"] is True
    assert result["canonical_complete"] is False
    assert result["compaction_summaries"] == []
    assert mgr.page_calls == [
        (
            "agent:main:webchat:test",
            {"limit": 2, "before": (4, 4), "after": None},
        )
    ]
    assert mgr.used_canonical is False


@pytest.mark.asyncio
async def test_chat_history_waits_for_same_connection_compaction_rewrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-compaction-race.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:compaction-race"
    await manager.create(session_key)
    persisted = [
        await manager.append_message(session_key, "user", f"message {index}")
        for index in range(4)
    ]

    mutation_lock = asyncio.Lock()
    archive_written = asyncio.Event()
    allow_rewrite = asyncio.Event()
    history_requested_lock = asyncio.Event()
    original_archive = storage._archive_transcript_entries

    async def _pause_after_archive(**kwargs):
        await original_archive(**kwargs)
        archive_written.set()
        await allow_rewrite.wait()

    monkeypatch.setattr(storage, "_archive_transcript_entries", _pause_after_archive)

    class _LockingTurnRunner:
        def get_session_lock(self, key: str) -> asyncio.Lock:
            assert key == session_key
            history_requested_lock.set()
            return mutation_lock

    async def _compact() -> None:
        async with mutation_lock:
            await manager.persist_compaction_result(
                session_key,
                "summary",
                [{"role": "user", "content": "message 3"}],
                compaction_id="cmp-history-race",
            )

    compaction_task = asyncio.create_task(_compact())
    history_task = None
    try:
        await asyncio.wait_for(archive_written.wait(), timeout=2)
        history_task = asyncio.create_task(
            _handle_chat_history(
                {
                    "sessionKey": session_key,
                    "limit": 10,
                    "includeSummaries": False,
                },
                RpcContext(
                    conn_id="test",
                    principal=SimpleNamespace(role="operator"),
                    session_manager=manager,
                    turn_runner=_LockingTurnRunner(),
                ),
            )
        )
        await asyncio.wait_for(history_requested_lock.wait(), timeout=2)
        assert not history_task.done()

        allow_rewrite.set()
        await asyncio.wait_for(compaction_task, timeout=2)
        result = await asyncio.wait_for(history_task, timeout=2)
    finally:
        allow_rewrite.set()
        pending = [
            task
            for task in (compaction_task, history_task)
            if task is not None and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await storage.close()

    assert [message["message_id"] for message in result["messages"]] == [
        entry.message_id for entry in persisted
    ]
    assert len({message["message_id"] for message in result["messages"]}) == 4
    assert result["canonical_complete"] is True


@pytest.mark.asyncio
async def test_chat_history_session_lock_wait_is_bounded_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rpc_chat_module, "_CHAT_HISTORY_LOCK_BUDGET_SECONDS", 0.05)
    session_key = "agent:main:webchat:bounded-history-lock"
    mutation_lock = asyncio.Lock()
    await mutation_lock.acquire()
    manager = _FakeSessionManager([_entry(1)], canonical_entries=[_entry(1)])

    class _LockingTurnRunner:
        def get_session_lock(self, key: str) -> asyncio.Lock:
            assert key == session_key
            return mutation_lock

    context = RpcContext(
        conn_id="test",
        principal=SimpleNamespace(role="operator"),
        session_manager=manager,
        turn_runner=_LockingTurnRunner(),
    )
    try:
        with pytest.raises(StorageBusyError) as caught:
            await asyncio.wait_for(
                _handle_chat_history(
                    {
                        "sessionKey": session_key,
                        "limit": 10,
                        "includeSummaries": False,
                    },
                    context,
                ),
                timeout=0.5,
            )

        assert caught.value.operation == "chat.history"
        assert caught.value.retry_after_ms == 100
        assert mutation_lock.locked() is True

        mutation_lock.release()
        result = await _handle_chat_history(
            {
                "sessionKey": session_key,
                "limit": 10,
                "includeSummaries": False,
            },
            context,
        )
        assert [message["text"] for message in result["messages"]] == ["message 1"]
    finally:
        if mutation_lock.locked():
            mutation_lock.release()


@pytest.mark.asyncio
async def test_chat_history_busy_maps_to_retryable_wire_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rpc_chat_module, "_CHAT_HISTORY_LOCK_BUDGET_SECONDS", 0.01)
    session_key = "agent:main:webchat:history-wire-busy"
    mutation_lock = asyncio.Lock()
    await mutation_lock.acquire()

    class _LockingTurnRunner:
        def get_session_lock(self, key: str) -> asyncio.Lock:
            assert key == session_key
            return mutation_lock

    try:
        response = await get_dispatcher().dispatch(
            "history-wire-busy",
            "chat.history",
            {"sessionKey": session_key, "includeSummaries": False},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(
                    role="operator",
                    scopes=frozenset({"operator.read"}),
                ),
                session_manager=_FakeSessionManager([], canonical_entries=[]),
                turn_runner=_LockingTurnRunner(),
            ),
        )
    finally:
        mutation_lock.release()

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "STORAGE_BUSY"
    assert response.error.retryable is True
    assert response.error.retry_after_ms == 100
    assert response.error.details["operation"] == "chat.history"
    assert response.error.details["waited_ms"] >= 0
    assert response.error.details["stage"] == "lock_acquire"
    assert response.error.details["resource"] == "session_mutation_lock"


@pytest.mark.asyncio
async def test_chat_history_keeps_explicit_active_transcript_view_compatible() -> None:
    mgr = _FakePagedSessionManager(
        [_entry(3), _entry(4)],
        page=SimpleNamespace(
            entries=[_entry(1), _entry(2)],
            has_more=True,
            canonical_complete=True,
        ),
    )

    result = await _handle_chat_history(
        {
            "sessionKey": "agent:main:webchat:test",
            "limit": 10,
            "includeCanonical": False,
        },
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert [msg["text"] for msg in result["messages"]] == ["message 3", "message 4"]
    assert result["canonical_available"] is False
    assert result["canonical_complete"] is False
    assert mgr.page_calls == []


@pytest.mark.asyncio
async def test_chat_history_falls_back_when_canonical_unavailable() -> None:
    entries = [_entry(1)]
    mgr = _FakeSessionManager(entries)

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 10},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert mgr.used_canonical is True
    assert [msg["text"] for msg in result["messages"]] == ["message 1"]
    assert result["canonical_available"] is False
    assert result["canonical_complete"] is False


@pytest.mark.asyncio
async def test_chat_history_falls_back_to_active_when_paged_canonical_read_fails() -> None:
    mgr = _FakePagedSessionManager(
        [_entry(1)],
        page_exception=OSError("temporary database read failure"),
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 10},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert [msg["text"] for msg in result["messages"]] == ["message 1"]
    assert result["canonical_available"] is False
    assert result["canonical_complete"] is False
    assert mgr.used_canonical is False


@pytest.mark.asyncio
async def test_chat_history_does_not_fallback_when_canonical_storage_is_busy() -> None:
    busy = StorageBusyError(
        "get_canonical_transcript_page",
        waited_ms=2000,
        retry_after_ms=100,
    )
    mgr = _FakePagedSessionManager([_entry(1)], page_exception=busy)

    with pytest.raises(StorageBusyError) as caught:
        await _handle_chat_history(
            {"sessionKey": "agent:main:webchat:test", "limit": 10},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=mgr,
            ),
        )

    assert caught.value is busy
    assert mgr.used_canonical is False


@pytest.mark.asyncio
async def test_chat_history_skips_summaries_when_not_requested() -> None:
    summaries_called = False

    class _SlowSummaryManager(_FakeSessionManager):
        async def get_summaries(self, session_key):
            nonlocal summaries_called
            summaries_called = True
            await asyncio.Event().wait()

    manager = _SlowSummaryManager([_entry(1)], canonical_entries=[_entry(1)])
    result = await asyncio.wait_for(
        _handle_chat_history(
            {
                "sessionKey": "agent:main:webchat:test",
                "limit": 10,
                "includeSummaries": False,
            },
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=manager,
            ),
        ),
        timeout=0.5,
    )

    assert [message["text"] for message in result["messages"]] == ["message 1"]
    assert result["compaction_summaries"] == []
    assert summaries_called is False


@pytest.mark.asyncio
async def test_chat_history_falls_back_when_canonical_session_missing() -> None:
    entries = [_entry(1)]
    mgr = _FakeSessionManager(
        entries,
        canonical_exception=KeyError("Session not found: agent:main:webchat:test"),
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test", "limit": 10},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert mgr.used_canonical is True
    assert [msg["text"] for msg in result["messages"]] == ["message 1"]
    assert result["canonical_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key",
    [
        "agent:main:webchat:new123",
        "agent:ops:webchat:new123",
    ],
)
async def test_chat_history_returns_empty_for_missing_webchat_session(
    session_key: str,
) -> None:
    mgr = _FakeSessionManager(
        [],
        canonical_exception=KeyError(f"Session not found: {session_key}"),
        transcript_exception=KeyError(f"Session not found: {session_key}"),
    )

    result = await _handle_chat_history(
        {"sessionKey": session_key, "limit": "2"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=mgr,
        ),
    )

    assert result == {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": 2,
            "canonical_available": False,
            "canonical_complete": True,
            "compaction_summaries": [],
            "turn_outcomes": [],
        }


@pytest.mark.asyncio
async def test_chat_history_keeps_not_found_for_missing_non_webchat_session() -> None:
    session_key = "agent:main:cli:new123"
    mgr = _FakeSessionManager(
        [],
        canonical_exception=KeyError(f"Session not found: {session_key}"),
        transcript_exception=KeyError(f"Session not found: {session_key}"),
    )

    with pytest.raises(KeyError):
        await _handle_chat_history(
            {"sessionKey": session_key},
            RpcContext(
                conn_id="test",
                principal=SimpleNamespace(role="operator"),
                session_manager=mgr,
            ),
        )


@pytest.mark.asyncio
async def test_chat_history_exposes_subagent_completion_provenance() -> None:
    entry = TranscriptEntry(
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="system",
        content='{"type":"subagent_completion","child_session_key":"agent:main:subagent:abc123"}',
    )
    entry.provenance_kind = "internal_system"
    entry.provenance_source_session_key = "agent:main:subagent:abc123"
    entry.provenance_source_tool = "subagent_completion"

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    assert result["messages"] == [
        {
            "id": entry.message_id,
            "message_id": entry.message_id,
            "role": "system",
            "text": entry.content,
            "timestamp": entry.created_at,
            "provenance_kind": "internal_system",
            "provenance_source_session_key": "agent:main:subagent:abc123",
            "provenance_source_tool": "subagent_completion",
        }
    ]


@pytest.mark.asyncio
async def test_chat_history_exposes_stable_message_identity() -> None:
    entry = TranscriptEntry(
        id=123,
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="assistant",
        content="done",
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    msg = result["messages"][0]
    assert msg["id"] == entry.message_id
    assert msg["message_id"] == entry.message_id
    assert msg["transcript_id"] == 123


@pytest.mark.asyncio
async def test_chat_history_preserves_text_segment_presentation_and_order() -> None:
    segments = [
        {
            "type": "text",
            "text": "I will inspect the source.",
            "presentation": "intermediate",
        },
        {
            "type": "tool_use",
            "tool_use_id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "name": "read_file",
            "content": "ok",
        },
        {
            "type": "text",
            "text": "The source is valid.",
            "presentation": "answer",
        },
    ]
    entry = TranscriptEntry(
        id=124,
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="assistant",
        content="The source is valid.",
        tool_calls=segments,
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    assert result["messages"][0]["tool_calls"] == segments


@pytest.mark.asyncio
async def test_chat_history_returns_requested_compaction_summaries() -> None:
    summary = SessionSummary(
        id=7,
        session_id="parent",
        session_key="agent:main:webchat:test",
        compaction_index=1,
        compaction_id="compact-1",
        trigger_reason="manual",
        summary_text="older context",
        removed_count=3,
        kept_count=1,
        covered_through_id=42,
    )
    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([], summaries=[summary]),
        ),
    )

    assert result["compaction_summaries"][0]["covered_through_id"] == 42
    assert result["history_scope"] == "compacted"


@pytest.mark.asyncio
async def test_chat_history_degrades_requested_summaries_when_storage_is_busy() -> None:
    class _BusySummaryManager(_FakeSessionManager):
        async def get_summaries(self, session_key):
            raise StorageBusyError(
                "get_all_summaries",
                waited_ms=2000,
                retry_after_ms=100,
                stage="lock_acquire",
                resource="session_storage_operation_lock",
            )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_BusySummaryManager([_entry(1)]),
        ),
    )

    assert [message["text"] for message in result["messages"]] == ["message 1"]
    assert result["compaction_summaries"] == []


@pytest.mark.asyncio
async def test_chat_history_exposes_persisted_turn_usage() -> None:
    entry = TranscriptEntry(
        session_id="parent",
        session_key="agent:main:webchat:test",
        role="assistant",
        content="done",
        turn_usage={
            "model": "openai/gpt-test",
            "input_tokens": 11,
            "output_tokens": 5,
            "cost_usd": 0.0123,
            "cached_tokens": 2,
            "routed_tier": "economy",
            "routing_source": "squilla_router",
            "total_savings_pct": 42.0,
            "router_model_call_id": "1.0",
            "router_iteration": 1,
        },
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    msg = result["messages"][0]
    assert msg["usage"]["input_tokens"] == 11
    assert msg["usage"]["output_tokens"] == 5
    assert msg["usage"]["cost_usd"] == 0.0123
    assert msg["usage"]["router_model_call_id"] == "1.0"
    assert msg["usage"]["router_iteration"] == 1
    assert msg["model"] == "openai/gpt-test"
    assert msg["input"] == 11
    assert msg["output"] == 5


@pytest.mark.asyncio
async def test_chat_history_exposes_assistant_artifacts() -> None:
    artifact = {
        "id": "art-1",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 12,
        "sha256": "c" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:webchat:test",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-1?sessionKey=agent%3Amain%3Awebchat%3Atest",
    }
    entry = TranscriptEntry(
        session_id="session-1",
        session_key="agent:main:webchat:test",
        role="assistant",
        content='{"text":"done","artifacts":[' + json.dumps(artifact) + "]}",
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    assert result["messages"][0]["text"] == "done"
    output_artifact = result["messages"][0]["artifacts"][0]
    assert output_artifact["download_url"] == "/api/v1/artifacts/art-1"
    assert "session_key" not in output_artifact
    assert "sessionKey" not in json.dumps(output_artifact)


@pytest.mark.asyncio
async def test_chat_history_strips_artifact_omitted_marker_from_visible_text() -> None:
    artifact = {
        "id": "art-1",
        "kind": "artifact_ref",
        "name": "peppa_and_mummy_correct.png",
        "mime": "image/jpeg",
        "size": 339_000,
        "sha256": "c" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:webchat:test",
        "source": "image_generate",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-1",
    }
    marker = "[generated artifact omitted: peppa_and_mummy_correct.png (image/jpeg)]"
    entry = TranscriptEntry(
        session_id="session-1",
        session_key="agent:main:webchat:test",
        role="assistant",
        content=json.dumps(
            {
                "text": f"图片已经生成。\n\n{marker}",
                "artifacts": [artifact],
            }
        ),
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    msg = result["messages"][0]
    assert msg["text"] == "图片已经生成。"
    assert msg["artifacts"][0]["name"] == "peppa_and_mummy_correct.png"


@pytest.mark.asyncio
async def test_chat_history_prefers_attachment_display_text() -> None:
    entry = TranscriptEntry(
        session_id="session-1",
        session_key="agent:main:webchat:test",
        role="user",
        content=json.dumps(
            {
                "text": "Describe these attachments",
                "display_text": "",
                "attachments": [
                    {
                        "type": "image/png",
                        "name": "image.png",
                        "data": "aW1hZ2U=",
                    }
                ],
            }
        ),
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    msg = result["messages"][0]
    assert msg["text"] == ""
    assert msg["attachments"][0]["name"] == "image.png"


@pytest.mark.asyncio
async def test_chat_history_exposes_download_url_for_transcript_attachment_refs() -> None:
    sha = "d" * 64
    entry = TranscriptEntry(
        session_id="session-1",
        session_key="agent:main:webchat:test",
        role="user",
        content=json.dumps(
            {
                "text": "Please process the attached pasted text.",
                "attachments": [
                    {
                        "sha256_ref": sha,
                        "name": "webchat-paste-test.txt",
                        "mime": "text/plain",
                        "size": 12,
                    }
                ],
            }
        ),
    )

    result = await _handle_chat_history(
        {"sessionKey": "agent:main:webchat:test"},
        RpcContext(
            conn_id="test",
            principal=SimpleNamespace(role="operator"),
            session_manager=_FakeSessionManager([entry]),
        ),
    )

    attachment = result["messages"][0]["attachments"][0]
    assert attachment["download_url"] == (
        f"/api/v1/attachments/{sha}?sessionKey=agent%3Amain%3Awebchat%3Atest"
        "&name=webchat-paste-test.txt&mime=text%2Fplain"
    )
