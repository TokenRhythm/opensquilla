from __future__ import annotations

from typing import Any

from opensquilla import token_estimation
from opensquilla.session import tokenizer


class _SizedTokens:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class _RecordingEncoding:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[Any, ...]]] = []

    def encode(
        self,
        text: str,
        *,
        disallowed_special: tuple[Any, ...],
    ) -> _SizedTokens:
        self.calls.append((len(text), disallowed_special))
        return _SizedTokens((len(text) + 9) // 10)


def test_large_text_uses_bounded_tokenizer_chunks(
    monkeypatch: Any,
) -> None:
    encoding = _RecordingEncoding()
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    count, source = tokenizer.estimate_tokens_with_source("x" * 250_001)

    assert [size for size, _ in encoding.calls] == [100_000, 100_000, 50_001]
    assert all(disallowed_special == () for _, disallowed_special in encoding.calls)
    assert count == 10_000 + 10_000 + 5_001
    assert source == "tiktoken_cl100k_base_chunked"


def test_unicode_fallback_uses_utf8_density_instead_of_chars_div_four(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)

    ascii_count, ascii_source = tokenizer.estimate_tokens_with_source("a" * 8)
    cjk_count, cjk_source = tokenizer.estimate_tokens_with_source("模型压缩")
    emoji_count, emoji_source = tokenizer.estimate_tokens_with_source("🦑🦑")

    assert ascii_count == 4
    assert cjk_count == 6
    assert emoji_count == 4
    assert {
        ascii_source,
        cjk_source,
        emoji_source,
    } == {"utf8_unicode_conservative"}


def test_integer_only_api_remains_backward_compatible(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        tokenizer,
        "estimate_tokens_with_source",
        lambda _text: (37, "synthetic"),
    )

    assert tokenizer.estimate_tokens("payload") == 37
