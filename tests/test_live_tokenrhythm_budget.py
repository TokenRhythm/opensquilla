from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from scripts.live_tokenrhythm_budget import (
    BillingObservation,
    BudgetLedger,
    BudgetRejectedError,
    BudgetRelay,
    ModelCostBound,
    reviewed_catalog_bounds,
)


def _bound(**overrides) -> ModelCostBound:
    return replace(
        ModelCostBound(
            model="test-model",
            input_limit=1000,
            output_limit=1000,
            input_cny_per_token=Decimal("0.005"),
            output_cny_per_token=Decimal("0.005"),
            valid_until=time.time() + 600,
            catalog_sha256="a" * 64,
        ),
        **overrides,
    )


def _request(**overrides) -> dict:
    return {"model": "test-model", "max_tokens": 100, "messages": [], **overrides}


def _ledger(tmp_path: Path, *, enabled: bool = True) -> BudgetLedger:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", enabled=enabled)
    ledger.select_phase(bucket="probe", variant="baseline", case_id="synthetic-task")
    return ledger


def test_live_is_disabled_by_default_before_transport_dispatch(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite")
    sent: list[httpx.Request] = []
    relay = BudgetRelay(
        ledger,
        {"test-model": _bound()},
        api_key="synthetic-secret",
        transport=httpx.MockTransport(lambda request: sent.append(request)),
    )
    try:
        with pytest.raises(BudgetRejectedError, match="live_requests_disabled"):
            with relay.forward(json.dumps(_request()).encode()):
                pytest.fail("disabled relay yielded an upstream response")
    finally:
        relay.close()
    assert sent == []
    assert ledger.snapshot()["requests"] == []


def test_atomic_reservations_include_all_concurrent_requests(tmp_path) -> None:
    ledger = _ledger(tmp_path)

    def reserve(_index: int) -> str:
        worker = BudgetLedger(ledger.path, enabled=True)
        try:
            return worker.reserve(_bound(), _request())
        except BudgetRejectedError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))
    assert results.count("bucket_budget_exhausted") == 3
    rows = ledger.snapshot()["requests"]
    assert len(rows) == 5
    assert sum(row["reserved_nanos"] for row in rows) == 50_000_000_000


def test_confirmed_charge_releases_only_unused_reservation_across_variants(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    request_id = ledger.reserve(_bound(), _request())
    with pytest.raises(BudgetRejectedError, match="requests_still_in_flight"):
        ledger.select_phase(bucket="probe", variant="new", case_id="synthetic-task")
    ledger.settle(request_id, cost_cny=Decimal("2"), input_tokens=10, output_tokens=20)
    reopened = BudgetLedger(ledger.path, enabled=True)
    reopened.select_phase(bucket="probe", variant="new", case_id="synthetic-task")
    for _ in range(4):
        reopened.reserve(_bound(), _request())
    with pytest.raises(BudgetRejectedError, match="bucket_budget_exhausted"):
        reopened.reserve(_bound(), _request())
    snapshot = reopened.snapshot()
    assert snapshot["total_limit_cny"] == 1000
    assert snapshot["requests"][0]["charged_nanos"] == 2_000_000_000
    assert snapshot["requests"][0]["variant"] == "baseline"
    assert snapshot["requests"][-1]["variant"] == "new"


@pytest.mark.parametrize("cost", [None, Decimal("11")])
def test_unknown_or_excessive_charge_halts_every_bucket(tmp_path, cost) -> None:
    ledger = _ledger(tmp_path)
    request_id = ledger.reserve(_bound(), _request())
    ledger.settle(request_id, cost_cny=cost)
    ledger.select_phase(bucket="extended", variant="new", case_id="next-task")
    with pytest.raises(BudgetRejectedError, match="ledger_halted"):
        ledger.reserve(_bound(), _request())
    row = ledger.snapshot()["requests"][0]
    assert row["reserved_nanos"] == 10_000_000_000
    assert row["status"] == ("unknown" if cost is None else "over_bound")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"model": "test-model"}, "unbounded_output_tokens"),
        (_request(max_tokens=1001), "unbounded_output_tokens"),
        (_request(n=2), "unbounded_multiple_completions"),
        (_request(tools=[{"type": "web_search"}]), "unbounded_hosted_tool_cost"),
        (
            _request(messages=[{"content": [{"type": "image_url", "image_url": "synthetic"}]}]),
            "unbounded_image_cost",
        ),
        (_request(messages=[{"content": [{"type": "input_audio"}]}]), "unbounded_media_cost"),
        (_request(background=True), "unsupported_deferred_request"),
    ],
)
def test_unknown_cost_dimensions_never_reserve(tmp_path, payload, reason) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(BudgetRejectedError, match=reason):
        ledger.reserve(_bound(), payload)
    assert ledger.snapshot()["requests"] == []


@pytest.mark.parametrize("deadline", [0.0, float("nan"), float("inf")])
def test_stale_or_invalid_price_snapshot_is_rejected(tmp_path, deadline) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(BudgetRejectedError, match="price_snapshot_expired"):
        ledger.reserve(_bound(valid_until=deadline), _request())


def test_reviewed_catalog_uses_maximum_prices_and_requires_native_currency() -> None:
    row = {
        "id": "test-model",
        "name": "Synthetic model",
        "type": "chat",
        "status": "online",
        "contextWindow": 1000,
        "maxOutputTokens": 1000,
        "billingMode": "per_1m_tokens",
        "billingUnit": 1000,
        "currency": "CNY",
        "inputPrice": "5",
        "outputPrice": "6",
        "cacheReadPrice": "7",
        "effectiveInputPrice": "1",
        "effectiveOutputPrice": "2",
    }
    bounds = reviewed_catalog_bounds(
        {"code": 0, "data": [row]},
        valid_until=time.time() + 600,
        reviewed_models={"test-model"},
    )
    assert bounds["test-model"].reserve_nanos(_request(), time.time()) == 13_000_000_000
    with pytest.raises(BudgetRejectedError, match="unknown_billing_mode"):
        reviewed_catalog_bounds(
            {"code": 0, "data": [{**row, "currency": "USD"}]},
            valid_until=time.time() + 600,
            reviewed_models={"test-model"},
        )


def test_image_bound_requires_reviewed_token_billing_and_image_modality() -> None:
    row = {
        "id": "synthetic-vision", "type": "chat", "status": "online",
        "modalities": ["text", "image"], "capabilities": {"vision": True},
        "contextWindow": 1000, "maxOutputTokens": 100,
        "currency": "CNY", "billingMode": "per_1m_tokens", "billingUnit": 1000000,
        "inputPrice": "10", "outputPrice": "20", "pricePerImage": None,
    }
    options = {
        "valid_until": time.time() + 600, "reviewed_models": {"synthetic-vision"},
        "image_tokens_bounded_models": frozenset({"synthetic-vision"}),
    }
    bound = reviewed_catalog_bounds({"code": 0, "data": [row]}, **options)["synthetic-vision"]
    request = {
        "model": "synthetic-vision", "max_tokens": 100,
        "messages": [{"content": [{"type": "image_url", "image_url": "synthetic"}]}],
    }
    assert bound.reserve_nanos(request, time.time()) == 12_000_000
    for changed, reason in (
        ({"pricePerImage": "0.1"}, "unbounded_image_price"),
        ({"billingMode": "per_image"}, "unknown_billing_mode"),
        ({"modalities": ["text"], "capabilities": {"vision": False}}, "unverified_image"),
    ):
        with pytest.raises(BudgetRejectedError, match=reason):
            reviewed_catalog_bounds({"code": 0, "data": [{**row, **changed}]}, **options)


def test_relay_reserves_before_real_transport_and_preserves_request_and_response(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    body = json.dumps(_request(messages=[{"role": "user", "content": "private-prompt"}])).encode()
    wire = (
        b'data: {"choices":[{"delta":{"reasoning_content":"private-reasoning"}}]}\n\n'
        b'data: {"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\n'
        b"data: [DONE]\n\n"
        b'data: {"billing_pending":false,"cost_cny":"0.025"}\n\n'
    )

    def transport(request: httpx.Request) -> httpx.Response:
        rows = ledger.snapshot()["requests"]
        assert len(rows) == 1 and rows[0]["status"] == "reserved"
        assert str(request.url) == "https://tokenrhythm.studio/v1/chat/completions"
        assert request.content == body
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    relay = BudgetRelay(
        ledger,
        {"test-model": _bound()},
        api_key="synthetic-secret",
        transport=httpx.MockTransport(transport),
    )
    try:
        with relay.forward(body) as response:
            assert b"".join(response.chunks) == wire
    finally:
        relay.close()
    report = ledger.snapshot()
    row = report["requests"][0]
    assert row["charged_nanos"] == 25_000_000
    assert row["input_tokens"] == 9 and row["output_tokens"] == 4
    assert row["status"] == "confirmed"
    assert row["http_status"] == 200
    assert row["started"] <= row["response_headers_at"] <= row["first_event_at"] <= row["ended"]
    assert all(
        secret not in json.dumps(report)
        for secret in ["private-prompt", "private-reasoning", "synthetic-secret"]
    )


def test_retry_after_incomplete_response_is_blocked_before_second_transport(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    sent = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        raise httpx.ReadError("synthetic disconnect", request=request)

    relay = BudgetRelay(
        ledger,
        {"test-model": _bound()},
        api_key="synthetic-secret",
        transport=httpx.MockTransport(transport),
    )
    try:
        with pytest.raises(httpx.ReadError):
            with relay.forward(json.dumps(_request()).encode()):
                pytest.fail("transport failed before yielding")
        with pytest.raises(BudgetRejectedError, match="ledger_halted"):
            with relay.forward(json.dumps(_request()).encode()):
                pytest.fail("unknown first charge allowed a retry")
    finally:
        relay.close()
    assert sent == 1
    row = ledger.snapshot()["requests"][0]
    assert row["status"] == "unknown"
    assert row["http_status"] is None and row["first_event_at"] is None


def test_billing_observation_handles_split_utf8_and_pending_receipts() -> None:
    observer = BillingObservation(event_stream=True)
    payload = (
        'data: {"choices":[{"delta":{"content":"页面"}}]}\n\n'
        'data: {"billing_pending":true,"cost_cny":"1"}\n\n'
    ).encode()
    for byte in payload:
        observer.feed(bytes([byte]))
    assert observer.finish() is None


def test_response_timing_records_first_upstream_data_event_without_bodies(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    wire = (
        b': keepalive\n\n'
        b'data: {"choices":[{"delta":{"reasoning_content":"private-thinking"}}]}\n\n'
        b'data: {"billing_pending":false,"cost_cny":"0.01"}\n\n'
    )
    relay = BudgetRelay(
        ledger, {"test-model": _bound()}, api_key="synthetic-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=wire
            )
        ),
    )
    try:
        with relay.forward(json.dumps(_request()).encode()) as response:
            row = ledger.snapshot()["requests"][0]
            assert row["http_status"] == 200 and row["response_headers_at"] is not None
            assert row["first_event_at"] is None
            chunks = iter(response.chunks)
            assert next(chunks) == wire
            row = ledger.snapshot()["requests"][0]
            assert row["status"] == "reserved" and row["first_event_at"] is not None
            first_event = row["first_event_at"]
            assert list(chunks) == []
        row = ledger.snapshot()["requests"][0]
        assert row["first_event_at"] == first_event
        assert b"private-thinking" not in ledger.path.read_bytes()
        assert b"synthetic-secret" not in ledger.path.read_bytes()
        observer = BillingObservation(event_stream=True)
        observer.feed(b": keepalive\n\ndata: ")
        assert observer.first_event_at is None
        observer.feed(b'{"choices":[]}\n\n')
        assert observer.first_event_at is not None
    finally:
        relay.close()


def test_response_headers_survive_stream_failure_without_an_event(tmp_path) -> None:
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b": keepalive\n\n"
            raise httpx.ReadError("synthetic interruption")

    ledger = _ledger(tmp_path)
    relay = BudgetRelay(
        ledger, {"test-model": _bound()}, api_key="synthetic-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
            )
        ),
    )
    try:
        with pytest.raises(httpx.ReadError, match="synthetic interruption"):
            with relay.forward(json.dumps(_request()).encode()) as response:
                list(response.chunks)
        row = ledger.snapshot()["requests"][0]
        assert row["http_status"] == 503
        assert row["response_headers_at"] is not None and row["first_event_at"] is None
        assert row["reason"] == "incomplete_http_response"
        assert row["status"] == "unknown" and row["reserved_nanos"] == 10_000_000_000
    finally:
        relay.close()


def test_existing_ledger_receives_idempotent_observation_columns(tmp_path) -> None:
    path = tmp_path / "budget.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests (id TEXT PRIMARY KEY, bucket TEXT, variant TEXT, "
            "case_id TEXT, model TEXT, started REAL, ended REAL, reserved_nanos INTEGER, "
            "charged_nanos INTEGER, status TEXT, reason TEXT, catalog_sha256 TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER)"
        )
        db.execute(
            "INSERT INTO requests VALUES ('legacy-request', 'probe', 'baseline', 'synthetic', "
            "'test-model', 1, 2, 10000000000, 4000000000, 'confirmed', NULL, ?, 10, 20)",
            ("a" * 64,),
        )
    for _ in range(2):
        ledger = BudgetLedger(path, enabled=True)
        row = ledger.snapshot()["requests"][0]
        assert row["charged_nanos"] == 4_000_000_000
        assert row["first_event_at"] is None and row["http_status"] is None
        assert row["response_headers_at"] is None
    ledger.select_phase(bucket="probe", variant="new", case_id="synthetic-upgrade")
    request_id = ledger.reserve(_bound(), _request())
    ledger.observe_response(request_id, http_status=200, first_event_at=10)
    ledger.observe_response(request_id, http_status=503, first_event_at=20)
    row = ledger.snapshot()["requests"][-1]
    assert row["first_event_at"] == 10 and row["http_status"] == 200
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(requests)")]
    assert all(columns.count(name) == 1 for name in (
        "http_status", "response_headers_at", "first_event_at"
    ))
