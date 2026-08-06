"""Bounded token estimation shared across package boundaries."""

from __future__ import annotations

from collections.abc import Iterator

import structlog

log = structlog.get_logger(__name__)

_encoding = None
_tiktoken_available: bool | None = None
_TOKENIZER_CHUNK_CHARS = 100_000

TokenEstimateSource = str


def _get_encoding():
    global _encoding, _tiktoken_available
    if _tiktoken_available is False:
        return None
    if _encoding is not None:
        return _encoding
    try:
        import tiktoken

        _encoding = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
        return _encoding
    except ImportError:
        _tiktoken_available = False
        log.info("tiktoken_unavailable_fallback")
        return None
    except Exception as exc:  # noqa: BLE001
        _tiktoken_available = False
        log.warning("tiktoken_encoding_unavailable_fallback", error=str(exc))
        return None


def _text_chunks(text: str) -> Iterator[str]:
    for offset in range(0, len(text), _TOKENIZER_CHUNK_CHARS):
        yield text[offset : offset + _TOKENIZER_CHUNK_CHARS]


def _conservative_utf8_estimate(text: str) -> int:
    """Estimate conservatively while accounting for Unicode byte density."""

    utf8_bytes = 0
    control_chars = 0
    for chunk in _text_chunks(text):
        utf8_bytes += len(chunk.encode("utf-8", errors="replace"))
        control_chars += sum(
            ord(char) < 32 or 0x7F <= ord(char) < 0xA0
            for char in chunk
        )
    return max(1, (utf8_bytes + control_chars + 1) // 2)


def estimate_tokens_with_source(text: str) -> tuple[int, TokenEstimateSource]:
    """Return a bounded token estimate and the estimator used."""

    enc = _get_encoding()
    if enc is not None:
        try:
            if len(text) <= _TOKENIZER_CHUNK_CHARS:
                count = len(enc.encode(text, disallowed_special=()))
                return max(1, count), "tiktoken_cl100k_base"
            count = sum(
                len(enc.encode(chunk, disallowed_special=()))
                for chunk in _text_chunks(text)
            )
            return max(1, count), "tiktoken_cl100k_base_chunked"
        except Exception as exc:  # noqa: BLE001
            log.warning("tiktoken_estimate_failed_fallback", error=str(exc))
    return _conservative_utf8_estimate(text), "utf8_unicode_conservative"


def estimate_tokens(text: str) -> int:
    """Estimate token count while keeping the historical integer-only API."""

    return estimate_tokens_with_source(text)[0]
