from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import opensquilla.scheduler.heartbeat_service as heartbeat_service_module
from opensquilla.engine.types import DoneEvent, TextDeltaEvent
from opensquilla.scheduler.heartbeat_service import HeartbeatService


class _LegacyStreamingTurnRunner:
    def __init__(self, text: str) -> None:
        self.text = text

    async def run(self, **_kwargs):
        yield TextDeltaEvent(text=self.text)
        # Legacy providers can omit both terminal text fields.  HeartbeatService
        # must then sanitize its accumulated streaming fallback before delivery.
        yield DoneEvent()


@pytest.mark.asyncio
async def test_empty_heartbeat_summary_is_not_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    service = HeartbeatService(
        turn_runner=None,
        session_storage=None,
        channel_manager_ref=lambda: None,
    )
    collect_output = AsyncMock(return_value="")
    send_delivery = AsyncMock(return_value=None)
    infer_delivery = AsyncMock(
        return_value=SimpleNamespace(
            channel_name="feishu",
            channel_id="chat-id",
            account_id="",
            thread_id="",
        )
    )
    monkeypatch.setattr(service, "_collect_output", collect_output)
    monkeypatch.setattr(service, "_send_delivery", send_delivery)
    monkeypatch.setattr(heartbeat_service_module, "infer_delivery", infer_delivery)

    result = await service.run_once(
        reason="scheduled",
        agent_id="main",
        session_key="agent:main:main",
        prompt="Reply HEARTBEAT_OK.",
    )

    assert result.status == "skipped"
    assert result.delivery_status == "skipped"
    assert result.reason == "heartbeat_ack"
    assert result.summary == ""
    infer_delivery.assert_not_awaited()
    send_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_fallback_removes_think_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HeartbeatService(
        turn_runner=_LegacyStreamingTurnRunner(
            "<think>private fallback reasoning</think>\nDisk usage reached 95%."
        ),
        session_storage=None,
        channel_manager_ref=lambda: None,
    )
    send_delivery = AsyncMock(return_value=None)
    infer_delivery = AsyncMock(
        return_value=SimpleNamespace(
            channel_name="feishu",
            channel_id="chat-id",
            account_id="",
            thread_id="",
        )
    )
    monkeypatch.setattr(service, "_send_delivery", send_delivery)
    monkeypatch.setattr(heartbeat_service_module, "infer_delivery", infer_delivery)

    result = await service.run_once(
        reason="scheduled",
        agent_id="main",
        session_key="agent:main:main",
        prompt="Check system health.",
    )

    assert result.status == "delivered"
    assert result.summary == "Disk usage reached 95%."
    send_delivery.assert_awaited_once()
    assert send_delivery.await_args.args[1] == "Disk usage reached 95%."


@pytest.mark.asyncio
async def test_streaming_fallback_ack_is_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HeartbeatService(
        turn_runner=_LegacyStreamingTurnRunner(
            "<think>private fallback reasoning</think>\nHEARTBEAT_OK"
        ),
        session_storage=None,
        channel_manager_ref=lambda: None,
    )
    send_delivery = AsyncMock(return_value=None)
    infer_delivery = AsyncMock()
    monkeypatch.setattr(service, "_send_delivery", send_delivery)
    monkeypatch.setattr(heartbeat_service_module, "infer_delivery", infer_delivery)

    result = await service.run_once(
        reason="scheduled",
        agent_id="main",
        session_key="agent:main:main",
        prompt="Check system health.",
    )

    assert result.status == "skipped"
    assert result.reason == "heartbeat_ack"
    assert result.summary == ""
    infer_delivery.assert_not_awaited()
    send_delivery.assert_not_awaited()
