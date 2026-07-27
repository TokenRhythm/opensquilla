"""/meta is registered on Gateway-backed surfaces only."""

from __future__ import annotations

from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface


def test_meta_command_present_on_gateway_backed_surfaces() -> None:
    cmd = DEFAULT_REGISTRY.find("/meta")
    assert cmd is not None
    assert cmd.name == "/meta"
    for surface in (Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CHANNEL):
        assert cmd.execution_for(surface) is not None, surface
    assert cmd.execution_for(Surface.CLI_STANDALONE) is None


def test_meta_channel_execution_is_list_rpc() -> None:
    cmd = DEFAULT_REGISTRY.find("/meta")
    channel = cmd.execution_for(Surface.CHANNEL)
    # Channel lists via RPC; run is intentionally not wired here (channel = list only).
    assert channel is not None
    assert channel.rpc_method == "meta.list"
