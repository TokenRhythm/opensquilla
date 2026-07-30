from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult
from opensquilla.cli.tui.contracts import TuiOutputHandle


class _FakeGatewayClient:
    def __init__(self) -> None:
        self.reset_calls: list[str] = []

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str:
        raise AssertionError("create_session is not used by these tests")

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        raise AssertionError("list_sessions is not used by these tests")

    async def resolve_session(self, key: str) -> dict[str, Any]:
        raise AssertionError("resolve_session is not used by these tests")

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        del limit
        return {
            "session": {"session_key": key, "model": "openai/test"},
            "history": {
                "messages": [],
                "history_scope": "complete",
                "loaded_count": 0,
                "canonical_available": True,
            },
            "stream_cursor": 0,
        }

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]:
        raise AssertionError("delete_sessions is not used by these tests")

    async def reset_session(self, session_key: str) -> dict[str, object]:
        self.reset_calls.append(session_key)
        return {"reset": True, "key": session_key}

    async def compact_session(self, key: str) -> dict[str, Any]:
        raise AssertionError("compact_session is not used by these tests")

    async def list_models(
        self,
        provider: str | None = None,
        capabilities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        raise AssertionError("list_models is not used by these tests")

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]:
        raise AssertionError("patch_session is not used by these tests")

    async def usage_status(self) -> dict[str, Any]:
        raise AssertionError("usage_status is not used by these tests")

    async def upload_file(self, path: Path, mime: str, name: str) -> str:
        raise AssertionError("upload_file is not used by these tests")

    def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async def _unused_events() -> AsyncIterator[dict[str, Any]]:
            raise AssertionError("send_message is not used by these tests")
            yield {}

        return _unused_events()

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        del choice
        raise AssertionError("resolve_approval is not used by these tests")

    async def abort_session(self, key: str) -> dict[str, Any]:
        raise AssertionError("abort_session is not used by these tests")


class _RecordingOutputHandle:
    @property
    def approval_surface(self) -> object:
        return "approval-surface"

    async def write_through(self, payload: str) -> None:
        return None

    def stream_output(self):
        raise AssertionError("stream_output is only consumed by the renderer")


def test_gateway_slash_adapter_exposes_typed_context() -> None:
    from opensquilla.cli.repl.slash_adapter import GatewaySlashContext

    tui_output = _RecordingOutputHandle()
    context = GatewaySlashContext(
        state=ChatSessionState(session_key="agent:main:test", model="openai/test"),
        client=_FakeGatewayClient(),
        elevated_state={"mode": None},
        tui_output=tui_output,
    )

    assert isinstance(context.tui_output, TuiOutputHandle)


@pytest.mark.asyncio
async def test_gateway_slash_adapter_handles_clear_without_chat_command_state() -> None:
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    state = ChatSessionState(session_key="agent:main:test", model="openai/test")
    state.transcript.add("user", "hello")
    client = _FakeGatewayClient()

    handled = await handle_gateway_slash_command(
        "/clear",
        GatewaySlashContext(
            state=state,
            client=client,
            elevated_state={"mode": None},
        ),
    )

    assert handled is True
    assert client.reset_calls == ["agent:main:test"]
    assert state.transcript.turns == []


@pytest.mark.asyncio
async def test_gateway_slash_adapter_threads_tui_output_to_streaming_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    state = ChatSessionState(session_key="agent:main:test", model="openai/test")
    client = SimpleNamespace(is_local_gateway=True)
    tui_output = _RecordingOutputHandle()
    captured: dict[str, Any] = {}

    def fake_path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
        captured["path_command"] = command
        return "inspect /repo", []

    async def fake_stream_response_gateway(
        gateway_client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None],
        *,
        attachments: list[dict[str, Any]] | None = None,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult:
        captured.update(
            {
                "client": gateway_client,
                "session_key": session_key,
                "message": message,
                "elevated_state": elevated_state,
                "attachments": attachments,
                "tui_output": tui_output,
            }
        )
        return TurnResult(text="done")

    monkeypatch.setattr(
        slash_adapter,
        "path_prompt_and_attachments",
        fake_path_prompt_and_attachments,
    )
    monkeypatch.setattr(
        slash_adapter,
        "stream_response_gateway",
        fake_stream_response_gateway,
    )

    handled = await handle_gateway_slash_command(
        "/path /repo inspect",
        GatewaySlashContext(
            state=state,
            client=client,
            elevated_state={"mode": "on"},
            tui_output=tui_output,
        ),
    )

    assert handled is True
    assert captured["path_command"] == "/path /repo inspect"
    assert captured["session_key"] == "agent:main:test"
    assert captured["message"] == "inspect /repo"
    assert captured["elevated_state"] == {"mode": "on"}
    assert captured["attachments"] == []
    assert captured["tui_output"] is tui_output
    assert state.transcript.to_markdown()
