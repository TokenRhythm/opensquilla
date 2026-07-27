from __future__ import annotations

from types import SimpleNamespace

from opensquilla.channels.manager import ChannelManager


def test_channel_session_key_prefers_explicit_direct_metadata() -> None:
    message = SimpleNamespace(
        sender_id="user-1",
        channel_id="C-general",
        metadata={"is_group": False, "channel_type": "channel"},
    )

    key = ChannelManager._build_session_key("slack", message, agent_id="ops")

    assert key == "agent:ops:slack:direct:user-1"


def test_channel_session_key_uses_group_room_and_thread_when_marked_group() -> None:
    message = SimpleNamespace(
        sender_id="user-1",
        channel_id="C-general",
        metadata={"is_group": True, "thread_ts": "171234.000"},
    )

    key = ChannelManager._build_session_key("slack", message, agent_id="ops")

    assert key == (
        "agent:ops:slack:group:C-general:sender:user-1:thread:171234.000"
    )
