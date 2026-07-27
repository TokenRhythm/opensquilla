"""Tests for chat command — verify CLI interface and routing."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Iterable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from opensquilla.cli import chat_cmd
from opensquilla.cli.chat.turn_stream import turn_stream_error_message
from opensquilla.cli.main import app
from opensquilla.cli.repl import commands as repl_commands
from opensquilla.cli.repl import slash_bridge
from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface
from opensquilla.engine.types import (
    ArtifactEvent,
    DoneEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from opensquilla.session.compaction import CompactionConfig
from opensquilla.tools.types import CallerKind, ToolContext

runner = CliRunner()


def test_cli_surfaces_actionable_ensemble_image_rejection() -> None:
    event = SimpleNamespace(
        code="ensemble_multimodal_unsupported",
        message=(
            "Ensemble does not support image input yet. "
            "Switch to a single-model routing mode and try again."
        ),
    )

    assert turn_stream_error_message(event) == event.message


def _install_fake_inputs(monkeypatch, inputs: Iterable[str]) -> None:
    """Install a fake TUI surface that yields lines from *inputs*.

    The chat command reaches input through the selected TUI runtime. Patch the
    TUI surface seam so these command tests stay behind the same boundary as
    production code.
    """

    iterator = iter(inputs)

    class _FakeOutputHandle:
        approval_surface = Surface.CLI_GATEWAY

        async def write_through(self, payload: str) -> None:
            return None

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            return None

        def set_toolbar(self, key: str, value) -> None:
            return None

        def invalidate(self) -> None:
            return None

    class _FakeSurface:
        output_handle = _FakeOutputHandle()

        async def next_line(self) -> str | None:
            try:
                return next(iterator)
            except StopIteration:
                return None

        def set_cancel_callback(self, cb) -> None:
            return None

        def set_shutdown_callback(self, cb) -> None:
            return None

        def emit_eof(self) -> None:
            return None

        def invalidate(self) -> None:
            return None

        async def write_through(self, payload: str) -> None:
            return None

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            return None

        @property
        def redraw_callback(self):
            return self.output_handle.invalidate

    @asynccontextmanager
    async def _fake_session(**kwargs):
        yield _FakeSurface()

    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.runtime.open_opentui_surface",
        _fake_session,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.native_bridge.open_native_terminal_surface",
        _fake_session,
    )


EXPECTED_GATEWAY_COMMANDS = {
    "/new",
    "/reset",
    "/compact",
    "/cmp",
    "/help",
    "/status",
    "/model",
    "/models",
    "/cost",
    "/usage",
    "/save",
    "/image",
    "/path",
    "/file",
    "/approvals",
    "/permissions",
    "/forget",
    "/sessions",
    "/resume",
    "/delete",
    "/exit",
}

EXPECTED_STANDALONE_COMMANDS = EXPECTED_GATEWAY_COMMANDS - {
    "/approvals",
    "/delete",
    "/file",
    "/forget",
    "/models",
    "/permissions",
    "/resume",
    "/sessions",
    "/usage",
}


def _handler_words(surface: Surface) -> set[str]:
    return {word for command in DEFAULT_REGISTRY.for_surface(surface) for word in command.words()}


def test_gateway_registry_commands_have_gateway_handlers() -> None:
    gateway_words = _handler_words(Surface.CLI_GATEWAY)

    assert EXPECTED_GATEWAY_COMMANDS <= gateway_words
    assert gateway_words == chat_cmd.GATEWAY_SLASH_HANDLER_WORDS
    assert "/elevated" in chat_cmd.GATEWAY_SLASH_HANDLER_WORDS
    assert "/clear" in chat_cmd.GATEWAY_SLASH_HANDLER_WORDS
    assert "/quit" in chat_cmd.GATEWAY_SLASH_HANDLER_WORDS
    assert "/usage" in chat_cmd.GATEWAY_SLASH_HANDLER_WORDS
    assert "/file" in chat_cmd.GATEWAY_SLASH_HANDLER_WORDS


def test_standalone_registry_commands_have_standalone_handlers() -> None:
    standalone_words = _handler_words(Surface.CLI_STANDALONE)

    assert EXPECTED_STANDALONE_COMMANDS <= standalone_words
    assert standalone_words == chat_cmd.STANDALONE_SLASH_HANDLER_WORDS
    assert "/clear" in chat_cmd.STANDALONE_SLASH_HANDLER_WORDS
    assert "/quit" in chat_cmd.STANDALONE_SLASH_HANDLER_WORDS
    assert "/usage" not in chat_cmd.STANDALONE_SLASH_HANDLER_WORDS
    assert "/file" not in chat_cmd.STANDALONE_SLASH_HANDLER_WORDS
    assert "/models" not in chat_cmd.STANDALONE_SLASH_HANDLER_WORDS


def test_usage_is_gateway_only_and_not_standalone_help() -> None:
    assert "/usage" in _handler_words(Surface.CLI_GATEWAY)
    assert "/usage" not in _handler_words(Surface.CLI_STANDALONE)
    assert "/models" in _handler_words(Surface.CLI_GATEWAY)
    assert "/models" not in _handler_words(Surface.CLI_STANDALONE)

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=100, highlight=False)
    console.print(repl_commands.render_help_table(Surface.CLI_STANDALONE))

    assert "/usage" not in buffer.getvalue()
    assert "/models" not in buffer.getvalue()


def test_file_is_gateway_only() -> None:
    assert "/file" in _handler_words(Surface.CLI_GATEWAY)
    assert "/file" not in _handler_words(Surface.CLI_STANDALONE)


def test_interactive_chat_clear_screen_only_on_terminal(monkeypatch) -> None:
    calls: list[str] = []

    class FakeConsole:
        is_terminal = True

        def clear(self) -> None:
            calls.append("clear")

    monkeypatch.setattr(chat_cmd._launch_bridge, "console", FakeConsole())

    chat_cmd._clear_screen_for_interactive_chat()

    assert calls == ["clear"]


def test_interactive_chat_clear_screen_skips_non_terminal(monkeypatch) -> None:
    calls: list[str] = []

    class FakeConsole:
        is_terminal = False

        def clear(self) -> None:
            calls.append("clear")

    monkeypatch.setattr(chat_cmd._launch_bridge, "console", FakeConsole())

    chat_cmd._clear_screen_for_interactive_chat()

    assert calls == []


class TestChatCommand:
    def test_chat_command_builds_typed_launch_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        def fake_launch_chat_command(request, *, overrides=None) -> None:
            captured["request"] = request
            captured["overrides"] = overrides

        monkeypatch.setattr(
            chat_cmd,
            "_launch_chat_command",
            fake_launch_chat_command,
            raising=False,
        )

        chat_cmd.run_chat(
            model="openrouter/test",
            session_id="agent:main:existing",
            standalone=True,
            workspace="repo",
            workspace_strict=True,
            timeout=12.5,
        )

        request = captured["request"]
        assert isinstance(request, chat_cmd._ChatCommandRequest)
        assert request.model == "openrouter/test"
        assert request.session_id == "agent:main:existing"
        assert request.ui is None
        assert request.standalone is True
        assert request.workspace == "repo"
        assert request.workspace_strict is True
        assert request.timeout == 12.5
        overrides = captured["overrides"]
        assert isinstance(overrides, chat_cmd._ChatCommandLaunchOverrides)
        assert overrides.launch_chat is None
        assert overrides.standalone_runner is None
        assert overrides.gateway_runner is None

    def test_chat_help(self) -> None:
        result = runner.invoke(
            app,
            ["chat", "--help"],
            env={"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"},
        )
        assert result.exit_code == 0
        assert "--model" in result.output
        assert "--session" in result.output
        assert "--ui" in result.output
        assert "default: auto" in result.output
        assert "RC default: plain" not in result.output

    def test_chat_invokes_run_chat(self) -> None:
        """Default chat calls run_chat with correct defaults."""
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(app, ["chat"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="",
            session_id="",
            ui=None,
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        )

    def test_chat_model_option_forwarded(self) -> None:
        """--model option is forwarded to run_chat."""
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(app, ["chat", "--model", "ollama/llama3"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="ollama/llama3",
            session_id="",
            ui=None,
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        )

    def test_chat_session_option_forwarded(self) -> None:
        """--session option is forwarded to run_chat."""
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(app, ["chat", "--session", "abc123"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="",
            session_id="abc123",
            ui=None,
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        )

    def test_chat_rejects_unknown_public_ui_mode_before_launch(self) -> None:
        result = runner.invoke(
            app,
            ["chat", "--ui", "bogus"],
            env={
                "COLUMNS": "120",
                "NO_COLOR": "1",
                "TERM": "dumb",
            },
        )

        assert result.exit_code == 2
        assert "Unsupported chat UI" in result.output
        assert "bogus" in result.output
        assert "auto, plain, tui" in result.output

    def test_chat_timeout_option_forwarded(self) -> None:
        """--timeout option is forwarded to run_chat."""
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(app, ["chat", "--timeout", "12.5"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="",
            session_id="",
            ui=None,
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=12.5,
        )

    def test_chat_workspace_options_forwarded(self) -> None:
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(
                app,
                ["chat", "--workspace", "repo", "--workspace-strict"],
            )
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="",
            session_id="",
            ui=None,
            standalone=False,
            workspace="repo",
            workspace_strict=True,
            timeout=None,
        )

    def test_chat_ui_option_forwarded(self) -> None:
        mock_run = MagicMock()
        with patch("opensquilla.cli.chat_cmd.run_chat", mock_run):
            result = runner.invoke(app, ["chat", "--ui", "plain"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            model="",
            session_id="",
            ui="plain",
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        )

    def test_gateway_chat_workspace_options_warn_without_forwarding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        buffer = io.StringIO()
        called: dict[str, object] = {}

        async def fake_gateway_chat(model: str | None, session_id: str | None) -> None:
            called["model"] = model
            called["session_id"] = session_id

        monkeypatch.setattr(
            chat_cmd._launch_bridge.sys,
            "stdin",
            SimpleNamespace(isatty=lambda: True),
        )
        # The real log-quieting swaps the process-global structlog config to an
        # interactive log file; leaking that into later tests poisons every
        # subsequent structlog emit once another test closes the handle.
        monkeypatch.setattr(
            chat_cmd._launch_bridge,
            "quiet_logs_for_interactive_chat",
            lambda: None,
        )
        monkeypatch.setattr(
            chat_cmd._launch_bridge,
            "preflight_gateway_chat_or_exit",
            lambda: None,
        )
        monkeypatch.setattr(
            chat_cmd._launch_bridge,
            "console",
            Console(file=buffer, force_terminal=True, color_system=None, no_color=True),
        )
        monkeypatch.setattr(chat_cmd, "_gateway_chat", fake_gateway_chat)

        chat_cmd.run_chat(
            model="",
            session_id="",
            standalone=False,
            workspace="repo",
            workspace_strict=True,
            timeout=None,
        )

        assert called == {"model": None, "session_id": None}
        output = buffer.getvalue()
        assert "--workspace only affects --standalone chat" in output
        assert "requires the path to be visible to the gateway runtime" in output

    def test_chat_rejects_extra_args(self) -> None:
        """Extra positional args (like 'send Hello') are rejected."""
        result = runner.invoke(app, ["chat", "send", "Hello"])
        assert result.exit_code != 0


@pytest.mark.asyncio
async def test_standalone_repl_delegates_to_runtime_bridge(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_standalone_chat(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        chat_cmd._runtime_bridge,
        "run_standalone_chat",
        fake_run_standalone_chat,
    )

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
    )

    assert captured["model"] == "openrouter/test"
    assert captured["session_id"] == "standalone:test"
    assert captured["workspace"] == "repo"
    assert captured["workspace_strict"] is True
    assert captured["timeout"] == 7.25
    assert "stream_response" not in captured
    assert "image_command_handler" not in captured
    assert "run_concurrent_repl" not in captured
    assert "output_console" not in captured
    assert "error_panel_factory" not in captured


@pytest.mark.asyncio
async def test_gateway_chat_delegates_to_runtime_bridge(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_gateway_chat(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        chat_cmd._runtime_bridge,
        "run_gateway_chat",
        fake_run_gateway_chat,
    )

    await chat_cmd._gateway_chat(
        model="anthropic/claude-sonnet-4",
        session_id="agent:main:resumed-key",
    )

    assert captured["model"] == "anthropic/claude-sonnet-4"
    assert captured["session_id"] == "agent:main:resumed-key"
    assert "stream_response" not in captured
    assert "handle_slash_command" not in captured
    assert "run_concurrent_repl" not in captured
    assert "output_console" not in captured
    assert "error_panel_factory" not in captured


class _FakeSessionManager:
    def __init__(self) -> None:
        self.get_or_create_calls: list[dict[str, str]] = []
        self.compact_calls: list[tuple[str, int, object | None]] = []
        self.truncate_calls: list[tuple[str, int]] = []
        self.transcripts: dict[str, list[object]] = {}

    async def get_or_create(self, session_key: str, agent_id: str = "main") -> object:
        self.get_or_create_calls.append({"session_key": session_key, "agent_id": agent_id})
        return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    async def append_message(self, session_key: str, role: str, content: str) -> object:
        entry = SimpleNamespace(role=role, content=content)
        self.transcripts.setdefault(session_key, []).append(entry)
        return entry

    async def get_transcript(self, session_key: str) -> list[object]:
        return list(self.transcripts.get(session_key, []))

    async def truncate(self, session_key: str, max_messages: int = 0) -> None:
        self.truncate_calls.append((session_key, max_messages))
        if max_messages <= 0:
            self.transcripts[session_key] = []
        else:
            self.transcripts[session_key] = self.transcripts.get(session_key, [])[-max_messages:]

    async def compact(self, session_key: str, context_window_tokens: int, config=None) -> str:
        self.compact_calls.append((session_key, context_window_tokens, config))
        return "summary"


class _LegacyCompactSessionManager(_FakeSessionManager):
    async def compact(self, session_key: str, context_window_tokens: int) -> str:
        self.compact_calls.append((session_key, context_window_tokens, None))
        return "summary"


class _FakeCompactionProvider:
    provider_name = "openai"

    def __init__(self) -> None:
        self._api_key = "cli-provider-key"
        self._model = "provider/model"
        self._base_url = "https://openrouter.ai/api/v1"

    @property
    def model(self) -> str:
        return self._model


class _FakeProviderSelector:
    def __init__(self, provider: _FakeCompactionProvider | None = None) -> None:
        self.provider = provider or _FakeCompactionProvider()

    def clone(self) -> _FakeProviderSelector:
        return self

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _FakeServices:
    def __init__(self) -> None:
        self.memory_sync_managers = {"main": object()}
        self.memory_retrievers = {"main": object()}
        self.turn_capture_services = {"main": object()}
        self.flush_service = None
        self.model_catalog = object()
        self.provider_selector = MagicMock()
        self.tool_registry = None
        self.session_manager = _FakeSessionManager()
        self.skill_loader = None
        self.usage_tracker = None
        self.config = None

    async def close(self) -> None:
        return None


class _DummyLive:
    def __init__(self, *args, **kwargs) -> None:
        return None

    def __enter__(self) -> _DummyLive:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def update(self, *args, **kwargs) -> None:
        return None


class _RecordingRenderer:
    instances: list[_RecordingRenderer] = []

    def __init__(self, *args, **kwargs) -> None:
        self.buffer = ""
        self.pulses = 0
        self.errors: list[str] = []
        self.finalized = False
        self.statuses: list[str] = []
        self.tool_starts: list[tuple[str, object, str | None]] = []
        self.tool_finishes: list[tuple[str | None, bool, object | None]] = []
        _RecordingRenderer.instances.append(self)

    def __enter__(self) -> _RecordingRenderer:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def append_text(self, delta: str) -> None:
        self.buffer += delta

    async def aappend_text(self, delta: str) -> None:
        # Async sibling used by production stream paths; mirror sync logic
        # so existing test assertions against `self.buffer` still hold.
        self.buffer += delta

    def pulse(self) -> None:
        self.pulses += 1

    def tool_call(self, name: str, args=None) -> None:
        # Legacy shim kept for tests that haven't migrated.
        self.tool_starts.append((name, args, None))

    def tool_start(self, name: str, args=None, tool_use_id: str | None = None) -> None:
        self.tool_starts.append((name, args, tool_use_id))

    def tool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
        result: object | None = None,
    ) -> None:
        self.tool_finishes.append((tool_use_id, success, result))

    def status(self, message: str, **_kwargs) -> None:
        self.statuses.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def finalize(
        self,
        usage=None,
        *,
        cancelled: bool = False,
    ) -> None:
        self.finalized = True


@pytest.mark.asyncio
async def test_standalone_repl_forwards_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}
    inputs = iter(["hello", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            captured["message"] = message
            captured["session_key"] = session_key
            captured["timeout"] = kwargs.get("timeout")
            captured["tool_context"] = kwargs["tool_context"]
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return _FakeServices()

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        timeout=7.25,
    )

    assert captured["message"] == "hello"
    assert captured["session_key"] == "standalone:test"
    assert captured["timeout"] == 7.25
    assert captured["tool_context"].channel_kind == "cli"
    assert captured["tool_context"].channel_id == "cli:chat"
    assert captured["tool_context"].sender_id


@pytest.mark.asyncio
async def test_standalone_chat_uses_workspace_in_tool_context(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    inputs = iter(["hello", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            captured["tool_context"] = kwargs["tool_context"]
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return _FakeServices()

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        workspace=str(tmp_path),
        workspace_strict=True,
    )

    tool_context = captured["tool_context"]
    assert tool_context.workspace_dir == str(tmp_path)
    assert tool_context.workspace_strict is True


@pytest.mark.asyncio
async def test_standalone_path_command_runs_as_plain_message(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "large.log"
    target.write_text("hello\n", encoding="utf-8")
    captured: dict[str, object] = {}
    inputs = iter([f"/path {target} inspect", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            captured["message"] = message
            captured["kwargs"] = kwargs
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return _FakeServices()

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
    )

    assert "inspect" in captured["message"]
    assert str(target.resolve(strict=False)) in captured["message"]
    assert "attachments" not in captured["kwargs"]


def test_chat_workspace_strict_resolution_matches_agent_precedence(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.cli.agent_cmd import _resolve_workspace_strict

    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_STRICT", "false")
    assert (
        _resolve_workspace_strict(
            cli_value=True,
            config_value=False,
            entrypoint_default=bool(tmp_path),
        )
        is True
    )
    assert (
        _resolve_workspace_strict(
            cli_value=None,
            config_value=True,
            entrypoint_default=bool(tmp_path),
        )
        is False
    )
    monkeypatch.delenv("OPENSQUILLA_WORKSPACE_STRICT")
    assert (
        _resolve_workspace_strict(
            cli_value=None,
            config_value=True,
            entrypoint_default=False,
        )
        is True
    )
    assert (
        _resolve_workspace_strict(
            cli_value=None,
            config_value=None,
            entrypoint_default=True,
        )
        is True
    )


@pytest.mark.asyncio
async def test_standalone_repl_wires_memory_services_into_turnrunner(monkeypatch) -> None:
    services = _FakeServices()
    captured: dict[str, object] = {}
    inputs = iter(["/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        timeout=7.25,
    )

    assert captured["memory_sync_managers"] is services.memory_sync_managers
    assert captured["memory_retrievers"] is services.memory_retrievers
    assert captured["turn_capture_services"] is services.turn_capture_services
    assert captured["session_flush_service"] is services.flush_service
    assert captured["model_catalog"] is services.model_catalog


@pytest.mark.asyncio
async def test_standalone_turnrunner_stream_uses_heartbeat_wrapper(monkeypatch) -> None:
    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            await asyncio.sleep(0.03)
            yield TextDeltaEvent(text="ok")
            yield DoneEvent()

    _RecordingRenderer.instances.clear()
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.renderer.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.turn_stream_defaults.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    # Renderer choice is now backend-aware; pin the OpenTUI backend so the
    # recording renderer (patched above) is the one the bridge selects.
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.runtime_bridge.validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    svc = SimpleNamespace(
        config=SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=0.01,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        session_manager=_FakeSessionManager(),
    )
    tool_ctx = ToolContext(caller_kind=CallerKind.CLI, channel_kind="cli", channel_id="cli:chat")

    result = await chat_cmd._stream_response_turnrunner(
        FakeTurnRunner(),
        "agent:main:standalone:test",
        tool_ctx,
        "hello",
        svc=svc,
    )

    renderer = _RecordingRenderer.instances[-1]
    assert result.text == "ok"
    assert renderer.pulses >= 1
    assert renderer.finalized is True


@pytest.mark.asyncio
async def test_standalone_turnrunner_stream_collects_artifacts(monkeypatch) -> None:
    artifact = {
        "id": "art-chat",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 4,
        "sha256": "e" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:standalone:test",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-chat?sessionKey=agent%3Amain%3Astandalone%3Atest",
        "store": "artifacts",
    }

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ArtifactEvent(**artifact)
            yield TextDeltaEvent(text="ok")
            yield DoneEvent()

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.renderer.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.turn_stream_defaults.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    # Renderer choice is now backend-aware; pin the OpenTUI backend so the
    # recording renderer (patched above) is the one the bridge selects.
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.runtime_bridge.validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    svc = SimpleNamespace(
        config=SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        session_manager=_FakeSessionManager(),
    )
    tool_ctx = ToolContext(caller_kind=CallerKind.CLI, channel_kind="cli", channel_id="cli:chat")

    result = await chat_cmd._stream_response_turnrunner(
        FakeTurnRunner(),
        "agent:main:standalone:test",
        tool_ctx,
        "hello",
        svc=svc,
    )

    assert result.text == "ok"
    assert result.artifacts[0]["download_url"] == "/api/v1/artifacts/art-chat"
    assert "session_key" not in result.artifacts[0]
    assert "sessionKey" not in json.dumps(result.artifacts[0])


@pytest.mark.asyncio
async def test_standalone_turnrunner_wires_tool_strip(monkeypatch) -> None:
    """Tool events must drive ``tool_start``/``tool_finished`` on the renderer.

    Without this wiring the ToolCallStrip pairing/coalescing never runs against
    real traffic and the 12-call ``execute_code`` flood reappears.
    """

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ToolUseStartEvent(tool_use_id="call-1", tool_name="execute_code")
            yield ToolResultEvent(
                tool_use_id="call-1",
                tool_name="execute_code",
                result="2",
                is_error=False,
            )
            yield ToolUseStartEvent(tool_use_id="call-2", tool_name="execute_code")
            yield ToolResultEvent(
                tool_use_id="call-2",
                tool_name="execute_code",
                result="boom",
                is_error=True,
            )
            yield ToolUseStartEvent(tool_use_id="call-3", tool_name="execute_code")
            yield ToolResultEvent(
                tool_use_id="call-3",
                tool_name="execute_code",
                result="approval pending",
                is_error=False,
                execution_status={
                    "version": 1,
                    "status": "unknown",
                    "exit_code": None,
                    "timed_out": False,
                    "truncated": False,
                    "reason": "approval_pending",
                    "source": "tool_runtime",
                    "preservation_class": "ephemeral",
                },
            )
            yield DoneEvent()

    _RecordingRenderer.instances.clear()
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.renderer.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.turn_stream_defaults.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    # Renderer choice is now backend-aware; pin the OpenTUI backend so the
    # recording renderer (patched above) is the one the bridge selects.
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.runtime_bridge.validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    svc = SimpleNamespace(
        config=SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        session_manager=_FakeSessionManager(),
    )
    tool_ctx = ToolContext(caller_kind=CallerKind.CLI, channel_kind="cli", channel_id="cli:chat")

    await chat_cmd._stream_response_turnrunner(
        FakeTurnRunner(),
        "agent:main:standalone:test",
        tool_ctx,
        "hello",
        svc=svc,
    )

    renderer = _RecordingRenderer.instances[-1]
    assert renderer.tool_starts == [
        ("execute_code", None, "call-1"),
        ("execute_code", None, "call-2"),
        ("execute_code", None, "call-3"),
    ]
    assert renderer.tool_finishes == [
        ("call-1", True, "2"),
        ("call-2", False, "boom"),
        ("call-3", False, "approval pending"),
    ]


@pytest.mark.asyncio
async def test_standalone_repl_uses_exact_slash_tokens(monkeypatch) -> None:
    services = _FakeServices()
    inputs = iter(["/newer", "/models", "/quit"])
    run_calls: list[str] = []

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            run_calls.append(message)
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        timeout=7.25,
    )

    assert services.session_manager.get_or_create_calls == [
        {"session_key": "standalone:test", "agent_id": "main"}
    ]
    assert run_calls == []


@pytest.mark.asyncio
async def test_standalone_slash_compact_passes_provider_config(monkeypatch) -> None:
    services = _FakeServices()
    services.provider_selector = _FakeProviderSelector()
    services.config = SimpleNamespace(
        context_budget_tokens=1234,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    inputs = iter(["/compact", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        timeout=7.25,
    )

    assert len(services.session_manager.compact_calls) == 1
    session_key, context_window, config = services.session_manager.compact_calls[0]
    assert session_key == "standalone:test"
    assert context_window == 1234
    assert isinstance(config, CompactionConfig)
    assert config.api_key == "cli-provider-key"
    assert config.model == "openrouter/test"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.timeout_seconds == 12.5


@pytest.mark.asyncio
async def test_standalone_reset_refuses_non_empty_transcript_without_flush_service(
    monkeypatch,
) -> None:
    services = _FakeServices()
    services.flush_service = None
    session_key = "standalone:test"
    services.session_manager.transcripts[session_key] = [
        SimpleNamespace(role="user", content="persisted")
    ]
    inputs = iter(["/reset", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(model="openrouter/test", session_id=session_key)

    assert services.session_manager.truncate_calls == []
    assert await services.session_manager.get_transcript(session_key)


@pytest.mark.asyncio
async def test_standalone_compact_missing_flush_service_does_not_block_compaction(
    monkeypatch,
) -> None:
    services = _FakeServices()
    services.flush_service = None
    session_key = "standalone:test"
    services.session_manager.transcripts[session_key] = [
        SimpleNamespace(role="user", content="persisted")
    ]
    services.provider_selector = _FakeProviderSelector()
    services.config = SimpleNamespace(
        context_budget_tokens=1234,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    inputs = iter(["/compact", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(model="openrouter/test", session_id=session_key)

    assert len(services.session_manager.compact_calls) == 1


class _FakeFlushService:
    def __init__(self, receipt: object | None = None, error: Exception | None = None) -> None:
        self.receipt = receipt or SimpleNamespace(
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
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def execute(self, transcript: object, session_key: str, **kwargs) -> object:
        self.calls.append({"transcript": transcript, "session_key": session_key, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.receipt


@pytest.mark.asyncio
async def test_standalone_compact_flushes_before_compacting(monkeypatch) -> None:
    services = _FakeServices()
    session_key = "standalone:test"
    services.session_manager.transcripts[session_key] = [
        SimpleNamespace(role="user", content="persisted")
    ]
    services.flush_service = _FakeFlushService()
    services.provider_selector = _FakeProviderSelector()
    services.config = SimpleNamespace(
        context_budget_tokens=1234,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    inputs = iter(["/compact", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(model="openrouter/test", session_id=session_key)

    assert len(services.flush_service.calls) == 1
    assert services.flush_service.calls[0]["session_key"] == session_key
    assert services.flush_service.calls[0]["kwargs"]["message_window"] == 0
    assert services.flush_service.calls[0]["kwargs"]["segment_mode"] == "auto"
    assert len(services.session_manager.compact_calls) == 1


@pytest.mark.asyncio
async def test_standalone_compact_continues_when_flush_fails(monkeypatch) -> None:
    services = _FakeServices()
    session_key = "standalone:test"
    services.session_manager.transcripts[session_key] = [
        SimpleNamespace(role="user", content="persisted")
    ]
    services.flush_service = _FakeFlushService(
        receipt=SimpleNamespace(mode="error", error="provider down")
    )
    services.provider_selector = _FakeProviderSelector()
    services.config = SimpleNamespace(
        context_budget_tokens=1234,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    inputs = iter(["/compact", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(model="openrouter/test", session_id=session_key)

    assert len(services.flush_service.calls) == 1
    assert len(services.session_manager.compact_calls) == 1


@pytest.mark.asyncio
async def test_standalone_slash_compact_keeps_legacy_compact_manager_compatible(
    monkeypatch,
) -> None:
    services = _FakeServices()
    services.session_manager = _LegacyCompactSessionManager()
    services.provider_selector = _FakeProviderSelector()
    services.config = SimpleNamespace(
        context_budget_tokens=1234,
        compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=12.5),
    )
    inputs = iter(["/compact", "/quit"])

    class FakeTurnRunner:
        def __init__(self, **kwargs) -> None:
            return None

        async def run(self, message: str, session_key: str, **kwargs):
            yield DoneEvent()

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._standalone_repl(
        model="openrouter/test",
        session_id="standalone:test",
        timeout=7.25,
    )

    assert services.session_manager.compact_calls == [("standalone:test", 1234, None)]


# ---------------------------------------------------------------------------
# Gateway-mode flag forwarding
# ---------------------------------------------------------------------------


class _FakeGatewayClient:
    """Fake GatewayClient that records create/send calls and feeds the REPL exit.

    Patched in place of the real `GatewayClient` class so `_stream_response_gateway`'s
    ``isinstance(client, GatewayClient)`` assertion passes. Each instance registers
    itself in a class-level ``instances`` list so tests can grab the one created
    by the function-under-test.
    """

    instances: list[_FakeGatewayClient]

    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.send_calls: list[dict[str, object]] = []
        self.resolve_calls: list[str] = []
        self.bootstrap_calls: list[dict[str, object]] = []
        self.delete_calls: list[list[str]] = []
        self.history_calls: list[dict[str, object]] = []
        self.abort_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.compact_calls: list[dict[str, object]] = []
        self.config_get_calls: list[str | None] = []
        self.config_patch_safe_calls: list[dict[str, object]] = []
        self.config_values: dict[str, object] = {}
        self.usage_status_calls = 0
        self.list_models_calls = 0
        self.delete_result: dict[str, object] = {"deleted": [], "errors": []}
        self.resolved_payload: dict[str, object] = {
            "session_key": "agent:main:resolved",
            "model": "openai/test",
        }
        self.connected = False
        self.closed = False
        type(self).instances.append(self)

    async def connect(self, url: str, *, token: str | None = None) -> None:
        self.connected = True

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str:
        self.create_calls.append(
            {"agent_id": agent_id, "model": model, "display_name": display_name}
        )
        return "agent:main:fake12345"

    async def resolve_session(self, key: str) -> dict[str, object]:
        self.resolve_calls.append(key)
        return self.resolved_payload

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, object]:
        self.bootstrap_calls.append({"key": key, "limit": limit})
        return {
            "session": {
                "session_key": key,
                "model": self.resolved_payload.get("model"),
            },
            "history": {
                "messages": [],
                "history_scope": "complete",
                "loaded_count": 0,
                "canonical_available": True,
            },
        }

    async def delete_sessions(self, keys: list[str]) -> dict[str, object]:
        self.delete_calls.append(keys)
        return self.delete_result

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, object]:
        self.history_calls.append(
            {
                "session_key": session_key,
                "limit": limit,
                "before": before,
                "after": after,
                "include_canonical": include_canonical,
                "include_summaries": include_summaries,
            }
        )
        return {
            "messages": [
                {"role": "user", "text": "persisted hello"},
                {"role": "assistant", "text": "persisted reply"},
            ],
            "has_more": False,
        }

    async def list_models(self) -> list[dict[str, object]]:
        self.list_models_calls += 1
        return [{"id": "openai/test", "provider": "openai"}]

    async def usage_status(self) -> dict[str, object]:
        self.usage_status_calls += 1
        return {"totalTokens": 1234, "totalCostUsd": 0.05678}

    async def abort_session(self, session_key: str) -> dict[str, object]:
        self.abort_calls.append(session_key)
        return {"aborted": True, "key": session_key}

    async def reset_session(self, session_key: str) -> dict[str, object]:
        self.reset_calls.append(session_key)
        return {"reset": True, "key": session_key}

    async def compact_session(self, session_key: str) -> dict[str, object]:
        self.compact_calls.append({"session_key": session_key})
        return {
            "key": session_key,
            "compacted": True,
            "mode": "summary",
            "summary_len": 37,
        }

    async def get_config(self, path: str | None = None) -> object:
        self.config_get_calls.append(path)
        if path is None:
            return dict(self.config_values)
        return self.config_values.get(path)

    async def patch_config_safe(self, patches: dict[str, object]) -> dict[str, object]:
        self.config_patch_safe_calls.append(dict(patches))
        self.config_values.update(patches)
        return {"patched": list(patches)}

    async def send_message(self, session_key, message, attachments=None, elevated=None):
        self.send_calls.append(
            {
                "session_key": session_key,
                "message": message,
                "attachments": attachments,
                "elevated": elevated,
            }
        )
        # Drain immediately — REPL loop sees no events and proceeds to next prompt.
        if False:
            yield {}

    async def close(self) -> None:
        self.closed = True


_FakeGatewayClient.instances = []


@pytest.mark.asyncio
async def test_gateway_chat_forwards_model_to_create_session(monkeypatch) -> None:
    """`opensquilla chat --model X` (gateway mode) must reach create_session(model='X')."""
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    inputs = iter(["/quit"])

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._gateway_chat(model="anthropic/claude-sonnet-4", session_id=None)

    assert len(_FakeGatewayClient.instances) == 1
    fake = _FakeGatewayClient.instances[-1]
    assert fake.connected is True
    assert fake.closed is True
    assert fake.create_calls == [
        {
            "agent_id": "main",
            "model": "anthropic/claude-sonnet-4",
            "display_name": None,
        }
    ]
    assert fake.send_calls == []  # /quit on first prompt — no message sent


@pytest.mark.asyncio
async def test_gateway_chat_session_id_skips_create_session(monkeypatch) -> None:
    """`opensquilla chat --session abc` (gateway mode) must reuse the key without create."""
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    inputs = iter(["hi", "/quit"])

    async def fake_prompt_user(prefix: str = "[you] ", **kwargs):
        return next(inputs)

    _install_fake_inputs(monkeypatch, inputs)

    await chat_cmd._gateway_chat(model=None, session_id="agent:main:resumed-key")

    fake = _FakeGatewayClient.instances[-1]
    assert fake.create_calls == []  # MUST NOT create
    assert len(fake.send_calls) == 1
    assert fake.send_calls[0]["session_key"] == "agent:main:resumed-key"
    assert fake.send_calls[0]["message"] == "hi"


@pytest.mark.asyncio
async def test_gateway_slash_new_passes_title_as_display_name(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:old", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command(
        "/new Research Notes", state, fake, {"mode": None}
    )

    assert handled is True
    assert fake.create_calls == [
        {
            "agent_id": "main",
            "model": "openai/test",
            "display_name": "Research Notes",
        }
    ]
    assert state.session_key == "agent:main:fake12345"


@pytest.mark.asyncio
async def test_gateway_path_command_sends_prompt_without_attachments_or_upload(
    monkeypatch,
    tmp_path,
) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    fake.is_local_gateway = True

    async def fail_upload_file(*args, **kwargs):
        raise AssertionError("upload_file must not be called for /path")

    fake.upload_file = fail_upload_file
    target = tmp_path / "large.log"
    target.write_text("hello\n", encoding="utf-8")
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command(
        f"/path {target} summarize", state, fake, {"mode": None}
    )

    assert handled is True
    assert len(fake.send_calls) == 1
    assert fake.send_calls[0]["attachments"] == []
    assert "summarize" in fake.send_calls[0]["message"]
    assert str(target.resolve(strict=False)) in fake.send_calls[0]["message"]


@pytest.mark.asyncio
async def test_gateway_path_command_remote_rejects_before_send(
    monkeypatch,
    tmp_path,
) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    fake.is_local_gateway = False
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")
    buffer = io.StringIO()
    monkeypatch.setattr(
        slash_bridge,
        "console",
        Console(file=buffer, force_terminal=False, width=100, highlight=False),
    )
    nonexistent = tmp_path / "does-not-exist.log"

    handled = await chat_cmd._handle_gateway_slash_command(
        f"/path {nonexistent} inspect", state, fake, {"mode": None}
    )

    assert handled is True
    assert fake.send_calls == []
    assert "Use /file to upload from this CLI machine" in buffer.getvalue()
    assert "File not found" not in buffer.getvalue()


@pytest.mark.asyncio
async def test_gateway_chat_does_not_forward_workspace_fields() -> None:
    from opensquilla.cli.gateway_client import GatewayClient

    client = GatewayClient()
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    client._recv_queue.put_nowait({"event": "session.event.done", "payload": {}})

    events = [
        event
        async for event in client.send_message(
            "agent:main:abc123",
            "hello",
            attachments=[],
        )
    ]

    assert events[-1]["event"] == "session.event.done"
    method, params = calls[1]
    assert method == "sessions.send"
    source = params["_source"]
    assert "workspace_dir" not in source
    assert "workspace_strict" not in source


@pytest.mark.asyncio
async def test_gateway_client_resolve_approval_forwards_choice() -> None:
    from opensquilla.cli.gateway_client import GatewayClient

    client = GatewayClient()
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"status": "resolved"}

    client._call = fake_call  # type: ignore[method-assign]

    result = await client.resolve_approval(
        "approval-1",
        True,
        choice="allow_same_type",
    )

    assert result == {"status": "resolved"}
    assert calls == [
        (
            "exec.approval.resolve",
            {
                "id": "approval-1",
                "approved": True,
                "choice": "allow_same_type",
            },
        )
    ]


@pytest.mark.asyncio
async def test_gateway_client_follows_background_task_group_until_terminal() -> None:
    from opensquilla.cli.gateway_client import GatewayClient

    client = GatewayClient()
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    group_id = "subagent:agent:main:abc123:task-parent"
    for frame in (
        {"event": "session.event.task_group.waiting", "payload": {"group_id": group_id}},
        {"event": "session.event.done", "payload": {"reason": "parent_yielded"}},
        {
            "event": "session.event.task_group.synthesizing",
            "payload": {"group_id": group_id, "synthesis_task_id": "task-synth"},
        },
        {"event": "session.event.done", "payload": {"reason": "synthesis_done"}},
        {"event": "task.succeeded", "payload": {"task_id": "task-synth"}},
        {
            "event": "session.event.task_group.done",
            "payload": {"group_id": group_id, "delivery_status": "not_applicable"},
        },
    ):
        client._recv_queue.put_nowait(frame)

    events = [
        event
        async for event in client.send_message(
            "agent:main:abc123",
            "hello",
            attachments=[],
        )
    ]

    assert [event["event"] for event in events] == [
        "session.event.task_group.waiting",
        "session.event.done",
        "session.event.task_group.synthesizing",
        "session.event.done",
        "task.succeeded",
        "session.event.task_group.done",
    ]
    assert events[-1]["delivery_status"] == "not_applicable"
    assert calls[0][0] == "sessions.messages.subscribe"
    assert calls[1][0] == "sessions.send"


@pytest.mark.asyncio
async def test_gateway_client_does_not_wait_for_late_task_group_after_done() -> None:
    from opensquilla.cli.gateway_client import GatewayClient

    client = GatewayClient()

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    client._recv_queue.put_nowait({"event": "session.event.done", "payload": {}})
    client._recv_queue.put_nowait(
        {
            "event": "session.event.task_group.synthesizing",
            "payload": {"group_id": "late-group"},
        }
    )

    events = [
        event
        async for event in client.send_message(
            "agent:main:abc123",
            "hello",
            attachments=[],
        )
    ]

    assert [event["event"] for event in events] == ["session.event.done"]


@pytest.mark.asyncio
async def test_gateway_client_does_not_end_on_untracked_task_group_terminal() -> None:
    from opensquilla.cli.gateway_client import GatewayClient

    client = GatewayClient()

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    client._recv_queue.put_nowait(
        {
            "event": "session.event.task_group.done",
            "payload": {"group_id": "untracked-group", "delivery_status": "not_applicable"},
        }
    )
    client._recv_queue.put_nowait({"event": "session.event.done", "payload": {}})

    events = [
        event
        async for event in client.send_message(
            "agent:main:abc123",
            "hello",
            attachments=[],
        )
    ]

    assert [event["event"] for event in events] == [
        "session.event.task_group.done",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_gateway_slash_clear_resets_session_state(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")
    state.transcript.add("user", "hello")

    handled = await chat_cmd._handle_gateway_slash_command("/clear", state, fake, {"mode": None})

    assert handled is True
    assert fake.reset_calls == ["agent:main:abc123"]
    assert state.transcript.turns == []


@pytest.mark.asyncio
async def test_gateway_slash_compact_calls_session_rpc(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command("/compact", state, fake, {"mode": None})

    assert handled is True
    assert fake.compact_calls == [{"session_key": "agent:main:abc123"}]


@pytest.mark.asyncio
async def test_gateway_slash_compact_skipped_uses_context_budget_wording(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()

    async def compact_skipped(session_key: str) -> dict[str, object]:
        fake.compact_calls.append({"session_key": session_key})
        return {"key": session_key, "compacted": False}

    fake.compact_session = compact_skipped
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")
    buffer = io.StringIO()
    monkeypatch.setattr(
        slash_bridge,
        "console",
        Console(file=buffer, force_terminal=False, width=100, highlight=False),
    )

    handled = await chat_cmd._handle_gateway_slash_command("/compact", state, fake, {"mode": None})

    assert handled is True
    assert fake.compact_calls == [{"session_key": "agent:main:abc123"}]
    output = buffer.getvalue()
    assert "compact skipped" in output
    assert "already within context budget; no compact was applied" in output


@pytest.mark.asyncio
async def test_gateway_slash_compact_reports_started_and_failure(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()

    async def compact_failed(session_key: str) -> dict[str, object]:
        fake.compact_calls.append({"session_key": session_key})
        raise RuntimeError("provider down")

    fake.compact_session = compact_failed
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")
    buffer = io.StringIO()
    monkeypatch.setattr(
        slash_bridge,
        "console",
        Console(file=buffer, force_terminal=False, width=100, highlight=False),
    )

    handled = await chat_cmd._handle_gateway_slash_command("/compact", state, fake, {"mode": None})

    assert handled is True
    assert fake.compact_calls == [{"session_key": "agent:main:abc123"}]
    output = buffer.getvalue()
    assert "compacting context" in output
    assert "compact failed: provider down" in output


@pytest.mark.asyncio
async def test_gateway_slash_delete_resolves_and_reports_errors(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    fake.resolved_payload = {"session_key": "agent:main:abc123"}
    fake.delete_result = {"deleted": [], "errors": ["agent:main:abc123: locked"]}
    state = ChatSessionState(session_key="agent:main:current", model="openai/test")
    buffer = io.StringIO()
    monkeypatch.setattr(
        slash_bridge,
        "console",
        Console(file=buffer, force_terminal=False, width=100, highlight=False),
    )

    handled = await chat_cmd._handle_gateway_slash_command(
        "/delete abc", state, fake, {"mode": None}
    )

    assert handled is True
    assert fake.resolve_calls == ["abc"]
    assert fake.delete_calls == [["agent:main:abc123"]]
    output = buffer.getvalue()
    assert "Delete failed" in output
    assert "locked" in output


@pytest.mark.asyncio
async def test_gateway_slash_save_exports_persisted_history(monkeypatch, tmp_path) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")
    output = tmp_path / "saved.md"

    handled = await chat_cmd._handle_gateway_slash_command(
        f"/save {output}", state, fake, {"mode": None}
    )

    assert handled is True
    assert fake.history_calls == [
        {
            "session_key": "agent:main:abc123",
            "limit": 200,
            "before": None,
            "after": None,
            "include_canonical": True,
            "include_summaries": False,
        }
    ]
    text = output.read_text(encoding="utf-8")
    assert "## You" in text
    assert "persisted hello" in text
    assert "## Assistant" in text
    assert "persisted reply" in text


@pytest.mark.asyncio
async def test_gateway_slash_models_does_not_hit_model_prefix(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command("/models", state, fake, {"mode": None})

    assert handled is True
    assert fake.list_models_calls == 1
    assert state.model == "openai/test"


@pytest.mark.asyncio
async def test_gateway_slash_usage_calls_usage_status(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command("/usage", state, fake, {"mode": None})

    assert handled is True
    assert fake.usage_status_calls == 1


@pytest.mark.asyncio
async def test_gateway_slash_unknown_prefix_is_not_handled(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command("/newer", state, fake, {"mode": None})

    assert handled is False
    assert fake.create_calls == []
    assert state.session_key == "agent:main:abc123"


@pytest.mark.asyncio
async def test_gateway_stream_keyboard_interrupt_aborts_turn(monkeypatch) -> None:
    class InterruptingGatewayClient(_FakeGatewayClient):
        async def send_message(self, session_key, message, attachments=None, elevated=None):
            self.send_calls.append(
                {
                    "session_key": session_key,
                    "message": message,
                    "attachments": attachments,
                    "elevated": elevated,
                }
            )
            raise KeyboardInterrupt
            yield {}

    InterruptingGatewayClient.instances = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", InterruptingGatewayClient)
    fake = InterruptingGatewayClient()

    result = await chat_cmd._stream_response_gateway(
        fake,
        "agent:main:abc123",
        "hello",
        {"mode": None},
    )

    assert result.cancelled is True
    assert fake.abort_calls == ["agent:main:abc123"]
    assert fake.send_calls[0]["message"] == "hello"


@pytest.mark.asyncio
async def test_gateway_stream_cancelled_error_aborts_turn(monkeypatch) -> None:
    class CancelledGatewayClient(_FakeGatewayClient):
        async def send_message(self, session_key, message, attachments=None, elevated=None):
            self.send_calls.append(
                {
                    "session_key": session_key,
                    "message": message,
                    "attachments": attachments,
                    "elevated": elevated,
                }
            )
            raise asyncio.CancelledError
            yield {}

    CancelledGatewayClient.instances = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", CancelledGatewayClient)
    fake = CancelledGatewayClient()

    result = await chat_cmd._stream_response_gateway(
        fake,
        "agent:main:abc123",
        "hello",
        {"mode": None},
    )

    assert result.cancelled is True
    assert fake.abort_calls == ["agent:main:abc123"]
    assert fake.send_calls[0]["message"] == "hello"


@pytest.mark.asyncio
async def test_gateway_stream_renders_task_group_status_without_buffer_pollution(
    monkeypatch,
) -> None:
    class StatusGatewayClient(_FakeGatewayClient):
        async def send_message(self, session_key, message, attachments=None, elevated=None):
            yield {
                "event": "session.event.task_group.waiting",
                "group_id": "group-1",
                "pending_count": 2,
            }
            yield {
                "event": "session.event.task_group.synthesizing",
                "group_id": "group-1",
                "child_count": 2,
            }
            yield {"event": "session.event.text_delta", "text": "answer"}
            yield {
                "event": "session.event.task_group.done",
                "group_id": "group-1",
                "delivery_status": "not_applicable",
            }
            yield {"event": "session.event.done"}

    class RecordingRenderer:
        instances: list[RecordingRenderer] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.buffer = ""
            self.statuses: list[str] = []
            self.finalized = False
            RecordingRenderer.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def append_text(self, delta: str) -> None:
            self.buffer += delta

        async def aappend_text(self, delta: str) -> None:
            # Async sibling used by production stream paths.
            self.buffer += delta

        def status(self, message: str, **_kwargs) -> None:
            self.statuses.append(message)

        def tool_call(self, *_args, **_kwargs) -> None:
            return None

        def error(self, message: str) -> None:
            raise AssertionError(f"unexpected error render: {message}")

        def finalize(self, *_args, **_kwargs) -> None:
            self.finalized = True

    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.renderer.OpenTuiStreamRenderer",
        RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.turn_stream_defaults.OpenTuiStreamRenderer",
        RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.runtime_bridge.validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    fake = StatusGatewayClient()

    result = await chat_cmd._stream_response_gateway(
        fake,
        "agent:main:abc123",
        "hello",
        {"mode": None},
    )

    renderer = RecordingRenderer.instances[-1]
    assert result.text == "answer"
    assert renderer.buffer == "answer"
    assert renderer.finalized is True
    assert len(renderer.statuses) == 3
    assert "waiting" in renderer.statuses[0]
    assert "synthesizing" in renderer.statuses[1]
    assert "complete" in renderer.statuses[2]


@pytest.mark.asyncio
async def test_gateway_stream_collects_artifact_events(monkeypatch) -> None:
    artifact = {
        "id": "art-chat",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 4,
        "sha256": "e" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:abc123",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-chat?sessionKey=agent%3Amain%3Aabc123",
    }

    class ArtifactGatewayClient(_FakeGatewayClient):
        async def send_message(self, session_key, message, attachments=None, elevated=None):
            yield {"event": "session.event.artifact", **artifact}
            yield {"event": "session.event.text_delta", "text": "answer"}
            yield {"event": "session.event.done"}

    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.renderer.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.turn_stream_defaults.OpenTuiStreamRenderer",
        _RecordingRenderer,
    )
    # Renderer choice is now backend-aware; pin the OpenTUI backend so the
    # recording renderer (patched above) is the one the bridge selects.
    monkeypatch.setattr(
        "opensquilla.cli.tui.adapters.runtime_bridge.validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    result = await chat_cmd._stream_response_gateway(
        ArtifactGatewayClient(),
        "agent:main:abc123",
        "hello",
        {"mode": None},
    )

    assert result.text == "answer"
    assert result.artifacts[0]["download_url"] == "/api/v1/artifacts/art-chat"
    assert "session_key" not in result.artifacts[0]
    assert "sessionKey" not in json.dumps(result.artifacts[0])


@pytest.mark.asyncio
async def test_gateway_elevated_unknown_prefix_is_not_handled(monkeypatch) -> None:
    _FakeGatewayClient.instances.clear()
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    fake = _FakeGatewayClient()
    state = ChatSessionState(session_key="agent:main:abc123", model="openai/test")

    handled = await chat_cmd._handle_gateway_slash_command(
        "/elevatedx", state, fake, {"mode": None}
    )

    assert handled is False
