from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, cast

import pytest

from opensquilla.cli import chat_cmd
from opensquilla.cli.repl.stream import TurnResult
from opensquilla.cli.tui.contracts import TuiOutputHandle
from opensquilla.engine.commands import Surface


class _RecordingOutputHandle:
    def __init__(self) -> None:
        self._approval_surface = object()

    @property
    def approval_surface(self) -> object:
        return self._approval_surface

    async def write_through(self, payload: str) -> None:
        return None

    def stream_output(self):
        raise AssertionError("stream_output is only consumed by renderers")


class _FalseyCallable:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def __bool__(self) -> bool:
        return False


class _FalseyConsole:
    def __bool__(self) -> bool:
        return False


class _ApprovalRenderer:
    buffer = ""

    def __init__(self, **_kwargs: Any) -> None:
        return None

    def __enter__(self) -> _ApprovalRenderer:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    async def aappend_text(self, _delta: str) -> None:
        return None

    def tool_start(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def tool_finished(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def pulse(self) -> None:
        return None

    def status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def finalize(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ApprovalClient:
    async def send_message(self, *_args: Any, **_kwargs: Any):
        yield {
            "event": "session.event.tool_result",
            "result": {
                "status": "approval_required",
                "approval_id": "approval-1",
                "command": "touch file",
            },
            "tool_use_id": "tool-1",
        }
        yield {"event": "session.event.done", "reason": "stop"}

    async def resolve_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def abort_session(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _SessionManager:
    def __init__(self) -> None:
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            agent_id="main",
            origin=None,
        )

    async def get_session(self, session_key: str) -> object | None:
        return self.node if session_key == self.node.session_key else None

    async def update(self, session_key: str, **fields: Any) -> object:
        for key, value in fields.items():
            setattr(self.node, key, value)
        return self.node


def _sandbox_config() -> SimpleNamespace:
    return SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )


def test_default_turn_stream_dependencies_preserves_explicit_falsey_overrides() -> None:
    from opensquilla.cli.repl import turn_stream

    renderer_factory = _FalseyCallable()
    output_console = _FalseyConsole()

    deps = turn_stream.default_turn_stream_dependencies(
        renderer_factory=renderer_factory,
        output_console=output_console,
    )

    assert deps.renderer_factory is renderer_factory
    assert deps.output_console is output_console


def test_turn_stream_dependencies_preserve_frontend_owned_approval_surfaces() -> None:
    from opensquilla.cli.repl import turn_stream

    gateway_surface = object()
    standalone_surface = object()

    deps = turn_stream.default_turn_stream_dependencies(
        gateway_approval_surface=gateway_surface,
        standalone_approval_surface=standalone_surface,
    )

    assert deps.gateway_approval_surface is gateway_surface
    assert deps.standalone_approval_surface is standalone_surface


def test_turn_stream_approval_surface_uses_structural_output_handle_value() -> None:
    from opensquilla.cli.repl import turn_stream

    output = _RecordingOutputHandle()
    default_surface = object()

    assert (
        turn_stream.approval_surface_for_tui_output(output, default_surface)
        is output.approval_surface
    )


def test_tui_turn_bridge_supplies_cli_approval_surface_defaults() -> None:
    from opensquilla.cli.repl import turn_bridge

    deps = turn_bridge.default_turn_stream_dependencies()

    assert deps.gateway_approval_surface is Surface.CLI_GATEWAY
    assert deps.standalone_approval_surface is Surface.CLI_STANDALONE


def test_tui_turn_bridge_rejects_non_surface_output_approval_value() -> None:
    from opensquilla.cli.repl import turn_bridge

    assert (
        turn_bridge.approval_surface_for_tui_output(
            _RecordingOutputHandle(),
            Surface.CLI_STANDALONE,
        )
        is Surface.CLI_STANDALONE
    )


@pytest.mark.asyncio
async def test_tui_gateway_stream_coerces_invalid_output_approval_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import turn_bridge
    from opensquilla.cli.tui import turn_stream_defaults

    captured: dict[str, Any] = {}

    async def fake_approval_handler(*_args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    # The default approval handler is built lazily per dependency set; patch
    # its factory so the coerced surface kwarg can be observed.
    monkeypatch.setattr(
        turn_stream_defaults,
        "tui_approval_handler",
        lambda: fake_approval_handler,
    )

    deps = turn_bridge.default_turn_stream_dependencies(
        renderer_factory=_ApprovalRenderer,
    )
    await turn_bridge.stream_response_gateway(
        _ApprovalClient(),
        "agent:main:test",
        "hello",
        tui_output=_RecordingOutputHandle(),
        deps=deps,
    )

    assert captured["surface"] is Surface.CLI_GATEWAY


@pytest.mark.asyncio
async def test_local_approval_resolver_threads_sandbox_choice(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import turn_stream
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import build_path_approval_params
    from opensquilla.sandbox.path_validation import MountDecision

    reset_approval_queue()
    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(outside.resolve(strict=False)),
            access="ro",
            reason="outside_sandbox_mounts",
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval_id = get_approval_queue().request(namespace="exec", params=params)
    captured: dict[str, Any] = {}

    async def fake_apply_sandbox_approval_choice(
        params: dict[str, Any] | None,
        *,
        approval_id: str | None = None,
        choice: str | None,
        approved: bool,
        session_manager: object,
        config: object,
    ) -> None:
        captured.update(
            {
                "params": params,
                "approval_id": approval_id,
                "choice": choice,
                "approved": approved,
                "session_manager": session_manager,
                "config": config,
            }
        )

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_approvals.apply_sandbox_approval_choice",
        fake_apply_sandbox_approval_choice,
    )

    resolver = turn_stream.local_approval_resolver(
        session_manager=manager,
        config=_sandbox_config(),
    )

    await resolver(approval_id, True, choice="allow_same_type")

    pending = get_approval_queue().get(approval_id)
    assert pending.resolved is True
    assert pending.approved is True
    assert pending.claim_token is None
    assert captured["choice"] == "allow_same_type"
    assert captured["approved"] is True
    assert captured["params"] == params
    assert captured["approval_id"] == approval_id
    assert captured["session_manager"] is manager

    reset_approval_queue()


@pytest.mark.asyncio
async def test_turn_stream_adapter_threads_gateway_tui_output_without_chat_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import turn_stream

    captured: dict[str, Any] = {}

    async def fake_stream_response_gateway(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult:
        captured.update(
            {
                "client": client,
                "session_key": session_key,
                "message": message,
                "elevated_state": elevated_state,
                "attachments": attachments,
                "tui_output": tui_output,
            }
        )
        return TurnResult(text="ok")

    monkeypatch.setattr(turn_stream, "stream_response_gateway", fake_stream_response_gateway)
    output = _RecordingOutputHandle()

    result = await turn_stream.dispatch_gateway_stream(
        cast(turn_stream.GatewayStreamingClient, object()),
        "agent:main:test",
        "hello",
        {"mode": "bypass"},
        attachments=[{"id": "file-1"}],
        tui_output=output,
    )

    assert result.text == "ok"
    assert captured == {
        "client": captured["client"],
        "session_key": "agent:main:test",
        "message": "hello",
        "elevated_state": {"mode": "bypass"},
        "attachments": [{"id": "file-1"}],
        "tui_output": output,
    }


def test_turn_stream_adapter_has_no_raw_prompt_application_dependency() -> None:
    from opensquilla.cli.repl import turn_stream

    source = inspect.getsource(turn_stream)
    blocked_module = "prompt" + "_toolkit"

    assert blocked_module not in source
    assert "ChatApplication" not in source


@pytest.mark.asyncio
async def test_chat_cmd_stream_wrapper_uses_bridge_owned_renderer_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import turn_stream
    from opensquilla.cli.tui.adapters import runtime_bridge
    from opensquilla.cli.tui.opentui import renderer as opentui_renderer

    captured: dict[str, Any] = {}

    class _TemporaryOpenTuiRenderer:
        pass

    async def fake_stream_response_gateway(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
        deps: turn_stream.TurnStreamDependencies | None = None,
    ) -> TurnResult:
        captured["renderer_dependency"] = None if deps is None else deps.renderer_factory
        return TurnResult(text="ok")

    # The bridge resolves the renderer from the selected backend, sourcing the
    # class from the opentui renderer module — pin both halves of that wiring.
    monkeypatch.setattr(
        runtime_bridge,
        "validate_tui_backend_selection",
        lambda env=None: "opentui",
    )
    monkeypatch.setattr(
        opentui_renderer,
        "OpenTuiStreamRenderer",
        _TemporaryOpenTuiRenderer,
    )
    monkeypatch.setattr(turn_stream, "stream_response_gateway", fake_stream_response_gateway)

    result = await chat_cmd._stream_response_gateway(
        cast(Any, object()),
        "agent:main:test",
        "hello",
        {"mode": "bypass"},
    )

    assert result.text == "ok"
    assert captured == {"renderer_dependency": _TemporaryOpenTuiRenderer}
