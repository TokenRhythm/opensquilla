from __future__ import annotations

import importlib
import inspect
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from opensquilla.cli.repl import standalone_slash_adapter
from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult, UsageSummary
from opensquilla.cli.tui.contracts import TuiOutputHandle
from opensquilla.engine.commands import Surface


def test_standalone_runtime_has_no_raw_prompt_application_dependency(monkeypatch) -> None:
    monkeypatch.delitem(
        sys.modules,
        "opensquilla.cli.repl.standalone_runtime",
        raising=False,
    )

    original_import = __import__
    blocked_module = "prompt" + "_toolkit"

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == blocked_module or name.startswith(f"{blocked_module}."):
            raise AssertionError(f"standalone runtime imported {blocked_module} via {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _guarded_import)

    module = importlib.import_module("opensquilla.cli.repl.standalone_runtime")
    source = inspect.getsource(module)

    assert "ChatApplication" not in source


def test_standalone_session_context_mirrors_state_to_legacy_scope() -> None:
    from opensquilla.cli.repl.standalone_runtime import StandaloneSessionContext

    original_tool_ctx = object()
    replacement_tool_ctx = object()
    state = ChatSessionState(
        session_key="agent:main:standalone:original",
        model="standalone/original",
    )
    replacement_state = ChatSessionState(
        session_key="agent:main:standalone:replacement",
        model="standalone/replacement",
    )

    context = StandaloneSessionContext.create(state=state, tool_ctx=original_tool_ctx)

    assert context.scope["session_key"] == "agent:main:standalone:original"
    assert context.scope["tool_ctx"] is original_tool_ctx
    assert context.scope["state"] is state
    assert context.scope["model"] == "standalone/original"

    context.replace_session(
        session_key="agent:main:standalone:replacement",
        tool_ctx=replacement_tool_ctx,
        state=replacement_state,
        model="standalone/replacement",
    )

    assert context.session_key == "agent:main:standalone:replacement"
    assert context.tool_ctx is replacement_tool_ctx
    assert context.model == "standalone/replacement"
    assert context.scope["session_key"] == "agent:main:standalone:replacement"
    assert context.scope["tool_ctx"] is replacement_tool_ctx
    assert context.scope["state"] is replacement_state
    assert context.scope["model"] == "standalone/replacement"


@pytest.mark.asyncio
async def test_standalone_runtime_mirrors_turn_model_update_to_legacy_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import standalone_runtime

    activity: list[dict[str, Any]] = []

    async def record_active(**kwargs: Any) -> bool:
        activity.append(kwargs)
        return True

    class _FakeSessionManager:
        async def get_or_create(self, session_key: str, agent_id: str = "main") -> object:
            return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    class _FakeServices:
        def __init__(self) -> None:
            self.config = None
            self.session_manager = _FakeSessionManager()
            self.growth_event_sink = SimpleNamespace(record_product_active=record_active)

        async def close(self) -> None:
            return None

    services = _FakeServices()
    turn_runner = object()
    output = cast(TuiOutputHandle, object())
    pending_provider = SimpleNamespace(drain_pending=lambda: [])
    captured: dict[str, Any] = {}

    async def fake_build_services() -> _FakeServices:
        return services

    def fake_build_turn_runner_from_services(_services: object) -> object:
        return turn_runner

    async def fake_stream_response(
        active_turn_runner: object,
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
        captured["stream"] = {
            "turn_runner": active_turn_runner,
            "session_key": session_key,
            "tool_ctx": tool_ctx,
            "message": message,
            "model": model,
            "svc": svc,
            "timeout": timeout,
            "tui_output": tui_output,
            "pending_input_provider": pending_input_provider,
        }
        return TurnResult(
            text="assistant reply",
            usage=UsageSummary(input_tokens=5, output_tokens=8),
            model_after="standalone/after",
        )

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch,
        on_surface_ready=None,
        on_user_activity=None,
    ) -> None:
        captured["surface"] = surface
        captured["initial_scope"] = dict(scope)
        scope["pending_input_provider"] = pending_provider

        await on_surface_ready()
        await on_user_activity()
        assert await dispatch("hello") is True

        captured["scope_after_message"] = dict(scope)
        captured["state_after_message"] = scope["state"]

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        fake_build_turn_runner_from_services,
    )

    deps = standalone_runtime.StandaloneRuntimeDependencies(
        stream_response=fake_stream_response,
        image_command_handler=fake_stream_response,
        run_concurrent_repl=fake_run_concurrent_repl,
        slash_services_factory=lambda _svc: standalone_slash_adapter.StandaloneSlashServices(),
        sync_slash_adapter_io=lambda: None,
        get_tui_output=lambda _scope: output,
    )

    await standalone_runtime.run_standalone_chat(
        model="openrouter/test",
        session_id="agent:main:standalone:test",
        deps=deps,
        timeout=7.25,
    )

    assert captured["surface"] is Surface.CLI_STANDALONE
    assert captured["initial_scope"]["session_key"] == "agent:main:standalone:test"
    assert captured["initial_scope"]["model"] == "openrouter/test"
    assert captured["stream"]["turn_runner"] is turn_runner
    assert captured["stream"]["session_key"] == "agent:main:standalone:test"
    assert captured["stream"]["message"] == "hello"
    assert captured["stream"]["model"] == "openrouter/test"
    assert captured["stream"]["svc"] is services
    assert captured["stream"]["timeout"] == 7.25
    assert captured["stream"]["tui_output"] is output
    assert captured["stream"]["pending_input_provider"] is pending_provider
    assert captured["scope_after_message"]["model"] == "standalone/after"
    assert captured["state_after_message"].model == "standalone/after"
    assert captured["state_after_message"].usage.input_tokens == 5
    assert captured["state_after_message"].usage.output_tokens == 8
    assert activity == [{"surface": "tui"}, {"surface": "tui"}]


@pytest.mark.asyncio
async def test_standalone_runtime_matches_exit_with_standalone_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import standalone_runtime

    class _FakeSessionManager:
        async def get_or_create(self, session_key: str, agent_id: str = "main") -> object:
            return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    class _FakeServices:
        def __init__(self) -> None:
            self.config = None
            self.session_manager = _FakeSessionManager()

        async def close(self) -> None:
            return None

    surfaces: list[Surface] = []

    def fake_is_exit_command(value: str, surface: Surface) -> bool:
        surfaces.append(surface)
        return value == "/exit"

    async def fake_build_services() -> _FakeServices:
        return _FakeServices()

    async def fake_run_concurrent_repl(
        *,
        surface: Surface,
        scope: standalone_runtime.StandaloneRuntimeScope,
        dispatch,
    ) -> None:
        assert surface is Surface.CLI_STANDALONE
        assert await dispatch("/exit") is False

    monkeypatch.setattr("opensquilla.gateway.build_services", fake_build_services)
    monkeypatch.setattr(
        "opensquilla.gateway.build_turn_runner_from_services",
        lambda _services: object(),
    )
    monkeypatch.setattr(standalone_runtime, "is_exit_command", fake_is_exit_command)

    deps = standalone_runtime.StandaloneRuntimeDependencies(
        stream_response=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        image_command_handler=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        run_concurrent_repl=fake_run_concurrent_repl,
        slash_services_factory=lambda _svc: standalone_slash_adapter.StandaloneSlashServices(),
        sync_slash_adapter_io=lambda: None,
        get_tui_output=lambda _scope: None,
    )

    await standalone_runtime.run_standalone_chat(
        model=None,
        session_id="agent:main:standalone:test",
        deps=deps,
    )

    assert surfaces == [Surface.CLI_STANDALONE]
