from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.channels.types import IncomingMessage


def test_channel_dispatch_no_longer_uses_gateway_cron_intent_preflight() -> None:
    source = Path("src/opensquilla/gateway/channel_dispatch.py").read_text(
        encoding="utf-8"
    )

    assert "channel_cron_intent" not in source
    assert "_dispatch_channel_cron_intent" not in source


def test_gateway_cron_intent_module_removed_from_runtime_surface() -> None:
    assert not Path("src/opensquilla/gateway/channel_cron_intent.py").exists()


@pytest.mark.asyncio
async def test_schedule_like_channel_text_reaches_normal_runtime() -> None:
    from opensquilla.gateway.channel_dispatch import _ChannelInFlightSet, run_channel_dispatch

    msg = IncomingMessage(
        sender_id="ou_user",
        channel_id="oc_chat",
        content="每过五分钟提醒我喝水",
        metadata={"account_id": "tenant-a", "native_thread_id": "thread-1"},
    )
    channel = MagicMock()
    channel.channel_id = "feishu"
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None
    calls = 0

    async def _receive() -> IncomingMessage:
        nonlocal calls
        calls += 1
        if calls == 1:
            return msg
        raise asyncio.CancelledError

    channel.receive = _receive
    session_manager = MagicMock()
    session_manager.get_or_create = AsyncMock(return_value=(MagicMock(), False))
    session_manager.update = AsyncMock()
    task_runtime = MagicMock()
    turn_runner = MagicMock()
    turn_runner._get_session_lock.return_value = None
    status_reactor = SimpleNamespace(
        received=AsyncMock(),
        running=AsyncMock(),
        completed=AsyncMock(),
        failed=AsyncMock(),
    )

    with (
        patch(
            "opensquilla.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._transcript_watermark",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch.start_turn_via_runtime",
            new=AsyncMock(return_value=SimpleNamespace(task_id="task-1")),
        ) as start_turn,
        patch(
            "opensquilla.gateway.channel_dispatch._append_channel_user_message",
            new=AsyncMock(return_value=(MagicMock(), msg.content)),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._deliver_runtime_channel_reply",
            new=AsyncMock(),
        ),
        patch(
            "opensquilla.gateway.channel_dispatch._status_reactor",
            new=MagicMock(return_value=status_reactor),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_channel_dispatch(
                channel=channel,
                turn_runner=turn_runner,
                session_manager=session_manager,
                session_key_builder=lambda _msg: "agent:main:feishu:ou_user",
                session_prefix="feishu",
                task_runtime=task_runtime,
                _in_flight=_ChannelInFlightSet(cap=1),
            )

    start_turn.assert_awaited_once()
    assert start_turn.await_args.kwargs["run_kind"] == "channel_turn"
    channel.send.assert_not_awaited()
