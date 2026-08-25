from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent


class _RecordingSessionManager:
    def __init__(self) -> None:
        self.compact_calls: list[tuple[str, int]] = []
        self.messages: list[tuple[str, str, str]] = []

    async def compact(self, session_key: str, budget: int) -> str:
        self.compact_calls.append((session_key, budget))
        return "summary"

    async def append_message(self, session_key: str, *, role: str, content: str) -> None:
        self.messages.append((session_key, role, content))


@pytest.mark.asyncio
async def test_provider_request_too_large_error_persistence_does_not_compact_transcript() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message=(
                "The request is too large for the provider context window after "
                "automatic context compaction and payload reduction."
            ),
            code="provider_request_too_large",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "Error: The request is too large for the provider context window after "
            "automatic context compaction and payload reduction. OpenSquilla "
            "preserved the recoverable state; retry with a narrower request "
            "or a larger-context model.",
        )
    ]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_terminal_reply() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Provider output limit reached before completion",
            code="provider_output_truncated",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_message_fallback() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with patch("opensquilla.engine.runtime.log") as log:
        await runner._persist_turn_error(
            "agent:main:webchat:test",
            ErrorEvent(
                message="Provider output limit reached before completion",
                code="agent_error",
            ),
        )

    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]
    log.info.assert_called_once()
    assert log.info.call_args.kwargs["code"] == "provider_output_truncated"
    assert log.info.call_args.kwargs["turn_outcome"]["kind"] == "partial"


@pytest.mark.asyncio
async def test_terminal_reset_error_records_row_without_duplicate_transcript_message() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )
    record_error = AsyncMock(return_value="error-terminal-reset")
    runner._record_turn_error = record_error

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The fallback model also failed.",
            code="ensemble_fixed_error",
            failure_kind="provider_error",
        ),
        append_transcript=False,
    )

    record_error.assert_awaited_once()
    assert manager.messages == []
