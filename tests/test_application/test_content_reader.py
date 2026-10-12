"""Bounded legacy transcript content reader contract tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from opensquilla.application.content_reader import (
    MAX_CONTENT_RANGE_BYTES,
    ContentEncodingError,
    ContentRange,
    ContentRangeError,
    ContentReader,
    LegacyContentRef,
    iter_content_ranges,
    read_utf8_text,
)


@dataclass
class FakeReader(ContentReader):
    body: bytes
    calls: list[tuple[int, int]]

    async def get_ref(self, session_id: str, message_id: str, *, source=None):
        del source
        return LegacyContentRef(session_id, message_id, "active", len(self.body))

    async def read_range(self, ref, *, offset=0, limit=MAX_CONTENT_RANGE_BYTES):
        self.calls.append((offset, limit))
        return ContentRange(ref, offset, limit, self.body[offset : offset + limit])


@pytest.mark.asyncio
async def test_large_json_body_is_consumed_in_bounded_ranges() -> None:
    # Keep the body virtual at the reader boundary: this test proves the
    # reader contract without requiring a 128 MiB Python allocation.  The
    # shape matches the old JSON envelope stored in transcript content.
    prefix = b'{"text":"'
    suffix = b'"}'
    body_len = 128 * 1024 * 1024
    ref = LegacyContentRef("s", "m", "active", body_len)

    class VirtualReader(FakeReader):
        def __init__(self) -> None:
            super().__init__(b"", [])

        async def read_range(self, ref, *, offset=0, limit=MAX_CONTENT_RANGE_BYTES):
            self.calls.append((offset, limit))
            data = bytearray()
            for index in range(offset, min(offset + limit, ref.byte_length)):
                if index < len(prefix):
                    data.append(prefix[index])
                elif index >= ref.byte_length - len(suffix):
                    data.append(suffix[index - (ref.byte_length - len(suffix))])
                else:
                    data.append(ord("x"))
            return ContentRange(ref, offset, limit, bytes(data))

    reader = VirtualReader()
    chunks = []
    async for chunk in iter_content_ranges(reader, ref, chunk_bytes=64 * 1024):
        assert len(chunk.data) <= 64 * 1024
        chunks.append(chunk)
    assert sum(len(chunk.data) for chunk in chunks) == body_len
    assert len(chunks) == (body_len + 64 * 1024 - 1) // (64 * 1024)
    assert all(limit == 64 * 1024 for _, limit in reader.calls)
    assert chunks[0].data == prefix + b"x" * (64 * 1024 - len(prefix))
    assert chunks[-1].data.endswith(suffix)


@pytest.mark.asyncio
async def test_utf8_codepoint_may_cross_ranges() -> None:
    reader = FakeReader("a你好b".encode(), [])
    ref = await reader.get_ref("s", "m")
    assert await read_utf8_text(reader, ref, chunk_bytes=2) == "a你好b"
    assert reader.calls == [(0, 2), (2, 2), (4, 2), (6, 2)]


def test_range_limit_is_hard_and_invalid_ranges_are_rejected() -> None:
    with pytest.raises(ContentRangeError):
        # The reader must never silently widen a caller's allocation budget.
        from opensquilla.application.content_reader import validate_content_range

        validate_content_range(0, MAX_CONTENT_RANGE_BYTES + 1)
    with pytest.raises(ContentRangeError):
        from opensquilla.application.content_reader import validate_content_range

        validate_content_range(-1, 1)


@pytest.mark.asyncio
async def test_invalid_utf8_is_request_scoped_error() -> None:
    reader = FakeReader(b"good\xffbad", [])
    ref = await reader.get_ref("s", "m")
    with pytest.raises(ContentEncodingError):
        await read_utf8_text(reader, ref, chunk_bytes=3)
