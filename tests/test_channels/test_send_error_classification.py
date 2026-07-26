"""The single producer of the channel error taxonomy, and the retry loop.

Every adapter declares the taxonomy verbatim and doctor/console consumers
branch on it, so a misclassification is worse than no classification: it
sends a confident wrong answer to code that acts on it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from opensquilla.channels._util import MAX_RETRY_AFTER_S, retry_request
from opensquilla.channels.contract import (
    ALL_ERROR_CLASSES,
    REQUIRED_FATAL_ERROR_CLASSES,
    REQUIRED_RETRYABLE_ERROR_CLASSES,
    UNCLASSIFIED_ERROR_CLASS,
    classify_channel_send_error,
    classify_send_error_status,
)


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("POST", "https://provider.example/send"),
    )


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    response = _response(status)
    return httpx.HTTPStatusError("boom", request=response.request, response=response)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_unclassified_is_outside_both_taxonomy_halves() -> None:
    # The whole point of the sentinel: retrying a permanent failure forever and
    # discarding a recoverable one are both worse than parking for a human.
    assert UNCLASSIFIED_ERROR_CLASS not in REQUIRED_RETRYABLE_ERROR_CLASSES
    assert UNCLASSIFIED_ERROR_CLASS not in REQUIRED_FATAL_ERROR_CLASSES
    assert UNCLASSIFIED_ERROR_CLASS not in ALL_ERROR_CLASSES


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "payload_rejected"),
        (401, "auth_invalid"),
        (403, "auth_invalid"),
        (404, "target_missing"),
        (410, "target_missing"),
        (413, "payload_rejected"),
        (422, "payload_rejected"),
        (429, "rate_limited"),
        (500, "transport_transient"),
        (502, "transport_transient"),
        (503, "transport_transient"),
        (504, "transport_transient"),
    ],
)
def test_http_status_maps_onto_the_taxonomy(status: int, expected: str) -> None:
    assert classify_send_error_status(status) == expected
    assert classify_channel_send_error(_http_status_error(status)) == expected
    assert expected in ALL_ERROR_CLASSES


@pytest.mark.parametrize("status", [None, 200, 302, 418])
def test_unmapped_status_is_never_guessed(status: int | None) -> None:
    assert classify_send_error_status(status) == UNCLASSIFIED_ERROR_CLASS


def test_transport_exceptions_are_transient() -> None:
    for exc in (
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("garbled"),
        TimeoutError(),
        ConnectionError(),
    ):
        assert classify_channel_send_error(exc) == "transport_transient"


def test_adapter_declared_class_wins_over_generic_rules() -> None:
    # An adapter knows its provider's semantics better than any status map:
    # a 403 that really means "message rejected" must stay the adapter's call.
    class DeclaredError(Exception):
        error_class = "payload_rejected"
        response = _response(403)

    assert classify_channel_send_error(DeclaredError()) == "payload_rejected"


def test_declared_class_outside_the_taxonomy_is_not_trusted() -> None:
    class RogueError(Exception):
        error_class = "totally_made_up"

    assert classify_channel_send_error(RogueError()) == UNCLASSIFIED_ERROR_CLASS


def test_retry_after_hint_means_rate_limited() -> None:
    class FloodWaitError(RuntimeError):
        retry_after = 30

    assert classify_channel_send_error(FloodWaitError()) == "rate_limited"


def test_business_code_is_not_read_as_an_http_status() -> None:
    # The trap: several providers number their own business errors in `code`.
    # Reading 99991663 as an HTTP status would misclassify confidently, and a
    # provider code that happens to equal 404 would invent target_missing.
    class BusinessError(Exception):
        def __init__(self, code: int) -> None:
            self.code = code
            super().__init__("api said no")

    assert classify_channel_send_error(BusinessError(99991663)) == UNCLASSIFIED_ERROR_CLASS
    assert classify_channel_send_error(BusinessError(404)) == UNCLASSIFIED_ERROR_CLASS


def test_unrecognized_error_is_unclassified() -> None:
    assert classify_channel_send_error(ValueError("?")) == UNCLASSIFIED_ERROR_CLASS


# ---------------------------------------------------------------------------
# retry_request
# ---------------------------------------------------------------------------


@pytest.fixture
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return slept


async def test_exhausted_429_returns_the_response_instead_of_destroying_it(
    no_real_sleep: list[float],
) -> None:
    # Regression: the 429 branch lacked the `attempt < max_retries` guard the
    # 5xx branch has, so it slept past the final attempt and fell out of the
    # loop with last_exc unset — raising RuntimeError and discarding the
    # provider's actual answer, which the caller needs to classify.
    async def always_rate_limited() -> httpx.Response:
        return _response(429, {"Retry-After": "5"})

    resp = await retry_request(always_rate_limited, max_retries=2)

    assert resp.status_code == 429
    assert classify_channel_send_error(_http_status_error(resp.status_code)) == "rate_limited"
    # Three attempts, but the last one must not sleep before giving up.
    assert no_real_sleep == [5.0, 5.0]


async def test_hostile_retry_after_cannot_park_the_channel_for_hours(
    no_real_sleep: list[float],
) -> None:
    async def long_park() -> httpx.Response:
        return _response(429, {"Retry-After": "3600"})

    await retry_request(long_park, max_retries=1)

    assert no_real_sleep == [MAX_RETRY_AFTER_S]


async def test_http_date_retry_after_does_not_raise(no_real_sleep: list[float]) -> None:
    # `float("Wed, 21 Oct ...")` raised ValueError straight out of the retry
    # loop; RFC 9110 allows the HTTP-date form.
    async def dated() -> httpx.Response:
        return _response(429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})

    resp = await retry_request(dated, max_retries=1)

    assert resp.status_code == 429
    assert no_real_sleep == [MAX_RETRY_AFTER_S]


async def test_unparseable_retry_after_falls_back_to_backoff(
    no_real_sleep: list[float],
) -> None:
    async def junk() -> httpx.Response:
        return _response(429, {"Retry-After": "soon"})

    await retry_request(junk, max_retries=1, base_delay=2.0)

    assert no_real_sleep == [2.0]


async def test_exhausted_5xx_still_returns_the_response(no_real_sleep: list[float]) -> None:
    async def always_503() -> httpx.Response:
        return _response(503)

    resp = await retry_request(always_503, max_retries=1)

    assert resp.status_code == 503


async def test_success_after_a_retry_returns_the_success(no_real_sleep: list[float]) -> None:
    calls = 0

    async def flaky() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(200) if calls > 1 else _response(429, {"Retry-After": "1"})

    resp = await retry_request(flaky, max_retries=3)

    assert resp.status_code == 200
    assert calls == 2
