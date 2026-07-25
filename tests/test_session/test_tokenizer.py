"""Tests for the shared token estimator."""

from __future__ import annotations

import threading
import time

import pytest

from opensquilla.session import tokenizer


class _FakeEncoding:
    """Deterministic stand-in for a cl100k_base encoding."""

    def __init__(self, chars_per_token: int = 2) -> None:
        self._chars_per_token = chars_per_token

    def encode(self, text: str) -> list[int]:
        return [0] * (len(text) // self._chars_per_token)


@pytest.fixture(autouse=True)
def _reset_tokenizer_state(monkeypatch):
    """Keep the module's process-wide load verdict out of other tests."""
    monkeypatch.setattr(tokenizer, "_encoding", None)
    monkeypatch.setattr(tokenizer, "_tiktoken_available", None)
    monkeypatch.delenv(tokenizer._ENCODING_LOAD_TIMEOUT_ENV, raising=False)
    yield


def test_estimate_tokens_uses_the_encoding_when_it_loads(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer, "_load_encoding", lambda: _FakeEncoding(2))

    assert tokenizer.estimate_tokens("abcdefgh") == 4
    assert tokenizer._tiktoken_available is True


def test_estimate_tokens_falls_back_when_tiktoken_is_missing(monkeypatch) -> None:
    def _missing():
        raise ImportError("no tiktoken")

    monkeypatch.setattr(tokenizer, "_load_encoding", _missing)

    assert tokenizer.estimate_tokens("a" * 400) == 100
    assert tokenizer._tiktoken_available is False


def test_estimate_tokens_falls_back_when_the_encoding_fetch_fails(monkeypatch) -> None:
    def _offline():
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(tokenizer, "_load_encoding", _offline)

    assert tokenizer.estimate_tokens("a" * 400) == 100
    assert tokenizer._tiktoken_available is False


def test_estimate_tokens_never_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer, "_load_encoding", lambda: _FakeEncoding(2))

    assert tokenizer.estimate_tokens("") == 1
    assert tokenizer.estimate_tokens("a") == 1


def test_oversized_text_skips_the_encoder_entirely(monkeypatch) -> None:
    def _must_not_load():
        raise AssertionError("encoding must not be loaded for oversized text")

    monkeypatch.setattr(tokenizer, "_load_encoding", _must_not_load)
    text = "a" * (tokenizer._FAST_ESTIMATE_CHAR_LIMIT + 4)

    assert tokenizer.estimate_tokens(text) == len(text) // 4


def test_a_wedged_encoding_load_cannot_block_the_caller(monkeypatch) -> None:
    """A hung BPE fetch must not stall the coroutine that asked for an estimate.

    ``tiktoken.get_encoding`` fetches over HTTPS with no timeout, so on a
    network that drops the connection the load never returns. Since
    ``estimate_tokens`` is called synchronously from gateway coroutines, an
    unbounded wait here freezes the whole event loop.
    """
    release = threading.Event()
    entered = threading.Event()

    def _wedged():
        entered.set()
        release.wait(30)
        return _FakeEncoding(2)

    monkeypatch.setattr(tokenizer, "_load_encoding", _wedged)
    monkeypatch.setenv(tokenizer._ENCODING_LOAD_TIMEOUT_ENV, "0.2")

    started = time.monotonic()
    try:
        estimate = tokenizer.estimate_tokens("a" * 400)
        elapsed = time.monotonic() - started

        assert entered.is_set()
        assert estimate == 100  # len//4 fallback
        assert elapsed < 5.0
        assert tokenizer._tiktoken_available is False
    finally:
        release.set()


def test_a_timed_out_load_is_sticky_and_is_not_retried(monkeypatch) -> None:
    """A late-arriving encoding must not shift estimates mid-process."""
    release = threading.Event()
    calls: list[int] = []

    def _wedged():
        calls.append(1)
        release.wait(30)
        return _FakeEncoding(2)

    monkeypatch.setattr(tokenizer, "_load_encoding", _wedged)
    monkeypatch.setenv(tokenizer._ENCODING_LOAD_TIMEOUT_ENV, "0.2")

    try:
        first = tokenizer.estimate_tokens("a" * 400)
        release.set()
        time.sleep(0.05)  # let the abandoned worker finish
        second = tokenizer.estimate_tokens("a" * 400)

        assert first == second == 100
        assert len(calls) == 1  # the verdict stuck; no second load attempt
    finally:
        release.set()


@pytest.mark.parametrize(
    "raw", ["", "   ", "not-a-number", "0", "-1", "inf", "-inf", "nan"]
)
def test_unusable_timeout_overrides_fall_back_to_the_default(monkeypatch, raw) -> None:
    """``inf`` counts as unusable: ``join(inf)`` is the stall this budget prevents."""
    monkeypatch.setenv(tokenizer._ENCODING_LOAD_TIMEOUT_ENV, raw)

    assert tokenizer._load_timeout_seconds() == tokenizer._ENCODING_LOAD_TIMEOUT_SECONDS


def test_valid_timeout_override_is_honored(monkeypatch) -> None:
    monkeypatch.setenv(tokenizer._ENCODING_LOAD_TIMEOUT_ENV, "1.5")

    assert tokenizer._load_timeout_seconds() == 1.5


def test_concurrent_callers_load_the_encoding_once(monkeypatch) -> None:
    calls: list[int] = []

    def _slow_load():
        calls.append(1)
        time.sleep(0.05)
        return _FakeEncoding(2)

    monkeypatch.setattr(tokenizer, "_load_encoding", _slow_load)
    results: list[int] = []

    def _worker() -> None:
        results.append(tokenizer.estimate_tokens("abcdefgh"))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert results == [4] * 8
    assert len(calls) == 1
