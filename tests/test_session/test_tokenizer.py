"""Tests for the shared bounded token estimator."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from opensquilla import token_estimation
from opensquilla.session import tokenizer
from opensquilla.session.compaction import estimate_entry_model_replay_tokens


class _SizedTokens:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class _FakeEncoding:
    def __init__(self, chars_per_token: int = 2) -> None:
        self._chars_per_token = chars_per_token

    def encode(
        self,
        text: str,
        *,
        disallowed_special: tuple[Any, ...] = (),
    ) -> _SizedTokens:
        del disallowed_special
        return _SizedTokens(len(text) // self._chars_per_token)


class _RecordingEncoding:
    def __init__(self, chars_per_token: int = 10) -> None:
        self._chars_per_token = chars_per_token
        self.calls: list[tuple[int, tuple[Any, ...]]] = []

    def encode(
        self,
        text: str,
        *,
        disallowed_special: tuple[Any, ...],
    ) -> _SizedTokens:
        self.calls.append((len(text), disallowed_special))
        return _SizedTokens(
            (len(text) + self._chars_per_token - 1) // self._chars_per_token
        )


@pytest.fixture(autouse=True)
def _reset_tokenizer_state(monkeypatch: pytest.MonkeyPatch):
    """Keep the process-wide loader verdict isolated between tests."""

    monkeypatch.setattr(token_estimation, "_encoding", None)
    monkeypatch.setattr(token_estimation, "_load_lock", threading.Lock())
    monkeypatch.delenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, raising=False)
    yield


def test_estimate_tokens_uses_the_encoding_when_it_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(token_estimation, "_load_encoding", lambda: _FakeEncoding(2))

    assert tokenizer.estimate_tokens("abcdefgh") == 4
    assert isinstance(token_estimation._encoding, _FakeEncoding)


def test_estimate_tokens_falls_back_when_tiktoken_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing():
        raise ImportError("no tiktoken")

    monkeypatch.setattr(token_estimation, "_load_encoding", _missing)

    assert tokenizer.estimate_tokens_with_source("a" * 400) == (
        200,
        "utf8_unicode_conservative",
    )
    assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE


def test_estimate_tokens_falls_back_when_the_encoding_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _offline():
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(token_estimation, "_load_encoding", _offline)

    assert tokenizer.estimate_tokens_with_source("a" * 400) == (
        200,
        "utf8_unicode_conservative",
    )
    assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE


def test_estimate_tokens_never_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_estimation, "_load_encoding", lambda: _FakeEncoding(2))

    assert tokenizer.estimate_tokens("") == 1
    assert tokenizer.estimate_tokens("a") == 1


def test_large_text_keeps_legacy_tokenizer_chunk_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _RecordingEncoding()
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    count, source = tokenizer.estimate_tokens_with_source("x" * 250_001)

    assert [size for size, _ in encoding.calls] == [100_000, 100_000, 50_001]
    assert all(disallowed_special == () for _, disallowed_special in encoding.calls)
    assert count == 10_000 + 10_000 + 5_001
    assert source == "tiktoken_cl100k_base_chunked"


@pytest.mark.parametrize(
    "text",
    [
        "x" * 200_000,
        "模型" * 100_000,
        "🦑é" * 100_000,
    ],
    ids=["long-repeated-ascii", "long-repeated-cjk", "long-repeated-unicode"],
)
def test_attachment_long_repeated_text_uses_bounded_tokenizer_chunks(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    encoding = _RecordingEncoding(chars_per_token=1)
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    count = token_estimation.estimate_attachment_text_tokens(text)

    expected_chunk_sizes = [
        min(
            token_estimation._ATTACHMENT_TOKENIZER_CHUNK_CHARS,
            len(text) - offset,
        )
        for offset in range(
            0,
            len(text),
            token_estimation._ATTACHMENT_TOKENIZER_CHUNK_CHARS,
        )
    ]
    assert [size for size, _ in encoding.calls] == expected_chunk_sizes
    assert max(expected_chunk_sizes) == 16_384
    assert all(disallowed_special == () for _, disallowed_special in encoding.calls)
    raw_chunk_tokens = sum(expected_chunk_sizes)
    expected_boundary_reserve = (
        (len(expected_chunk_sizes) - 1)
        * token_estimation._ATTACHMENT_TOKENIZER_CHUNK_BOUNDARY_RESERVE_TOKENS
    )
    assert count == raw_chunk_tokens + expected_boundary_reserve


def test_attachment_chunk_boundary_does_not_change_legacy_estimator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _RecordingEncoding(chars_per_token=1)
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    attachment_at_boundary = token_estimation.estimate_attachment_text_tokens(
        "x" * 16_384
    )
    attachment_after_boundary = token_estimation.estimate_attachment_text_tokens(
        "x" * 16_385
    )
    assert attachment_at_boundary == 16_384
    assert attachment_after_boundary == 16_386
    assert [size for size, _ in encoding.calls] == [16_384, 16_384, 1]

    encoding.calls.clear()
    legacy_at_boundary = tokenizer.estimate_tokens("x" * 16_384)
    legacy_after_boundary = tokenizer.estimate_tokens("x" * 16_385)
    assert legacy_at_boundary == 16_384
    assert legacy_after_boundary == 16_385
    assert [size for size, _ in encoding.calls] == [16_384, 16_385]


@pytest.mark.parametrize(
    "text",
    [
        "z" * 20_000,
        "模型压缩容量边界" * 2_500,
        "🦑éΩ模型a\u0301" * 2_500,
    ],
    ids=["repeated-ascii", "repeated-cjk", "repeated-unicode"],
)
def test_attachment_chunked_estimate_is_not_below_monolithic_encoding(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("cl100k_base")
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    monolithic = len(encoding.encode(text, disallowed_special=()))
    estimated = token_estimation.estimate_attachment_text_tokens(text)

    assert estimated >= monolithic


def test_concurrent_long_estimates_are_bounded_and_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _RecordingEncoding(chars_per_token=1)
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)
    texts = [
        "x" * 200_000,
        "z" * 200_000,
        "模型" * 100_000,
        "🦑é" * 100_000,
    ]

    with ThreadPoolExecutor(max_workers=len(texts)) as executor:
        first = list(
            executor.map(token_estimation.estimate_attachment_text_tokens, texts)
        )
        second = list(
            executor.map(token_estimation.estimate_attachment_text_tokens, texts)
        )

    assert first == second
    assert encoding.calls
    assert max(size for size, _ in encoding.calls) <= 16_384


def test_history_compaction_keeps_legacy_estimator_at_attachment_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoding = _RecordingEncoding(chars_per_token=1)
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: encoding)

    estimated = estimate_entry_model_replay_tokens(
        {"role": "user", "content": "x" * 16_385}
    )

    assert estimated == 16_385
    assert [size for size, _ in encoding.calls] == [16_385]


def test_unicode_fallback_uses_utf8_density_instead_of_chars_div_four(
    monkeypatch: pytest.MonkeyPatch,
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


def test_integer_only_api_remains_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tokenizer,
        "estimate_tokens_with_source",
        lambda _text: (37, "synthetic"),
    )

    assert tokenizer.estimate_tokens("payload") == 37


def test_a_wedged_encoding_load_returns_within_the_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    entered = threading.Event()
    completed = threading.Event()

    def _wedged():
        entered.set()
        release.wait(30)
        completed.set()
        return _FakeEncoding(2)

    monkeypatch.setattr(token_estimation, "_load_encoding", _wedged)
    monkeypatch.setenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, "0.05")

    started = time.monotonic()
    try:
        estimate = tokenizer.estimate_tokens_with_source("a" * 400)
        elapsed = time.monotonic() - started

        assert entered.is_set()
        assert estimate == (200, "utf8_unicode_conservative")
        assert elapsed < 0.5
        assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE
    finally:
        release.set()
        assert completed.wait(1)


def test_a_timed_out_load_is_sticky_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    completed = threading.Event()
    calls: list[int] = []

    def _wedged():
        calls.append(1)
        release.wait(30)
        completed.set()
        return _FakeEncoding(2)

    monkeypatch.setattr(token_estimation, "_load_encoding", _wedged)
    monkeypatch.setenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, "0.05")

    try:
        first = tokenizer.estimate_tokens_with_source("a" * 400)
        release.set()
        assert completed.wait(1)
        second = tokenizer.estimate_tokens_with_source("a" * 400)

        assert first == second == (200, "utf8_unicode_conservative")
        assert len(calls) == 1
        assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE
    finally:
        release.set()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-number",
        "0",
        "-1",
        "inf",
        "-inf",
        "nan",
        str(token_estimation._ENCODING_LOAD_TIMEOUT_MAX_SECONDS + 1),
    ],
)
def test_unusable_timeout_overrides_fall_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, raw)

    assert (
        token_estimation._load_timeout_seconds()
        == token_estimation._ENCODING_LOAD_TIMEOUT_SECONDS
    )


def test_valid_timeout_override_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, "1.5")

    assert token_estimation._load_timeout_seconds() == 1.5


def test_concurrent_callers_load_the_encoding_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _slow_load():
        calls.append(1)
        time.sleep(0.05)
        return _FakeEncoding(2)

    monkeypatch.setattr(token_estimation, "_load_encoding", _slow_load)
    results: list[int] = []

    def _worker() -> None:
        results.append(tokenizer.estimate_tokens("abcdefgh"))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert results == [4] * 8
    assert len(calls) == 1


def test_concurrent_callers_share_one_timed_out_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    completed = threading.Event()
    start = threading.Event()
    calls: list[int] = []
    results: list[tuple[int, str]] = []

    def _wedged():
        calls.append(1)
        release.wait(30)
        completed.set()
        return _FakeEncoding(2)

    def _caller() -> None:
        start.wait()
        results.append(tokenizer.estimate_tokens_with_source("a" * 400))

    monkeypatch.setattr(token_estimation, "_load_encoding", _wedged)
    monkeypatch.setenv(token_estimation._ENCODING_LOAD_TIMEOUT_ENV, "0.05")
    threads = [threading.Thread(target=_caller) for _ in range(8)]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    try:
        start.set()
        for thread in threads:
            thread.join(2)
        elapsed = time.monotonic() - started

        assert all(not thread.is_alive() for thread in threads)
        assert results == [(200, "utf8_unicode_conservative")] * 8
        assert len(calls) == 1
        assert elapsed < 0.5
    finally:
        release.set()
        assert completed.wait(1)


def test_thread_start_failure_is_sticky_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[int] = []

    class _UnstartableThread:
        def __init__(self, **_kwargs: Any) -> None:
            constructed.append(1)

        def start(self) -> None:
            raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(token_estimation.threading, "Thread", _UnstartableThread)

    first = tokenizer.estimate_tokens_with_source("a" * 400)
    second = tokenizer.estimate_tokens_with_source("a" * 400)

    assert first == second == (200, "utf8_unicode_conservative")
    assert constructed == [1]
    assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE


def test_join_failure_is_sticky_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _UnjoinableThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, _timeout: float) -> None:
            raise OverflowError("timeout too large")

    monkeypatch.setattr(token_estimation.threading, "Thread", _UnjoinableThread)

    assert tokenizer.estimate_tokens_with_source("a" * 400) == (
        200,
        "utf8_unicode_conservative",
    )
    assert token_estimation._encoding is token_estimation._ENCODING_UNAVAILABLE


def test_fork_child_resets_a_possibly_orphaned_loader_lock() -> None:
    inherited_lock = token_estimation._load_lock
    inherited_lock.acquire()
    try:
        token_estimation._reset_load_lock_after_fork()
        assert token_estimation._load_lock is not inherited_lock
        assert token_estimation._load_lock.acquire(blocking=False)
        token_estimation._load_lock.release()
    finally:
        inherited_lock.release()
