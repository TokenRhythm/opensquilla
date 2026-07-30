from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult
from opensquilla.cli.tui.contracts import TuiOutputHandle
from opensquilla.provider.types import ProviderRequestCorrelation


class _StandaloneSlashHarness:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, str]] = []
        self.truncate_calls: list[tuple[str, int]] = []
        self.compact_calls: list[tuple[str, int, object | None]] = []
        self.flush_calls: list[dict[str, object]] = []
        self.transcripts: dict[str, list[object]] = {}

    async def create_session(self, session_key: str, *, agent_id: str = "main") -> object:
        self.create_calls.append({"session_key": session_key, "agent_id": agent_id})
        return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    async def read_transcript(self, session_key: str) -> list[object]:
        return list(self.transcripts.get(session_key, []))

    async def truncate_session(self, session_key: str, *, max_messages: int = 0) -> None:
        self.truncate_calls.append((session_key, max_messages))
        self.transcripts[session_key] = []

    async def compact_session(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
    ) -> str:
        self.compact_calls.append((session_key, context_window_tokens, config))
        return "summary"

    async def flush_transcript(
        self,
        transcript: object,
        session_key: str,
        **kwargs: object,
    ) -> object:
        self.flush_calls.append(
            {"transcript": transcript, "session_key": session_key, "kwargs": kwargs}
        )
        return SimpleNamespace(
            mode="llm",
            error=None,
            indexed_chunk_count=1,
            integrity_status="ok",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
        )


def _slash_services(harness: _StandaloneSlashHarness):
    from opensquilla.cli.repl.standalone_slash_adapter import StandaloneSlashServices

    return StandaloneSlashServices(
        create_session=harness.create_session,
        read_transcript=harness.read_transcript,
        truncate_session=harness.truncate_session,
        compact_session=harness.compact_session,
        flush_transcript=harness.flush_transcript,
    )


@pytest.mark.asyncio
async def test_standalone_slash_adapter_leaves_exit_to_the_runtime_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit commands are owned by the runtime loop, not the slash handler.

    The handler has no exit branch: an ``/exit`` that reaches it anyway is
    answered with the unknown-command notice and ``True`` (handled), so
    the dispatch loop keeps running instead of terminating from inside
    slash dispatch.
    """
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        handle_standalone_slash_command,
    )

    state = ChatSessionState(session_key="agent:main:standalone:test", model="openai/test")
    context = StandaloneSlashContext(
        state=state,
        session_key=state.session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=_slash_services(_StandaloneSlashHarness()),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )

    handled = await handle_standalone_slash_command("/exit", context)

    assert handled is True
    assert "Unknown command." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_standalone_slash_adapter_updates_model_without_chat_cmd_loop() -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        handle_standalone_slash_command,
    )

    state = ChatSessionState(session_key="agent:main:standalone:test", model="old/model")
    context = StandaloneSlashContext(
        state=state,
        session_key=state.session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=_slash_services(_StandaloneSlashHarness()),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )

    handled = await handle_standalone_slash_command("/model new/model", context)

    assert handled is True
    assert state.model == "new/model"
    assert context.model == "new/model"


@pytest.mark.asyncio
async def test_standalone_slash_adapter_streams_path_without_chat_cmd_loop(
    tmp_path,
) -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        handle_standalone_slash_command,
    )

    target = tmp_path / "notes.md"
    target.write_text("hello\n", encoding="utf-8")
    state = ChatSessionState(session_key="agent:main:standalone:test", model="openai/test")
    tool_ctx = object()
    stream_calls: list[dict[str, Any]] = []

    async def stream_response(
        turn_runner: object,
        session_key: str,
        tool_context: object,
        message: str,
        *,
        model: str | None = None,
        services: object = None,
        timeout: float | None = None,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult:
        stream_calls.append(
            {
                "turn_runner": turn_runner,
                "session_key": session_key,
                "tool_context": tool_context,
                "message": message,
                "model": model,
                "services": services,
                "timeout": timeout,
                "tui_output": tui_output,
            }
        )
        return TurnResult(text="done")

    runtime_services = SimpleNamespace()
    turn_runner = object()
    tui_output = cast(TuiOutputHandle, object())
    context = StandaloneSlashContext(
        state=state,
        session_key=state.session_key,
        model=state.model,
        tool_ctx=tool_ctx,
        slash_services=_slash_services(_StandaloneSlashHarness()),
        runtime_services=runtime_services,
        turn_runner=turn_runner,
        timeout=7.25,
        tui_output=tui_output,
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
        stream_response=stream_response,
    )

    handled = await handle_standalone_slash_command(f"/path {target} inspect", context)

    assert handled is True
    assert len(stream_calls) == 1
    assert stream_calls[0]["turn_runner"] is turn_runner
    assert stream_calls[0]["session_key"] == "agent:main:standalone:test"
    assert stream_calls[0]["tool_context"] is tool_ctx
    assert stream_calls[0]["model"] == "openai/test"
    assert stream_calls[0]["services"] is runtime_services
    assert stream_calls[0]["timeout"] == 7.25
    assert stream_calls[0]["tui_output"] is tui_output
    assert "inspect" in stream_calls[0]["message"]
    assert str(target.resolve(strict=False)) in stream_calls[0]["message"]
    assert state.transcript.to_markdown()


@pytest.mark.asyncio
async def test_standalone_slash_adapter_new_session_uses_typed_create_handle() -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        handle_standalone_slash_command,
    )

    harness = _StandaloneSlashHarness()
    replacement_calls: list[dict[str, object]] = []
    state = ChatSessionState(session_key="agent:main:standalone:old", model="openai/test")
    context = StandaloneSlashContext(
        state=state,
        session_key=state.session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=_slash_services(harness),
        turn_runner=object(),
        build_tool_ctx=lambda session_key: {"session_key": session_key},
        replace_session=lambda **updates: replacement_calls.append(updates),
    )

    handled = await handle_standalone_slash_command("/new scratch", context)

    assert handled is True
    assert len(harness.create_calls) == 1
    new_session_key = harness.create_calls[0]["session_key"]
    assert new_session_key.startswith("agent:main:standalone:")
    assert harness.create_calls[0]["agent_id"] == "main"
    assert context.session_key == new_session_key
    assert context.state.session_key == new_session_key
    assert context.tool_ctx == {"session_key": new_session_key}
    assert replacement_calls[0]["session_key"] == new_session_key


@pytest.mark.asyncio
async def test_standalone_slash_adapter_reset_uses_typed_flush_and_truncate_handles() -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        handle_standalone_slash_command,
    )

    harness = _StandaloneSlashHarness()
    session_key = "agent:main:standalone:test"
    harness.transcripts[session_key] = [SimpleNamespace(role="user", content="persisted")]
    state = ChatSessionState(session_key=session_key, model="openai/test")
    state.transcript.add("user", "local")
    context = StandaloneSlashContext(
        state=state,
        session_key=session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=_slash_services(harness),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )

    handled = await handle_standalone_slash_command("/reset", context)

    assert handled is True
    assert len(harness.flush_calls) == 1
    assert harness.flush_calls[0]["session_key"] == session_key
    assert harness.flush_calls[0]["kwargs"] == {
        "agent_id": "main",
        "timeout": 30.0,
        "message_window": 0,
        "segment_mode": "auto",
    }
    assert harness.truncate_calls == [(session_key, 0)]
    assert not state.transcript.to_markdown()


@pytest.mark.asyncio
async def test_standalone_slash_adapter_compact_uses_typed_compact_handles() -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        StandaloneSlashServices,
        handle_standalone_slash_command,
    )

    harness = _StandaloneSlashHarness()
    session_key = "agent:main:standalone:test"
    harness.transcripts[session_key] = [SimpleNamespace(role="user", content="persisted")]
    config = SimpleNamespace(
        context_budget_tokens=4321,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    state = ChatSessionState(session_key=session_key, model="openai/test")
    context = StandaloneSlashContext(
        state=state,
        session_key=session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=StandaloneSlashServices(
            create_session=harness.create_session,
            read_transcript=harness.read_transcript,
            compact_session=harness.compact_session,
            flush_transcript=harness.flush_transcript,
            config=config,
            provider_selector=None,
        ),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )

    handled = await handle_standalone_slash_command("/compact", context)

    assert handled is True
    assert len(harness.flush_calls) == 1
    assert len(harness.compact_calls) == 1
    compact_session_key, context_window, compaction_config = harness.compact_calls[0]
    assert compact_session_key == session_key
    assert context_window == 4321
    assert compaction_config is not None


@pytest.mark.asyncio
async def test_standalone_compact_correlates_flush_and_compaction_to_durable_session() -> None:
    from opensquilla.cli.repl.standalone_slash_adapter import (
        StandaloneSlashContext,
        StandaloneSlashServices,
        handle_standalone_slash_command,
    )

    harness = _StandaloneSlashHarness()
    session_key = "agent:main:standalone:test"
    harness.transcripts[session_key] = [SimpleNamespace(role="user", content="persisted")]
    compact_kwargs: dict[str, object] = {}

    async def get_session(_session_key: str) -> object:
        return SimpleNamespace(session_id="durable-session-1")

    async def compact_with_result(
        _session_key: str,
        _context_window_tokens: int,
        _compaction_config: object,
        **kwargs: object,
    ) -> object:
        compact_kwargs.update(kwargs)
        return SimpleNamespace(
            summary="summary",
            tokens_before=100,
            tokens_after=20,
            remaining_budget_tokens=80,
            summary_source="llm",
        )

    state = ChatSessionState(session_key=session_key, model="tokenrhythm/test")
    context = StandaloneSlashContext(
        state=state,
        session_key=session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=StandaloneSlashServices(
            get_session=get_session,
            read_transcript=harness.read_transcript,
            compact_with_result=compact_with_result,
            flush_transcript=harness.flush_transcript,
            config=SimpleNamespace(context_budget_tokens=100),
        ),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )

    assert await handle_standalone_slash_command("/compact", context) is True

    flush_correlation = cast(
        ProviderRequestCorrelation,
        harness.flush_calls[0]["kwargs"]["provider_request_correlation"],
    )
    compact_correlation = cast(
        ProviderRequestCorrelation,
        compact_kwargs["provider_request_correlation"],
    )
    assert flush_correlation.session_id == "durable-session-1"
    assert compact_correlation.session_id == "durable-session-1"
    assert flush_correlation.turn_id == compact_correlation.turn_id
    assert compact_kwargs["compaction_id"] == compact_correlation.turn_id
    assert flush_correlation.execution_id != compact_correlation.execution_id
    assert flush_correlation.call_kind == "auxiliary.session_flush"
    assert compact_correlation.call_kind == "auxiliary.compaction"
