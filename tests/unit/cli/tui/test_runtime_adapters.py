"""Runtime adapter behavior: native terminal output/interrupts, standalone
dispatch model bookkeeping and error recovery, and legacy compat renderer
selection."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console

from opensquilla.cli.chat.turn import TurnResult, UsageSummary
from opensquilla.cli.tui.adapters import native_bridge, slash_standalone
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.engine.commands import Surface
from opensquilla.gateway.config import GatewayConfig
from opensquilla.project_workspaces import project_path_key
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


class _FakeSessionManager:
    async def get_or_create(self, session_key: str, agent_id: str = "main") -> object:
        return SimpleNamespace(session_key=session_key, agent_id=agent_id)


class _FakeServices:
    def __init__(
        self,
        *,
        config: Any = None,
        session_manager: Any = None,
    ) -> None:
        self.config = config
        self.session_manager = session_manager or _FakeSessionManager()

    async def close(self) -> None:
        return None


class _RecordingConsole:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.payloads.extend(args)


def _retarget_directory_link(link: Path, target: Path, backup: Path) -> None:
    link.rename(backup)
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    result = subprocess.run(
        [
            "cmd",
            "/d",
            "/s",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        backup.rename(link)
        pytest.skip(f"could not create junction: {result.stderr or result.stdout}")


def _restore_retargeted_directory(link: Path, backup: Path) -> None:
    if not backup.exists():
        return
    if os.path.lexists(link):
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()
    backup.rename(link)


def _standalone_deps(
    *,
    stream_response: Any,
    run_concurrent_repl: Any,
    output_console: Any,
    error_panel_factory: Any,
) -> Any:
    from opensquilla.cli.tui import standalone_runtime

    return standalone_runtime.StandaloneRuntimeDependencies(
        stream_response=stream_response,
        image_command_handler=stream_response,
        run_concurrent_repl=run_concurrent_repl,
        slash_services_factory=lambda _svc: slash_standalone.StandaloneSlashServices(),
        sync_slash_adapter_io=lambda: None,
        get_tui_output=lambda _scope: None,
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )


def _patch_standalone_services(monkeypatch: pytest.MonkeyPatch) -> _FakeServices:
    services = _FakeServices()

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    return services


# --- Native terminal output ---------------------------------------------------


@pytest.mark.asyncio
async def test_native_write_through_streams_midline_chunks_without_rewrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Streamed flushes land mid-line; Rich must not word-wrap each chunk as if
    # the cursor were at column 0, or wrap points misalign and break paragraphs.
    narrow = Console(width=20)
    monkeypatch.setattr(native_bridge, "console", narrow)
    handle = native_bridge.NativeTerminalOutputHandle(
        approval_surface=Surface.CLI_STANDALONE,
    )
    chunks = ["Hello ", "world ", "this is a much longer sentence streamed in chunks"]

    with narrow.capture() as capture:
        for chunk in chunks:
            await handle.write_through(chunk)

    assert capture.get() == "".join(chunks)


# --- Native interrupt fallback -------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_fallback_owns_process_sigint_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Where loop signal handlers are unavailable (e.g. Windows), the surface
    # must own the process-level SIGINT handler for the session so Ctrl+C
    # cancels the turn instead of unwinding asyncio.run, then restore it.
    surface = native_bridge.NativeTerminalSurface(approval_surface=Surface.CLI_STANDALONE)
    loop = asyncio.get_running_loop()

    def _unsupported(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)
    cancelled: list[bool] = []
    shutdown: list[bool] = []
    surface.set_cancel_callback(lambda: cancelled.append(True))
    surface.set_shutdown_callback(lambda: shutdown.append(True))
    before = signal.getsignal(signal.SIGINT)

    try:
        surface.install_interrupt_handler()
        installed = signal.getsignal(signal.SIGINT)
        assert installed is not before
        assert callable(installed)
        installed(signal.SIGINT, None)  # simulate signal delivery
        await asyncio.sleep(0.01)
        assert cancelled == [True]
        assert shutdown == []
    finally:
        surface.remove_interrupt_handler()

    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="os.kill-based SIGINT delivery is POSIX-only in tests",
)
async def test_interrupt_fallback_handles_real_sigint_without_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = native_bridge.NativeTerminalSurface(approval_surface=Surface.CLI_STANDALONE)
    loop = asyncio.get_running_loop()

    def _unsupported(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)
    cancelled: list[bool] = []
    shutdown: list[bool] = []
    surface.set_cancel_callback(lambda: cancelled.append(True))
    surface.set_shutdown_callback(lambda: shutdown.append(True))
    before = signal.getsignal(signal.SIGINT)

    try:
        surface.install_interrupt_handler()
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.05)
        assert cancelled == [True]
        assert shutdown == []
    finally:
        surface.remove_interrupt_handler()

    assert signal.getsignal(signal.SIGINT) is before


# --- Standalone dispatch model bookkeeping --------------------------------------


@pytest.mark.asyncio
async def test_standalone_dispatch_never_feeds_routed_model_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without --model every turn must dispatch with model=None so per-turn
    # routing stays live; the routed result is display-only state.
    from opensquilla.cli.tui import standalone_runtime

    _patch_standalone_services(monkeypatch)
    models_seen: list[str | None] = []
    captured: dict[str, Any] = {}

    async def fake_stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
        pending_input_provider: object | None = None,
    ) -> TurnResult:
        models_seen.append(model)
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after="router/turn-pick",
        )

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch: Any,
    ) -> None:
        assert await dispatch("first") is True
        assert await dispatch("second") is True
        captured["scope_model"] = scope["model"]

    deps = _standalone_deps(
        stream_response=fake_stream_response,
        run_concurrent_repl=fake_run_concurrent_repl,
        output_console=_RecordingConsole(),
        error_panel_factory=lambda message: message,
    )

    await standalone_runtime.run_standalone_chat(
        model=None,
        session_id="agent:main:standalone:test",
        deps=deps,
    )

    assert models_seen == [None, None]
    # The routed model is still mirrored to display state for the HUD.
    assert captured["scope_model"] == "router/turn-pick"


@pytest.mark.asyncio
async def test_standalone_dispatch_uses_bound_project_workspace_over_tampered_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    storage = await SessionStorage.open(str(tmp_path / "tui-project.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    outside = tmp_path / "outside"
    project_path.mkdir()
    outside.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = "agent:main:standalone:project"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": str(outside),
            }
        },
    )
    services = _FakeServices(
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    captured_contexts: list[Any] = []

    async def stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> TurnResult:
        captured_contexts.append(tool_ctx)
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def run_repl(*, surface: Surface, scope: Any, dispatch: Any) -> None:
        assert await dispatch("pwd") is True

    deps = _standalone_deps(
        stream_response=stream_response,
        run_concurrent_repl=run_repl,
        output_console=_RecordingConsole(),
        error_panel_factory=lambda message: message,
    )
    try:
        await standalone_runtime.run_standalone_chat(
            model=None,
            session_id=key,
            deps=deps,
        )
    finally:
        await storage.close()

    assert captured_contexts[0].workspace_dir == project.path


@pytest.mark.asyncio
async def test_standalone_dispatch_refreshes_unbound_saved_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    storage = await SessionStorage.open(str(tmp_path / "tui-unbound.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    saved_workspace = tmp_path / "saved-workspace"
    saved_workspace.mkdir()
    key = "agent:main:standalone:unbound"
    await manager.create(
        key,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "run_mode_source": "user",
                "workspace": str(saved_workspace),
                "domains": [
                    {
                        "domain": "saved.example",
                        "scope": "chat",
                        "source": "manual",
                    }
                ],
            }
        },
    )
    services = _FakeServices(
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    captured_contexts: list[Any] = []

    async def stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> TurnResult:
        captured_contexts.append(tool_ctx)
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def run_repl(*, surface: Surface, scope: Any, dispatch: Any) -> None:
        assert await dispatch("pwd") is True

    deps = _standalone_deps(
        stream_response=stream_response,
        run_concurrent_repl=run_repl,
        output_console=_RecordingConsole(),
        error_panel_factory=lambda message: message,
    )
    try:
        await standalone_runtime.run_standalone_chat(
            model=None,
            session_id=key,
            deps=deps,
        )
    finally:
        await storage.close()

    tool_context = captured_contexts[0]
    assert tool_context.run_mode == "safe"
    assert tool_context.workspace_dir == str(saved_workspace.resolve())
    assert tool_context.sandbox_run_context.run_mode_source == "user"
    assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
        "saved.example"
    ]
    assert getattr(tool_context, "_sandbox_run_context_fresh", False) is True


@pytest.mark.asyncio
async def test_standalone_dispatch_revalidates_project_on_every_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    storage = await SessionStorage.open(str(tmp_path / "tui-retarget.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    project_backup = tmp_path / "project-old"
    replacement = tmp_path / "replacement"
    project_path.mkdir()
    replacement.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = "agent:main:standalone:retarget"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    services = _FakeServices(
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    calls: list[str] = []
    output = _RecordingConsole()

    async def stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> TurnResult:
        calls.append(message)
        return TurnResult(
            text="unsafe",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def run_repl(*, surface: Surface, scope: Any, dispatch: Any) -> None:
        _retarget_directory_link(project_path, replacement, project_backup)
        assert await dispatch("pwd") is True

    deps = _standalone_deps(
        stream_response=stream_response,
        run_concurrent_repl=run_repl,
        output_console=output,
        error_panel_factory=lambda message: message,
    )
    try:
        await standalone_runtime.run_standalone_chat(
            model=None,
            session_id=key,
            deps=deps,
        )
    finally:
        _restore_retargeted_directory(project_path, project_backup)
        await storage.close()

    assert calls == []
    assert any("canonical_changed" in str(payload) for payload in output.payloads)


@pytest.mark.asyncio
async def test_standalone_revalidates_session_replaced_by_slash_command_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    storage = await SessionStorage.open(str(tmp_path / "tui-replaced-session.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    project_backup = tmp_path / "project-old"
    replacement = tmp_path / "replacement"
    project_path.mkdir()
    replacement.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    initial_key = "agent:main:standalone:before-new"
    original_get_or_create = manager.get_or_create

    async def bind_replacement_session(
        session_key: str,
        agent_id: str = "main",
        **kwargs: Any,
    ):
        session, created = await original_get_or_create(
            session_key,
            agent_id=agent_id,
            **kwargs,
        )
        if session_key != initial_key:
            session = await manager.update(
                session_key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "standard",
                        "workspace": project.path,
                    }
                },
            )
        return session, created

    manager.get_or_create = bind_replacement_session  # type: ignore[method-assign]
    services = _FakeServices(
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )

    async def fake_build_services() -> _FakeServices:
        return services

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    calls: list[tuple[str, str]] = []
    output = _RecordingConsole()

    async def stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        **kwargs: Any,
    ) -> TurnResult:
        del turn_runner, tool_ctx, kwargs
        calls.append((session_key, message))
        return TurnResult(
            text="unsafe",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def run_repl(*, surface: Surface, scope: Any, dispatch: Any) -> None:
        assert await dispatch("/new") is True
        assert scope["session_key"] != initial_key
        _retarget_directory_link(project_path, replacement, project_backup)
        assert await dispatch("pwd") is True

    deps = replace(
        _standalone_deps(
            stream_response=stream_response,
            run_concurrent_repl=run_repl,
            output_console=output,
            error_panel_factory=lambda message: message,
        ),
        slash_services_factory=standalone_runtime.standalone_slash_services_from_runtime,
    )
    try:
        await standalone_runtime.run_standalone_chat(
            model=None,
            session_id=initial_key,
            deps=deps,
        )
    finally:
        _restore_retargeted_directory(project_path, project_backup)
        await storage.close()

    assert calls == []
    assert any("canonical_changed" in str(payload) for payload in output.payloads)


@pytest.mark.asyncio
async def test_standalone_requested_model_follows_model_command_not_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    _patch_standalone_services(monkeypatch)
    models_seen: list[str | None] = []

    async def fake_stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
        pending_input_provider: object | None = None,
    ) -> TurnResult:
        models_seen.append(model)
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after="router/other",
        )

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch: Any,
    ) -> None:
        assert await dispatch("first") is True
        assert await dispatch("second") is True
        assert await dispatch("/model user/second") is True
        assert await dispatch("third") is True

    deps = _standalone_deps(
        stream_response=fake_stream_response,
        run_concurrent_repl=fake_run_concurrent_repl,
        output_console=_RecordingConsole(),
        error_panel_factory=lambda message: message,
    )

    await standalone_runtime.run_standalone_chat(
        model="user/requested",
        session_id="agent:main:standalone:test",
        deps=deps,
    )

    # --model survives routed turns; only /model changes the dispatch model.
    assert models_seen == ["user/requested", "user/requested", "user/second"]


# --- Standalone dispatch error recovery ------------------------------------------


@pytest.mark.asyncio
async def test_standalone_dispatch_maps_stream_errors_to_error_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    _patch_standalone_services(monkeypatch)
    output_console = _RecordingConsole()
    messages: list[str] = []

    async def flaky_stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
        pending_input_provider: object | None = None,
    ) -> TurnResult:
        messages.append(message)
        if len(messages) == 1:
            raise RuntimeError("provider exploded")
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch: Any,
    ) -> None:
        # The failing turn is reported and the loop keeps accepting input.
        assert await dispatch("boom") is True
        assert await dispatch("recover") is True

    deps = _standalone_deps(
        stream_response=flaky_stream_response,
        run_concurrent_repl=fake_run_concurrent_repl,
        output_console=output_console,
        error_panel_factory=lambda message: f"<panel {message}>",
    )

    await standalone_runtime.run_standalone_chat(
        model=None,
        session_id="agent:main:standalone:test",
        deps=deps,
    )

    assert messages == ["boom", "recover"]
    assert "<panel RuntimeError: provider exploded>" in output_console.payloads


@pytest.mark.asyncio
async def test_standalone_dispatch_maps_slash_errors_to_error_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui import standalone_runtime

    _patch_standalone_services(monkeypatch)
    output_console = _RecordingConsole()

    async def broken_slash_handler(
        cmd: str,
        context: slash_standalone.StandaloneSlashContext,
    ) -> bool:
        raise OSError("db locked")

    monkeypatch.setattr(
        slash_standalone,
        "handle_standalone_slash_command",
        broken_slash_handler,
    )

    async def fake_stream_response(
        turn_runner: object,
        session_key: str,
        tool_ctx: object,
        message: str,
        model: str | None = None,
        svc: object = None,
        timeout: float | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
        pending_input_provider: object | None = None,
    ) -> TurnResult:
        return TurnResult(
            text="reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after=None,
        )

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch: Any,
    ) -> None:
        assert await dispatch("/new") is True
        assert await dispatch("still alive") is True

    deps = _standalone_deps(
        stream_response=fake_stream_response,
        run_concurrent_repl=fake_run_concurrent_repl,
        output_console=output_console,
        error_panel_factory=lambda message: f"<panel {message}>",
    )

    await standalone_runtime.run_standalone_chat(
        model=None,
        session_id="agent:main:standalone:test",
        deps=deps,
    )

    assert "<panel OSError: db locked>" in output_console.payloads


# --- Legacy compat renderer selection --------------------------------------------


def test_chat_compat_default_deps_follow_selected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.tui.adapters import chat_compat, runtime_bridge
    from opensquilla.cli.tui.native.renderer import NativeStreamRenderer
    from opensquilla.cli.tui.opentui.renderer import OpenTuiStreamRenderer

    monkeypatch.setattr(
        runtime_bridge,
        "validate_tui_backend_selection",
        lambda env=None: "native",
    )
    deps = chat_compat.default_turn_stream_dependencies()
    assert deps.renderer_factory is NativeStreamRenderer
    assert deps.stream_wrapper is cast(Any, chat_compat.wrap_cli_turn_stream)

    monkeypatch.setattr(
        runtime_bridge,
        "validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    deps = chat_compat.default_turn_stream_dependencies()
    assert deps.renderer_factory is OpenTuiStreamRenderer


def test_chat_compat_no_longer_exports_tool_compress_wrapper() -> None:
    from opensquilla.cli.tui.adapters import chat_compat

    assert not hasattr(chat_compat, "handle_tool_compress_command")
