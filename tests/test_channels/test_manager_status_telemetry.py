"""B1/B3/B4 additive channel telemetry: status pushes, uptime, restart counts."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.channels.manager import ChannelManager
from opensquilla.channels.types import ChannelHealth


class _RecordingBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    async def broadcast_scoped(self, event_name, payload=None, *, required_scope):
        self.events.append((event_name, payload or {}, required_scope))


class _StubAdapter:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(connected=self._connected)


@pytest.mark.asyncio
async def test_dispatch_state_change_pushes_channel_status_event():
    bridge = _RecordingBridge()
    manager = ChannelManager({"slack": _StubAdapter()}, None, None, _event_bridge=bridge)

    manager._set_dispatch_state("slack", "running")
    manager._set_dispatch_state("slack", "running")  # no change → no event
    manager._set_dispatch_state("slack", "dead")
    await asyncio.sleep(0)  # let the fire-and-forget broadcast tasks run

    names = [(e[0], e[1]["status"], e[2]) for e in bridge.events]
    assert names == [
        ("channel.status", "running", "operator.read"),
        ("channel.status", "dead", "operator.read"),
    ]


@pytest.mark.asyncio
async def test_no_bridge_is_a_silent_noop():
    manager = ChannelManager({"slack": _StubAdapter()}, None, None)
    manager._set_dispatch_state("slack", "running")  # must not raise
    manager._set_dispatch_state("slack", "dead")


@pytest.mark.asyncio
async def test_running_stamps_connected_since_and_health_mirrors_telemetry():
    manager = ChannelManager({"slack": _StubAdapter()}, None, None)
    manager._restart_counts["slack"] = 2
    manager._set_dispatch_state("slack", "running")

    health = await manager.health()
    extra = health["slack"].extra
    assert extra["dispatch_state"] == "running"
    assert extra["restart_attempts"] == 2
    assert isinstance(extra["connected_since"], int) and extra["connected_since"] > 0


@pytest.mark.asyncio
async def test_dead_clears_connected_since():
    manager = ChannelManager({"slack": _StubAdapter()}, None, None)
    manager._set_dispatch_state("slack", "running")
    assert "slack" in manager._running_since
    manager._set_dispatch_state("slack", "dead")
    assert "slack" not in manager._running_since

    health = await manager.health()
    assert "connected_since" not in health["slack"].extra
