"""Tests for the /meta gateway slash command (list + run)."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult


class _FakeGatewayClient:
    """Records generic RPC ``call`` invocations and returns canned payloads."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses = responses or {}

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, params))
        return self.responses.get(method, {})


def _make_console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=100, highlight=False, no_color=True)
    return console, buffer


@pytest.mark.asyncio
async def test_meta_no_arg_lists_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    console, buffer = _make_console()
    monkeypatch.setattr(slash_adapter, "console", console)

    client = _FakeGatewayClient(
        responses={
            "meta.list": {
                "skills": [
                    {"name": "meta-tiny", "description": "A tiny meta-skill."},
                    {"name": "meta-plan", "description": "Plan things."},
                ]
            }
        }
    )
    state = ChatSessionState(session_key="agent:main:test", model="local/test")

    handled = await handle_gateway_slash_command(
        "/meta",
        GatewaySlashContext(state=state, client=client, elevated_state={"mode": None}),
    )

    assert handled is True
    assert client.calls == [("meta.list", {})]
    rendered = buffer.getvalue()
    assert "meta-tiny" in rendered
    assert "meta-plan" in rendered


@pytest.mark.asyncio
async def test_meta_no_arg_disabled_prints_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    console, buffer = _make_console()
    monkeypatch.setattr(slash_adapter, "console", console)

    client = _FakeGatewayClient(responses={"meta.list": {"disabled": True}})
    state = ChatSessionState(session_key="agent:main:test", model="local/test")

    handled = await handle_gateway_slash_command(
        "/meta",
        GatewaySlashContext(state=state, client=client, elevated_state={"mode": None}),
    )

    assert handled is True
    assert client.calls == [("meta.list", {})]
    assert "disabled" in buffer.getvalue().lower()


@pytest.mark.asyncio
async def test_meta_run_ok_triggers_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    console, _buffer = _make_console()
    monkeypatch.setattr(slash_adapter, "console", console)

    captured: dict[str, Any] = {}

    async def fake_stream_response_gateway(
        gateway_client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None],
        attachments: list[dict[str, Any]] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        captured.update(
            {
                "client": gateway_client,
                "session_key": session_key,
                "message": message,
                "elevated_state": elevated_state,
            }
        )
        return TurnResult(text="launched")

    monkeypatch.setattr(slash_adapter, "stream_response_gateway", fake_stream_response_gateway)

    client = _FakeGatewayClient(responses={"meta.run": {"ok": True}})
    state = ChatSessionState(session_key="agent:main:test", model="local/test")

    handled = await handle_gateway_slash_command(
        "/meta meta-tiny",
        GatewaySlashContext(state=state, client=client, elevated_state={"mode": "on"}),
    )

    assert handled is True
    assert client.calls == [
        ("meta.run", {"name": "meta-tiny", "sessionKey": "agent:main:test"}),
    ]
    # A turn was triggered through the same path /image uses.
    assert captured["session_key"] == "agent:main:test"
    assert captured["message"] == "/meta meta-tiny"
    assert captured["elevated_state"] == {"mode": "on"}
    assert state.transcript.to_markdown()


@pytest.mark.asyncio
async def test_meta_run_keeps_inline_request_out_of_skill_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    console, _buffer = _make_console()
    monkeypatch.setattr(slash_adapter, "console", console)

    captured: dict[str, Any] = {}

    async def fake_stream_response_gateway(
        gateway_client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None],
        attachments: list[dict[str, Any]] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        captured["message"] = message
        return TurnResult(text="launched")

    monkeypatch.setattr(slash_adapter, "stream_response_gateway", fake_stream_response_gateway)

    client = _FakeGatewayClient(responses={"meta.run": {"ok": True}})
    state = ChatSessionState(session_key="agent:main:test", model="local/test")
    command = "/meta meta-tiny create a competitor research meta-skill"

    handled = await handle_gateway_slash_command(
        command,
        GatewaySlashContext(state=state, client=client, elevated_state={"mode": None}),
    )

    assert handled is True
    assert client.calls == [
        ("meta.run", {"name": "meta-tiny", "sessionKey": "agent:main:test"}),
    ]
    assert captured["message"] == command
    assert command in state.transcript.to_markdown()


@pytest.mark.asyncio
async def test_meta_run_not_ok_prints_error_and_skips_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import slash_adapter
    from opensquilla.cli.repl.slash_adapter import (
        GatewaySlashContext,
        handle_gateway_slash_command,
    )

    console, buffer = _make_console()
    monkeypatch.setattr(slash_adapter, "console", console)

    stream_calls: list[str] = []

    async def fake_stream_response_gateway(*args: Any, **kwargs: Any) -> TurnResult:
        stream_calls.append("called")
        return TurnResult(text="should-not-happen")

    monkeypatch.setattr(slash_adapter, "stream_response_gateway", fake_stream_response_gateway)

    client = _FakeGatewayClient(
        responses={"meta.run": {"ok": False, "error": "unknown meta-skill: bad"}}
    )
    state = ChatSessionState(session_key="agent:main:test", model="local/test")

    handled = await handle_gateway_slash_command(
        "/meta bad",
        GatewaySlashContext(state=state, client=client, elevated_state={"mode": None}),
    )

    assert handled is True
    assert client.calls == [
        ("meta.run", {"name": "bad", "sessionKey": "agent:main:test"}),
    ]
    # No turn fired.
    assert stream_calls == []
    assert "unknown meta-skill: bad" in buffer.getvalue()
