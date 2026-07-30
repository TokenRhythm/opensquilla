from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.types import IncomingMessage, IngressVerification


def _channel(tmp_path, **overrides) -> MatrixChannel:
    return MatrixChannel(
        MatrixChannelConfig(
            name=overrides.pop("name", "matrix-main"),
            homeserver_url=overrides.pop("homeserver_url", "https://matrix.example.test"),
            user_id=overrides.pop("user_id", "@bot:example.test"),
            workspace_dir=str(tmp_path),
            **overrides,
        )
    )


def test_matrix_session_state_is_account_scoped_and_private(tmp_path) -> None:
    first = _channel(tmp_path, name="first")
    second = _channel(tmp_path, name="second")

    assert first._session_path() != second._session_path()

    first._save_session(
        user_id="@bot:example.test",
        device_id="DEVICE",
        access_token="secret-token",
    )

    path = first._session_path()
    if os.name != "nt":  # POSIX permission bits are meaningless on Windows
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert first._load_session() is not None


def test_matrix_session_is_bound_to_homeserver_and_user(tmp_path) -> None:
    original = _channel(tmp_path)
    original._save_session(
        user_id="@bot:example.test",
        device_id="DEVICE",
        access_token="secret-token",
    )
    session_path = original._session_path()

    mismatched = _channel(tmp_path, homeserver_url="https://elsewhere.example.test")
    # Copy into the other account scope to prove payload binding is checked,
    # not merely path isolation.
    mismatched._session_path().write_bytes(session_path.read_bytes())

    assert mismatched._load_session() is None


@pytest.mark.asyncio
async def test_matrix_uses_m_direct_instead_of_member_count_for_admission(tmp_path) -> None:
    channel = _channel(tmp_path)
    room = SimpleNamespace(room_id="!room:example.test", member_count=2)
    event = SimpleNamespace(
        event_id="$event",
        sender="@user:example.test",
        body="hello",
        server_timestamp=1,
        source={"content": {"body": "hello"}},
    )

    await channel._on_room_message_text(room, event)
    group_message = await channel.receive()
    assert group_message.metadata["is_group"] is True

    channel._direct_room_ids.add(room.room_id)
    event.event_id = "$event-2"
    await channel._on_room_message_text(room, event)
    direct_message = await channel.receive()

    assert direct_message.metadata["is_group"] is False
    assert direct_message.provenance.verification == IngressVerification.SDK_SESSION
    assert direct_message.provenance.principal is not None
    assert direct_message.provenance.principal.subject_id == "@user:example.test"


@pytest.mark.asyncio
async def test_matrix_reply_uses_m_in_reply_to_relation(tmp_path) -> None:
    sent: list[dict] = []

    class Client:
        async def room_send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(event_id="$reply")

    channel = _channel(tmp_path)
    channel._client = Client()
    message = IncomingMessage(
        sender_id="@user:example.test",
        channel_id="!room:example.test",
        content="hello",
        metadata={"event_id": "$original", "is_group": False},
    )
    reply = channel.build_reply_message("world", message)

    await channel.send(reply)

    assert sent[0]["content"]["m.relates_to"] == {"m.in_reply_to": {"event_id": "$original"}}


@pytest.mark.asyncio
async def test_matrix_health_requires_a_live_sync_task(tmp_path) -> None:
    channel = _channel(tmp_path)
    channel._connected = True
    channel._sync_task = asyncio.create_task(asyncio.sleep(0))
    await channel._sync_task

    assert (await channel.health_check()).connected is False
