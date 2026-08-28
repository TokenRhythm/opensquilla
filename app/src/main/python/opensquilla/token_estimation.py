"""Bounded token estimation shared across package boundaries."""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Iterator
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ENCODING_UNAVAILABLE = object()
_encoding = None
_TOKENIZER_CHUNK_CHARS = 100_000
_ATTACHMENT_TOKENIZER_CHUNK_CHARS = 16_384
# Encoding chunks independently prevents a BPE token from spanning a cut and
# therefore normally rounds upward. Keep one additional token at every cut as
# an explicit boundary reserve without materially skewing format parity.
_ATTACHMENT_TOKENIZER_CHUNK_BOUNDARY_RESERVE_TOKENS = 1
_ENCODING_LOAD_TIMEOUT_SECONDS = 5.0
_ENCODING_LOAD_TIMEOUT_MAX_SECONDS = min(60.0, threading.TIMEOUT_MAX)
_ENCODING_LOAD_TIMEOUT_ENV = "OPENSQUILLA_TIKTOKEN_LOAD_TIMEOUT_SECONDS"
_load_lock = threading.Lock()

TokenEstimateSource = str


def _is_cjk_token_like(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def estimate_material_text_tokens(text: str) -> int:
    """Estimate routing capacity for provider-visible source material.

    Routing only needs a cheap, format-independent signal, so ASCII text uses
    four characters per token while CJK-like characters count one-for-one.
    Pasted and extracted attachment material share this exact estimator.
    """

    if not text:
        return 0
    if text.isascii():
        return max(1, len(text) // 4)

    ascii_chars = 0
    cjk_chars = 0
    other_non_ascii_chars = 0
    for char in text:
        codepoint = ord(char)
        if codepoint < 128:
            ascii_chars += 1
        elif _is_cjk_token_like(codepoint):
            cjk_chars += 1
        else:
            other_non_ascii_chars += 1
    estimate = (ascii_chars // 4) + cjk_chars + ((other_non_ascii_chars + 1) // 2)
    return max(1, estimate)


def _reset_load_lock_after_fork() -> None:
    """Discard a possibly orphaned loader lock in a forked child."""

    global _load_lock
    _load_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_load_lock_after_fork)


def _load_timeout_seconds() -> float:
    """Return a finite, platform-safe budget for the one-time encoding load."""

    raw = os.environ.get(_ENCODING_LOAD_TIMEOUT_ENV) or ""
    try:
        value = float(raw.strip())
    except ValueError:
        return _ENCODING_LOAD_TIMEOUT_SECONDS
    if (
        not math.isfinite(value)
        or value <= 0
        or value > _ENCODING_LOAD_TIMEOUT_MAX_SECONDS
    ):
        return _ENCODING_LOAD_TIMEOUT_SECONDS
    return value


def _load_encoding():
    """Import tiktoken and resolve cl100k_base. May block on network I/O."""

    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def _get_encoding():
    global _encoding
    if _encoding is _ENCODING_UNAVAILABLE:
        return None
    if _encoding is not None:
        return _encoding
    with _load_lock:
        # Re-check under the lock; a concurrent caller may have settled it.
        if _encoding is _ENCODING_UNAVAILABLE:
            return None
        if _encoding is not None:
            return _encoding

        outcome: dict[str, object] = {}

        def _work() -> None:
            try:
                outcome["encoding"] = _load_encoding()
            except ImportError as exc:
                outcome["import_error"] = exc
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        timeout = _load_timeout_seconds()
        worker = threading.Thread(
            target=_work,
            name="opensquilla-tiktoken-load",
            daemon=True,
        )
        try:
            worker.start()
            worker.join(timeout)
        except Exception as exc:  # noqa: BLE001
            # Thread exhaustion and platform timeout errors must preserve the
            # estimator's historical fallback contract rather than escape into
            # request admission or gateway coroutines.
            _encoding = _ENCODING_UNAVAILABLE
            log.warning("tiktoken_encoding_load_worker_failed", error=str(exc))
            return None

        if worker.is_alive():
            # The daemon may finish later and populate tiktoken's own cache, but
            # this process keeps a stable fallback verdict until restart.
            _encoding = _ENCODING_UNAVAILABLE
            log.warning("tiktoken_encoding_load_timeout", timeout_seconds=timeout)
            return None
        if "encoding" in outcome:
            _encoding = outcome["encoding"]
            return _encoding
        if "import_error" in outcome:
            _encoding = _ENCODING_UNAVAILABLE
            log.info("tiktoken_unavailable_fallback")
            return None
        _encoding = _ENCODING_UNAVAILABLE
        log.warning(
            "tiktoken_encoding_unavailable_fallback",
            error=str(outcome.get("error")),
        )
        return None


def _text_chunks(
    text: str,
    *,
    chunk_chars: int = _TOKENIZER_CHUNK_CHARS,
) -> Iterator[str]:
    for offset in range(0, len(text), chunk_chars):
        yield text[offset : offset + chunk_chars]


def _bounded_tokenizer_count(
    encoding: Any,
    text: str,
    *,
    chunk_chars: int,
    boundary_reserve_tokens: int,
) -> int:
    """Encode long text with bounded work and conservative cut reserves."""

    count = 0
    for chunk_index, chunk in enumerate(
        _text_chunks(text, chunk_chars=chunk_chars)
    ):
        if chunk_index:
            count += boundary_reserve_tokens
        count += len(encoding.encode(chunk, disallowed_special=()))
    return count


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


def _estimate_tokens_with_policy(
    text: str,
    *,
    tokenizer_chunk_chars: int,
    boundary_reserve_tokens: int,
    chunked_source: TokenEstimateSource,
) -> tuple[int, TokenEstimateSource]:
    enc = _get_encoding()
    if enc is not None:
        try:
            if len(text) <= tokenizer_chunk_chars:
                count = len(enc.encode(text, disallowed_special=()))
                return max(1, count), "tiktoken_cl100k_base"
            count = _bounded_tokenizer_count(
                enc,
                text,
                chunk_chars=tokenizer_chunk_chars,
                boundary_reserve_tokens=boundary_reserve_tokens,
            )
            return max(1, count), chunked_source
        except Exception as exc:  # noqa: BLE001
            log.warning("tiktoken_estimate_failed_fallback", error=str(exc))
    return _conservative_utf8_estimate(text), "utf8_unicode_conservative"


def estimate_tokens_with_source(text: str) -> tuple[int, TokenEstimateSource]:
    """Return the legacy shared estimate and its source.

    The 100k chunk contract is intentionally stable for pure text, history,
    and compaction callers. Attachment material uses the separately bounded
    estimator below so its extraction workload cannot change those budgets.
    """

    return _estimate_tokens_with_policy(
        text,
        tokenizer_chunk_chars=_TOKENIZER_CHUNK_CHARS,
        boundary_reserve_tokens=0,
        chunked_source="tiktoken_cl100k_base_chunked",
    )


def estimate_tokens(text: str) -> int:
    """Estimate token count while keeping the historical integer-only API."""

    return estimate_tokens_with_source(text)[0]


def estimate_attachment_text_tokens(text: str) -> int:
    """Conservatively estimate a provider-visible attachment text block.

    Long extracted/replayed file content uses smaller tokenizer chunks to
    avoid platform-sensitive superlinear runs. A one-token reserve per cut
    prevents boundary rounding from lowering admission, while the material
    heuristic preserves the existing cross-format routing floor.
    """

    tokenizer_tokens, _source = _estimate_tokens_with_policy(
        text,
        tokenizer_chunk_chars=_ATTACHMENT_TOKENIZER_CHUNK_CHARS,
        boundary_reserve_tokens=(
            _ATTACHMENT_TOKENIZER_CHUNK_BOUNDARY_RESERVE_TOKENS
        ),
        chunked_source="tiktoken_cl100k_base_attachment_chunked",
    )
    return max(estimate_material_text_tokens(text), tokenizer_tokens)
