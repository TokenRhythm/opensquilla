from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from opensquilla.artifacts import ArtifactStore
from opensquilla.channels.contract import ChannelCapabilityProfile
from opensquilla.channels.stream_policy import resolve_channel_stream_policy
from opensquilla.channels.types import (
    Attachment,
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ArtifactEvent,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from opensquilla.gateway.attachment_ingest import (
    MAX_STAGED_PDF_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    AttachmentTotalTooLargeError,
)
from opensquilla.gateway.channel_dispatch import (
    _artifact_fallback_lines,
    _build_reply_message,
    _clarify_tool_arguments,
    _deliver_artifacts_as_channel_files,
    _deliver_runtime_channel_reply,
    _dispatch_channel_slash_command,
    _dispatch_combined_message_after_debounce,
    _ingest_channel_message_attachments,
    _preserve_route_channel_metadata,
    _route_envelope_reply_message,
    _run_turn_batch_path,
    _run_turn_with_streaming,
    _RuntimeChannelStreamRelay,
)
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.routing import build_channel_route_envelope
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    project_path_key,
)
from opensquilla.safety.permission_matrix import Principal, is_tool_allowed
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class _StableReplaceableFakeChannel(_FakeChannel):
    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="stable-replaceable-test",
            edit=True,
            delete=True,
            streamed_message_replacement=True,
        )

    async def edit(self, message_id: str, content: str, **kwargs) -> None:
        del message_id, content, kwargs


class _FakeEventBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, session_key: str, event_name: str, payload: dict) -> None:
        self.events.append((session_key, event_name, payload))


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


@pytest.mark.asyncio
async def test_atomic_channel_acceptance_does_not_hold_session_lock() -> None:
    from opensquilla.gateway.channel_dispatch import _channel_acceptance_lock

    lock = asyncio.Lock()

    async with _channel_acceptance_lock(lock, atomic=True):
        assert lock.locked() is False

    async with _channel_acceptance_lock(lock, atomic=False):
        assert lock.locked() is True

    assert lock.locked() is False


class _RunContextSessionManager:
    def __init__(self, origin: dict | None) -> None:
        self.node = SimpleNamespace(origin=origin)

    async def get_session(self, session_key: str):
        return self.node


def test_clarify_protocol_can_be_recovered_from_tool_result_json() -> None:
    protocol = {
        "kind": "user_input",
        "paused": True,
        "step": "plan",
        "run_id": "plan-turn-1",
        "clarify_schema": {
            "mode": "form",
            "fields": [
                {
                    "name": "scope",
                    "type": "enum",
                    "required": True,
                    "choices": ["Core", "Full"],
                }
            ],
        },
    }
    event = ToolResultEvent(
        tool_use_id="request-input-1",
        tool_name="request_user_input",
        result=json.dumps(protocol),
        arguments={"questions": [{"id": "scope", "question": "Which scope?"}]},
    )

    assert _clarify_tool_arguments(event) == protocol


def _message() -> IncomingMessage:
    return IncomingMessage(sender_id="u1", channel_id="c1", content="hello")


def _authenticated_message() -> IncomingMessage:
    return _message().model_copy(
        update={
            "provenance": IngressProvenance(
                provider="feishu",
                verification=IngressVerification.SDK_SESSION,
                principal=AuthenticatedPrincipal(subject_id="u1"),
            )
        }
    )


def _tool_ctx(agent_id: str = "main") -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id)


def _exact_pdf(size: int) -> bytes:
    header = b"%PDF-1.4\n"
    return header + b"a" * (size - len(header))


def test_channel_reply_sanitizes_provider_compaction_markers() -> None:
    reply = _build_reply_message(
        _FakeChannel(),
        "Reply to user:\n[opensquilla_compacted:assistant_content:165:82bb251511c20cec]\n?",
        _message(),
    )

    assert "opensquilla_compacted" not in reply.content
    assert "assistant_content" not in reply.content
    assert reply.content == "Reply to user:\n?"


def test_route_envelope_reply_preserves_channel_for_thread_target() -> None:
    route_envelope = SimpleNamespace(channel_id="C42", thread_id="1700000000.000100")

    reply = _route_envelope_reply_message("busy", route_envelope)

    assert reply.reply_to == "1700000000.000100"
    assert reply.metadata == {"channel": "C42"}


def test_preserve_route_channel_metadata_for_registry_thread_reply() -> None:
    route_envelope = SimpleNamespace(channel_id="C42", thread_id="1700000000.000100")
    reply = OutgoingMessage(
        content="done",
        reply_to="1700000000.000100",
        metadata={"command": "compact"},
    )

    fixed = _preserve_route_channel_metadata(reply, route_envelope)

    assert fixed.reply_to == "1700000000.000100"
    assert fixed.metadata == {"command": "compact", "channel": "C42"}


def test_preserve_route_metadata_allows_only_interaction_reply_context() -> None:
    route_envelope = SimpleNamespace(
        channel_id="C42",
        thread_id=None,
        metadata={
            "interaction_token": "interaction-secret",
            "application_id": "app-1",
            "interaction_deferred": True,
            "authorization": "must-not-leak",
            "guild_id": "must-not-leak",
        },
    )
    reply = OutgoingMessage(content="done", metadata={"command": "help"})

    fixed = _preserve_route_channel_metadata(reply, route_envelope)

    assert fixed.metadata == {
        "command": "help",
        "interaction_token": "interaction-secret",
        "application_id": "app-1",
        "interaction_deferred": True,
    }


@pytest.mark.asyncio
async def test_registered_slash_command_preserves_channel_for_thread_target() -> None:
    msg = IncomingMessage(
        sender_id="U1",
        channel_id="C42",
        content="/compact",
        metadata={"thread_ts": "1700000000.000100"},
    )
    route_envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:slack:group:C42:thread:1700000000.000100",
        session_prefix="slack",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(
                req_id,
                {
                    "status": "skipped",
                    "compacted": False,
                },
            )

    reply = await _dispatch_channel_slash_command(
        route_envelope=route_envelope,
        msg=msg,
        session_manager=object(),
        session_key=route_envelope.session_key,
        session_prefix="slack",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.reply_to == "1700000000.000100"
    assert reply.metadata["channel"] == "C42"
    assert reply.metadata["command"] == "compact"


def test_channel_stream_policy_prefers_adapter_stream_updates() -> None:
    class StreamingChannel:
        async def send_streaming(self, chunks):
            async for _ in chunks:
                pass

    policy = resolve_channel_stream_policy(StreamingChannel())

    assert policy.mode == "adapter_stream"
    assert policy.relay_stream is True
    assert policy.typing_keepalive is False


def test_channel_stream_policy_uses_typing_placeholder_without_stream_editing() -> None:
    class TypingOnlyChannel:
        async def send_typing(self) -> None:
            pass

        async def send(self, message: OutgoingMessage) -> None:
            pass

    policy = resolve_channel_stream_policy(TypingOnlyChannel())

    assert policy.mode == "typing_final"
    assert policy.relay_stream is False
    assert policy.typing_keepalive is True


def test_channel_stream_policy_allows_adapter_final_only_override() -> None:
    class FinalOnlyChannel:
        stream_update_strategy = "final_only"

        async def send_streaming(self, chunks):
            async for _ in chunks:
                pass

    policy = resolve_channel_stream_policy(FinalOnlyChannel())

    assert policy.mode == "final_only"
    assert policy.relay_stream is False
    assert policy.typing_keepalive is False


@pytest.mark.asyncio
async def test_direct_channel_batch_uses_authoritative_done_snapshot() -> None:
    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = _FakeChannel()

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot",
        _tool_ctx(),
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert [message.content for message in channel.sent] == ["canonical"]


@pytest.mark.asyncio
async def test_direct_channel_batch_terminal_reset_replaces_partial_with_failure() -> None:
    drained = False

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            nonlocal drained
            yield TextDeltaEvent(text="superseded partial")
            yield AnswerGenerationResetEvent(
                terminal=True,
                terminal_text_snapshot="The fallback model also failed.",
                terminal_error_message="internal safe failure",
                terminal_error_code="ensemble_fixed_error",
                terminal_failure_kind="provider_error",
            )
            await asyncio.sleep(0.01)
            drained = True

    channel = _FakeChannel()
    bridge = _FakeEventBridge()

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:terminal-reset-batch",
        _tool_ctx(),
        bridge,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert [message.content for message in channel.sent] == [
        "The fallback model also failed."
    ]
    assert drained is True
    reset_payload = next(
        payload
        for _, event_name, payload in bridge.events
        if event_name == "session.event.answer_generation_reset"
    )
    assert reset_payload["terminal"] is True
    assert "terminal_error_message" not in reset_payload
    assert "terminal_error_code" not in reset_payload
    assert "terminal_failure_kind" not in reset_payload


@pytest.mark.asyncio
async def test_direct_channel_error_log_does_not_expose_provider_prose() -> None:
    raw_detail = "RAW_PROVIDER_BODY_DO_NOT_PERSIST"

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            del message, session_key, kwargs
            yield ErrorEvent(
                message=f"provider rejected request: {raw_detail}",
                code="400",
                failure_kind="bad_request",
            )

    channel = _FakeChannel()
    with structlog.testing.capture_logs() as logs:
        await _run_turn_batch_path(
            channel,
            FakeTurnRunner(),
            _message(),
            "agent:main:provider-error",
            _tool_ctx(),
            None,
            None,
            SimpleNamespace(
                agent_stream_heartbeat_interval_seconds=0.0,
                agent_stream_idle_timeout_seconds=1.0,
            ),
        )

    assert raw_detail not in json.dumps(logs)
    agent_error = next(
        row for row in logs if row["event"] == "channel_dispatch.agent_error"
    )
    assert agent_error["failure_kind"] == "bad_request"
    assert "message" not in agent_error
    assert channel.sent[-1].content == "The task failed before it could finish."


@pytest.mark.asyncio
async def test_direct_channel_stream_replaces_preview_with_done_snapshot() -> None:
    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.preview_chunks: list[str] = []
            self.edits: list[tuple[str, str, str | None]] = []

        def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, str]:
            return {"room_id": inbound.channel_id}

        async def send_streaming(self, chunks, *, room_id: str | None = None):
            assert room_id == "c1"
            async for chunk in chunks:
                self.preview_chunks.append(chunk)
            return "message-1"

        async def edit(
            self,
            message_id: str,
            content: str,
            *,
            room_id: str | None = None,
        ) -> None:
            self.edits.append((message_id, content, room_id))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = StreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-stream",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.preview_chunks == ["stale"]
    assert channel.edits == [("message-1", "canonical", "c1")]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_direct_channel_stream_terminal_reset_replaces_preview_with_failure() -> None:
    drained = False

    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.preview_chunks: list[str] = []
            self.edits: list[tuple[str, str, str | None]] = []

        def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, str]:
            return {"room_id": inbound.channel_id}

        async def send_streaming(self, chunks, *, room_id: str | None = None):
            assert room_id == "c1"
            async for chunk in chunks:
                self.preview_chunks.append(chunk)
            return "message-1"

        async def edit(
            self,
            message_id: str,
            content: str,
            *,
            room_id: str | None = None,
        ) -> None:
            self.edits.append((message_id, content, room_id))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            nonlocal drained
            yield TextDeltaEvent(text="superseded partial")
            yield AnswerGenerationResetEvent(
                terminal=True,
                terminal_text_snapshot="The fallback model also failed.",
                terminal_error_code="ensemble_fixed_error",
                terminal_failure_kind="provider_error",
            )
            await asyncio.sleep(0.01)
            drained = True

    channel = StreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:terminal-reset-stream",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.preview_chunks == ["superseded partial"]
    assert channel.edits == [
        ("message-1", "The fallback model also failed.", "c1")
    ]
    assert channel.sent == []
    assert drained is True


@pytest.mark.asyncio
async def test_direct_channel_stream_deletes_preview_for_explicit_empty_snapshot() -> None:
    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.preview_chunks: list[str] = []
            self.deleted: list[tuple[str, str | None]] = []

        def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, str]:
            return {"channel": inbound.channel_id}

        async def send_streaming(self, chunks, *, channel: str | None = None):
            assert channel == "c1"
            async for chunk in chunks:
                self.preview_chunks.append(chunk)
            return "message-1"

        async def delete(
            self,
            message_id: str,
            *,
            channel: str | None = None,
        ) -> None:
            self.deleted.append((message_id, channel))

        async def edit(self, message_id: str, content: str) -> None:
            raise AssertionError("explicit empty snapshot should prefer delete")

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="", text_snapshot="")

    channel = StreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-empty",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.preview_chunks == ["stale"]
    assert channel.deleted == [("message-1", "c1")]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_typed_stream_without_edit_method_buffers_terminal_snapshot() -> None:
    class UnreplaceableStreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        @property
        def capability_profile(self) -> ChannelCapabilityProfile:
            return ChannelCapabilityProfile(channel_type="misdeclared", edit=True)

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = UnreplaceableStreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-unreplaceable",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.chunks == ["canonical"]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_direct_channel_terminal_edit_uses_stream_creation_route() -> None:
    class RoutedStreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.stream_routes: list[str | None] = []
            self.edits: list[tuple[str, str, str | None]] = []

        def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, str]:
            return {"channel": inbound.channel_id}

        async def send_streaming(self, chunks, *, channel: str | None = None):
            self.stream_routes.append(channel)
            async for _ in chunks:
                pass
            return "message-1"

        async def edit(
            self,
            message_id: str,
            content: str,
            *,
            channel: str | None = None,
        ) -> None:
            self.edits.append((message_id, content, channel))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = RoutedStreamingChannel()
    inbound = IncomingMessage(sender_id="u1", channel_id="dynamic-room", content="hello")

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        inbound,
        "agent:main:done-snapshot-route",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.stream_routes == ["dynamic-room"]
    assert channel.edits == [("message-1", "canonical", "dynamic-room")]


@pytest.mark.asyncio
async def test_append_only_channel_buffers_until_done_snapshot() -> None:
    class AppendOnlyChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        @property
        def capability_profile(self) -> ChannelCapabilityProfile:
            return ChannelCapabilityProfile(channel_type="append-only")

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = AppendOnlyChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-append-only",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.chunks == ["canonical"]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_append_only_channel_sends_nothing_for_explicit_empty_snapshot() -> None:
    class AppendOnlyChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        @property
        def capability_profile(self) -> ChannelCapabilityProfile:
            return ChannelCapabilityProfile(channel_type="append-only")

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="", text_snapshot="")

    channel = AppendOnlyChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-append-only-empty",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.chunks == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_untyped_stream_with_edit_but_no_handle_contract_buffers_snapshot() -> None:
    class CustomStreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.edits: list[tuple[str, str]] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)
            return None

        async def edit(self, message_id: str, content: str) -> None:
            self.edits.append((message_id, content))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="canonical", text_snapshot="canonical")

    channel = CustomStreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-custom-unreplaceable",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.chunks == ["canonical"]
    assert channel.edits == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_untyped_stream_with_edit_but_no_handle_contract_honors_empty_snapshot() -> None:
    class CustomStreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.edits: list[tuple[str, str]] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)
            return None

        async def edit(self, message_id: str, content: str) -> None:
            self.edits.append((message_id, content))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="stale")
            yield DoneEvent(text="", text_snapshot="")

    channel = CustomStreamingChannel()

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:done-snapshot-custom-empty",
        None,
        None,
        SimpleNamespace(agent_stream_idle_timeout_seconds=1.0),
    )

    assert channel.chunks == []
    assert channel.edits == []
    assert channel.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expected_chunks"),
    [("canonical", ["canonical"]), ("", [])],
)
async def test_runtime_untyped_stream_with_edit_but_no_handle_contract_buffers_snapshot(
    snapshot: str,
    expected_chunks: list[str],
) -> None:
    class CustomStreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.edits: list[tuple[str, str]] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)
            return None

        async def edit(self, message_id: str, content: str) -> None:
            self.edits.append((message_id, content))

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    channel = CustomStreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
    )
    assert relay is not None

    await relay.emit(TextDeltaEvent(text="stale"))
    await relay.emit(DoneEvent(text=snapshot, text_snapshot=snapshot))
    await relay.close()

    assert channel.chunks == expected_chunks
    assert channel.edits == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_runtime_stream_relay_reconciles_against_persisted_final_text() -> None:
    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.preview_chunks: list[str] = []
            self.edits: list[tuple[str, str, str | None]] = []

        def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, str]:
            return {"room_id": inbound.channel_id}

        async def send_streaming(self, chunks, *, room_id: str | None = None):
            assert room_id == "c1"
            async for chunk in chunks:
                self.preview_chunks.append(chunk)
            return "message-1"

        async def edit(
            self,
            message_id: str,
            content: str,
            *,
            room_id: str | None = None,
        ) -> None:
            self.edits.append((message_id, content, room_id))

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

        async def wait(self, task_id: str):
            return SimpleNamespace(status="succeeded")

    class SessionManager:
        async def read_transcript(self, session_key: str):
            return [SimpleNamespace(role="assistant", content="canonical")]

    channel = StreamingChannel()
    runtime = FakeTaskRuntime()
    inbound = _message()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, inbound, runtime)
    assert relay is not None
    await relay.emit(TextDeltaEvent(text="stale"))

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=runtime,
        session_manager=SessionManager(),
        session_key="agent:main:runtime-done-snapshot",
        task_id="task-1",
        route_envelope=build_channel_route_envelope(
            inbound,
            session_key="agent:main:runtime-done-snapshot",
            session_prefix="test",
        ),
        inbound=inbound,
        transcript_watermark=0,
        stream_relay=relay,
    )

    assert channel.preview_chunks == ["stale"]
    assert channel.edits == [("message-1", "canonical", "c1")]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_direct_channel_turn_emits_run_heartbeat_while_stream_is_quiet() -> None:
    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            await asyncio.sleep(0.03)
            yield TextDeltaEvent(text="ok")
            yield DoneEvent()

    channel = _FakeChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.01,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        bridge,
        None,
        config,
    )

    assert any(event_name == "session.event.run_heartbeat" for _, event_name, _ in bridge.events)
    assert channel.sent[-1].content == "ok"


def test_direct_channel_batch_turn_emits_tool_events_to_webui() -> None:
    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ToolUseStartEvent(
                tool_use_id="meta_step_outline",
                tool_name="meta-step:outline",
            )
            yield ToolResultEvent(
                tool_use_id="meta_step_outline",
                tool_name="meta-step:outline",
                result="outline done",
                arguments={"kind": "llm_chat", "output_chars": 12},
            )
            yield TextDeltaEvent(
                text="ok",
                generation_epoch=4,
                model_call_id="3.0",
                iteration=3,
            )
            yield DoneEvent()

    channel = _FakeChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    asyncio.run(
        _run_turn_batch_path(
            channel,
            FakeTurnRunner(),
            _message(),
            "agent:main:channel-test",
            _tool_ctx(),
            bridge,
            None,
            config,
        )
    )

    assert (
        "agent:main:channel-test",
        "session.event.tool_use_start",
        {
            "tool_use_id": "meta_step_outline",
            "tool_name": "meta-step:outline",
            "name": "meta-step:outline",
            "synthetic_from_text": False,
        },
    ) in bridge.events
    assert any(
        event_name == "session.event.tool_result"
        and payload["tool_name"] == "meta-step:outline"
        and payload["result"] == "outline done"
        and payload["arguments"]["kind"] == "llm_chat"
        for _, event_name, payload in bridge.events
    )
    assert (
        "agent:main:channel-test",
        "session.event.text_delta",
        {
            "text": "ok",
            "presentation": "answer",
            "generation_epoch": 4,
            "model_call_id": "3.0",
            "iteration": 3,
        },
    ) in bridge.events
    assert channel.sent[-1].content == "ok"


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_sends_clarify_card_to_feishu_channel() -> None:
    class FeishuLikeChannel(_FakeChannel):
        @property
        def capability_profile(self) -> ChannelCapabilityProfile:
            return ChannelCapabilityProfile(
                channel_type="feishu",
                cards=True,
                interactive_cards=True,
                card_actions=True,
            )

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ToolResultEvent(
                tool_use_id="meta_step_clarify",
                tool_name="meta-step:clarify",
                result="paused: awaiting user input",
                arguments={
                    "kind": "user_input",
                    "paused": True,
                    "step": "clarify",
                    "run_id": "run-1",
                    "clarify_schema": {
                        "mode": "form",
                        "intro": "需要确认几件事。",
                        "fields": [
                            {
                                "name": "destination",
                                "type": "string",
                                "required": True,
                                "prompt": "目的地",
                            },
                            {
                                "name": "budget",
                                "type": "enum",
                                "required": False,
                                "prompt": "预算",
                                "choices": ["低", "中", "高"],
                                "default": "中",
                            },
                        ],
                        "cancel_keywords": ["取消", "cancel"],
                    },
                },
            )
            yield TextDeltaEvent(text="请回复以下字段：\n  1) destination — 目的地 [必填]")
            yield DoneEvent()

    channel = FeishuLikeChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        IncomingMessage(
            sender_id="u1",
            channel_id="c1",
            content="hello",
            metadata={"is_group": False, "chat_type": "p2p"},
        ),
        "agent:main:channel-clarify",
        _tool_ctx(),
        bridge,
        None,
        config,
    )

    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent.reply_to == "c1"
    card = sent.metadata["card"]
    assert card["header"]["title"]["content"] == "需要补充信息"
    assert "destination" in json.dumps(card, ensure_ascii=False)
    assert "预算" in json.dumps(card, ensure_ascii=False)
    assert '"opensquilla_action": "clarify_submit"' in json.dumps(card)
    assert '"is_group": false' in json.dumps(card)
    assert '"chat_type": "p2p"' in json.dumps(card)
    assert "请回复以下字段" not in sent.content
    assert any(event_name == "session.event.tool_result" for _, event_name, _ in bridge.events)


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_keeps_text_fallback_without_card_support() -> None:
    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ToolResultEvent(
                tool_use_id="meta_step_clarify",
                tool_name="meta-step:clarify",
                result="paused: awaiting user input",
                arguments={
                    "kind": "user_input",
                    "paused": True,
                    "step": "clarify",
                    "run_id": "run-1",
                    "clarify_schema": {
                        "mode": "form",
                        "intro": "需要确认几件事。",
                        "fields": [
                            {
                                "name": "destination",
                                "type": "string",
                                "required": True,
                                "prompt": "目的地",
                            },
                        ],
                    },
                },
            )
            yield TextDeltaEvent(text="请回复以下字段：\n  1) destination — 目的地 [必填]")
            yield DoneEvent()

    channel = _FakeChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-clarify",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        config,
    )

    assert len(channel.sent) == 1
    assert "请回复以下字段" in channel.sent[0].content
    assert "card" not in channel.sent[0].metadata


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_sends_artifact_fallback() -> None:
    artifact = {
        "id": "art-channel",
        "kind": "artifact_ref",
        "name": "report.txt",
        "mime": "text/plain",
        "size": 4,
        "sha256": "f" * 64,
        "session_id": "session-1",
        "session_key": "agent:main:channel-test",
        "source": "publish_artifact",
        "created_at": "2026-05-06T12:00:00Z",
        "download_url": "/api/v1/artifacts/art-channel?sessionKey=agent%3Amain%3Achannel-test",
        "store": "artifacts",
    }

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ArtifactEvent(**artifact)
            yield DoneEvent()

    channel = _FakeChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        bridge,
        None,
        config,
    )

    assert channel.sent[-1].content == (
        "Generated file: report.txt -> available in the OpenSquilla task"
    )
    assert "/api/v1/artifacts" not in channel.sent[-1].content
    assert "sessionKey" not in channel.sent[-1].content
    event_artifact = bridge.events[-1][2]
    assert bridge.events[-1] == (
        "agent:main:channel-test",
        "session.event.artifact",
        event_artifact,
    )
    assert event_artifact["download_url"] == "/api/v1/artifacts/art-channel"
    assert "session_key" not in event_artifact
    assert "sessionKey" not in json.dumps(event_artifact)


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_sends_artifact_with_adapter_file_upload(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"deck bytes",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="report.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="publish_artifact",
    )
    artifact = ref.to_dict()

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="done")
            yield ArtifactEvent(**artifact)
            yield DoneEvent()

    class FileUploadingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    channel = FileUploadingChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(tmp_path)),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        bridge,
        None,
        config,
    )

    assert channel.sent[-1].content == "done"
    assert channel.files == [("c1", "report.pptx")]


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_sends_artifact_with_original_filename(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"\x89PNG\r\n\x1a\nimage bytes",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="思考快与慢_信息图.png",
        mime="image/png",
        source="publish_artifact",
    )

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="done")
            yield ArtifactEvent(**ref.to_dict())
            yield DoneEvent()

    class FileUploadingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    channel = FileUploadingChannel()
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(tmp_path)),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.sent[-1].content == "done"
    assert channel.files == [("c1", "思考快与慢_信息图.png")]


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_removes_delivered_markdown_image_reference(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"\x89PNG\r\n\x1a\nimage bytes",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="thinking_fast_slow_v3.png",
        mime="image/png",
        source="publish_artifact",
    )

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(
                text=(
                    "新版改进：\n\n"
                    "![Thinking, Fast and Slow Infographic v3](thinking_fast_slow_v3.png)\n\n"
                    "点击附件保存原图。"
                )
            )
            yield ArtifactEvent(**ref.to_dict())
            yield DoneEvent()

    class FileUploadingChannel(_FakeChannel):
        async def send_file(self, chat_id: str, file_path: str) -> None:
            return None

    channel = FileUploadingChannel()
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(tmp_path)),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.sent[-1].content == "新版改进：\n\n点击附件保存原图。"
    assert "![Thinking" not in channel.sent[-1].content


@pytest.mark.asyncio
async def test_direct_channel_batch_turn_removes_artifact_markers_from_channel_text(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"image bytes",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="chart.png",
        mime="image/png",
        source="publish_artifact",
    )
    marker = "[generated artifact omitted: chart.png (image/png)]"
    artifact = ref.to_dict()

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text=f"ready {marker}")
            yield ArtifactEvent(**artifact)
            yield DoneEvent()

    class FileUploadingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    channel = FileUploadingChannel()
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(tmp_path)),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:channel-test",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.sent[-1].content == "ready"
    assert marker not in channel.sent[-1].content
    assert channel.files == [("c1", "chart.png")]


@pytest.mark.asyncio
async def test_channel_admin_sender_gets_owner_tool_context_for_agent_turn(tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            captured["tool_context"] = kwargs["tool_context"]
            yield TextDeltaEvent(text="ok")
            yield DoneEvent()

    msg = _authenticated_message()
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    config = SimpleNamespace(
        channel_admin_senders={"feishu": ["u1"]},
        workspace_dir=str(tmp_path),
        workspace_strict=True,
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        _FakeChannel(),
        FakeTurnRunner(),
        msg,
        "agent:main:feishu:u1",
        config=config,
        route_envelope=envelope,
    )

    tool_context = captured["tool_context"]
    assert tool_context.is_owner is True
    assert tool_context.channel_admin_verified is True
    assert tool_context.caller_kind is CallerKind.CHANNEL
    assert tool_context.channel_kind == "feishu"
    assert tool_context.sender_id == "u1"
    decision = is_tool_allowed(
        "write_file",
        "dm",
        Principal(role="operator", channel_id=tool_context.session_key),
    )
    assert decision.allowed is True
    assert decision.reason == "operator_override"


@pytest.mark.asyncio
async def test_saved_channel_run_context_is_applied_to_route_envelope(tmp_path) -> None:
    from opensquilla.gateway.channel_dispatch import _apply_saved_channel_run_context

    msg = _authenticated_message()
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    manager = _RunContextSessionManager(
        {
            "sandbox_run_context": {
                "run_mode": "full",
                "workspace": str(tmp_path),
                "mounts": [],
                "domains": [],
                "bundles": [],
                "public_network": [],
                "temporary_grants": [],
            }
        }
    )
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )

    await _apply_saved_channel_run_context(
        envelope,
        session_manager=manager,
        config=config,
        workspace_dir=str(tmp_path),
        principal_is_owner=True,
    )

    assert envelope.metadata["run_mode"] == RunMode.FULL.value
    assert envelope.metadata["elevated"] == "full"
    assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"


@pytest.mark.asyncio
async def test_global_full_mode_is_applied_to_channel_without_saved_override(tmp_path) -> None:
    from opensquilla.gateway.channel_dispatch import _apply_saved_channel_run_context

    envelope = build_channel_route_envelope(
        _message(),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    manager = _RunContextSessionManager(None)
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="full", sandbox=False, security_grading=False),
        permissions=SimpleNamespace(default_mode="full"),
    )

    await _apply_saved_channel_run_context(
        envelope,
        session_manager=manager,
        config=config,
        workspace_dir=str(tmp_path),
        principal_is_owner=True,
    )

    assert envelope.metadata["run_mode"] == RunMode.FULL.value
    assert envelope.metadata["elevated"] == "full"
    assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"


@pytest.mark.asyncio
async def test_unlisted_channel_sender_keeps_restricted_tool_context_for_agent_turn(
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            captured["tool_context"] = kwargs["tool_context"]
            yield TextDeltaEvent(text="ok")
            yield DoneEvent()

    msg = _authenticated_message()
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    config = SimpleNamespace(
        channel_admin_senders={"feishu": ["other-user"]},
        workspace_dir=str(tmp_path),
        workspace_strict=True,
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        _FakeChannel(),
        FakeTurnRunner(),
        msg,
        "agent:main:feishu:u1",
        config=config,
        route_envelope=envelope,
    )

    tool_context = captured["tool_context"]
    assert tool_context.is_owner is False
    assert tool_context.channel_admin_verified is False
    assert tool_context.caller_kind is CallerKind.CHANNEL
    assert tool_context.channel_kind == "feishu"
    assert tool_context.sender_id == "u1"


def test_channel_artifact_fallback_uses_only_channel_safe_absolute_links() -> None:
    assert _artifact_fallback_lines(
        [
            {
                "id": "art-1",
                "name": "report.txt",
                "download_url": "/api/v1/artifacts/art-1?sessionKey=secret",
            }
        ]
    ) == ["Generated file: report.txt -> available in the OpenSquilla task"]

    assert _artifact_fallback_lines(
        [
            {
                "id": "art-2",
                "name": "signed.txt",
                "signed_download_url": "https://gateway.example/artifacts/art-2?sig=short",
            }
        ]
    ) == ["Generated file: signed.txt -> https://gateway.example/artifacts/art-2?sig=short"]

    assert _artifact_fallback_lines(
        [
            {
                "id": "art-3",
                "name": "bad.txt",
                "channel_download_url": "/api/v1/artifacts/art-3?token=long",
            }
        ]
    ) == ["Generated file: bad.txt -> available in the OpenSquilla task"]


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_emits_artifact_fallback() -> None:
    class StreamingChannel:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, _message(), FakeTaskRuntime())

    assert relay is not None

    await relay.emit(
        {
            "kind": "artifact",
            "id": "art-stream",
            "name": "stream.txt",
            "download_url": "/api/v1/artifacts/art-stream?sessionKey=secret",
        }
    )
    await relay.close()

    assert channel.chunks == [
        "Generated file: stream.txt -> available in the OpenSquilla task"
    ]
    assert relay.text_emitted is True


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_appends_artifact_fallback_to_text() -> None:
    class StreamingChannel:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, _message(), FakeTaskRuntime())

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="done"))
    await relay.emit(
        {
            "kind": "artifact",
            "id": "art-stream",
            "name": "stream.txt",
            "download_url": "/api/v1/artifacts/art-stream?sessionKey=secret",
        }
    )
    await relay.close()

    assert channel.chunks == [
        "done",
        "\n\nGenerated file: stream.txt -> available in the OpenSquilla task",
    ]


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_replaces_preview_with_terminal_reset() -> None:
    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.edits: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)
            return "stream-message-1"

        async def edit(self, message_id: str, content: str, **kwargs) -> None:
            assert message_id == "stream-message-1"
            self.edits.append(content)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

        async def wait(self, task_id: str):
            return SimpleNamespace(status="failed", error_message="provider failed")

    channel = StreamingChannel()
    runtime = FakeTaskRuntime()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, _message(), runtime)

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="superseded partial"))
    await relay.emit(
        {
            "kind": "answer_generation_reset",
            "terminal": True,
            "authoritative_text_snapshot": "",
            "terminal_text_snapshot": "The fallback model also failed.",
            # A runtime sink must never turn internal accounting metadata into
            # visible channel content, even if a custom producer includes it.
            "terminal_error_message": "INTERNAL_DO_NOT_RENDER",
        }
    )
    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=runtime,
        session_manager=None,
        session_key="agent:main:channel-reset",
        task_id="task-reset",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=_message(),
        transcript_watermark=0,
        stream_relay=relay,
    )

    assert channel.chunks == ["superseded partial"]
    assert channel.edits == ["The fallback model also failed."]
    assert channel.sent == []
    assert relay.has_terminal_generation_reset is True


@pytest.mark.asyncio
async def test_runtime_channel_buffered_relay_publishes_only_terminal_reset() -> None:
    class BufferedChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

        async def wait(self, task_id: str):
            return SimpleNamespace(status="failed", error_message="provider failed")

    channel = BufferedChannel()
    runtime = FakeTaskRuntime()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, _message(), runtime)

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="superseded partial"))
    await relay.emit(
        {
            "kind": "answer_generation_reset",
            "terminal": True,
            "terminal_text_snapshot": "The fallback model also failed.",
        }
    )
    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=runtime,
        session_manager=None,
        session_key="agent:main:channel-reset-buffered",
        task_id="task-reset-buffered",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=_message(),
        transcript_watermark=0,
        stream_relay=relay,
    )

    assert channel.chunks == ["The fallback model also failed."]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_sends_artifact_with_adapter_upload(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"deck bytes",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="report.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="publish_artifact",
    )

    class StreamingFileChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.files: list[tuple[str, str]] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))
    channel = StreamingFileChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="done"))
    await relay.emit(ArtifactEvent(**ref.to_dict()))
    await relay.close()

    assert channel.chunks == ["done"]
    assert channel.files == [("c1", "report.pptx")]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_stream_failure_does_not_replay_attempted_artifact_at_terminal_reply(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"%PDF-1.4\nreport",
        session_id="session-1",
        session_key="agent:main:channel-test",
        name="report.pdf",
        mime="application/pdf",
        source="publish_artifact",
    )

    class FailingStreamingFileChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.file_attempts = 0

        async def send_streaming(self, chunks, **kwargs):
            del chunks, kwargs
            raise RuntimeError("stream transport failed")

        async def send_file(self, chat_id: str, file_path: str) -> None:
            del chat_id, file_path
            self.file_attempts += 1
            raise RuntimeError("visible artifact result is unknown")

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            del envelope, message, stream_event_sink

        async def wait(self, task_id: str):
            del task_id
            return SimpleNamespace(status="succeeded")

    class FakeSessionManager:
        async def read_transcript(self, key: str):
            del key
            return [
                {"role": "user", "content": "make report"},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"text": "Report ready.", "artifacts": [ref.to_dict()]}
                    ),
                },
            ]

    channel = FailingStreamingFileChannel()
    runtime = FakeTaskRuntime()
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))
    inbound = _message()
    relay = _RuntimeChannelStreamRelay.maybe_start(channel, inbound, runtime, config)
    assert relay is not None
    await relay.emit(TextDeltaEvent(text="Report ready."))
    await relay.emit(ArtifactEvent(**ref.to_dict()))

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=runtime,
        session_manager=FakeSessionManager(),
        session_key="agent:main:channel-test",
        task_id="task-1",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=inbound,
        transcript_watermark=1,
        config=config,
        stream_relay=relay,
    )

    assert channel.file_attempts == 1
    fallback_messages = [
        message.content
        for message in channel.sent
        if "Generated file: report.pdf" in message.content
    ]
    assert fallback_messages == [
        "Generated file: report.pdf -> available in the OpenSquilla task"
    ]


@pytest.mark.asyncio
async def test_channel_file_delivery_dedupes_same_artifact_material(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b"\x89PNG\r\n\x1a\nimage bytes"
    first = store.publish_bytes(
        payload,
        session_id="session-1",
        session_key="agent:main:feishu:direct:u1",
        name="image.png",
        mime="image/png",
        source="image_generate",
    )
    second = store.publish_bytes(
        payload,
        session_id="session-1",
        session_key="agent:main:feishu:direct:u1",
        name="image.png",
        mime="image/png",
        source="publish_artifact",
    )

    class FileChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    channel = FileChannel()
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))

    undelivered = await _deliver_artifacts_as_channel_files(
        channel,
        _message(),
        [first.to_dict(), second.to_dict()],
        config,
    )

    assert undelivered == []
    assert channel.files == [("c1", "image.png")]


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_does_not_redeliver_transcript_artifact(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"\x89PNG\r\n\x1a\nimage bytes",
        session_id="session-1",
        session_key="agent:main:discord:direct:u1",
        name="chart.png",
        mime="image/png",
        source="publish_artifact",
    )

    class StreamingFileChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []
            self.files: list[tuple[str, str]] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

        async def wait(self, task_id: str):
            return SimpleNamespace(status="succeeded")

    class FakeSessionManager:
        async def read_transcript(self, key: str):
            return [
                {"role": "user", "content": "draw chart"},
                {
                    "role": "assistant",
                    "content": json.dumps({"text": "", "artifacts": [ref.to_dict()]}),
                },
            ]

    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))
    channel = StreamingFileChannel()
    runtime = FakeTaskRuntime()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        runtime,
        config,
    )

    assert relay is not None

    await relay.emit(ArtifactEvent(**ref.to_dict()))
    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=runtime,
        session_manager=FakeSessionManager(),
        session_key="agent:main:discord:direct:u1",
        task_id="task-1",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=_message(),
        transcript_watermark=1,
        config=config,
        stream_relay=relay,
    )

    assert channel.files == [("c1", "chart.png")]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_direct_channel_turn_idle_timeout_sends_error_reply() -> None:
    class SlowTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            await asyncio.sleep(1.0)
            yield TextDeltaEvent(text="late")

    channel = _FakeChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=0.01,
    )

    await _run_turn_batch_path(
        channel,
        SlowTurnRunner(),
        _message(),
        "agent:main:channel-timeout",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.sent
    assert channel.sent[-1].content == "The task timed out before it could finish."
    assert "Stream idle" not in channel.sent[-1].content


@pytest.mark.asyncio
async def test_direct_channel_turn_honors_final_only_stream_policy() -> None:
    class FinalOnlyStreamingChannel(_FakeChannel):
        stream_update_strategy = "final_only"

        def __init__(self) -> None:
            super().__init__()
            self.streamed = False

        async def send_streaming(self, chunks):
            self.streamed = True
            text = ""
            async for chunk in chunks:
                text += chunk
            self.sent.append(OutgoingMessage(content=text))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="final only")
            yield DoneEvent()

    channel = FinalOnlyStreamingChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:final-only",
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.streamed is False
    assert channel.sent[-1].content == "final only"


@pytest.mark.asyncio
async def test_direct_streaming_path_falls_back_when_adapter_stream_fails() -> None:
    class FailingStreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered_chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.delivered_chunks.append(chunk)
                raise RuntimeError("stream edit failed")

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="part-one")
            yield TextDeltaEvent(text="part-two")
            yield DoneEvent()

    channel = FailingStreamingChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:stream-fallback",
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.delivered_chunks == ["part-one"]
    assert channel.sent
    assert "part-one" in channel.sent[-1].content
    assert "part-two" in channel.sent[-1].content


def test_direct_streaming_path_emits_tool_events_to_webui() -> None:
    class StreamingChannel(_FakeChannel):
        async def send_streaming(self, chunks, **kwargs):
            text = ""
            async for chunk in chunks:
                text += chunk
            self.sent.append(OutgoingMessage(content=text))

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield ToolUseStartEvent(
                tool_use_id="meta_step_section",
                tool_name="meta-step:section_introduction",
            )
            yield ToolResultEvent(
                tool_use_id="meta_step_section",
                tool_name="meta-step:section_introduction",
                result="section done",
            )
            yield TextDeltaEvent(
                text="finished",
                generation_epoch=3,
                model_call_id="2.1",
                iteration=2,
            )
            yield DoneEvent()

    channel = StreamingChannel()
    bridge = _FakeEventBridge()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    asyncio.run(
        _run_turn_with_streaming(
            channel,
            FakeTurnRunner(),
            _message(),
            "agent:main:stream-tool-events",
            bridge,
            None,
            config,
        )
    )

    event_names = [event_name for _, event_name, _ in bridge.events]
    assert "session.event.tool_use_start" in event_names
    assert "session.event.tool_result" in event_names
    assert any(
        event_name == "session.event.tool_use_start"
        and payload["tool_name"] == "meta-step:section_introduction"
        for _, event_name, payload in bridge.events
    )
    assert any(
        event_name == "session.event.tool_result"
        and payload["tool_name"] == "meta-step:section_introduction"
        and payload["result"] == "section done"
        for _, event_name, payload in bridge.events
    )
    assert (
        "agent:main:stream-tool-events",
        "session.event.text_delta",
        {
            "text": "finished",
            "presentation": "answer",
            "generation_epoch": 3,
            "model_call_id": "2.1",
            "iteration": 2,
        },
    ) in bridge.events
    assert channel.sent[-1].content == "finished"


@pytest.mark.asyncio
async def test_direct_streaming_path_fallback_skips_delivered_chunks() -> None:
    class FailingLateStreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered_chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            count = 0
            async for chunk in chunks:
                count += 1
                if count == 3:
                    raise RuntimeError("late stream edit failed")
                self.delivered_chunks.append(chunk)

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            for chunk in ("alpha", "beta", "gamma", "delta"):
                yield TextDeltaEvent(text=chunk)
            yield DoneEvent()

    channel = FailingLateStreamingChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:stream-fallback-late",
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.delivered_chunks == ["alpha", "beta"]
    assert channel.sent
    fallback = channel.sent[-1].content
    assert "gamma" in fallback
    assert "delta" in fallback
    assert "alpha" not in fallback
    assert "beta" not in fallback


@pytest.mark.asyncio
async def test_direct_streaming_fallback_sanitizes_queued_directive_tags() -> None:
    class FailingStreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered_chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.delivered_chunks.append(chunk)
                raise RuntimeError("stream edit failed")

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="visible ")
            yield TextDeltaEvent(text="[[reply_to_current]]hidden")
            yield DoneEvent()

    channel = FailingStreamingChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:stream-fallback-directive",
        _FakeEventBridge(),
        None,
        config,
    )

    assert channel.delivered_chunks == ["visible "]
    assert channel.sent
    fallback = channel.sent[-1].content
    assert "[[reply_to_current]]" not in fallback
    assert "hidden" in fallback


@pytest.mark.asyncio
async def test_direct_streaming_sanitizes_split_provider_compaction_marker() -> None:
    class StreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered_chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.delivered_chunks.append(chunk)

    class FakeTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs):
            yield TextDeltaEvent(text="Visible\n[opensquilla_")
            yield TextDeltaEvent(text="compacted:assistant_content:165:abc]\nDone")
            yield DoneEvent()

    channel = StreamingChannel()
    config = SimpleNamespace(
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        channel,
        FakeTurnRunner(),
        _message(),
        "agent:main:stream-marker",
        _FakeEventBridge(),
        None,
        config,
    )

    delivered = "".join(channel.delivered_chunks)
    assert "opensquilla_compacted" not in delivered
    assert "assistant_content" not in delivered
    assert delivered == "Visible\nDone"


@pytest.mark.asyncio
async def test_channel_batch_turn_uses_agent_registry_model() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run(self, message: str, session_key: str, **kwargs):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()
    config = GatewayConfig(
        agents=[AgentEntryConfig(id="ops", model="agent/default")],
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_batch_path(
        _FakeChannel(),
        runner,
        _message(),
        "agent:ops:channel-test",
        _tool_ctx("ops"),
        _FakeEventBridge(),
        None,
        config,
    )

    assert runner.calls[0]["model"] == "agent/default"


@pytest.mark.asyncio
async def test_channel_ingest_resolves_adapter_bytes_to_engine_attachment() -> None:
    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            return Attachment(
                name=attachment.name,
                mime_type=attachment.mime_type,
                data=b"hello",
                size=5,
            )

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read",
        attachments=[
            Attachment(
                name="note.txt",
                mime_type="text/plain",
                url="https://example.test/note.txt",
            )
        ],
    )

    result = await _ingest_channel_message_attachments(channel=ResolvingChannel(), msg=msg)

    assert result.text == "read"
    assert result.failures == []
    assert result.attachments == [
        {
            "name": "note.txt",
            "type": "text/plain",
            "data": base64.b64encode(b"hello").decode("ascii"),
            "_was_staged": True,
        }
    ]


@pytest.mark.asyncio
async def test_channel_ingest_honors_strict_admission_config() -> None:
    from types import SimpleNamespace

    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            return Attachment(
                name=attachment.name,
                mime_type=attachment.mime_type,
                data=b"\x00\x01binary",
                size=8,
            )

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read",
        attachments=[
            Attachment(
                name="x.bin",
                mime_type="application/x-unknown",
                url="https://example.test/x.bin",
            )
        ],
    )
    strict = SimpleNamespace(attachments=SimpleNamespace(accept_opaque=False))

    result = await _ingest_channel_message_attachments(
        channel=ResolvingChannel(), msg=msg, config=strict
    )

    assert result.attachments == []
    assert result.failures[0].reason == "unsupported_mime"
    assert "[attachment unavailable: x.bin: unsupported_mime]" in result.text


@pytest.mark.asyncio
async def test_channel_ingest_honors_opaque_byte_cap_config() -> None:
    from types import SimpleNamespace

    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            return Attachment(
                name=attachment.name,
                mime_type=attachment.mime_type,
                data=b"\x00" + b"a" * 4096,
                size=4097,
            )

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read",
        attachments=[
            Attachment(
                name="x.bin",
                mime_type="application/x-unknown",
                url="https://example.test/x.bin",
            )
        ],
    )
    capped = SimpleNamespace(attachments=SimpleNamespace(accept_opaque=True, opaque_max_bytes=1024))

    result = await _ingest_channel_message_attachments(
        channel=ResolvingChannel(), msg=msg, config=capped
    )

    assert result.attachments == []
    assert result.failures[0].reason == "oversize"


@pytest.mark.asyncio
async def test_channel_ingest_hard_rejects_aggregate_attachment_cap() -> None:
    one_pdf = _exact_pdf(MAX_TOTAL_ATTACHMENT_BYTES // 3 + 1)
    assert len(one_pdf) < MAX_STAGED_PDF_BYTES

    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            return Attachment(
                name=attachment.name,
                mime_type="application/pdf",
                data=one_pdf,
                size=len(one_pdf),
            )

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read",
        attachments=[
            Attachment(
                name=f"{index}.pdf",
                mime_type="application/pdf",
                url=f"https://example.test/{index}.pdf",
            )
            for index in range(3)
        ],
    )

    with pytest.raises(AttachmentTotalTooLargeError, match="total raw bytes"):
        await _ingest_channel_message_attachments(channel=ResolvingChannel(), msg=msg)


@pytest.mark.asyncio
async def test_channel_batch_turn_passes_normalized_attachments() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run(self, message: str, session_key: str, **kwargs):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()
    attachment = {
        "type": "text/plain",
        "name": "note.txt",
        "data": base64.b64encode(b"hello").decode("ascii"),
    }

    await _run_turn_batch_path(
        _FakeChannel(),
        runner,
        _message(),
        "agent:main:channel-attachment",
        _tool_ctx(),
        _FakeEventBridge(),
        None,
        SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        [attachment],
    )

    assert runner.calls[0]["attachments"] == [attachment]


@pytest.mark.asyncio
async def test_debounce_channel_turn_rejects_aggregate_cap_before_runtime_start() -> None:
    one_pdf = _exact_pdf(MAX_TOTAL_ATTACHMENT_BYTES // 3 + 1)
    assert len(one_pdf) < MAX_STAGED_PDF_BYTES

    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            return Attachment(
                name=attachment.name,
                mime_type="application/pdf",
                data=one_pdf,
                size=len(one_pdf),
            )

    class FakeSessionManager:
        def __init__(self) -> None:
            self.delivery_contexts: list[tuple[str, str]] = []
            self.entries: list[dict[str, str]] = []

        async def get_or_create(self, key: str, **kwargs):
            return SimpleNamespace(session_key=key, **kwargs), True

        async def update(self, key: str, **kwargs) -> None:
            self.delivery_contexts.append((key, kwargs.get("last_channel") or ""))

        async def append_message(self, key: str, role: str, content: str):
            self.entries.append({"role": role, "content": content})
            return SimpleNamespace(content=content)

        async def read_transcript(self, key: str):
            return list(self.entries)

    class FakeTaskRuntime:
        def __init__(self) -> None:
            self.enqueue_calls: list[dict] = []

        async def enqueue(self, envelope, message: str, **kwargs):
            self.enqueue_calls.append({"message": message, **kwargs})
            return SimpleNamespace(task_id="t1")

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read",
        attachments=[
            Attachment(
                name=f"{index}.pdf",
                mime_type="application/pdf",
                url=f"https://example.test/{index}.pdf",
            )
            for index in range(3)
        ],
    )
    runtime = FakeTaskRuntime()
    manager = FakeSessionManager()

    with pytest.raises(AttachmentTotalTooLargeError):
        await _dispatch_combined_message_after_debounce(
            ResolvingChannel(),
            SimpleNamespace(message=msg, raw_content="read", coalesced_count=1),
            SimpleNamespace(),
            manager,
            "agent:main:matrix:direct:u1",
            "matrix",
            runtime,
            SimpleNamespace(),
        )

    assert runtime.enqueue_calls == []
    assert manager.entries == []


@pytest.mark.asyncio
async def test_channel_streaming_turn_uses_agent_registry_model() -> None:
    class StreamingChannel(_FakeChannel):
        async def send_streaming(self, chunks, **kwargs):
            async for _ in chunks:
                pass

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run(self, message: str, session_key: str, **kwargs):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()
    config = GatewayConfig(
        agents=[AgentEntryConfig(id="ops", model="agent/default")],
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    await _run_turn_with_streaming(
        StreamingChannel(),
        runner,
        _message(),
        "agent:ops:channel-test",
        _FakeEventBridge(),
        None,
        config,
    )

    assert runner.calls[0]["model"] == "agent/default"


@pytest.mark.asyncio
async def test_direct_channel_turn_uses_authoritative_project_workspace(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "channel-project.db"))
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
    key = "agent:main:matrix:project-channel"
    await storage.upsert_session(
        SessionNode(
            session_key=key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": str(outside),
                }
            },
        )
    )
    envelope = build_channel_route_envelope(
        _message(),
        session_key=key,
        session_prefix="matrix",
    )
    envelope.metadata["sandbox_run_context"] = {
        "run_mode": "standard",
        "workspace": str(outside),
    }
    object.__setattr__(envelope, "sandbox_run_context_fresh", True)

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()
    try:
        await _run_turn_with_streaming(
            _FakeChannel(),
            runner,
            _message(),
            key,
            _FakeEventBridge(),
            None,
            GatewayConfig(
                workspace_dir=str(tmp_path / "default"),
                agent_stream_heartbeat_interval_seconds=0.0,
                agent_stream_idle_timeout_seconds=1.0,
            ),
            route_envelope=envelope,
            session_manager=manager,
        )
    finally:
        await storage.close()

    assert runner.calls[0]["tool_context"].workspace_dir == project.path


@pytest.mark.asyncio
async def test_direct_channel_unbound_turn_refreshes_durable_context(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.project_workspace_runtime import (
        authoritative_project_run_context,
    )
    from opensquilla.gateway.rpc_sessions import _apply_run_context_route_metadata

    storage = await SessionStorage.open(str(tmp_path / "channel-unbound.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    stale_workspace = tmp_path / "stale"
    current_workspace = tmp_path / "current"
    default_workspace = tmp_path / "default"
    stale_workspace.mkdir()
    current_workspace.mkdir()
    key = "agent:main:matrix:unbound"
    await manager.create(
        key,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "full",
                "run_mode_source": "user",
                "workspace": str(stale_workspace),
                "domains": [
                    {
                        "domain": "revoked.example",
                        "scope": "chat",
                        "source": "manual",
                    }
                ],
            }
        },
    )
    config = GatewayConfig(
        workspace_dir=str(default_workspace),
        channel_admin_senders={"matrix": ["u1"]},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    envelope = build_channel_route_envelope(
        _message(),
        session_key=key,
        session_prefix="matrix",
    )
    session = await storage.get_session(key)
    assert session is not None
    stale_context, workspace_guard = await authoritative_project_run_context(
        storage=storage,
        session_manager=manager,
        session=session,
        config=config,
        default_workspace=str(default_workspace),
    )
    assert workspace_guard is None
    _apply_run_context_route_metadata(
        envelope,
        stale_context,
        principal_is_owner=True,
    )
    await manager.update(
        key,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "run_mode_source": "operator_default",
                "workspace": str(current_workspace),
                "domains": [
                    {
                        "domain": "current.example",
                        "scope": "chat",
                        "source": "manual",
                    }
                ],
            }
        },
    )

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent(text="ok")

    channel = _FakeChannel()
    runner = RecordingTurnRunner()
    try:
        await _run_turn_with_streaming(
            channel,
            runner,
            _message(),
            key,
            _FakeEventBridge(),
            None,
            config,
            route_envelope=envelope,
            session_manager=manager,
        )
    finally:
        await storage.close()

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.run_mode == "safe"
    assert tool_context.workspace_dir == str(current_workspace.resolve())
    assert tool_context.sandbox_run_context.run_mode_source == "operator_default"
    assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
        "current.example"
    ]
    assert getattr(tool_context, "_sandbox_run_context_fresh", False) is True


@pytest.mark.asyncio
async def test_direct_channel_revalidates_post_accept_retarget_before_tool_context(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "channel-retarget.db"))
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
    key = "agent:main:matrix:retargeted-project-channel"
    await storage.upsert_session(
        SessionNode(
            session_key=key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
    )
    accepted_entry = await manager.append_message(key, "user", "hello")
    original_get_session = storage.get_session
    retargeted = False

    async def retarget_at_execution(session_key: str):
        nonlocal retargeted
        session = await original_get_session(session_key)
        if session_key == key and not retargeted:
            retargeted = True
            _retarget_directory_link(project_path, replacement, project_backup)
        return session

    storage.get_session = retarget_at_execution  # type: ignore[method-assign]

    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.stream_calls = 0

        async def send_streaming(self, chunks, **kwargs):
            self.stream_calls += 1
            async for _ in chunks:
                pass

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    channel = StreamingChannel()
    runner = RecordingTurnRunner()
    transcript: list[Any] = []
    try:
        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await _run_turn_with_streaming(
                channel,
                runner,
                _message(),
                key,
                _FakeEventBridge(),
                None,
                GatewayConfig(
                    workspace_dir=str(tmp_path / "default"),
                    agent_stream_heartbeat_interval_seconds=0.0,
                    agent_stream_idle_timeout_seconds=1.0,
                ),
                session_manager=manager,
            )
        transcript = await manager.get_transcript(key)
    finally:
        if retargeted:
            _restore_retargeted_directory(project_path, project_backup)
        await storage.close()

    assert raised.value.reason == "canonical_changed"
    assert retargeted is True
    assert [entry.content for entry in transcript] == [accepted_entry.content]
    assert runner.calls == []
    assert channel.stream_calls == 0
    assert channel.sent == []


@pytest.mark.asyncio
async def test_channel_streaming_turn_passes_normalized_attachments() -> None:
    class StreamingChannel(_FakeChannel):
        async def send_streaming(self, chunks, **kwargs):
            async for _ in chunks:
                pass

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run(self, message: str, session_key: str, **kwargs):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()
    attachment = {
        "type": "text/plain",
        "name": "note.txt",
        "data": base64.b64encode(b"hello").decode("ascii"),
    }

    await _run_turn_with_streaming(
        StreamingChannel(),
        runner,
        _message(),
        "agent:main:channel-stream-attachment",
        _FakeEventBridge(),
        None,
        SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        attachments=[attachment],
    )

    assert runner.calls[0]["attachments"] == [attachment]


@pytest.mark.asyncio
async def test_debounce_channel_turn_honors_attachment_persistence_config(tmp_path) -> None:
    class RecordingLock:
        def __init__(self) -> None:
            self.in_lock = False

        def locked(self) -> bool:
            return self.in_lock

        async def __aenter__(self):
            self.in_lock = True

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.in_lock = False

    lock = RecordingLock()

    class ResolvingChannel(_FakeChannel):
        channel_id = "test"

        async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
            assert lock.in_lock is False
            return Attachment(
                name=attachment.name,
                mime_type=attachment.mime_type,
                data=b"%PDF-1.4\nbody\n",
            )

    class FakeSessionManager:
        def __init__(self) -> None:
            self.entries: list[dict[str, str]] = []

        async def get_or_create(self, key: str, **kwargs):
            return SimpleNamespace(session_key=key, **kwargs), True

        async def update(self, key: str, **kwargs) -> None:
            pass

        async def append_message(self, key: str, role: str, content: str):
            entry = {"role": role, "content": content}
            self.entries.append(entry)
            return SimpleNamespace(content=content)

        async def read_transcript(self, key: str):
            return list(self.entries)

    class FakeTaskRuntime:
        def __init__(self) -> None:
            self.enqueue_calls: list[dict] = []
            self.envelopes: list[object] = []

        async def enqueue(self, envelope, message: str, **kwargs):
            self.envelopes.append(envelope)
            self.enqueue_calls.append({"message": message, **kwargs})
            return SimpleNamespace(task_id="t1")

        async def wait(self, task_id: str):
            return SimpleNamespace(status="succeeded")

    class FakeTurnRunner:
        def _get_session_lock(self, key: str):
            return lock

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="read this",
        attachments=[Attachment(name="doc.pdf", mime_type="application/pdf", url="mxc://doc")],
        provenance=IngressProvenance(
            provider="matrix",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="u1"),
        ),
    )
    runtime = FakeTaskRuntime()
    session_manager = FakeSessionManager()
    config = SimpleNamespace(
        channel_admin_senders={"matrix": ["u1"]},
        attachments=SimpleNamespace(
            persist_transcripts=False,
            media_root=str(tmp_path),
            transcript_disk_budget_bytes=1024,
        )
    )

    await _dispatch_combined_message_after_debounce(
        ResolvingChannel(),
        SimpleNamespace(message=msg, raw_content="read this", coalesced_count=1),
        FakeTurnRunner(),
        session_manager,
        "agent:main:matrix:direct:u1",
        "matrix",
        runtime,
        config,
    )

    persisted = json.loads(session_manager.entries[-1]["content"])
    assert persisted["attachments"][0] == {
        "name": "doc.pdf",
        "mime": "application/pdf",
        "size": len(b"%PDF-1.4\nbody\n"),
        "missing_reason": "attachment persistence disabled",
    }
    assert "sha256_ref" not in persisted["attachments"][0]
    assert not (tmp_path / "transcripts").exists()
    assert runtime.enqueue_calls[0]["attachments"][0]["_was_staged"] is True
    assert runtime.envelopes[0].metadata["principal_is_owner"] is True


@pytest.mark.asyncio
async def test_runtime_reply_delivers_transcript_artifact_with_adapter_upload(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"\x89PNG\r\n\x1a\nimage bytes",
        session_id="session-1",
        session_key="agent:main:feishu:direct:u1",
        name="思考快与慢_信息图.png",
        mime="image/png",
        source="publish_artifact",
    )

    class FileUploadingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    class FakeTaskRuntime:
        async def wait(self, task_id: str):
            return SimpleNamespace(status="succeeded")

    class FakeSessionManager:
        async def read_transcript(self, key: str):
            return [
                {"role": "user", "content": "create image"},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "text": "做好了，点击上方按钮下载。",
                            "artifacts": [ref.to_dict()],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

    channel = FileUploadingChannel()
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=FakeTaskRuntime(),
        session_manager=FakeSessionManager(),
        session_key="agent:main:feishu:direct:u1",
        task_id="task-1",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=_message(),
        transcript_watermark=1,
        config=config,
    )

    assert channel.sent[-1].content == "做好了，点击上方按钮下载。"
    assert channel.files == [("c1", "思考快与慢_信息图.png")]


@pytest.mark.asyncio
async def test_runtime_reply_delivers_file_artifact_with_adapter_upload(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"%PDF-1.4\nreport",
        session_id="session-1",
        session_key="agent:main:feishu:direct:u1",
        name="report.pdf",
        mime="application/pdf",
        source="publish_artifact",
    )

    class FileUploadingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[str, str]] = []

        async def send_file(self, chat_id: str, file_path: str) -> None:
            assert Path(file_path).is_file()
            self.files.append((chat_id, Path(file_path).name))

    class FakeTaskRuntime:
        async def wait(self, task_id: str):
            return SimpleNamespace(status="succeeded")

    class FakeSessionManager:
        async def read_transcript(self, key: str):
            return [
                {"role": "user", "content": "make report"},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "text": "报告已生成。",
                            "artifacts": [ref.to_dict()],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

    channel = FileUploadingChannel()
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=FakeTaskRuntime(),
        session_manager=FakeSessionManager(),
        session_key="agent:main:feishu:direct:u1",
        task_id="task-1",
        route_envelope=SimpleNamespace(reply_target=None),
        inbound=_message(),
        transcript_watermark=1,
        config=config,
    )

    assert channel.sent[-1].content == "报告已生成。"
    assert channel.files == [("c1", "report.pdf")]


# ── Stream relay coalescing + per-event fallback ────────────────────────────


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_coalesces_consecutive_deltas() -> None:
    """Consecutive text deltas are batched into a single chunk under the
    char threshold once the window expires.
    """

    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=50.0,
            stream_relay_coalesce_chars=256,
        ),
    )
    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    # Push four small deltas in quick succession then close. The relay
    # must coalesce them rather than yield four separate chunks.
    await relay.emit(TextDeltaEvent(text="hel"))
    await relay.emit(TextDeltaEvent(text="lo "))
    await relay.emit(TextDeltaEvent(text="wor"))
    await relay.emit(TextDeltaEvent(text="ld"))
    await relay.close()

    full_text = "".join(channel.chunks)
    assert full_text == "hello world"
    # Coalescing should land them in a single chunk; allow up to two chunks
    # in case scheduler latency split the batch in half.
    assert len(channel.chunks) <= 2


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_coalesces_at_char_threshold() -> None:
    """A single delta exceeding the char threshold yields immediately."""

    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=10_000.0,  # very long window
            stream_relay_coalesce_chars=8,
        ),
    )
    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    # Push enough characters to cross the char threshold without waiting
    # for the time window. The relay must yield without delay.
    for _ in range(4):
        await relay.emit(TextDeltaEvent(text="abcd"))
    await relay.close()

    assert "".join(channel.chunks) == "abcdabcdabcdabcd"
    # First chunk must have crossed the 8-char threshold.
    assert len(channel.chunks[0]) >= 8


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_falls_back_on_mid_stream_failure() -> None:
    """When send_streaming raises mid-stream, the relay flushes the
    not-yet-delivered chunks via channel.send so the user still sees the
    rest of the reply.
    """

    class FailingStreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered_chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            count = 0
            async for chunk in chunks:
                self.delivered_chunks.append(chunk)
                count += 1
                if count == 1:
                    raise RuntimeError("network blip")

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=0.0,
            stream_relay_coalesce_chars=0,
        ),
    )
    channel = FailingStreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="part-one"))
    await relay.emit(TextDeltaEvent(text="part-two"))
    await relay.emit(TextDeltaEvent(text="part-three"))
    await relay.close()

    # First chunk was consumed before the consumer raised — it appears in
    # the consumer-side delivered list but the relay treats it as
    # not-delivered because the consumer failed to fully process it.
    assert channel.delivered_chunks == ["part-one"]
    # Streaming error recorded.
    assert isinstance(relay.stream_error, Exception)
    # Fallback batch carries every chunk the consumer did not finish
    # processing successfully — including the chunk that crashed it so the
    # user does not lose content.
    assert channel.sent, "fallback channel.send must fire when streaming fails"
    fallback = channel.sent[-1].content
    assert "part-one" in fallback
    assert "part-two" in fallback
    assert "part-three" in fallback


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_no_fallback_on_success() -> None:
    """Successful streams must not trigger the fallback channel.send."""

    class StreamingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=0.0,
            stream_relay_coalesce_chars=0,
        ),
    )
    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    await relay.emit(TextDeltaEvent(text="hello"))
    await relay.close()

    assert channel.chunks == ["hello"]
    assert channel.sent == [], "no fallback send on a successful stream"
    assert relay.stream_error is None


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_disabled_coalescing_yields_each_delta() -> None:
    """Both window=0 and chars=0 disables coalescing — each delta yields."""

    class StreamingChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.chunks: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            async for chunk in chunks:
                self.chunks.append(chunk)

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=0.0,
            stream_relay_coalesce_chars=0,
        ),
    )
    channel = StreamingChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    for chunk in ("a", "b", "c"):
        await relay.emit(TextDeltaEvent(text=chunk))
    await relay.close()

    assert channel.chunks == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_runtime_channel_stream_relay_handles_late_failure_gracefully() -> None:
    """When the failure happens after most chunks delivered, only the
    remaining slice is sent via fallback — already-delivered chunks are
    not duplicated.
    """

    class FailingLateChannel(_StableReplaceableFakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.delivered: list[str] = []

        async def send_streaming(self, chunks, **kwargs):
            count = 0
            async for chunk in chunks:
                count += 1
                if count == 3:
                    raise RuntimeError("very late blip")
                self.delivered.append(chunk)

        async def edit(self, message_id: str, content: str) -> None:
            pass

    class FakeTaskRuntime:
        async def enqueue(self, envelope, message: str, *, stream_event_sink=None):
            return None

    config = SimpleNamespace(
        task_runtime=SimpleNamespace(
            stream_relay_coalesce_ms=0.0,
            stream_relay_coalesce_chars=0,
        ),
    )
    channel = FailingLateChannel()
    relay = _RuntimeChannelStreamRelay.maybe_start(
        channel,
        _message(),
        FakeTaskRuntime(),
        config,
    )

    assert relay is not None

    for chunk in ("alpha", "beta", "gamma", "delta"):
        await relay.emit(TextDeltaEvent(text=chunk))
    await relay.close()

    # First two chunks reached the consumer (and were appended to delivered);
    # gamma was pulled from the iterator but the consumer raised before
    # appending it; delta never left the relay queue.
    assert channel.delivered == ["alpha", "beta"]
    # Fallback delivers the un-acknowledged slice. The chunk that crashed
    # the consumer (gamma) and the queued tail (delta) must appear so the
    # user does not lose content.
    assert channel.sent, "fallback must fire on late mid-stream failure"
    fallback = channel.sent[-1].content
    assert "gamma" in fallback
    assert "delta" in fallback
    # Successfully-yielded chunks must NOT be duplicated.
    assert "alpha" not in fallback
    assert "beta" not in fallback
