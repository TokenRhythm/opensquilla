from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import pytest

from opensquilla.cli.tui.opentui.messages import (
    CompletionContext,
    HistoryMessage,
    HistoryReplace,
    HostApprovalResponse,
    HostCompletionRequest,
    HostError,
    HostInputCancel,
    HostInputEof,
    HostInputSubmit,
    HostProtocolUnknown,
    HostResize,
    RouterPluginState,
    ScrollbackWrite,
)
from opensquilla.cli.tui.opentui.surface import (
    OpenTuiOutputHandle,
    OpenTuiSurface,
    open_opentui_surface,
)
from opensquilla.engine.commands import Surface


class FakeOpenTuiBridge:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[tuple[str, dict[str, Any] | None]] = []

    async def send(self, message_type: str, payload: object | None = None) -> None:
        if payload is None:
            self.sent.append((message_type, None))
            return
        self.sent.append((message_type, payload if isinstance(payload, dict) else asdict(payload)))

    async def next_message(self) -> object | None:
        return await self.messages.get()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_opentui_surface_returns_submitted_lines_and_eof() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostInputSubmit(text="中文 prompt"))
    assert await surface.next_line() == "中文 prompt"

    bridge.messages.put_nowait(HostInputEof())
    assert await surface.next_line() is None


@pytest.mark.asyncio
async def test_open_surface_hydrates_history_before_enabling_input() -> None:
    bridge = FakeOpenTuiBridge()
    history = HistoryReplace(
        session_key="agent:main:session-1",
        history_scope="complete",
        loaded_count=1,
        messages=(HistoryMessage(id="m1", role="user", text="persisted"),),
    )

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        bridge=bridge,
        history_replace=history,
        print_ready_marker=False,
    ):
        pass

    message_types = [message_type for message_type, _payload in bridge.sent]
    assert message_types[:3] == ["composer.set", "history.replace", "composer.set"]
    assert bridge.sent[0][1]["disabled"] is True
    assert bridge.sent[1][1]["session_key"] == "agent:main:session-1"
    assert bridge.sent[1][1]["messages"][0]["id"] == "m1"
    assert bridge.sent[2][1]["disabled"] is False


@pytest.mark.asyncio
async def test_opentui_surface_delegates_cancel_and_keeps_waiting() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)
    cancelled: list[str] = []

    surface.set_cancel_callback(lambda: cancelled.append("cancel"))
    bridge.messages.put_nowait(HostInputCancel())
    bridge.messages.put_nowait(HostInputSubmit(text="after cancel"))

    assert await surface.next_line() == "after cancel"
    assert cancelled == ["cancel"]


@pytest.mark.asyncio
async def test_host_error_frames_are_non_fatal_diagnostics() -> None:
    """The host recovers from per-message failures and keeps running; one
    error frame must not end the session."""
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    for _ in range(3):
        bridge.messages.put_nowait(HostError(message="render hiccup"))
    bridge.messages.put_nowait(HostInputSubmit(text="still alive"))

    assert await surface.next_line() == "still alive"


@pytest.mark.asyncio
async def test_uninterrupted_host_error_flood_tears_the_session_down() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    for _ in range(8):
        bridge.messages.put_nowait(HostError(message="dispatch is broken"))

    with pytest.raises(RuntimeError, match="OpenTUI host error: dispatch is broken"):
        await surface.next_line()


@pytest.mark.asyncio
async def test_host_error_streak_resets_on_any_other_frame() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    for _ in range(7):
        bridge.messages.put_nowait(HostError(message="hiccup"))
    bridge.messages.put_nowait(HostResize(width=100, height=30))
    for _ in range(7):
        bridge.messages.put_nowait(HostError(message="hiccup"))
    bridge.messages.put_nowait(HostInputSubmit(text="survived"))

    assert await surface.next_line() == "survived"


@pytest.mark.asyncio
async def test_protocol_unknown_frames_are_logged_and_skipped() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostProtocolUnknown(message_type="future.type"))
    bridge.messages.put_nowait(HostInputSubmit(text="next"))

    assert await surface.next_line() == "next"


@pytest.mark.asyncio
async def test_resize_frames_update_last_known_size() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    assert surface.last_known_size is None
    bridge.messages.put_nowait(HostResize(width=120, height=40))
    bridge.messages.put_nowait(HostInputSubmit(text="go"))

    assert await surface.next_line() == "go"
    assert surface.last_known_size == (120, 40)


@pytest.mark.asyncio
async def test_approval_response_resolves_waiter_and_next_line_keeps_pumping() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)
    output = surface.output_handle

    request_task = asyncio.create_task(
        output.request_approval(
            {"id": "appr-1", "tool": "shell", "summary": "touch demo.txt", "choices": []},
        )
    )
    await asyncio.sleep(0)
    assert bridge.sent[-1][0] == "approval.request"
    assert bridge.sent[-1][1]["id"] == "appr-1"

    bridge.messages.put_nowait(HostApprovalResponse(id="appr-1", approved=True, choice=None))
    bridge.messages.put_nowait(HostInputSubmit(text="next input"))

    # The decision frame resolves the waiter and is never surfaced as input.
    assert await surface.next_line() == "next input"
    response = await request_task
    assert response == HostApprovalResponse(id="appr-1", approved=True, choice=None)
    # A delivered decision already closed the overlay host-side; no dismiss.
    assert "approval.dismiss" not in [message_type for message_type, _payload in bridge.sent]


@pytest.mark.asyncio
async def test_gateway_approval_resolution_reuses_and_dismisses_pushed_overlay() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    request = {
        "id": "appr-shared",
        "tool": "shell",
        "summary": "echo hello",
        "choices": [],
    }

    assert await output.present_gateway_approval(request) is True
    waiter = asyncio.create_task(output.request_approval(request))
    await asyncio.sleep(0)
    assert [kind for kind, _payload in bridge.sent].count("approval.request") == 1

    matched = await output.resolve_gateway_approval(
        "appr-shared",
        approved=True,
        resolution="approved",
    )
    response = await waiter

    assert matched is True
    assert response is not None
    assert response.approved is True
    assert response.resolved_externally is True
    assert bridge.sent[-1][0] == "approval.dismiss"


@pytest.mark.asyncio
async def test_canonical_approval_refreshes_push_first_overlay_choices() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    preview = {
        "id": "appr-richer",
        "tool": "shell",
        "summary": "echo hello",
        "choices": [],
    }
    canonical = {
        **preview,
        "choices": ["allow_once", "allow_same_type"],
    }

    assert await output.present_gateway_approval(preview) is True
    waiter = asyncio.create_task(output.request_approval(canonical))
    await asyncio.sleep(0)

    requests = [payload for kind, payload in bridge.sent if kind == "approval.request"]
    assert requests == [preview, canonical]

    await output.resolve_gateway_approval(
        "appr-richer",
        approved=True,
        resolution="allow_same_type",
    )
    response = await waiter
    assert response is not None
    assert response.approved is True
    assert response.resolution == "allow_same_type"


@pytest.mark.asyncio
async def test_late_push_preview_does_not_downgrade_canonical_approval() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    canonical = {
        "id": "appr-canonical-first",
        "tool": "shell",
        "summary": "echo hello",
        "choices": ["allow_once", "allow_same_type"],
    }
    preview = {**canonical, "choices": []}

    waiter = asyncio.create_task(output.request_approval(canonical))
    await asyncio.sleep(0)
    assert await output.present_gateway_approval(preview) is True

    requests = [payload for kind, payload in bridge.sent if kind == "approval.request"]
    assert requests == [canonical]

    await output.resolve_gateway_approval(
        "appr-canonical-first",
        approved=False,
        resolution="denied",
    )
    response = await waiter
    assert response is not None
    assert response.approved is False


@pytest.mark.asyncio
async def test_gateway_resolution_wins_race_with_uncommitted_local_keypress() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    request = {"id": "appr-race", "tool": "shell", "summary": "", "choices": []}
    waiter = asyncio.create_task(output.request_approval(request))
    await asyncio.sleep(0)

    output.deliver_approval_response(
        HostApprovalResponse(id="appr-race", approved=False, choice=None)
    )
    await output.resolve_gateway_approval(
        "appr-race",
        approved=True,
        resolution="approved",
    )

    response = await waiter
    assert response is not None
    assert response.approved is True
    assert response.resolved_externally is True


@pytest.mark.asyncio
async def test_late_preview_cannot_reopen_locally_resolved_overlay() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    canonical = {
        "id": "appr-local-race",
        "tool": "shell",
        "summary": "echo hello",
        "choices": ["allow_once", "deny"],
    }
    preview = {**canonical, "choices": []}
    waiter = asyncio.create_task(output.request_approval(canonical))
    await asyncio.sleep(0)

    assert output.deliver_approval_response(
        HostApprovalResponse(id="appr-local-race", approved=True, choice="allow_once")
    )
    # Reproduce the exact interleaving: the local future is complete, but its
    # coroutine has not resumed when a delayed pushed preview arrives.
    assert await output.present_gateway_approval(preview) is True

    response = await waiter
    assert response is not None
    assert response.approved is True
    requests = [payload for kind, payload in bridge.sent if kind == "approval.request"]
    assert requests == [canonical]
    assert output._approval_waiters == {}
    assert output._approval_requests == {}


@pytest.mark.asyncio
async def test_gateway_disconnect_fails_pending_approval_closed_and_dismisses() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    waiter = asyncio.create_task(
        output.request_approval(
            {"id": "appr-disconnect", "tool": "shell", "summary": "", "choices": []}
        )
    )
    await asyncio.sleep(0)

    await output.fail_pending_gateway_approvals()

    assert await waiter is None
    assert bridge.sent[-1] == ("approval.dismiss", {"id": "appr-disconnect"})


@pytest.mark.asyncio
async def test_plugin_output_wrapper_forwards_gateway_approval_lifecycle() -> None:
    from opensquilla.cli.tui.adapters.runtime_helpers import TuiPluginOutputHandle

    bridge = FakeOpenTuiBridge()
    inner = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    output = TuiPluginOutputHandle(inner, plugin_manager=object())
    request = {"id": "appr-wrapper", "tool": "shell", "summary": "", "choices": []}

    assert await output.present_gateway_approval(request) is True
    assert (
        await output.resolve_gateway_approval("appr-wrapper", approved=False, resolution="denied")
        is True
    )

    assert [kind for kind, _payload in bridge.sent] == [
        "approval.request",
        "approval.dismiss",
    ]


@pytest.mark.asyncio
async def test_approval_waiter_survives_next_line_task_recreation() -> None:
    """The waiter registry lives on the surface's output handle, so a pending
    approval resolves even when the runtime cancels and re-creates its input
    task while the request is outstanding."""
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)
    output = surface.output_handle

    request_task = asyncio.create_task(
        output.request_approval({"id": "appr-2", "tool": "shell", "summary": "", "choices": []})
    )
    await asyncio.sleep(0)

    first_reader = asyncio.create_task(surface.next_line())
    await asyncio.sleep(0)
    first_reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_reader

    bridge.messages.put_nowait(HostApprovalResponse(id="appr-2", approved=False, choice="deny"))
    bridge.messages.put_nowait(HostInputSubmit(text="after recreation"))

    assert await surface.next_line() == "after recreation"
    response = await request_task
    assert response == HostApprovalResponse(id="appr-2", approved=False, choice="deny")


@pytest.mark.asyncio
async def test_request_approval_times_out_to_none() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    response = await output.request_approval(
        {"id": "appr-3", "tool": "shell", "summary": "", "choices": []},
        timeout=0.01,
    )

    assert response is None


@pytest.mark.asyncio
async def test_attachment_output_api_tracks_sanitized_state_and_clears_ready() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    attachment_id = await output.add_attachment(
        kind="file",
        label="/private/tmp/redaction-fixture/brief.pdf",
    )
    assert await output.update_attachment(attachment_id, status="uploading") is True
    assert await output.update_attachment(attachment_id, status="ready") is True
    assert await output.clear_attachments(status="ready") == 1

    assert [message_type for message_type, _payload in bridge.sent] == [
        "attachment.add",
        "attachment.update",
        "attachment.update",
        "attachment.clear",
    ]
    assert bridge.sent[0][1] == {
        "id": attachment_id,
        "kind": "file",
        "label": "brief.pdf",
        "status": "reading",
        "message": "",
    }
    assert "/private/tmp/redaction-fixture" not in str(bridge.sent)


@pytest.mark.asyncio
async def test_attachment_failure_remains_visible_and_redacts_diagnostics() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    attachment_id = await output.add_attachment(
        kind="image",
        label=r"C:\private\secret.png",
    )
    await output.update_attachment(
        attachment_id,
        status="failed",
        message=r"Could not read C:\private\secret.png (12345 bytes)",
    )

    assert await output.clear_attachments(status="ready") == 0
    failure = bridge.sent[-1][1]
    assert failure["status"] == "failed"
    assert failure["message"] == "Could not read <path> (<size>)"
    assert "private" not in str(bridge.sent)
    assert "12345" not in str(bridge.sent)


@pytest.mark.asyncio
async def test_pending_duplicate_attachment_reuses_one_chip() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    first = await output.add_attachment(kind="file", label="brief.pdf")
    second = await output.add_attachment(kind="file", label="brief.pdf")

    assert first == second
    assert [kind for kind, _payload in bridge.sent] == ["attachment.add"]


@pytest.mark.asyncio
async def test_attachment_remove_api_is_idempotent() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    attachment_id = await output.add_attachment(kind="path", label="report.md")

    assert await output.remove_attachment(attachment_id) is True
    assert await output.remove_attachment(attachment_id) is False
    assert [kind for kind, _payload in bridge.sent] == [
        "attachment.add",
        "attachment.remove",
    ]


@pytest.mark.asyncio
async def test_request_approval_timeout_dismisses_host_overlay() -> None:
    """A timed-out approval must close the host overlay, or the stale modal
    swallows the user's next Enter/Esc/y/n keypress."""
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    response = await output.request_approval(
        {"id": "appr-6", "tool": "shell", "summary": "", "choices": []},
        timeout=0.01,
    )

    assert response is None
    assert bridge.sent[-1] == ("approval.dismiss", {"id": "appr-6"})


@pytest.mark.asyncio
async def test_cancelled_approval_request_dismisses_host_overlay() -> None:
    """Turn cancellation (Ctrl+C) stops waiting on the overlay; the host must
    be told to close it instead of leaving a dead modal mounted."""
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    request_task = asyncio.create_task(
        output.request_approval({"id": "appr-7", "tool": "shell", "summary": "", "choices": []})
    )
    await asyncio.sleep(0)
    assert bridge.sent[-1][0] == "approval.request"

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert bridge.sent[-1] == ("approval.dismiss", {"id": "appr-7"})
    assert output._approval_waiters == {}


@pytest.mark.asyncio
async def test_request_approval_returns_none_when_bridge_send_fails() -> None:
    bridge = _FailingBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    response = await output.request_approval(
        {"id": "appr-4", "tool": "shell", "summary": "", "choices": []},
    )

    assert response is None


@pytest.mark.asyncio
async def test_request_approval_without_id_returns_none_without_sending() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    assert await output.request_approval({"tool": "shell"}) is None
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_host_eof_denies_pending_approval_waiters() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)
    output = surface.output_handle

    request_task = asyncio.create_task(
        output.request_approval({"id": "appr-5", "tool": "shell", "summary": "", "choices": []})
    )
    await asyncio.sleep(0)

    bridge.messages.put_nowait(HostInputEof())
    assert await surface.next_line() is None
    assert await request_task is None


@pytest.mark.asyncio
async def test_unmatched_approval_response_is_skipped_and_loop_continues() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostApprovalResponse(id="ghost", approved=True, choice=None))
    bridge.messages.put_nowait(HostInputSubmit(text="still serving input"))

    assert await surface.next_line() == "still serving input"


@pytest.mark.asyncio
async def test_opentui_output_handle_writes_to_scrollback() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    await output.write_through("tool output\nfinal answer")

    assert bridge.sent == [
        (
            "scrollback.write",
            asdict(ScrollbackWrite(text="tool output\nfinal answer")),
        )
    ]


@pytest.mark.asyncio
async def test_open_opentui_surface_sends_completion_context_on_startup() -> None:
    bridge = FakeOpenTuiBridge()

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        ready_marker="",
        print_ready_marker=False,
        bridge=bridge,
        completion_context=CompletionContext(catalog=(), files=()),
    ):
        pass

    assert bridge.sent == [
        (
            "composer.set",
            {
                "placeholder": "send a message",
                "text": "",
                "disabled": False,
            },
        ),
        (
            "router.update",
            {
                "model": "default",
                "route": "gateway",
                "saving": "-",
                "context": "ready",
                "style": "dim",
                "baseline_model": "",
                "source": "new session",
                "routing_applied": False,
                "rollout_phase": "observe",
                "io": "",
            },
        ),
        (
            "context.update",
            {
                "agent": {"id": "main", "name": "main", "emoji": None},
                "task": "New session",
                "surface": "Web + TUI",
                "gateway": "connected",
                "model": "default",
                "permission": "normal",
                "workspace": "",
                "queue": "idle",
                "context": "",
            },
        ),
        (
            "completion.context",
            {
                "catalog": (),
                "files": (),
                "filters_sensitive_paths": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_open_opentui_surface_marks_ready_after_completion_context() -> None:
    bridge = FakeOpenTuiBridge()

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        ready_marker="READY",
        bridge=bridge,
        completion_context=CompletionContext(catalog=(), files=()),
    ):
        pass

    assert [message_type for message_type, _payload in bridge.sent] == [
        "composer.set",
        "router.update",
        "context.update",
        "completion.context",
        "scrollback.write",
    ]
    assert bridge.sent[-1] == (
        "scrollback.write",
        asdict(ScrollbackWrite(text="READY\n")),
    )


@pytest.mark.asyncio
async def test_open_opentui_surface_omits_ready_marker_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness sentinel is harness scaffolding — a real session must not
    render it unless the env var (or caller) explicitly opts in."""
    monkeypatch.delenv("OPENSQUILLA_TUI_READY_MARKER", raising=False)
    bridge = FakeOpenTuiBridge()

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        bridge=bridge,
        completion_context=CompletionContext(catalog=(), files=()),
    ):
        pass

    assert [message_type for message_type, _payload in bridge.sent] == [
        "composer.set",
        "router.update",
        "context.update",
        "completion.context",
    ]


@pytest.mark.asyncio
async def test_open_opentui_surface_env_var_opts_into_ready_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TUI_READY_MARKER", "HARNESS_READY")
    bridge = FakeOpenTuiBridge()

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        bridge=bridge,
        completion_context=CompletionContext(catalog=(), files=()),
    ):
        pass

    assert bridge.sent[-1] == (
        "scrollback.write",
        asdict(ScrollbackWrite(text="HARNESS_READY\n")),
    )


@pytest.mark.asyncio
async def test_open_opentui_surface_seeds_real_model_session_and_mode() -> None:
    bridge = FakeOpenTuiBridge()

    async with open_opentui_surface(
        surface=Surface.CLI_GATEWAY,
        model="openai/gpt-test",
        session_id="agent:main:existing",
        bridge=bridge,
        completion_context=CompletionContext(catalog=(), files=()),
    ):
        pass

    router_message = next(payload for kind, payload in bridge.sent if kind == "router.update")
    assert router_message["model"] == "openai/gpt-test"
    assert router_message["route"] == "gateway"
    assert router_message["source"] == "agent:main:existing"
    assert router_message["context"] == "ready"
    context_message = next(payload for kind, payload in bridge.sent if kind == "context.update")
    assert context_message["model"] == "openai/gpt-test"
    assert context_message["task"] == "Session"
    assert context_message["surface"] == "Web + TUI"
    assert context_message["gateway"] == "connected"
    assert "agent:main:existing" not in repr(context_message)


@pytest.mark.asyncio
async def test_opentui_surface_answers_file_completion_without_returning_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from opensquilla.cli.tui.opentui import surface as surface_module

    calls: list[tuple[object, str, int]] = []

    def fake_enumerate_workspace_files(root, *, query: str, max_results: int):
        calls.append((root, query, max_results))
        return ["foo.py", "src/foo_bar.py"]

    monkeypatch.setattr(
        surface_module,
        "enumerate_workspace_files",
        fake_enumerate_workspace_files,
    )
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(
        bridge,
        approval_surface=Surface.CLI_GATEWAY,
        workspace_dir=tmp_path,
    )

    bridge.messages.put_nowait(HostCompletionRequest(kind="file", query="foo", request_id=7))
    bridge.messages.put_nowait(HostInputSubmit(text="hello"))

    # Completion is served off the input loop so a queued submit is never
    # delayed behind enumeration; drain the tracked task before asserting.
    assert await surface.next_line() == "hello"
    completion_task = surface._completion_task
    assert completion_task is not None
    await completion_task
    assert calls == [(tmp_path, "foo", 50)]
    assert bridge.sent == [
        (
            "completion.response",
            {
                "request_id": 7,
                "kind": "file",
                "items": [
                    {
                        "label": "foo.py",
                        "description": "foo.py",
                        "insert_text": "@foo.py ",
                        "category": "file",
                    },
                    {
                        "label": "src/foo_bar.py",
                        "description": "src/foo_bar.py",
                        "insert_text": "@src/foo_bar.py ",
                        "category": "file",
                    },
                ],
            },
        )
    ]


@pytest.mark.asyncio
async def test_opentui_surface_answers_file_completion_with_empty_items_without_workspace() -> None:
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostCompletionRequest(kind="file", query="foo", request_id=8))
    bridge.messages.put_nowait(HostInputSubmit(text="after completion"))

    assert await surface.next_line() == "after completion"
    completion_task = surface._completion_task
    assert completion_task is not None
    await completion_task
    assert bridge.sent == [
        (
            "completion.response",
            {
                "request_id": 8,
                "kind": "file",
                "items": [],
            },
        )
    ]


@pytest.mark.asyncio
async def test_newer_completion_request_supersedes_older_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from opensquilla.cli.tui.opentui import surface as surface_module

    def fake_enumerate_workspace_files(root, *, query: str, max_results: int):
        return [f"{query}.py"]

    monkeypatch.setattr(
        surface_module,
        "enumerate_workspace_files",
        fake_enumerate_workspace_files,
    )
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(
        bridge,
        approval_surface=Surface.CLI_GATEWAY,
        workspace_dir=tmp_path,
    )

    bridge.messages.put_nowait(HostCompletionRequest(kind="file", query="old", request_id=1))
    bridge.messages.put_nowait(HostCompletionRequest(kind="file", query="new", request_id=2))
    bridge.messages.put_nowait(HostInputSubmit(text="go"))

    assert await surface.next_line() == "go"
    completion_task = surface._completion_task
    assert completion_task is not None
    await completion_task

    responses = [
        payload for message_type, payload in bridge.sent if message_type == "completion.response"
    ]
    assert [response["request_id"] for response in responses] == [2]


@pytest.mark.asyncio
async def test_completion_failure_does_not_kill_the_input_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from opensquilla.cli.tui.opentui import surface as surface_module

    def broken_enumerate(root, *, query: str, max_results: int):
        raise OSError("workspace walk failed")

    monkeypatch.setattr(surface_module, "enumerate_workspace_files", broken_enumerate)
    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(
        bridge,
        approval_surface=Surface.CLI_GATEWAY,
        workspace_dir=tmp_path,
    )

    bridge.messages.put_nowait(HostCompletionRequest(kind="file", query="q", request_id=3))
    bridge.messages.put_nowait(HostInputSubmit(text="still here"))

    assert await surface.next_line() == "still here"
    completion_task = surface._completion_task
    assert completion_task is not None
    await completion_task
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_open_opentui_surface_uses_explicit_workspace_for_completion_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from opensquilla.cli.tui.opentui import surface as surface_module

    bridge = FakeOpenTuiBridge()
    captured: dict[str, Any] = {}

    def fake_build_completion_context(
        surface: Surface,
        *,
        workspace_dir,
    ) -> CompletionContext:
        captured["surface"] = surface
        captured["workspace_dir"] = workspace_dir
        return CompletionContext(catalog=(), files=("src/main.py",))

    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_DIR", "/tmp/wrong-env-workspace")
    monkeypatch.setattr(
        surface_module,
        "build_completion_context",
        fake_build_completion_context,
    )

    async with open_opentui_surface(
        surface=Surface.CLI_STANDALONE,
        ready_marker="",
        print_ready_marker=False,
        bridge=bridge,
        workspace_dir=tmp_path,
    ):
        pass

    assert captured == {
        "surface": Surface.CLI_STANDALONE,
        "workspace_dir": tmp_path,
    }
    assert bridge.sent[-1] == (
        "completion.context",
        {
            "catalog": (),
            "files": ("src/main.py",),
            "filters_sensitive_paths": True,
        },
    )
    context_message = next(payload for kind, payload in bridge.sent if kind == "context.update")
    assert context_message["workspace"] == tmp_path.name
    assert str(tmp_path.parent) not in repr(context_message)
    assert context_message["surface"] == "TUI"
    assert context_message["gateway"] == "isolated"


def test_opentui_output_toolbar_invalidates_router_plugin() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "route standard -> fake-terminal 99% save 42%")
    output.set_toolbar("router_hud_style", "normal")
    output.set_toolbar("router_baseline_model", "vendor/big-model")
    output.set_toolbar("router_source", "router")
    output.set_toolbar("router_routing_applied", True)
    output.set_toolbar("router_rollout_phase", "full")
    output.invalidate()

    assert bridge.sent == [
        (
            "router.update",
            asdict(
                RouterPluginState(
                    model="fake-terminal",
                    route="standard 99%",
                    saving="42%",
                    context="-",
                    style="normal",
                    baseline_model="vendor/big-model",
                    source="router",
                    routing_applied=True,
                    rollout_phase="full",
                )
            ),
        )
    ]


def test_router_plugin_state_carries_observe_source_and_usage() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "observe standard -> fake-terminal 80%")
    output.set_toolbar("router_hud_style", "dim")
    output.set_toolbar("router_source", "observe")
    output.set_toolbar("router_routing_applied", False)
    output.set_toolbar("router_rollout_phase", "observe")
    output.set_toolbar("router_usage", "1.2k/856")
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["source"] == "observe"
    assert payload["routing_applied"] is False
    assert payload["rollout_phase"] == "observe"
    assert payload["context"] == "-"
    assert payload["io"] == "1.2k/856"


def test_router_plugin_state_formats_cumulative_context_usage_percent() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "observe standard -> fake-terminal 80%")
    output.set_toolbar("router_hud_style", "dim")
    output.set_toolbar("router_usage", "1/2")
    output.set_toolbar("router_session_input", 84_000)
    output.set_toolbar("router_context_window", 200_000)
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["context"] == "42%"
    assert payload["io"] == "1/2"


@pytest.mark.parametrize(
    ("session_input", "context_window"),
    [
        (None, 200_000),
        (84_000, None),
    ],
)
def test_router_plugin_state_reports_no_pressure_without_context_data(
    session_input: int | None,
    context_window: int | None,
) -> None:
    """Without a known context window there is no honest pressure value: ctx
    shows "-" and the io field still carries the turn's token pair (the old
    fallback packed the pair into ctx, reading like "1/2 of a window")."""
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "observe standard -> fake-terminal 80%")
    output.set_toolbar("router_hud_style", "dim")
    output.set_toolbar("router_usage", "1/2")
    output.set_toolbar("router_session_input", session_input)
    output.set_toolbar("router_context_window", context_window)
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["context"] == "-"
    assert payload["io"] == "1/2"


def test_router_plugin_state_reports_pressure_even_before_turn_usage() -> None:
    """Pressure derives from session input vs window and no longer waits on a
    turn-usage pair; io stays empty until a turn reports traffic."""
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_session_input", 84_000)
    output.set_toolbar("router_context_window", 200_000)
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["context"] == "42%"
    assert payload["io"] == ""


def test_router_plugin_state_clamps_context_usage_percent() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "observe standard -> fake-terminal 80%")
    output.set_toolbar("router_hud_style", "dim")
    output.set_toolbar("router_usage", "1.2k/856")
    output.set_toolbar("router_session_input", 250_000)
    output.set_toolbar("router_context_window", 200_000)
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["context"] == "100%"
    assert payload["io"] == "1.2k/856"


def test_router_plugin_state_fallback_keeps_defaults_and_usage() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.set_toolbar("router_hud", "fallback -> fake-terminal")
    output.set_toolbar("router_hud_style", "warning")
    output.set_toolbar("router_source", "fallback")
    output.set_toolbar("router_usage", "856/12")
    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["model"] == "fake-terminal"
    assert payload["route"] == "fallback"
    assert payload["source"] == "fallback"
    assert payload["context"] == "-"
    assert payload["io"] == "856/12"
    assert payload["style"] == "warning"


def test_router_plugin_state_pending_defaults_without_usage() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    output.invalidate()

    (_, payload) = bridge.sent[0]
    assert payload["model"] == "pending"
    assert payload["route"] == "pending"
    assert payload["context"] == "-"
    assert payload["baseline_model"] == ""
    assert payload["routing_applied"] is True
    assert payload["rollout_phase"] == "full"


@pytest.mark.asyncio
async def test_output_handle_send_message_forwards_to_bridge() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    await output.send_message("turn.begin", {"id": "t1"})
    await output.send_message("model.text", {"text": "hi"})

    assert bridge.sent == [
        ("turn.begin", {"id": "t1"}),
        ("model.text", {"text": "hi"}),
    ]


@pytest.mark.asyncio
async def test_stream_output_preserves_delta_order_while_pruning_tasks() -> None:
    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    async with output.stream_output() as write:
        for index in range(50):
            write(f"delta-{index}")
            await asyncio.sleep(0)

    texts = [
        payload["text"]
        for message_type, payload in bridge.sent
        if message_type == "scrollback.write"
    ]
    assert texts == [f"delta-{index}" for index in range(50)]


class _FailingBridge(FakeOpenTuiBridge):
    async def send(self, message_type: str, payload: object | None = None) -> None:
        raise RuntimeError("pipe down")


@pytest.mark.asyncio
async def test_stream_output_surfaces_write_failure_on_next_delta() -> None:
    bridge = _FailingBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    async with output.stream_output() as write:
        write("first")
        # Let the failed write task complete so the next delta reports it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="pipe down"):
            write("second")


@pytest.mark.asyncio
async def test_stream_output_surfaces_write_failure_at_context_exit() -> None:
    bridge = _FailingBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)

    with pytest.raises(RuntimeError, match="pipe down"):
        async with output.stream_output() as write:
            write("only")


@pytest.mark.asyncio
async def test_approval_payloads_are_sanitized_on_both_send_sites() -> None:
    # The choke point must hold for the canonical request AND the gateway-push
    # preview: reverting either call site to dict(request) must fail here.
    hostile = {
        "id": "appr-hostile",
        "tool": "sh\x1b[31mell",
        "summary": "echo\x1b]0;pwn\x07 ok",
        "message": "why\x1b[2Jnot",
        "choices": ["allow_once"],
    }

    bridge = FakeOpenTuiBridge()
    output = OpenTuiOutputHandle(bridge, approval_surface=Surface.CLI_GATEWAY)
    await output.request_approval(dict(hostile), timeout=0.01)
    # The timeout appends an approval.dismiss; the request frame is first.
    sent_type, sent_payload = bridge.sent[0]
    assert sent_type == "approval.request"
    assert sent_payload["tool"] == "shell"
    assert sent_payload["summary"] == "echo ok"
    assert sent_payload["message"] == "whynot"
    # The raw id list is protocol data (round-trips in approval.response) and
    # is display-stripped host-side instead.
    assert sent_payload["choices"] == ["allow_once"]

    preview_bridge = FakeOpenTuiBridge()
    preview_output = OpenTuiOutputHandle(preview_bridge, approval_surface=Surface.CLI_GATEWAY)
    assert await preview_output.present_gateway_approval(dict(hostile)) is True
    preview_type, preview_payload = preview_bridge.sent[-1]
    assert preview_type == "approval.request"
    assert preview_payload["summary"] == "echo ok"
    assert preview_payload["tool"] == "shell"
