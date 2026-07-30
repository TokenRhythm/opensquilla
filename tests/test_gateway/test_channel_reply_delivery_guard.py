"""A user-visible reply must survive — or explain — a provider send failure.

Before the guard, ``_deliver_runtime_channel_reply`` did a bare
``await channel.send(...)``: one raised exception killed the reply task and
the user waited forever for an answer that was fully computed and paid for.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from opensquilla.channels._util import ChannelAccessPolicy
from opensquilla.channels.delivery_store import IngressClaim
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.gateway.channel_dispatch import (
    _REPLY_SEND_ATTEMPTS,
    _ChannelInFlightSet,
    _deliver_reply_or_notify,
    _send_channel_reply_guarded,
    run_channel_dispatch,
)


def _route() -> SimpleNamespace:
    return SimpleNamespace(channel_id="chat-1", thread_id=None, channel_name="slack-main")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://p.example/send")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("x", request=request, response=response)


class _Channel:
    """Records every send; fails the first ``fail_times`` with ``error``."""

    def __init__(self, *, fail_times: int = 0, error: BaseException | None = None) -> None:
        self.sent: list[OutgoingMessage] = []
        self._fail_times = fail_times
        self._error = error or _http_error(503)

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)
        if len(self.sent) <= self._fail_times:
            raise self._error


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("opensquilla.gateway.channel_dispatch.asyncio.sleep", _instant)


async def test_delivered_first_try_returns_none() -> None:
    channel = _Channel()
    result = await _send_channel_reply_guarded(
        channel, OutgoingMessage(content="hi", reply_to="chat-1"), session_key="s"
    )
    assert result is None
    assert len(channel.sent) == 1


async def test_transient_failure_is_retried_then_succeeds() -> None:
    channel = _Channel(fail_times=2, error=_http_error(503))
    result = await _send_channel_reply_guarded(
        channel, OutgoingMessage(content="hi", reply_to="chat-1"), session_key="s"
    )
    assert result is None
    assert len(channel.sent) == 3


async def test_all_attempts_share_one_delivery_id() -> None:
    # The outbox keys a row on delivery_id; retrying without a stable id would
    # spray one row per attempt. Stamp it once and every attempt reuses it.
    channel = _Channel(fail_times=_REPLY_SEND_ATTEMPTS, error=_http_error(503))
    await _send_channel_reply_guarded(
        channel, OutgoingMessage(content="hi", reply_to="chat-1"), session_key="s"
    )
    ids = {m.metadata.get("delivery_id") for m in channel.sent}
    assert len(channel.sent) == _REPLY_SEND_ATTEMPTS
    assert len(ids) == 1 and next(iter(ids))


async def test_fatal_failure_is_not_retried() -> None:
    # A 401 will fail identically on every attempt; retrying just stalls the
    # turn. Surface it after one try.
    channel = _Channel(fail_times=99, error=_http_error(401))
    result = await _send_channel_reply_guarded(
        channel, OutgoingMessage(content="hi", reply_to="chat-1"), session_key="s"
    )
    assert result == "auth_invalid"
    assert len(channel.sent) == 1


async def test_exhausted_retries_return_the_failure_class() -> None:
    channel = _Channel(fail_times=99, error=_http_error(503))
    result = await _send_channel_reply_guarded(
        channel, OutgoingMessage(content="hi", reply_to="chat-1"), session_key="s"
    )
    assert result == "transport_transient"
    assert len(channel.sent) == _REPLY_SEND_ATTEMPTS


async def test_on_final_loss_the_user_gets_a_delivery_notice() -> None:
    # A recoverable-looking class that never recovers: the reply is lost, but
    # the user is told it exists rather than left in silence.
    channel = _Channel(fail_times=99, error=_http_error(503))
    delivered = await _deliver_reply_or_notify(
        channel,
        OutgoingMessage(content="the answer", reply_to="chat-1"),
        route_envelope=_route(),
        session_key="s",
    )
    assert delivered is False
    # attempts for the reply, plus exactly one notice send.
    notices = [m for m in channel.sent if m.metadata.get("delivery_failure_notice")]
    assert len(notices) == 1


async def test_no_notice_when_every_send_to_the_target_is_hopeless() -> None:
    # target_missing: a notice would fail identically, so don't burn the call.
    channel = _Channel(fail_times=99, error=_http_error(404))
    delivered = await _deliver_reply_or_notify(
        channel,
        OutgoingMessage(content="the answer", reply_to="chat-1"),
        route_envelope=_route(),
        session_key="s",
    )
    assert delivered is False
    assert not any(m.metadata.get("delivery_failure_notice") for m in channel.sent)


async def test_a_failing_notice_never_masks_the_original_failure() -> None:
    class _AlwaysFails:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send(self, message: OutgoingMessage) -> None:
            self.sent.append(message)
            raise _http_error(503)

    channel = _AlwaysFails()
    # Must not raise even though the notice send also fails.
    delivered = await _deliver_reply_or_notify(
        channel,
        OutgoingMessage(content="x", reply_to="chat-1"),
        route_envelope=_route(),
        session_key="s",
    )
    assert delivered is False


async def test_success_after_notice_path_is_never_reached_on_delivery() -> None:
    channel = _Channel()
    delivered = await _deliver_reply_or_notify(
        channel,
        OutgoingMessage(content="hi", reply_to="chat-1"),
        route_envelope=_route(),
        session_key="s",
    )
    assert delivered is True
    assert not any(m.metadata.get("delivery_failure_notice") for m in channel.sent)


def _failing_dispatch_channel(*, supports_slash_commands: bool) -> SimpleNamespace:
    """A channel whose every send fails transiently, with a claimed ingress row."""
    channel = SimpleNamespace(
        policy=ChannelAccessPolicy(),
        supports_slash_commands=supports_slash_commands,
        send=AsyncMock(side_effect=_http_error(503)),
        _delivery_store=MagicMock(
            claim_inbound=MagicMock(return_value=IngressClaim("evt-1", "tok-1"))
        ),
        _delivery_channel_name="slack-main",
    )
    receive_count = 0

    async def receive() -> IncomingMessage:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return IncomingMessage(
                sender_id="user-1",
                channel_id="chat-1",
                content="/meta" if supports_slash_commands else "hello",
                metadata={"is_group": False, "message_id": "m-1"},
            )
        raise asyncio.CancelledError

    channel.receive = receive
    return channel


def _dispatch_session_manager() -> SimpleNamespace:
    return SimpleNamespace(
        get_or_create=AsyncMock(),
        update=AsyncMock(),
        append_message=AsyncMock(),
    )


async def test_command_reply_send_failure_does_not_escape_dispatch_loop() -> None:
    """A failed slash-command reply must not burn dispatch restart budget."""
    channel = _failing_dispatch_channel(supports_slash_commands=True)
    command_reply = OutgoingMessage(
        content="meta output",
        reply_to="chat-1",
        metadata={"command": "/meta", "method": "meta.get"},
    )

    with (
        patch(
            "opensquilla.gateway.channel_dispatch._maybe_resolve_channel_approval",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._dispatch_channel_slash_command",
            new=AsyncMock(return_value=command_reply),
        ),
    ):
        # Pre-guard, the failed reply send raised out of the loop instead of
        # reaching the next receive() (which ends the test with cancellation).
        with pytest.raises(asyncio.CancelledError):
            await run_channel_dispatch(
                channel=channel,
                turn_runner=SimpleNamespace(),
                session_manager=_dispatch_session_manager(),
                session_key_builder=lambda _msg: "agent:main:slack:dm:chat-1",
                session_prefix="slack",
                task_runtime=SimpleNamespace(),
                rpc_dispatcher=SimpleNamespace(),
                channel_rpc_context_factory=lambda _envelope: SimpleNamespace(),
            )

    # Bounded retries plus one delivery-failure notice, all inside the loop.
    assert channel.send.await_count == _REPLY_SEND_ATTEMPTS + 1
    channel._delivery_store.complete_inbound.assert_called_once()
    assert (
        channel._delivery_store.complete_inbound.call_args.args[1]
        == "command_dispatched"
    )


async def test_busy_notice_send_failure_does_not_escape_dispatch_loop() -> None:
    """A failed capacity notice must not burn dispatch restart budget."""
    channel = _failing_dispatch_channel(supports_slash_commands=False)

    with (
        patch(
            "opensquilla.gateway.channel_dispatch._maybe_resolve_channel_approval",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._apply_saved_channel_run_context",
            new=AsyncMock(),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_channel_dispatch(
                channel=channel,
                turn_runner=SimpleNamespace(),
                session_manager=_dispatch_session_manager(),
                session_key_builder=lambda _msg: "agent:main:slack:dm:chat-1",
                session_prefix="slack",
                task_runtime=SimpleNamespace(),
                _in_flight=_ChannelInFlightSet(0),
            )

    assert channel.send.await_count == 1
    channel._delivery_store.complete_inbound.assert_called_once()
    assert (
        channel._delivery_store.complete_inbound.call_args.args[1]
        == "capacity_rejected"
    )
