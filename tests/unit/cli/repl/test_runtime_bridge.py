from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer
from rich.console import Console
from rich.panel import Panel

from opensquilla.cli.repl import gateway_runtime, standalone_runtime
from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult
from opensquilla.engine.commands import Surface

REMOVED_TEXT_BACKEND = "text" + "ual"
REMOVED_BACKEND_IDS = ["terminal", REMOVED_TEXT_BACKEND, f"live-{REMOVED_TEXT_BACKEND}"]


async def _fake_gateway_stream(*args: Any, **kwargs: Any) -> TurnResult:
    return TurnResult(text="gateway")


async def _fake_gateway_slash(*args: Any, **kwargs: Any) -> bool:
    return True


async def _fake_standalone_stream(*args: Any, **kwargs: Any) -> TurnResult:
    return TurnResult(text="standalone")


def _fake_error_panel(message: str, *, title: str = "Error") -> Panel:
    return Panel(message, title=title)


class _FakeStreamOutput:
    async def __aenter__(self):
        return lambda _payload: None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeOutputHandle:
    approval_surface = Surface.CLI_GATEWAY

    async def write_through(self, payload: str) -> None:
        return None

    def stream_output(self) -> _FakeStreamOutput:
        return _FakeStreamOutput()

    def set_toolbar(self, key: str, value: object | None) -> None:
        return None

    def invalidate(self) -> None:
        return None


class _FakeTuiSurface:
    output_handle = _FakeOutputHandle()

    async def next_line(self) -> str | None:
        return None

    def set_cancel_callback(self, cb) -> None:
        return None

    def set_shutdown_callback(self, cb) -> None:
        return None

    def emit_eof(self) -> None:
        return None

    async def write_through(self, payload: str) -> None:
        return None

    @property
    def redraw_callback(self):
        return lambda: None


class _RecordingConsole:
    def __init__(self) -> None:
        self.printed: list[object] = []

    def print(self, value: object) -> None:
        self.printed.append(value)


def test_gateway_runtime_notifier_maps_all_notice_kinds() -> None:
    from opensquilla.cli.repl import runtime_bridge

    output_console = _RecordingConsole()
    notify = runtime_bridge._gateway_runtime_notifier(
        output_console,
        _fake_error_panel,
    )

    for notice in (
        gateway_runtime.GatewayRuntimeNotice(
            kind="created",
            session_key="agent:main:new",
        ),
        gateway_runtime.GatewayRuntimeNotice(
            kind="resumed",
            session_key="agent:main:old",
        ),
        gateway_runtime.GatewayRuntimeNotice(kind="resume_model_ignored"),
        gateway_runtime.GatewayRuntimeNotice(kind="model", model="openai/test"),
        gateway_runtime.GatewayRuntimeNotice(kind="welcome"),
        gateway_runtime.GatewayRuntimeNotice(kind="goodbye"),
        gateway_runtime.GatewayRuntimeNotice(kind="unknown_command"),
        gateway_runtime.GatewayRuntimeNotice(kind="queued_behind_external"),
        gateway_runtime.GatewayRuntimeNotice(kind="error", message="boom"),
    ):
        notify(notice)

    assert output_console.printed[0] == ("[dim]Connected to gateway. Session: agent:main:new[/dim]")
    assert output_console.printed[1] == (
        "[dim]Connected to gateway. Resuming session: agent:main:old[/dim]"
    )
    assert output_console.printed[2] == (
        "[yellow]Note: --model is honored only at session creation; ignored "
        "when resuming a session.[/yellow]"
    )
    assert output_console.printed[3] == "[dim]Model: openai/test[/dim]"
    assert isinstance(output_console.printed[4], Panel)
    assert output_console.printed[5] == "[yellow]Goodbye.[/yellow]"
    assert output_console.printed[6] == "[red]Unknown command.[/red] [dim]Use /help.[/dim]"
    assert output_console.printed[7] == (
        "[dim]Queued behind a turn running from another client;"
        " your message sends when it finishes.[/dim]"
    )
    assert isinstance(output_console.printed[8], Panel)


def test_gateway_exit_receipt_is_plain_and_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    output_console = _RecordingConsole()
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "ws://127.0.0.1:18791/ws")

    runtime_bridge._print_session_exit_receipt(
        output_console,
        gateway_runtime.SessionExitSummary(
            session_key="agent:main:with spaces",
            title="Mac TUI work",
            model="openai/test",
            reason="surface_closed",
        ),
    )

    assert len(output_console.printed) == 1
    receipt = output_console.printed[0]
    assert getattr(receipt, "plain") == (
        "Session saved: Mac TUI work\n"
        "Resume: opensquilla chat --session agent:main:with spaces\n"
        "Web: http://127.0.0.1:18791/control/chat?session=agent%3Amain%3Awith%20spaces\n"
        "Exit: surface_closed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["host_crash", "gateway_disconnect", "runtime_error"])
async def test_gateway_runtime_bridge_prints_failure_receipt_once(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    output_console = _RecordingConsole()

    async def fake_run_gateway_chat(**kwargs: Any) -> gateway_runtime.SessionExitSummary:
        raise gateway_runtime.SessionExitError(
            gateway_runtime.SessionExitSummary(
                session_key="agent:main:recover",
                title="Recoverable session",
                model="openai/test",
                reason=cast(Any, reason),
                active=True,
                queued=2,
            )
        )

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    with pytest.raises(typer.Exit) as caught:
        await runtime_bridge.run_gateway_chat(
            model=None,
            session_id="agent:main:recover",
            output_console=output_console,
        )

    assert caught.value.exit_code == 1
    assert len(output_console.printed) == 1
    receipt = output_console.printed[0].plain
    assert receipt.count("Session saved:") == 1
    assert f"Exit: {reason}" in receipt
    assert "Remaining work: active=true, queued=2" in receipt


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_formats_missing_resume_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.gateway_client import GatewayRPCError
    from opensquilla.cli.repl import runtime_bridge

    output_console = _RecordingConsole()

    async def fake_run_gateway_chat(**_kwargs: Any) -> gateway_runtime.SessionExitSummary:
        raise GatewayRPCError(
            "sessions.bootstrap",
            code="NOT_FOUND",
            message="Session not found: main",
        )

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    with pytest.raises(typer.Exit) as caught:
        await runtime_bridge.run_gateway_chat(
            model=None,
            session_id="main",
            output_console=output_console,
            error_panel_factory=_fake_error_panel,
        )

    assert caught.value.exit_code == 2
    assert len(output_console.printed) == 1
    panel = cast(Panel, output_console.printed[0])
    assert panel.renderable == (
        "Session 'main' was not found. Run `opensquilla sessions list` to find "
        "a resumable key, or omit `--session` to create a new session."
    )


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_assembles_gateway_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    output_console = _RecordingConsole()

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    await runtime_bridge.run_gateway_chat(
        model="openai/test",
        session_id="agent:main:test",
        stream_response=_fake_gateway_stream,
        handle_slash_command=_fake_gateway_slash,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    assert captured["model"] == "openai/test"
    assert captured["session_id"] == "agent:main:test"
    assert deps.stream_response is _fake_gateway_stream
    assert deps.handle_slash_command is _fake_gateway_slash
    assert callable(deps.run_input_loop)
    assert deps.get_tui_output is runtime_bridge.get_tui_output
    deps.notify(gateway_runtime.GatewayRuntimeNotice(kind="error", message="boom"))
    assert isinstance(output_console.printed[-1], Panel)


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_resolves_default_repl_runner_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    runner_kwargs: dict[str, Any] = {}

    async def replacement_run_concurrent_repl(**kwargs: Any) -> None:
        runner_kwargs.update(kwargs)
        return None

    async def fake_dispatch(_value: str) -> bool:
        return True

    async def fake_abort() -> None:
        return None

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)
    monkeypatch.setattr(runtime_bridge, "run_concurrent_repl", replacement_run_concurrent_repl)

    await runtime_bridge.run_gateway_chat(
        model=None,
        session_id=None,
        stream_response=_fake_gateway_stream,
        handle_slash_command=_fake_gateway_slash,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    scope = {
        "session_key": "agent:main:test",
        "state": ChatSessionState(session_key="agent:main:test"),
        "model": None,
    }
    await deps.run_input_loop(
        scope=scope,
        dispatch=fake_dispatch,
        abort_active_turn=fake_abort,
    )
    assert runner_kwargs["surface"] is Surface.CLI_GATEWAY
    assert runner_kwargs["scope"] is scope
    assert runner_kwargs["dispatch"] is fake_dispatch
    assert runner_kwargs["abort_active_turn"] is fake_abort


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_owns_default_turn_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    await runtime_bridge.run_gateway_chat(model=None, session_id=None)

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    assert deps.stream_response is runtime_bridge.stream_response_gateway
    assert deps.handle_slash_command is runtime_bridge.handle_gateway_slash_command


@pytest.mark.asyncio
async def test_run_concurrent_repl_defaults_to_native_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    monkeypatch.delenv("OPENSQUILLA_TUI_BACKEND", raising=False)
    calls: list[dict[str, Any]] = []

    async def fake_native_repl(**kwargs: Any) -> None:
        calls.append(kwargs)

    async def fake_dispatch(_value: str) -> bool:
        return True

    scope: dict[str, Any] = {}
    monkeypatch.setattr(runtime_bridge._native_bridge, "run_concurrent_repl", fake_native_repl)

    await runtime_bridge.run_concurrent_repl(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
    )

    assert calls == [
        {
            "surface": Surface.CLI_GATEWAY,
            "scope": scope,
            "dispatch": fake_dispatch,
            "queue_max_size": runtime_bridge.PENDING_QUEUE_MAX_SIZE,
            "abort_active_turn": None,
        }
    ]


@pytest.mark.parametrize("backend_id", REMOVED_BACKEND_IDS)
def test_validate_tui_backend_selection_rejects_removed_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend_id: str,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    monkeypatch.setenv("OPENSQUILLA_TUI_BACKEND", backend_id)

    with pytest.raises(ValueError) as exc_info:
        runtime_bridge.validate_tui_backend_selection()

    assert "Unsupported TUI backend" in str(exc_info.value)
    assert backend_id in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_concurrent_repl_uses_opentui_bridge_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    monkeypatch.setenv("OPENSQUILLA_TUI_BACKEND", "opentui")
    calls: list[dict[str, Any]] = []

    async def fake_opentui_repl(**kwargs: Any) -> None:
        calls.append(kwargs)

    async def fake_dispatch(_value: str) -> bool:
        return True

    scope: dict[str, Any] = {}
    monkeypatch.setattr(
        runtime_bridge,
        "validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    monkeypatch.setattr(runtime_bridge._opentui_bridge, "run_concurrent_repl", fake_opentui_repl)

    await runtime_bridge.run_concurrent_repl(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=5,
    )

    assert calls == [
        {
            "surface": Surface.CLI_GATEWAY,
            "scope": scope,
            "dispatch": fake_dispatch,
            "queue_max_size": 5,
            "abort_active_turn": None,
        }
    ]


def test_turn_stream_dependencies_use_native_renderer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge
    from opensquilla.cli.tui.native.renderer import NativeStreamRenderer

    monkeypatch.delenv("OPENSQUILLA_TUI_BACKEND", raising=False)

    deps = runtime_bridge._turn_stream_dependencies()

    assert deps.renderer_factory is NativeStreamRenderer


def test_turn_stream_dependencies_use_opentui_renderer_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge
    from opensquilla.cli.tui.opentui.renderer import OpenTuiStreamRenderer

    monkeypatch.setattr(
        runtime_bridge,
        "validate_tui_backend_selection",
        lambda env=None: "opentui",
    )

    deps = runtime_bridge._turn_stream_dependencies()

    assert deps.renderer_factory is OpenTuiStreamRenderer


@pytest.mark.asyncio
async def test_opentui_chat_runtime_exposes_launch_scoped_plugin_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.backend.plugins import TuiPluginManager
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime
    from opensquilla.cli.tui.plugins.router_hud import RouterHudPlugin

    scope: dict[str, object] = {}
    captured: dict[str, object] = {}
    fake_surface = _FakeTuiSurface()

    @asynccontextmanager
    async def fake_open_opentui_surface(**_kwargs: object):
        yield fake_surface

    async def fake_run_tui_runtime(**kwargs: object):
        hooks = kwargs["hooks"]
        assert not opentui_runtime.get_tui_output(scope)
        hooks.expose_surface(fake_surface)
        output = opentui_runtime.get_tui_output(scope)
        captured["output"] = output
        captured["manager"] = getattr(output, "plugin_manager", None)
        hooks.clear_exposed_surface()
        return SimpleNamespace()

    monkeypatch.setattr(
        opentui_runtime,
        "open_opentui_surface",
        fake_open_opentui_surface,
    )
    monkeypatch.setattr(opentui_runtime, "run_tui_runtime", fake_run_tui_runtime)

    async def fake_dispatch(_value: str) -> bool:
        return True

    await opentui_runtime.run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=8,
    )

    assert opentui_runtime.get_tui_output(scope) is None
    manager = captured["manager"]
    assert isinstance(manager, TuiPluginManager)
    assert any(isinstance(plugin, RouterHudPlugin) for plugin in manager.plugins)


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_threads_stream_override_to_default_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    slash_captured: dict[str, Any] = {}
    output_console = Console(file=None, force_terminal=False)

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def fake_handle_gateway_slash_command(*args: Any, **kwargs: Any) -> bool:
        slash_captured.update({"args": args, **kwargs})
        return True

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)
    monkeypatch.setattr(
        runtime_bridge._slash_bridge,
        "handle_gateway_slash_command",
        fake_handle_gateway_slash_command,
    )

    await runtime_bridge.run_gateway_chat(
        model=None,
        session_id=None,
        stream_response=_fake_gateway_stream,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    handled = await deps.handle_slash_command(
        "/path report.md summarize",
        ChatSessionState(session_key="agent:main:test"),
        cast(gateway_runtime.GatewayClientLike, object()),
        {"mode": None},
        tui_output=None,
    )

    assert handled is True
    assert slash_captured["stream_response"] is _fake_gateway_stream
    assert slash_captured["output_console"] is output_console
    assert slash_captured["error_panel_factory"] is _fake_error_panel


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_assembles_standalone_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    output_console = Console(file=None, force_terminal=False)

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)

    await runtime_bridge.run_standalone_chat(
        model="openai/test",
        session_id="agent:main:test",
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
        stream_response=_fake_standalone_stream,
        image_command_handler=_fake_standalone_stream,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert captured["model"] == "openai/test"
    assert captured["session_id"] == "agent:main:test"
    assert captured["workspace"] == "repo"
    assert captured["workspace_strict"] is True
    assert captured["timeout"] == 7.25
    assert deps.stream_response is _fake_standalone_stream
    assert deps.image_command_handler is _fake_standalone_stream
    assert deps.run_concurrent_repl is runtime_bridge.run_concurrent_repl
    assert deps.slash_services_factory is runtime_bridge.standalone_slash_services_from_runtime
    assert deps.get_tui_output is runtime_bridge.get_tui_output
    assert deps.output_console is output_console


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_resolves_default_repl_runner_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def replacement_run_concurrent_repl(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)
    monkeypatch.setattr(runtime_bridge, "run_concurrent_repl", replacement_run_concurrent_repl)

    await runtime_bridge.run_standalone_chat(
        model=None,
        session_id=None,
        stream_response=_fake_standalone_stream,
        image_command_handler=_fake_standalone_stream,
    )

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert deps.run_concurrent_repl is replacement_run_concurrent_repl


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_owns_default_turn_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)

    await runtime_bridge.run_standalone_chat(model=None, session_id=None)

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert deps.stream_response is runtime_bridge.stream_response_turnrunner
    assert deps.image_command_handler is runtime_bridge.handle_image_command_turnrunner
