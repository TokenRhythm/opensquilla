"""Generated Contract registration tests for Session read Gateway methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from opensquilla.gateway.adapters.session_read_contract import (
    register_chat_history_contract,
    register_sessions_messages_hydrate_contract,
    register_sessions_messages_snapshot_contract,
    register_sessions_messages_subscribe_contract,
    register_sessions_messages_unsubscribe_contract,
    register_sessions_preview_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc.registry import RpcHandlerError


class RecordingRegistry:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[Any, object], Awaitable[Any]]] = {}
        self.scopes: dict[str, str] = {}

    def register(
        self,
        name: str,
        handler: Callable[[Any, object], Awaitable[Any]],
        scope: str,
    ) -> None:
        self.handlers[name] = handler
        self.scopes[name] = scope


EMPTY_METADATA = {
    "workspaceId": None,
    "projectWorkspace": None,
    "projectWorkspaceDeferred": False,
    "active_task_group_ids": [],
    "run_mode_lock": {"locked": False},
    "pendingUserInputs": [],
    "collaboration": None,
    "routing": None,
    "currentPlan": None,
    "activePlanRun": None,
    "goal": None,
    "goalSnapshotStreamSeq": 0,
    "tasks": [],
    "active_task": None,
    "last_task": None,
    "run_status": "idle",
    "hydration_complete": True,
    "deferred_fields": [],
}

VALID_RESULTS: dict[str, Any] = {
    "chat.history": {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": 50,
        "canonical_available": True,
        "canonical_complete": True,
        "compaction_summaries": [],
        "turn_outcomes": [],
    },
    "sessions.messages.subscribe": {
        "subscribed": True,
        "key": "agent:main:webchat:contract",
        "stream_generation": "generation-a",
        "current_stream_seq": 0,
        "replay_complete": True,
        "replay_gap_reason": None,
        "replayed_count": 0,
        **EMPTY_METADATA,
    },
    "sessions.messages.hydrate": {
        "key": "agent:main:webchat:contract",
        **EMPTY_METADATA,
    },
    "sessions.messages.snapshot": {
        "key": "agent:main:webchat:contract",
        "task_id": None,
        "stream_generation": "generation-a",
        "current_stream_seq": 0,
        "events": [],
    },
    "sessions.messages.unsubscribe": None,
    "sessions.preview": {"ts": 1, "previews": []},
}

REGISTRARS = {
    "chat.history": register_chat_history_contract,
    "sessions.messages.subscribe": register_sessions_messages_subscribe_contract,
    "sessions.messages.hydrate": register_sessions_messages_hydrate_contract,
    "sessions.messages.snapshot": register_sessions_messages_snapshot_contract,
    "sessions.messages.unsubscribe": register_sessions_messages_unsubscribe_contract,
    "sessions.preview": register_sessions_preview_contract,
}


@pytest.mark.asyncio
async def test_registers_every_session_read_method_from_generated_descriptors() -> None:
    registry = RecordingRegistry()
    calls: list[tuple[str, Any, object]] = []

    for method, registrar in REGISTRARS.items():
        async def implementation(
            params: Any,
            context: object,
            *,
            _method: str = method,
        ) -> Any:
            calls.append((_method, params, context))
            return VALID_RESULTS[_method]

        registrar(
            registry,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )

    assert set(registry.handlers) == set(REGISTRARS)
    assert set(registry.scopes.values()) == {"operator.read"}

    context = object()
    for method, handler in registry.handlers.items():
        params = None if method == "chat.history" else {"key": "agent:main:webchat:contract"}
        assert await handler(params, context) == VALID_RESULTS[method]

    assert [method for method, _params, _context in calls] == list(REGISTRARS)
    assert all(call_context is context for _method, _params, call_context in calls)


@pytest.mark.asyncio
async def test_invalid_success_result_is_rejected_at_the_gateway_adapter() -> None:
    registry = RecordingRegistry()

    async def invalid_snapshot(_params: Any, _context: object) -> dict[str, Any]:
        return {"key": "agent:main:webchat:contract", "events": []}

    register_sessions_messages_snapshot_contract(
        registry,
        invalid_snapshot,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with pytest.raises(RpcHandlerError) as exc_info:
        await registry.handlers["sessions.messages.snapshot"](
            {"key": "agent:main:webchat:contract"},
            object(),
        )

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert "violated its v4 contract" in str(exc_info.value)
