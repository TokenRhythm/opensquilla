"""Unit-aware length measurement, splitting, and truncation.

A cap measured in the wrong unit drops replies: our users send emoji-heavy
Telegram chat (UTF-16) and CJK Feishu/WeCom traffic (UTF-8 bytes), both of
which look in-budget by ``len()`` and are rejected by the provider.
"""

from __future__ import annotations

import pytest

from opensquilla.channels._util import (
    measured_len,
    split_text_for_channel,
    truncate_to_limit,
)
from opensquilla.channels.contract import ChannelLengthUnit as U


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("a" * 10, U.CODE_POINTS, 10),
        ("a" * 10, U.UTF8_BYTES, 10),
        ("a" * 10, U.UTF16_UNITS, 10),
        ("字" * 10, U.CODE_POINTS, 10),
        ("字" * 10, U.UTF8_BYTES, 30),
        ("字" * 10, U.UTF16_UNITS, 10),
        ("🎉" * 10, U.CODE_POINTS, 10),
        ("🎉" * 10, U.UTF8_BYTES, 40),
        ("🎉" * 10, U.UTF16_UNITS, 20),
    ],
)
def test_measured_len_counts_in_the_declared_unit(text: str, unit: U, expected: int) -> None:
    assert measured_len(text, unit) == expected


def test_telegram_emoji_splits_by_utf16_units() -> None:
    # 🎉×3000 is 3000 code points but 6000 UTF-16 units — in budget by len(),
    # rejected wholesale by Telegram. Splitting in UTF-16 keeps every chunk
    # under the 4096-unit cap.
    text = "🎉" * 3000
    chunks = split_text_for_channel(text, 4096, unit=U.UTF16_UNITS)
    assert "".join(chunks) == text
    assert len(chunks) >= 2
    assert all(_utf16(chunk) <= 4096 for chunk in chunks)
    # No half-surrogate: each chunk round-trips through UTF-16 cleanly.
    for chunk in chunks:
        assert chunk.encode("utf-16-le").decode("utf-16-le") == chunk


def test_wecom_cjk_splits_by_utf8_bytes() -> None:
    # 字×1000 is 1000 code points but 3000 UTF-8 bytes — over WeCom's 2048-byte
    # cap while looking fine by len().
    text = "字" * 1000
    chunks = split_text_for_channel(text, 2048, unit=U.UTF8_BYTES)
    assert "".join(chunks) == text
    assert len(chunks) >= 2
    assert all(len(chunk.encode("utf-8")) <= 2048 for chunk in chunks)


def test_default_unit_is_backward_compatible() -> None:
    # The 3 native-splitter callers pass no unit; behavior must be unchanged.
    assert split_text_for_channel("a b c", 100) == ["a b c"]
    assert split_text_for_channel("", 100) == [""]
    assert split_text_for_channel("x" * 10, 0) == ["x" * 10]
    # Paragraph boundary preferred, separator kept with the preceding chunk.
    text = "para one\n\npara two that is quite long"
    chunks = split_text_for_channel(text, 12)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_split_prefers_boundaries_then_hard_splits() -> None:
    # A single over-long token with no interior boundary is hard-split.
    token = "x" * 50
    chunks = split_text_for_channel(token, 10)
    assert "".join(chunks) == token
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert len(chunks) == 5


def test_truncate_appends_a_footer_within_budget() -> None:
    text = "字" * 1000
    result = truncate_to_limit(text, 100, unit=U.UTF8_BYTES)
    assert len(result.encode("utf-8")) <= 100
    assert "truncated" in result
    assert result.startswith("字")


def test_truncate_without_footer_when_footer_alone_overflows() -> None:
    # A cap smaller than the footer must still produce a within-cap result.
    text = "hello world this is long"
    result = truncate_to_limit(text, 5, unit=U.CODE_POINTS)
    assert len(result) <= 5
    assert "truncated" not in result


def test_truncate_is_a_noop_when_already_within_limit() -> None:
    assert truncate_to_limit("short", 100, unit=U.CODE_POINTS) == "short"
