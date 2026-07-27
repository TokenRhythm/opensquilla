"""A reply over the channel's per-message cap is split before it is sent.

Providers reject an over-length message wholesale, so the whole answer is
lost. The dispatcher enforces each channel's declared cap centrally — in the
unit that channel counts in — and splits or, as a last resort, truncates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.channels.contract import (
    ChannelCapabilityProfile,
    ChannelLengthUnit,
)
from opensquilla.channels.types import Attachment, OutgoingMessage
from opensquilla.gateway.channel_dispatch import (
    _as_chunk_message,
    _deliver_reply_or_notify,
    _plan_outbound_pieces,
)


def _route() -> SimpleNamespace:
    return SimpleNamespace(channel_id="chat-1", thread_id=None, channel_name="probe")


class _Channel:
    """Declares a capability profile and records every send."""

    def __init__(self, profile: ChannelCapabilityProfile | None) -> None:
        self._profile = profile
        self.sent: list[OutgoingMessage] = []

    def capability_profile(self) -> ChannelCapabilityProfile | None:
        return self._profile

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


def _profile(**kwargs: object) -> ChannelCapabilityProfile:
    return ChannelCapabilityProfile(channel_type="probe", **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("opensquilla.gateway.channel_dispatch.asyncio.sleep", _instant)


def test_in_budget_reply_is_sent_whole() -> None:
    channel = _Channel(_profile(max_message_len=100))
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content="hi", reply_to="chat-1"))
    assert len(pieces) == 1
    assert pieces[0].content == "hi"


def test_no_profile_passes_the_message_through() -> None:
    channel = _Channel(None)
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content="x" * 999, reply_to="c"))
    assert len(pieces) == 1


def test_zero_cap_means_unbounded_passthrough() -> None:
    channel = _Channel(_profile(max_message_len=0))
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content="x" * 999, reply_to="c"))
    assert len(pieces) == 1


def test_native_splitter_is_left_untouched() -> None:
    # An adapter that splits inside its own send() opts out; splitting here too
    # would double-split.
    channel = _Channel(_profile(max_message_len=10, splits_natively=True))
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content="x" * 50, reply_to="c"))
    assert len(pieces) == 1


def test_over_cap_reply_is_split_by_utf16_units() -> None:
    channel = _Channel(
        _profile(max_message_len=4096, length_unit=ChannelLengthUnit.UTF16_UNITS)
    )
    text = "🎉" * 3000  # 6000 UTF-16 units
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content=text, reply_to="c"))
    assert len(pieces) >= 2
    assert "".join(p.content for p in pieces) == text
    assert all(len(p.content.encode("utf-16-le")) // 2 <= 4096 for p in pieces)


def test_over_cap_reply_is_split_by_utf8_bytes() -> None:
    channel = _Channel(
        _profile(max_message_len=2048, length_unit=ChannelLengthUnit.UTF8_BYTES)
    )
    text = "字" * 1000  # 3000 UTF-8 bytes
    pieces = _plan_outbound_pieces(channel, OutgoingMessage(content=text, reply_to="c"))
    assert len(pieces) >= 2
    assert "".join(p.content for p in pieces) == text
    assert all(len(p.content.encode("utf-8")) <= 2048 for p in pieces)


def test_each_chunk_gets_a_distinct_delivery_id() -> None:
    # The outbox keys a row on delivery_id via INSERT-OR-IGNORE; if chunks
    # shared one id, every chunk after the first would collapse into one row
    # and the last receipt would win. Each chunk must mint a fresh id.
    original = OutgoingMessage(
        content="body", reply_to="c", metadata={"delivery_id": "shared-123"}
    )
    a = _as_chunk_message(original, "chunk-a", first=True)
    b = _as_chunk_message(original, "chunk-b", first=False)
    # The stamped id is dropped so the send guard mints a distinct one per chunk.
    assert "delivery_id" not in a.metadata
    assert "delivery_id" not in b.metadata
    assert original.metadata["delivery_id"] == "shared-123"  # original untouched


def test_attachments_ride_only_the_first_chunk() -> None:
    attachment = Attachment(name="a.txt", url="file://a")
    original = OutgoingMessage(
        content="x" * 50, reply_to="c", attachments=[attachment]
    )
    first = _as_chunk_message(original, "part 1", first=True)
    rest = _as_chunk_message(original, "part 2", first=False)
    assert first.attachments == [attachment]
    assert rest.attachments == []


async def test_every_chunk_is_delivered_end_to_end() -> None:
    channel = _Channel(
        _profile(max_message_len=2048, length_unit=ChannelLengthUnit.UTF8_BYTES)
    )
    text = "字" * 1000
    delivered = await _deliver_reply_or_notify(
        channel,
        OutgoingMessage(content=text, reply_to="chat-1"),
        route_envelope=_route(),
        session_key="s",
    )
    assert delivered is True
    assert len(channel.sent) >= 2
    assert "".join(m.content for m in channel.sent) == text
    # Distinct outbox identity per chunk.
    ids = [m.metadata.get("delivery_id") for m in channel.sent]
    assert len(set(ids)) == len(ids)
