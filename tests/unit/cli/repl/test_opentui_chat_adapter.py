from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.commands import Surface


class _FakeOutputHandle:
    approval_surface = Surface.CLI_GATEWAY

    async def write_through(self, payload: str) -> None:
        return None

    async def send_message(self, message_type: str, payload: dict) -> None:
        return None

    def stream_output(self):
        @asynccontextmanager
        async def _cm() -> AsyncIterator[Callable[[str], None]]:
            yield lambda _payload: None

        return _cm()


class _FakeOpenTuiSurface:
    output_handle = _FakeOutputHandle()

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def next_line(self) -> str | None:
        return None

    def set_cancel_callback(self, cb: Callable[[], None] | None) -> None:
        return None

    def set_shutdown_callback(self, cb: Callable[[], None] | None) -> None:
        return None

    def emit_eof(self) -> None:
        return None

    async def write_through(self, payload: str) -> None:
        self.writes.append(payload)

    async def send_message(self, message_type: str, payload: dict) -> None:
        self.writes.append(f"{message_type}:{payload.get('text', '')}")

    @property
    def redraw_callback(self) -> Callable[[], None]:
        return lambda: None


@pytest.mark.asyncio
async def test_opentui_chat_runtime_exposes_tui_output_and_reuses_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime

    scope: dict[str, Any] = {
        "model": "model-a",
        "session_key": "session-a",
        "tool_ctx": SimpleNamespace(workspace_dir="/tmp/opentui-workspace"),
    }
    captured: dict[str, Any] = {}
    fake_surface = _FakeOpenTuiSurface()

    @asynccontextmanager
    async def fake_open_opentui_surface(**kwargs: Any):
        captured["surface_kwargs"] = kwargs
        yield fake_surface

    async def fake_run_tui_runtime(**kwargs: Any):
        captured["runtime_kwargs"] = kwargs
        async with kwargs["surface_factory"]() as yielded:
            assert yielded is fake_surface
        hooks = kwargs["hooks"]
        assert not opentui_runtime.get_tui_output(scope)
        hooks.expose_surface(fake_surface)
        output = opentui_runtime.get_tui_output(scope)
        captured["output"] = output
        captured["manager"] = getattr(output, "plugin_manager", None)
        hooks.clear_exposed_surface()
        return object()

    monkeypatch.setattr(opentui_runtime, "open_opentui_surface", fake_open_opentui_surface)
    monkeypatch.setattr(opentui_runtime, "run_tui_runtime", fake_run_tui_runtime)

    async def fake_dispatch(_value: str) -> bool:
        return True

    await opentui_runtime.run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=8,
    )

    assert captured["surface_kwargs"]["surface"] is Surface.CLI_GATEWAY
    assert captured["surface_kwargs"]["model"] == "model-a"
    assert captured["surface_kwargs"]["session_id"] == "session-a"
    assert captured["surface_kwargs"]["workspace_dir"] == "/tmp/opentui-workspace"
    context_update = captured["surface_kwargs"]["context_update"]
    assert context_update.task == "Session"
    assert context_update.surface == "Web + TUI"
    assert context_update.workspace == "opentui-workspace"
    assert captured["runtime_kwargs"]["dispatch"] is fake_dispatch
    assert captured["runtime_kwargs"]["config"].concurrent_input_during_turn is True
    assert opentui_runtime.get_tui_output(scope) is None
    assert getattr(captured["output"], "_output_handle", None) is fake_surface.output_handle
    assert captured["manager"] is not None


@pytest.mark.asyncio
async def test_opentui_chat_runtime_forwards_workspace_dir_from_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime

    scope: dict[str, Any] = {
        "model": "model-a",
        "session_key": "session-a",
        "tool_ctx": SimpleNamespace(workspace_dir="/tmp/workspace-a"),
    }
    captured: dict[str, Any] = {}
    fake_surface = _FakeOpenTuiSurface()

    @asynccontextmanager
    async def fake_open_opentui_surface(**kwargs: Any):
        captured["surface_kwargs"] = kwargs
        yield fake_surface

    async def fake_run_tui_runtime(**kwargs: Any):
        async with kwargs["surface_factory"]() as yielded:
            assert yielded is fake_surface

    monkeypatch.setattr(opentui_runtime, "open_opentui_surface", fake_open_opentui_surface)
    monkeypatch.setattr(opentui_runtime, "run_tui_runtime", fake_run_tui_runtime)

    async def fake_dispatch(_value: str) -> bool:
        return True

    await opentui_runtime.run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=8,
    )

    assert captured["surface_kwargs"]["workspace_dir"] == "/tmp/workspace-a"


@pytest.mark.asyncio
async def test_opentui_chat_runtime_uses_footer_native_echo_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import runtime_helpers
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime

    assert runtime_helpers.classify_chat_input("/help") is not None

    scope: dict[str, Any] = {"model": "model-a", "session_key": "session-a"}
    fake_surface = _FakeOpenTuiSurface()

    @asynccontextmanager
    async def fake_open_opentui_surface(**_kwargs: Any):
        yield fake_surface

    async def fake_run_tui_runtime(**kwargs: Any):
        hooks = kwargs["hooks"]
        await hooks.on_user_input_echo(fake_surface, "hello opentui")
        await hooks.on_user_input_echo(fake_surface, "中文输入 CJK混合ASCII")
        await hooks.on_queued_turn_start(fake_surface)
        return object()

    monkeypatch.setattr(opentui_runtime, "open_opentui_surface", fake_open_opentui_surface)
    monkeypatch.setattr(opentui_runtime, "run_tui_runtime", fake_run_tui_runtime)

    async def fake_dispatch(_value: str) -> bool:
        return True

    await opentui_runtime.run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=8,
    )

    joined_writes = "".join(fake_surface.writes)
    assert "你 / you" not in joined_writes
    assert "prompt.echo:hello opentui" in joined_writes
    assert "中文输入 CJK混合ASCII" in joined_writes
    assert "running queued input" in joined_writes


@pytest.mark.asyncio
async def test_opentui_chat_runtime_reprints_exit_notices_to_real_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surface-error notice carries the only copy of a sidecar crash
    reason, and the Goodbye notice is emitted with the bridge already doomed.
    Both must reach the real terminal stderr after teardown instead of dying
    with the captured console."""
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime

    scope: dict[str, Any] = {"model": "model-a", "session_key": "session-a"}
    fake_surface = _FakeOpenTuiSurface()

    @asynccontextmanager
    async def fake_open_opentui_surface(**_kwargs: Any):
        yield fake_surface

    async def fake_run_tui_runtime(**kwargs: Any):
        hooks = kwargs["hooks"]
        hooks.notice("[red]Input surface error: OpenTUI host exited with code 7[/red]")
        hooks.notice("[yellow]Goodbye.[/yellow]")
        return object()

    monkeypatch.setattr(opentui_runtime, "open_opentui_surface", fake_open_opentui_surface)
    monkeypatch.setattr(opentui_runtime, "run_tui_runtime", fake_run_tui_runtime)

    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    async def fake_dispatch(_value: str) -> bool:
        return True

    await opentui_runtime.run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=fake_dispatch,
        queue_max_size=8,
    )

    output = fake_stderr.getvalue()
    assert "Input surface error: OpenTUI host exited with code 7" in output
    assert "Goodbye" in output


@pytest.mark.asyncio
async def test_forward_console_notice_prunes_completed_pending_tasks() -> None:
    """The session-scoped pending set must shed tasks as their sends finish —
    every captured console line schedules one, so retaining completed tasks
    grows without bound over a long interactive session."""
    from opensquilla.cli.tui.backend.output_binding import TuiOutputBinding
    from opensquilla.cli.tui.opentui import runtime as opentui_runtime

    scope: dict[str, Any] = {}
    TuiOutputBinding(scope).expose(_FakeOutputHandle())
    pending: set[asyncio.Task[None]] = set()

    for index in range(5):
        opentui_runtime.forward_console_notice(scope, f"line-{index}", pending_tasks=pending)
    assert len(pending) == 5

    await asyncio.gather(*pending)
    # Done callbacks run one call_soon hop after completion.
    await asyncio.sleep(0)
    assert pending == set()


def test_opentui_notice_renders_through_captured_console() -> None:
    from opensquilla.cli.tui.opentui.notice_capture import capture_stdout_as_notices
    from opensquilla.cli.tui.opentui.runtime import opentui_notice

    lines: list[str] = []
    # Runtime notices must render as clean styled host notices, never as raw Rich
    # markup. With the capture installed, console.print is forwarded line-by-line.
    with capture_stdout_as_notices(lines.append):
        opentui_notice({}, "[yellow]Hello[/yellow]")

    joined = "".join(lines)
    assert "Hello" in joined
    assert "[yellow]" not in joined  # markup must be rendered out, not leaked verbatim
