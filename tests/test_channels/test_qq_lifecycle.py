from __future__ import annotations

import asyncio

import pytest

from opensquilla.channels.qq import QQChannel, QQChannelConfig


def _channel() -> QQChannel:
    return QQChannel(QQChannelConfig(name="qq", app_id="app-id", app_secret="app-secret"))


@pytest.mark.asyncio
async def test_qq_health_is_not_connected_until_sdk_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel()
    release = asyncio.Event()

    async def fake_run_forever() -> None:
        await release.wait()

    monkeypatch.setattr(channel, "_run_forever", fake_run_forever)
    await channel.start()
    try:
        assert (await channel.health_check()).connected is False

        await channel.on_ready()

        assert (await channel.health_check()).connected is True
    finally:
        release.set()
        await channel.stop()


@pytest.mark.asyncio
async def test_qq_health_clears_when_gateway_task_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel()

    async def fake_run_forever() -> None:
        channel._connected = True
        await asyncio.sleep(0)
        channel._connected = False

    monkeypatch.setattr(channel, "_run_forever", fake_run_forever)
    await channel.start()
    task = channel._run_task
    assert task is not None
    await task

    assert (await channel.health_check()).connected is False
