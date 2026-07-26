from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from opensquilla.channels.manager import ChannelManager
from opensquilla.channels.types import IncomingMessage
from opensquilla.gateway.channel_dispatch import _resolve_channel_busy_input_mode
from opensquilla.gateway.config import DiscordChannelEntry
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


def _group_message(sender_id: str, *, thread_id: str | None = None) -> IncomingMessage:
    metadata: dict[str, Any] = {"is_group": True}
    if thread_id is not None:
        metadata["native_thread_id"] = thread_id
    return IncomingMessage(
        sender_id=sender_id,
        channel_id="room-1",
        content="hello",
        metadata=metadata,
    )


def test_group_sessions_are_isolated_by_sender_by_default() -> None:
    first = ChannelManager._build_session_key("discord", _group_message("user-1"))
    second = ChannelManager._build_session_key("discord", _group_message("user-2"))

    assert first == "agent:main:discord:group:room-1:sender:user-1"
    assert second == "agent:main:discord:group:room-1:sender:user-2"
    assert first != second


def test_shared_room_scope_is_explicit_compatibility_mode() -> None:
    first = ChannelManager._build_session_key(
        "discord",
        _group_message("user-1"),
        group_session_scope="shared_room",
    )
    second = ChannelManager._build_session_key(
        "discord",
        _group_message("user-2"),
        group_session_scope="shared_room",
    )

    assert first == second == "agent:main:discord:group:room-1"


def test_per_sender_group_thread_keeps_thread_isolation() -> None:
    key = ChannelManager._build_session_key(
        "discord",
        _group_message("user-1", thread_id="thread-9"),
    )

    assert key == "agent:main:discord:group:room-1:sender:user-1:thread:thread-9"


def test_channel_entry_policy_defaults_and_validation() -> None:
    entry = DiscordChannelEntry(name="community", token="token")

    assert entry.group_session_scope == "per_sender"
    assert entry.busy_input_mode == "followup"

    shared = DiscordChannelEntry(
        name="community",
        token="token",
        group_session_scope="shared_room",
        busy_input_mode="queue",
    )
    assert shared.group_session_scope == "shared_room"
    assert shared.busy_input_mode == "queue"

    with pytest.raises(ValidationError):
        DiscordChannelEntry(
            name="community",
            token="token",
            group_session_scope="global",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        DiscordChannelEntry(
            name="community",
            token="token",
            busy_input_mode="collect",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("mode", ["followup", "queue", "steer", "interrupt"])
def test_channel_busy_mode_requires_exact_runtime_support(mode: str) -> None:
    runtime = MagicMock()
    runtime.supports_queue_mode.side_effect = lambda value: value == mode

    assert _resolve_channel_busy_input_mode(runtime, mode) == mode
    runtime.supports_queue_mode.assert_called_once_with(mode)


def test_channel_busy_mode_falls_back_for_legacy_or_invalid_runtime() -> None:
    legacy_runtime = object()
    rejecting_runtime = MagicMock()
    rejecting_runtime.supports_queue_mode.return_value = False

    assert _resolve_channel_busy_input_mode(legacy_runtime, "queue") == "followup"
    assert _resolve_channel_busy_input_mode(rejecting_runtime, "steer") == "followup"
    assert _resolve_channel_busy_input_mode(rejecting_runtime, "unknown") == "followup"


def test_task_runtime_advertises_only_modes_with_exact_enqueue_semantics() -> None:
    assert {
        "collect",
        "followup",
        "interrupt",
        "queue",
        "steer",
        "steer+backlog",
        "steer-backlog",
    } == TaskRuntime.supported_queue_modes


def _storage() -> Any:
    storage = MagicMock()
    records: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        records[record.task_id] = record

    async def update(task_id: str, **fields: Any) -> None:
        record = records.get(task_id)
        if record is None:
            return
        for name, value in fields.items():
            if hasattr(record, name):
                object.__setattr__(record, name, value)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return records.get(task_id)

    storage.create_agent_task = AsyncMock(side_effect=create)
    storage.update_agent_task = AsyncMock(side_effect=update)
    storage.get_agent_task = AsyncMock(side_effect=get)
    return storage


def _envelope() -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name="discord",
        agent_id="main",
        session_key="agent:main:discord:direct:user-1",
        input_provenance={"kind": "test"},
    )


@pytest.mark.asyncio
async def test_task_runtime_rejects_unknown_mode_before_creating_task() -> None:
    async def handler(_run: Any) -> None:
        return None

    storage = _storage()
    runtime = TaskRuntime(storage=storage, turn_handler=handler)

    with pytest.raises(ValueError, match="mode must be one of"):
        await runtime.enqueue(_envelope(), "hello", mode="unknown")

    storage.create_agent_task.assert_not_called()


@pytest.mark.parametrize("mode", ["steer-backlog", "steer+backlog"])
@pytest.mark.asyncio
async def test_task_runtime_preserves_public_backlog_queue_modes(mode: str) -> None:
    observed_modes: list[str] = []

    async def handler(run: Any) -> None:
        observed_modes.append(run.queue_mode)

    runtime = TaskRuntime(storage=_storage(), turn_handler=handler)
    handle = await runtime.enqueue(_envelope(), "hello", mode=mode)

    record = await runtime.wait(handle.task_id, timeout=1.0)

    assert record.status == AgentTaskStatus.SUCCEEDED
    assert observed_modes == [mode]


@pytest.mark.parametrize("mode", ["steer", "interrupt"])
@pytest.mark.asyncio
async def test_task_runtime_busy_interrupt_modes_cancel_current_turn(mode: str) -> None:
    started = asyncio.Event()
    runs: list[tuple[str, str]] = []

    async def handler(run: Any) -> None:
        runs.append((run.message, run.queue_mode))
        if run.message == "first":
            started.set()
            await asyncio.Event().wait()

    runtime = TaskRuntime(storage=_storage(), turn_handler=handler)
    first = await runtime.enqueue(_envelope(), "first", mode="followup")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    second = await runtime.enqueue(_envelope(), "second", mode=mode)
    first_record = await runtime.wait(first.task_id, timeout=1.0)
    second_record = await runtime.wait(second.task_id, timeout=1.0)

    assert first_record.status == AgentTaskStatus.CANCELLED
    assert second_record.status == AgentTaskStatus.SUCCEEDED
    assert runs == [("first", "followup"), ("second", mode)]
