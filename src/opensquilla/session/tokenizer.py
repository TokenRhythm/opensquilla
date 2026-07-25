"""Token estimation — tiktoken when available, len//4 fallback."""

from __future__ import annotations

import math
import os
import threading

import structlog

log = structlog.get_logger(__name__)

_encoding = None
_tiktoken_available: bool | None = None
_FAST_ESTIMATE_CHAR_LIMIT = 100_000

# Bound the one-time cl100k_base load. ``tiktoken.get_encoding`` fetches the
# BPE table through ``requests.get(url)`` with no timeout, and
# :func:`estimate_tokens` is called synchronously from gateway coroutines
# (``apply_context_overflow_policy`` among them). On a network that drops the
# connection instead of refusing it, an unbounded fetch therefore stalls the
# event loop for every session in the process, not just the calling turn.
# Pre-seed ``TIKTOKEN_CACHE_DIR`` to keep the load local and instant.
_ENCODING_LOAD_TIMEOUT_SECONDS = 5.0
_ENCODING_LOAD_TIMEOUT_ENV = "OPENSQUILLA_TIKTOKEN_LOAD_TIMEOUT_SECONDS"
_load_lock = threading.Lock()


def _load_timeout_seconds() -> float:
    """Encoding-load budget in seconds; unusable values reset to the default.

    ``inf`` is rejected along with the non-positive and unparseable values:
    ``Thread.join(inf)`` waits forever, which is the exact stall this budget
    exists to prevent, so the budget must not be able to switch it back on.
    """
    raw = os.environ.get(_ENCODING_LOAD_TIMEOUT_ENV) or ""
    try:
        value = float(raw.strip())
    except ValueError:
        return _ENCODING_LOAD_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _ENCODING_LOAD_TIMEOUT_SECONDS
    return value


def _load_encoding():
    """Import tiktoken and resolve cl100k_base. May block on network I/O."""
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def _get_encoding():
    global _encoding, _tiktoken_available
    if _tiktoken_available is False:
        return None
    if _encoding is not None:
        return _encoding
    with _load_lock:
        # Re-check under the lock; a concurrent caller may have settled it.
        if _tiktoken_available is False:
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
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            # Abandon rather than join: the thread is a daemon, so a wedged
            # fetch blocks neither this caller nor interpreter exit. The
            # verdict is sticky so a late arrival cannot shift estimates
            # mid-process and make budget math irreproducible.
            _tiktoken_available = False
            log.warning("tiktoken_encoding_load_timeout", timeout_seconds=timeout)
            return None
        if "encoding" in outcome:
            _encoding = outcome["encoding"]
            _tiktoken_available = True
            return _encoding
        if "import_error" in outcome:
            _tiktoken_available = False
            log.info("tiktoken_unavailable_fallback")
            return None
        _tiktoken_available = False
        log.warning("tiktoken_encoding_unavailable_fallback", error=str(outcome.get("error")))
        return None


def estimate_tokens(text: str) -> int:
    """Estimate token count. Uses tiktoken cl100k_base if available, else len//4."""
    if len(text) > _FAST_ESTIMATE_CHAR_LIMIT:
        return max(1, len(text) // 4)
    enc = _get_encoding()
    if enc is not None:
        return max(1, len(enc.encode(text)))
    return max(1, len(text) // 4)
