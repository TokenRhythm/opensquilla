"""Bounded token estimation shared across package boundaries."""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Iterator

import structlog

log = structlog.get_logger(__name__)

_ENCODING_UNAVAILABLE = object()
_encoding = None
_TOKENIZER_CHUNK_CHARS = 100_000
_ENCODING_LOAD_TIMEOUT_SECONDS = 5.0
_ENCODING_LOAD_TIMEOUT_MAX_SECONDS = min(60.0, threading.TIMEOUT_MAX)
_ENCODING_LOAD_TIMEOUT_ENV = "OPENSQUILLA_TIKTOKEN_LOAD_TIMEOUT_SECONDS"
_load_lock = threading.Lock()

TokenEstimateSource = str


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
