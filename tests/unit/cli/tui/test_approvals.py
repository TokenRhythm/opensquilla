from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from opensquilla.cli.tui.adapters.approvals import (
    ApprovalChoice,
    decide_from_response,
    deny_decision,
    parse_approval_envelope,
    tui_approval_handler,
)
from opensquilla.engine.commands import Surface


class _RecordingRenderer:
    """Minimal async renderer capturing status lines."""

    def __init__(self, output_handle: Any | None = None, *, expose_handle: bool = True) -> None:
        if expose_handle:
            self.output_handle = output_handle
        self.statuses: list[tuple[str, str]] = []

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        self.statuses.append((message, style))


class _HeadlessRenderer:
    """Renderer with no output handle attribute at all (replay/eval shape)."""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        self.statuses.append((message, style))


class _HostOutputHandle:
    """Output handle exposing the OpenTUI approval round-trip."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.requests: list[dict[str, object]] = []

    async def request_approval(self, request: dict[str, object]) -> Any:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _WrappedHandle:
    """Plugin-wrapper shape: request_approval always callable, flag tells truth."""

    def __init__(self, *, supports: bool) -> None:
        self.supports_request_approval = supports
        self.requests: list[dict[str, object]] = []

    async def request_approval(self, request: dict[str, object]) -> Any:
        self.requests.append(request)
        return None

    async def write_through(self, payload: str) -> None:
        return None


class _NativeOutputHandle:
    """Native terminal handle shape: write-through only, no host IPC."""

    async def write_through(self, payload: str) -> None:
        return None


class _FakeConsole:
    def __init__(self, answers: list[str] | None = None, *, raise_eof: bool = False) -> None:
        self._answers = list(answers or [])
        self._raise_eof = raise_eof
        self.printed: list[str] = []
        self.prompts: list[str] = []

    def print(self, message: str) -> None:
        self.printed.append(message)

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._raise_eof or not self._answers:
            raise EOFError
        return self._answers.pop(0)


class _ExternalResolution:
    approved = True
    choice = None
    resolved_externally = True


class _FakeResponse:
    def __init__(self, approved: bool, choice: str | None = None) -> None:
        self.approved = approved
        self.choice = choice


class _Resolver:
    def __init__(self, error: Exception | None = None, result: Any = None) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []
        self._error = error
        self._result = result

    async def __call__(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        self.calls.append((approval_id, approved, choice))
        if self._error is not None:
            raise self._error
        return self._result


def _sandbox_envelope_payload() -> dict[str, Any]:
    return {
        "status": "approval_required",
        "approval_id": "appr-1",
        "message": "Network access needs approval.",
        "approvalKind": "sandbox_network",
        "host": "packages.example.test",
        "choices": [
            {"id": "allow_once", "label": "Allow once", "approved": True, "style": "primary"},
            {"id": "allow_same_type", "label": "Allow same type", "approved": True},
            {"id": "deny", "label": "Deny", "approved": False, "style": "danger"},
        ],
    }


def _exec_envelope_payload() -> dict[str, Any]:
    return {
        "status": "approval_required",
        "approval_id": "appr-2",
        "tool": "shell",
        "command": "touch demo.txt",
        "message": "This command needs approval.",
    }


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


def test_parse_approval_envelope_from_dict_and_json_string() -> None:
    payload = _sandbox_envelope_payload()

    from_dict = parse_approval_envelope(payload)
    from_string = parse_approval_envelope(json.dumps(payload))

    assert from_dict == from_string
    assert from_dict is not None
    assert from_dict.status == "approval_required"
    assert from_dict.approval_id == "appr-1"
    assert from_dict.tool == "sandbox_network"
    assert from_dict.summary == "network access to packages.example.test"
    assert from_dict.choices == (
        ApprovalChoice(id="allow_once", label="Allow once", approved=True),
        ApprovalChoice(id="allow_same_type", label="Allow same type", approved=True),
        ApprovalChoice(id="deny", label="Deny", approved=False),
    )
    assert from_dict.actionable


def test_parse_approval_envelope_exec_command_summary() -> None:
    envelope = parse_approval_envelope(_exec_envelope_payload())

    assert envelope is not None
    assert envelope.tool == "shell"
    assert envelope.summary == "touch demo.txt"


def test_parse_approval_envelope_blocked_and_pending() -> None:
    blocked = parse_approval_envelope(
        {
            "status": "blocked",
            "reason": "workspace_write_deny",
            "tool": "write_file",
            "path": "src/protected.py",
            "message": "write_file blocked by workspace write deny policy.",
        }
    )
    assert blocked is not None
    assert blocked.status == "blocked"
    assert not blocked.actionable

    pending = parse_approval_envelope(
        {"status": "approval_pending", "approval_id": "appr-9", "command": "ls"}
    )
    assert pending is not None
    assert pending.actionable


@pytest.mark.parametrize(
    "result",
    [
        None,
        42,
        ["not", "an", "envelope"],
        {"status": "success", "output": "ok"},
        {"status": "approval_denied", "approval_id": "appr-1"},
        "plain tool output",
        "{not valid json",
        '"a json string, not an object"',
        json.dumps({"status": "error", "message": "boom"}),
    ],
)
def test_parse_approval_envelope_rejects_non_approval_results(result: Any) -> None:
    assert parse_approval_envelope(result) is None


def test_decide_from_response_maps_choices_authoritatively() -> None:
    envelope = parse_approval_envelope(_sandbox_envelope_payload())
    assert envelope is not None

    # A named choice wins, and its approved flag is authoritative.
    assert decide_from_response(envelope, approved=True, choice="deny") == (False, "deny")
    # A bare approve maps to the first approving choice.
    assert decide_from_response(envelope, approved=True, choice=None) == (True, "allow_once")
    # A bare deny maps to the denying choice.
    assert decide_from_response(envelope, approved=False, choice=None) == (False, "deny")
    # Unknown choices fall back to polarity matching.
    assert decide_from_response(envelope, approved=False, choice="bogus") == (False, "deny")
    assert deny_decision(envelope) == (False, "deny")


def test_decide_from_response_without_choices_uses_boolean() -> None:
    envelope = parse_approval_envelope(_exec_envelope_payload())
    assert envelope is not None

    assert decide_from_response(envelope, approved=True, choice=None) == (True, None)
    assert decide_from_response(envelope, approved=False, choice=None) == (False, None)


# ---------------------------------------------------------------------------
# Handler behaviour
# ---------------------------------------------------------------------------


async def test_handler_is_noop_for_non_approval_results() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(True)))

    await handler({"status": "success", "output": "done"}, renderer, resolver)
    await handler("plain text result", renderer, resolver, surface=Surface.CLI_GATEWAY)
    await handler(None, renderer, resolver, elevated_state={"mode": None})

    assert resolver.calls == []
    assert renderer.statuses == []
    assert renderer.output_handle.requests == []


async def test_handler_renders_blocked_results_without_resolving() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(True)))

    await handler(
        {
            "status": "blocked",
            "tool": "write_file",
            "message": "write blocked by policy",
        },
        renderer,
        resolver,
    )

    assert resolver.calls == []
    assert renderer.output_handle.requests == []
    assert len(renderer.statuses) == 1
    message, _style = renderer.statuses[0]
    assert "blocked" in message
    assert "write_file" in message


async def test_handler_skips_resolution_when_envelope_has_no_id() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(True)))

    await handler(
        {"status": "approval_pending", "approval_id": "", "command": "ls"},
        renderer,
        resolver,
    )

    assert resolver.calls == []
    assert renderer.output_handle.requests == []
    assert len(renderer.statuses) == 1


async def test_handler_routes_through_host_overlay_and_resolves_approval() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    handle = _HostOutputHandle(_FakeResponse(True, choice=None))
    renderer = _RecordingRenderer(handle)

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert handle.requests == [
        {
            "id": "appr-1",
            "tool": "sandbox_network",
            "summary": "network access to packages.example.test",
            "choices": ["allow_once", "allow_same_type", "deny"],
            "message": "Network access needs approval.",
        }
    ]
    assert resolver.calls == [("appr-1", True, "allow_once")]


async def test_host_payload_omits_message_when_it_already_is_the_summary() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    handle = _HostOutputHandle(_FakeResponse(True, choice=None))
    renderer = _RecordingRenderer(handle)

    # No command/path/host: the summary falls back to the message itself, so
    # forwarding the message too would render the same line twice.
    await handler(
        {
            "status": "approval_required",
            "approval_id": "appr-3",
            "tool": "custom_tool",
            "message": "Custom action needs approval.",
        },
        renderer,
        resolver,
    )

    assert handle.requests == [
        {
            "id": "appr-3",
            "tool": "custom_tool",
            "summary": "Custom action needs approval.",
            "choices": [],
        }
    ]


async def test_handler_host_choice_response_is_authoritative() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    handle = _HostOutputHandle(_FakeResponse(True, choice="deny"))
    renderer = _RecordingRenderer(handle)

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-1", False, "deny")]


async def test_handler_denies_on_host_timeout() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    # request_approval returns None on timeout/teardown — the handler must deny.
    handle = _HostOutputHandle(None)
    renderer = _RecordingRenderer(handle)

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-1", False, "deny")]


async def test_handler_does_not_reresolve_approval_completed_in_webui() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _RecordingRenderer(_HostOutputHandle(_ExternalResolution()))

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == []
    assert any("another client" in message for message, _style in renderer.statuses)


async def test_handler_denies_on_dead_bridge() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    handle = _HostOutputHandle(RuntimeError("bridge is gone"))
    renderer = _RecordingRenderer(handle)

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-2", False, None)]


async def test_handler_survives_resolver_failure() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver(error=RuntimeError("rpc down"))
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(True)))

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-2", True, None)]
    assert any("failed to resolve" in message for message, _style in renderer.statuses)


async def test_handler_uses_canonical_cross_surface_resolution() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver(result={"resolved": True, "pending": False, "approved": True})
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(False)))

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-2", False, None)]
    assert renderer.statuses[-1] == (
        "approval approved by another surface — shell",
        "green",
    )


async def test_handler_renders_inflight_cross_surface_resolution_neutrally() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver(
        result={
            "resolved": False,
            "pending": True,
            "approved": False,
            "resolutionInProgress": True,
        }
    )
    renderer = _RecordingRenderer(_HostOutputHandle(_FakeResponse(True)))

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert renderer.statuses[-1] == (
        "approval is being resolved on another surface — shell",
        "yellow",
    )


async def test_handler_ignores_wrapped_handle_without_host_capability() -> None:
    """Plugin wrappers always expose a callable request_approval; the handler
    must honour the capability flag and fall back to the console prompt."""
    console = _FakeConsole(answers=["y"])
    handler = tui_approval_handler(output_console=console)
    resolver = _Resolver()
    handle = _WrappedHandle(supports=False)
    renderer = _RecordingRenderer(handle)

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert handle.requests == []
    assert resolver.calls == [("appr-2", True, None)]
    assert console.prompts == ["Approve? [y/N]: "]


async def test_native_console_prompt_approves_and_denies() -> None:
    resolver = _Resolver()
    renderer = _RecordingRenderer(_NativeOutputHandle())

    approve_console = _FakeConsole(answers=["y"])
    await tui_approval_handler(output_console=approve_console)(
        _exec_envelope_payload(), renderer, resolver
    )
    deny_console = _FakeConsole(answers=["n"])
    await tui_approval_handler(output_console=deny_console)(
        _exec_envelope_payload(), renderer, resolver
    )
    default_console = _FakeConsole(answers=[""])
    await tui_approval_handler(output_console=default_console)(
        _exec_envelope_payload(), renderer, resolver
    )

    assert resolver.calls == [
        ("appr-2", True, None),
        ("appr-2", False, None),
        ("appr-2", False, None),  # bare Enter must never approve
    ]
    assert any("approval required: shell" in line for line in approve_console.printed)
    assert any("touch demo.txt" in line for line in approve_console.printed)


async def test_native_console_prompt_supports_numbered_choices() -> None:
    console = _FakeConsole(answers=["2"])
    handler = tui_approval_handler(output_console=console)
    resolver = _Resolver()
    renderer = _RecordingRenderer(_NativeOutputHandle())

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-1", True, "allow_same_type")]
    assert console.prompts == ["Approve? [y/N/1-3]: "]
    assert any("1) Allow once" in line for line in console.printed)


async def test_native_console_prompt_denies_on_eof() -> None:
    console = _FakeConsole(raise_eof=True)
    handler = tui_approval_handler(output_console=console)
    resolver = _Resolver()
    renderer = _RecordingRenderer(_NativeOutputHandle())

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-1", False, "deny")]


async def test_native_console_prompt_denies_on_out_of_range_choice() -> None:
    console = _FakeConsole(answers=["9"])
    handler = tui_approval_handler(output_console=console)
    resolver = _Resolver()
    renderer = _RecordingRenderer(_NativeOutputHandle())

    await handler(_sandbox_envelope_payload(), renderer, resolver)

    assert resolver.calls == [("appr-1", False, "deny")]


async def test_cancelled_console_prompt_denies_and_reaps_the_reader() -> None:
    """Cancelling the turn mid-prompt must deny the pending approval and wait
    for the in-flight reader before propagating, so the runtime's next stdin
    reader never races an orphaned one for the user's next line."""
    reader_started = asyncio.Event()
    release_reader = asyncio.Event()
    reader_finished = asyncio.Event()

    async def blocking_reader(prompt: str) -> str:
        reader_started.set()
        try:
            await release_reader.wait()
        finally:
            reader_finished.set()
        return "y"

    handler = tui_approval_handler(output_console=_FakeConsole(), prompt_reader=blocking_reader)
    resolver = _Resolver()
    renderer = _RecordingRenderer(_NativeOutputHandle())

    turn = asyncio.create_task(handler(_exec_envelope_payload(), renderer, resolver))
    await reader_started.wait()
    turn.cancel()
    # The cancelled handler stays alive until the reader is reaped.
    done, _pending = await asyncio.wait({turn}, timeout=0.05)
    assert not done
    assert resolver.calls == [("appr-2", False, None)]

    release_reader.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert reader_finished.is_set()
    # The reaped reader's late answer is discarded, never re-resolved.
    assert resolver.calls == [("appr-2", False, None)]


async def test_cancelled_console_prompt_reaps_reader_even_when_deny_rpc_fails() -> None:
    release_reader = asyncio.Event()

    async def blocking_reader(prompt: str) -> str:
        await release_reader.wait()
        return "y"

    handler = tui_approval_handler(output_console=_FakeConsole(), prompt_reader=blocking_reader)
    resolver = _Resolver(error=RuntimeError("rpc down"))
    renderer = _RecordingRenderer(_NativeOutputHandle())

    turn = asyncio.create_task(handler(_exec_envelope_payload(), renderer, resolver))
    await asyncio.sleep(0)
    turn.cancel()
    done, _pending = await asyncio.wait({turn}, timeout=0.05)
    assert not done
    assert resolver.calls == [("appr-2", False, None)]

    release_reader.set()
    with pytest.raises(asyncio.CancelledError):
        await turn


async def test_headless_renderer_gets_notice_and_no_resolution() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _HeadlessRenderer()

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert resolver.calls == []
    assert len(renderer.statuses) == 1
    message, _style = renderer.statuses[0]
    assert "approval required" in message


async def test_renderer_with_none_output_handle_gets_notice() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    renderer = _RecordingRenderer(None)

    await handler(_exec_envelope_payload(), renderer, resolver)

    assert resolver.calls == []
    assert len(renderer.statuses) == 1


# ---------------------------------------------------------------------------
# Default wiring
# ---------------------------------------------------------------------------


def test_default_turn_stream_dependencies_wires_interactive_handler() -> None:
    from opensquilla.cli.tui.adapters import turn_stream_defaults

    deps = turn_stream_defaults.default_turn_stream_dependencies()

    assert deps.approval_handler is not turn_stream_defaults._noop_approval_handler
    assert deps.approval_handler.__module__ == "opensquilla.cli.tui.adapters.approvals"


def test_default_turn_stream_dependencies_keeps_explicit_handler() -> None:
    from opensquilla.cli.tui.adapters import turn_stream_defaults

    async def explicit_handler(*_args: Any, **_kwargs: Any) -> None:
        return None

    deps = turn_stream_defaults.default_turn_stream_dependencies(approval_handler=explicit_handler)

    assert deps.approval_handler is explicit_handler


async def test_default_handler_resolves_through_host_capable_renderer() -> None:
    """End-to-end over the default wiring: an approval envelope reaching the
    handler via a host-capable renderer is presented and resolved."""
    from opensquilla.cli.tui.adapters import turn_stream_defaults

    deps = turn_stream_defaults.default_turn_stream_dependencies()
    resolver = _Resolver()
    handle = _HostOutputHandle(_FakeResponse(True, choice=None))
    renderer = _RecordingRenderer(handle)

    await deps.approval_handler(
        json.dumps(_exec_envelope_payload()),
        renderer,
        resolver,
        elevated_state=None,
        surface=Surface.CLI_GATEWAY,
    )

    assert [request["id"] for request in handle.requests] == ["appr-2"]
    assert resolver.calls == [("appr-2", True, None)]


async def test_host_payload_strips_control_bytes_and_dedups_after_sanitizing() -> None:
    handler = tui_approval_handler(output_console=_FakeConsole())
    resolver = _Resolver()
    handle = _HostOutputHandle(_FakeResponse(True, choice=None))
    renderer = _RecordingRenderer(handle)

    # The message differs from the summary only by embedded control bytes:
    # after sanitizing they render identically, so no message is forwarded.
    await handler(
        {
            "status": "approval_required",
            "approval_id": "appr-4",
            "tool": "shell",
            "command": "echo\x1b[31m ok\x07",
            "message": "echo ok",
        },
        renderer,
        resolver,
    )

    assert handle.requests == [
        {
            "id": "appr-4",
            "tool": "shell",
            "summary": "echo ok",
            "choices": [],
        }
    ]
