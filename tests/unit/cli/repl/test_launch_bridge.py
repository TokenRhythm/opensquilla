from __future__ import annotations

import logging
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer


class FakeConsole:
    def __init__(self, *, is_terminal: bool = True) -> None:
        self.is_terminal = is_terminal
        self.clears = 0
        self.prints: list[Any] = []
        self.print_options: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.clears += 1

    def print(self, payload: Any, **kwargs: Any) -> None:
        self.prints.append(payload)
        self.print_options.append(kwargs)


class FakeTerminalStream(StringIO):
    def isatty(self) -> bool:
        return True


def _ui_selection(
    backend_id: str,
    *,
    fallback: Any | None = None,
) -> Any:
    from opensquilla.cli.tui.renderers.selection import ChatUiSelection

    return ChatUiSelection(
        requested_mode="auto" if fallback is not None else backend_id,
        backend=SimpleNamespace(backend_id=backend_id),
        fallback=fallback,
    )


def _missing_host_selection() -> Any:
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        RendererBackendUnavailableReason,
    )

    return _ui_selection(
        "native",
        fallback=ChatUiFallback(
            code=RendererBackendUnavailableReason.MISSING,
            detail="host missing",
        ),
    )


def test_bare_chat_preflight_selects_opentui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import (
        OPENSQUILLA_TUI_BACKEND_ENV,
        RendererBackendAvailability,
    )

    original_backend = os.environ.pop(OPENSQUILLA_TUI_BACKEND_ENV, None)
    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(available=True),
    )

    try:
        backend_id = launch_bridge.validate_tui_backend_or_exit()

        assert backend_id == "opentui"
        assert os.environ[OPENSQUILLA_TUI_BACKEND_ENV] == "opentui"
    finally:
        os.environ.pop(OPENSQUILLA_TUI_BACKEND_ENV, None)
        if original_backend is not None:
            os.environ[OPENSQUILLA_TUI_BACKEND_ENV] = original_backend


def test_bare_chat_ignores_stale_internal_backend_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import (
        OPENSQUILLA_TUI_BACKEND_ENV,
        RendererBackendAvailability,
        RendererBackendUnavailableReason,
    )

    monkeypatch.setenv(OPENSQUILLA_TUI_BACKEND_ENV, "opentui")
    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(
            available=False,
            reason="host missing",
            reason_code=RendererBackendUnavailableReason.MISSING,
        ),
    )

    selection = launch_bridge.resolve_tui_backend_or_exit()

    assert selection.requested_mode == "auto"
    assert selection.backend.backend_id == "native"
    assert selection.fallback is not None
    assert os.environ[OPENSQUILLA_TUI_BACKEND_ENV] == "native"


def test_bare_chat_installed_fallback_notice_shows_once_per_version_and_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.opentui import bridge as opentui_bridge
    from opensquilla.cli.tui.renderers.selection import (
        OPENSQUILLA_TUI_BACKEND_ENV,
        RendererBackendAvailability,
        RendererBackendUnavailableReason,
    )

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    original_backend = os.environ.pop(OPENSQUILLA_TUI_BACKEND_ENV, None)
    reason_code = RendererBackendUnavailableReason.MISSING
    monkeypatch.setattr(
        opentui_bridge.OpenTuiRendererBackend,
        "is_available",
        lambda self: RendererBackendAvailability(
            available=False,
            reason="host missing",
            reason_code=reason_code,
        ),
    )
    monkeypatch.setattr(source_checkout, "resolve_tui_source_checkout_hint", lambda: None)
    events: list[str] = []

    output_console = FakeConsole()
    monkeypatch.setattr(
        launch_bridge,
        "preflight_gateway_chat_or_exit",
        lambda: events.append("preflight"),
    )
    monkeypatch.setattr(
        launch_bridge,
        "validate_interactive_chat",
        lambda **_kwargs: events.append("validate"),
    )
    monkeypatch.setattr(
        launch_bridge,
        "prepare_interactive_chat",
        lambda **_kwargs: events.append("clear"),
    )

    async def fake_gateway(**_kwargs: Any) -> None:
        events.append("run")

    def launch() -> None:
        launch_bridge.launch_chat(
            model="",
            session_id="",
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
            standalone_runner=None,
            gateway_runner=fake_gateway,
            output_console=output_console,
        )

    try:
        # First launch on this install: exactly one dim explanation with the
        # remedy pointer, after the screen clear.
        launch()
        assert events == ["validate", "preflight", "clear", "run"]
        assert os.environ[OPENSQUILLA_TUI_BACKEND_ENV] == "native"
        assert len(output_console.prints) == 1
        notice = str(output_console.prints[0])
        assert "Full-screen TUI unavailable" in notice
        assert "using plain mode" in notice
        assert "opensquilla doctor" in notice

        # Second launch with the same version and reason: quiet again.
        launch()
        assert len(output_console.prints) == 1

        # A different unavailability reason is new information: one fresh line.
        reason_code = RendererBackendUnavailableReason.VERSION_MISMATCH
        launch()
        assert len(output_console.prints) == 2

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        os.environ.pop(OPENSQUILLA_TUI_BACKEND_ENV, None)
        if original_backend is not None:
            os.environ[OPENSQUILLA_TUI_BACKEND_ENV] = original_backend


def test_bare_chat_advertises_source_host_only_in_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge

    console = FakeConsole()
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda: _missing_host_selection(),
    )
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(launch_bridge, "preflight_gateway_chat_or_exit", lambda: None)
    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(
        source_checkout,
        "resolve_tui_source_checkout_hint",
        lambda: source_checkout.TuiSourceCheckoutHint(
            install_command="bun install --frozen-lockfile --cwd /repo/package",
            launch_command=(
                "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 "
                "uv --directory /repo run opensquilla chat --ui tui"
            ),
        ),
    )

    async def fake_gateway(**_kwargs: Any) -> None:
        return None

    launch_bridge.launch_chat(
        model="",
        session_id="",
        standalone=False,
        workspace="",
        workspace_strict=None,
        timeout=None,
        standalone_runner=None,
        gateway_runner=fake_gateway,
        output_console=console,
    )

    assert len(console.prints) == 5
    notice = "\n".join(str(payload) for payload in console.prints)
    assert "Full-screen TUI source is available in this checkout" in notice
    assert "bun install --frozen-lockfile" in notice
    assert "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1" in notice
    assert "opensquilla chat --ui tui" in notice
    assert "Continuing in plain mode for this launch" in notice
    assert console.print_options == [{}, {}, {"soft_wrap": True}, {"soft_wrap": True}, {}]


def test_source_hint_commands_are_not_hard_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich.console import Console

    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge

    install_command = "bun install --frozen-lockfile --cwd '/tmp/Open Squilla/package'"
    launch_command = (
        "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 uv --directory '/tmp/Open Squilla' "
        "run opensquilla chat --ui tui"
    )
    monkeypatch.setattr(
        source_checkout,
        "resolve_tui_source_checkout_hint",
        lambda: source_checkout.TuiSourceCheckoutHint(
            install_command=install_command,
            launch_command=launch_command,
        ),
    )
    output = StringIO()
    output_console = Console(file=output, width=40, color_system=None)

    launch_bridge._print_tui_fallback_after_clear(
        _missing_host_selection(),
        implicit_ui=True,
        output_console=output_console,
    )

    rendered = output.getvalue()
    assert install_command in rendered
    assert launch_command in rendered
    assert "packa\nge" not in rendered


def test_explicit_auto_fallback_does_not_advertise_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge

    console = FakeConsole()
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda mode: _missing_host_selection() if mode == "auto" else None,
    )
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(launch_bridge, "preflight_gateway_chat_or_exit", lambda: None)
    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(
        source_checkout,
        "resolve_tui_source_checkout_hint",
        lambda: pytest.fail("explicit --ui auto must not resolve a source hint"),
    )

    async def fake_gateway(**_kwargs: Any) -> None:
        return None

    launch_bridge.launch_chat(
        model="",
        session_id="",
        ui="auto",
        standalone=False,
        workspace="",
        workspace_strict=None,
        timeout=None,
        standalone_runner=None,
        gateway_runner=fake_gateway,
        output_console=console,
    )

    assert console.prints == ["[dim]OpenTUI unavailable; using plain mode: host missing[/dim]"]


def test_explicit_auto_fallback_sanitizes_terminal_controls() -> None:
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        RendererBackendUnavailableReason,
    )

    console = FakeConsole()
    selection = _ui_selection(
        "native",
        fallback=ChatUiFallback(
            code=RendererBackendUnavailableReason.MISSING,
            detail="host \x1b[31mmissing\x1b[0m\nretry",
        ),
    )

    launch_bridge._print_tui_fallback_after_clear(
        selection,
        implicit_ui=False,
        output_console=console,
    )

    assert console.prints == [
        "[dim]OpenTUI unavailable; using plain mode: host missing retry[/dim]"
    ]


def test_explicit_plain_keeps_quiet_rescue_renderer_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    console = FakeConsole()
    modes: list[str] = []
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda mode: modes.append(mode) or _ui_selection("native"),
    )
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(launch_bridge, "preflight_gateway_chat_or_exit", lambda: None)
    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)

    async def fake_gateway(**_kwargs: Any) -> None:
        return None

    launch_bridge.launch_chat(
        model="",
        session_id="",
        ui="plain",
        standalone=False,
        workspace="",
        workspace_strict=None,
        timeout=None,
        standalone_runner=None,
        gateway_runner=fake_gateway,
        output_console=console,
    )

    assert modes == ["plain"]
    assert console.prints == []


def test_launch_bridge_prepares_terminal_and_quiets_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[str] = []

    class FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        launch_bridge,
        "quiet_logs_for_interactive_chat",
        lambda: calls.append("quiet"),
    )

    console = FakeConsole(is_terminal=True)

    launch_bridge.prepare_interactive_chat(
        input_stream=FakeStdin(),
        output_console=console,
    )

    assert calls == ["quiet"]
    assert console.clears == 1


def test_launch_bridge_routes_interactive_structlog_to_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import structlog

    from opensquilla.cli.tui.adapters import launch_bridge

    original_config = structlog.get_config()
    root = logging.getLogger()
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_LOG_LEVEL", raising=False)
    for handler in original_root_handlers:
        root.removeHandler(handler)
    terminal_stream = FakeTerminalStream()
    root.addHandler(logging.StreamHandler(terminal_stream))

    try:
        launch_bridge.quiet_logs_for_interactive_chat()
        structlog.get_logger("opensquilla.test").warning(
            "ui.hidden_warning",
            answer=42,
        )
        logging.getLogger("opensquilla.test").warning("ui.hidden_stdlib_warning")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert terminal_stream.getvalue() == ""
        log_text = (tmp_path / "interactive.log").read_text()
        assert "ui.hidden_warning" in log_text
        assert "ui.hidden_stdlib_warning" in log_text
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_root_handlers:
            root.addHandler(handler)
        root.setLevel(original_root_level)
        handle = getattr(launch_bridge, "_INTERACTIVE_STRUCTLOG_FILE", None)
        if handle is not None:
            handle.close()
            setattr(launch_bridge, "_INTERACTIVE_STRUCTLOG_FILE", None)
        structlog.configure(**original_config)


def test_launch_bridge_rejects_non_interactive_input() -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    class FakeStdin:
        def isatty(self) -> bool:
            return False

    with pytest.raises(typer.Exit) as exc_info:
        launch_bridge.prepare_interactive_chat(
            input_stream=FakeStdin(),
            output_console=FakeConsole(is_terminal=True),
        )

    assert exc_info.value.exit_code == 2


def test_launch_bridge_prints_standalone_banner_and_runs_standalone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[dict[str, Any]] = []
    console = FakeConsole(is_terminal=True)

    async def fake_standalone(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda: _ui_selection("native"),
    )
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)

    launch_bridge.launch_chat(
        model="openai/test",
        session_id="agent:main:test",
        standalone=True,
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
        standalone_runner=fake_standalone,
        gateway_runner=None,
        output_console=console,
    )

    assert len(console.prints) == 3
    assert "OpenSquilla Chat" in str(console.prints[0].renderable)
    assert console.prints[1] == "[dim]Model: openai/test[/dim]"
    assert console.prints[2] == "[dim]Session: agent:main:test[/dim]"
    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "workspace": "repo",
            "workspace_strict": True,
            "timeout": 7.25,
        }
    ]


def test_launch_bridge_suppresses_native_banner_for_opentui_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The OpenTUI host draws its own full-screen footer; printing the native
    # banner first only makes it flash for ~1s before OpenTUI takes the screen.
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[dict[str, Any]] = []
    console = FakeConsole(is_terminal=True)

    async def fake_standalone(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda: _ui_selection("opentui"),
    )
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)

    launch_bridge.launch_chat(
        model="openai/test",
        session_id="agent:main:test",
        standalone=True,
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
        standalone_runner=fake_standalone,
        gateway_runner=None,
        output_console=console,
    )

    # No native chrome printed to the main screen, but the runner still launches.
    assert console.prints == []
    assert len(calls) == 1
    assert calls[0]["model"] == "openai/test"


def test_launch_bridge_warns_gateway_workspace_options_without_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[dict[str, Any]] = []
    console = FakeConsole(is_terminal=True)

    async def fake_gateway(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda: _ui_selection("native"),
    )
    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)
    monkeypatch.setattr(launch_bridge, "preflight_gateway_chat_or_exit", lambda: None)
    monkeypatch.setattr(launch_bridge, "validate_interactive_chat", lambda **_kwargs: None)

    launch_bridge.launch_chat(
        model="",
        session_id="",
        standalone=False,
        workspace="repo",
        workspace_strict=True,
        timeout=None,
        standalone_runner=None,
        gateway_runner=fake_gateway,
        output_console=console,
    )

    assert calls == [{"model": None, "session_id": None}]
    assert len(console.prints) == 1
    assert "--workspace only affects --standalone chat" in str(console.prints[0])


def test_launch_bridge_rejects_non_tty_before_gateway_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import launch_bridge

    class FakeStdin:
        def isatty(self) -> bool:
            return False

    preflight_calls: list[bool] = []
    console = FakeConsole(is_terminal=True)
    monkeypatch.setattr(
        launch_bridge,
        "resolve_tui_backend_or_exit",
        lambda: _missing_host_selection(),
    )
    monkeypatch.setattr(
        launch_bridge,
        "preflight_gateway_chat_or_exit",
        lambda: preflight_calls.append(True),
    )

    with pytest.raises(typer.Exit) as exc_info:
        launch_bridge.launch_chat(
            model="",
            session_id="",
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
            standalone_runner=None,
            gateway_runner=None,
            input_stream=FakeStdin(),
            output_console=console,
        )

    assert exc_info.value.exit_code == 2
    assert preflight_calls == []
    assert console.prints == []


def test_launch_chat_command_uses_typed_overrides() -> None:
    from opensquilla.cli.chat.launch import (
        ChatCommandLaunchOverrides,
        ChatCommandRequest,
    )
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[dict[str, Any]] = []

    async def fake_standalone(**kwargs: Any) -> None:
        return None

    async def fake_gateway(**kwargs: Any) -> None:
        return None

    def fake_launch_chat(**kwargs: Any) -> None:
        calls.append(kwargs)

    launch_bridge.launch_chat_command(
        ChatCommandRequest(
            model="openai/test",
            session_id="agent:main:test",
            standalone=True,
            workspace="repo",
            workspace_strict=True,
            timeout=7.25,
        ),
        overrides=ChatCommandLaunchOverrides(
            launch_chat=fake_launch_chat,
            standalone_runner=fake_standalone,
            gateway_runner=fake_gateway,
        ),
    )

    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "ui": None,
            "standalone": True,
            "workspace": "repo",
            "workspace_strict": True,
            "timeout": 7.25,
            "standalone_runner": fake_standalone,
            "gateway_runner": fake_gateway,
        }
    ]


def test_launch_chat_command_keeps_legacy_override_mapping() -> None:
    from opensquilla.cli.chat.launch import ChatCommandRequest
    from opensquilla.cli.tui.adapters import launch_bridge

    calls: list[dict[str, Any]] = []

    async def fake_standalone(**kwargs: Any) -> None:
        return None

    async def fake_gateway(**kwargs: Any) -> None:
        return None

    def fake_launch_chat(**kwargs: Any) -> None:
        calls.append(kwargs)

    launch_bridge.launch_chat_command(
        ChatCommandRequest(
            model="openai/test",
            session_id="agent:main:test",
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        ),
        legacy_overrides={
            "_launch_bridge": SimpleNamespace(launch_chat=fake_launch_chat),
            "_standalone_repl": fake_standalone,
            "_gateway_chat": fake_gateway,
        },
    )

    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "ui": None,
            "standalone": False,
            "workspace": "",
            "workspace_strict": None,
            "timeout": None,
            "standalone_runner": fake_standalone,
            "gateway_runner": fake_gateway,
        }
    ]


def test_fallback_notice_phrases_key_only_stable_reason_codes() -> None:
    from opensquilla.cli.tui.adapters.launch_bridge import _FALLBACK_NOTICE_PHRASES
    from opensquilla.cli.tui.renderers.selection import RendererBackendUnavailableReason

    codes = {reason.value for reason in RendererBackendUnavailableReason}
    assert set(_FALLBACK_NOTICE_PHRASES) <= codes


def test_fallback_notice_prints_raw_code_for_unmapped_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        RendererBackendUnavailableReason,
    )

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(source_checkout, "resolve_tui_source_checkout_hint", lambda: None)
    console = FakeConsole()
    selection = SimpleNamespace(
        fallback=ChatUiFallback(
            code=RendererBackendUnavailableReason.RUNTIME_CRASH,
            detail="host exited 1",
        )
    )

    launch_bridge._print_tui_fallback_after_clear(
        selection, implicit_ui=True, output_console=console
    )

    assert len(console.prints) == 1
    # No phrase mapping for runtime_crash: the stable code itself is printed.
    assert "runtime_crash" in str(console.prints[0])


def test_fallback_notice_repeats_when_the_record_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import source_checkout
    from opensquilla.cli.tui.adapters import launch_bridge
    from opensquilla.cli.tui.opentui import prefs
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        RendererBackendUnavailableReason,
    )

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(source_checkout, "resolve_tui_source_checkout_hint", lambda: None)
    monkeypatch.setattr(
        "opensquilla.cli.tui.opentui.prefs._store",
        lambda _prefs: None,  # simulate a write that silently fails
    )
    console = FakeConsole()
    selection = SimpleNamespace(
        fallback=ChatUiFallback(
            code=RendererBackendUnavailableReason.MISSING,
            detail="no companion",
        )
    )

    for _ in range(2):
        launch_bridge._print_tui_fallback_after_clear(
            selection, implicit_ui=True, output_console=console
        )

    # Fail open: with no durable record the notice repeats — degrading loud,
    # never silent.
    assert len(console.prints) == 2
    assert prefs.fallback_notice_due("x", "missing") is True
