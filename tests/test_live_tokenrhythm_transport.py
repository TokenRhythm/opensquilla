from __future__ import annotations

import json
import time
from decimal import Decimal

import httpx
import pytest

from scripts import live_tokenrhythm_transport as transport
from scripts.live_tokenrhythm_budget import BudgetLedger, BudgetRelay, ModelCostBound

_KEY = "live-budget-placeholder-" + "a" * 32
_RELAY = "http://127.0.0.1:18799/v1"


def _env() -> dict[str, str]:
    return {
        "OPENSQUILLA_LIVE_TRANSPORT": "1",
        "OPENSQUILLA_LIVE_RELAY_URL": _RELAY,
        "OPENSQUILLA_LIVE_RELAY_CLIENT_KEY": _KEY,
    }


@pytest.fixture
def clean_proxy_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_official_request_projects_only_transport_and_preserves_payload_timeout() -> None:
    body = b'{"model":"synthetic-model","max_tokens":42,"messages":[]}'
    request = httpx.Request(
        "POST",
        "https://tokenrhythm.studio/v1/chat/completions",
        headers={"Authorization": "Bearer " + _KEY, "X-Request-ID": "synthetic-request"},
        content=body,
        extensions={"timeout": {"connect": 1, "read": 123, "write": 2, "pool": 3}},
    )
    projected = transport.RelayTarget(_RELAY, _KEY).project(request)
    assert str(request.url) == "https://tokenrhythm.studio/v1/chat/completions"
    assert str(projected.url) == _RELAY + "/chat/completions"
    assert projected.read() == body
    assert projected.extensions == request.extensions
    assert projected.headers["X-Request-ID"] == "synthetic-request"
    assert projected.headers["Authorization"] == "Bearer " + _KEY
    assert projected.headers["Host"] == "127.0.0.1:18799"


@pytest.mark.parametrize(
    ("method", "url", "headers", "reason"),
    [
        ("POST", "https://other-provider.example/v1/chat/completions", {}, "unbudgeted"),
        (
            "GET", "https://other-provider.example/models",
            {"Authorization": "Bearer x"}, "unbudgeted",
        ),
        ("GET", "https://example.com/", {"X-Test": _KEY}, "placeholder"),
        ("GET", "https://example.com/?key=" + _KEY, {}, "placeholder"),
        ("POST", "https://tokenrhythm.studio/v1/responses", {}, "unsupported_provider"),
        ("POST", "https://tokenrhythm.studio/v1/chat/completions", {}, "unexpected_provider"),
        ("GET", "https://tokenrhythm.studio/api/models", {"Authorization": _KEY}, "placeholder"),
        ("GET", "https://tokenrhythm.studio:444/api/models", {}, "unsupported_provider"),
    ],
)
def test_unreviewed_paid_endpoints_and_placeholder_leaks_fail_before_network(
    method, url, headers, reason
) -> None:
    called = []
    guarded = transport.BudgetedTransport(
        transport.RelayTarget(_RELAY, _KEY),
        httpx.MockTransport(lambda request: called.append(request)),
    )
    with pytest.raises(transport.TransportRejectedError, match=reason):
        guarded.handle_request(httpx.Request(method, url, headers=headers))
    assert called == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://tokenrhythm.studio/api/models"),
        ("GET", "https://example.com/public-page"),
        ("POST", "http://127.0.0.1:18791/browser"),
        ("POST", "http://[::1]:18791/browser"),
    ],
)
def test_public_metadata_pages_and_local_browser_transport_remain_available(method, url) -> None:
    request = httpx.Request(method, url)
    assert transport.RelayTarget(_RELAY, _KEY).project(request) is request


async def test_standard_client_mounts_cover_async_sync_and_keep_response_request_official(
    monkeypatch, clean_proxy_environment
) -> None:
    seen: list[httpx.Request] = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [], "billing_pending": False, "cost_cny": "0"})

    async_type = transport.BudgetedAsyncTransport
    sync_type = transport.BudgetedTransport
    monkeypatch.setattr(
        transport, "BudgetedAsyncTransport",
        lambda target: async_type(target, httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(
        transport, "BudgetedTransport",
        lambda target: sync_type(target, httpx.MockTransport(respond)),
    )
    restore = transport.install_from_env({**_env(), "TOKENRHYTHM_API_KEY": _KEY})
    official = "https://tokenrhythm.studio/v1/chat/completions"
    payload = {"model": "synthetic-model", "max_tokens": 10, "messages": []}
    try:
        async with httpx.AsyncClient(timeout=37, trust_env=True, proxy=None) as client:
            response = await client.post(
                official, json=payload, headers={"Authorization": "Bearer " + _KEY}
            )
            assert str(response.request.url) == official
        with httpx.Client(timeout=41) as client:
            response = client.post(
                official, json=payload, headers={"Authorization": "Bearer " + _KEY}
            )
            assert str(response.request.url) == official
        with pytest.raises(transport.TransportRejectedError, match="custom_http_transport"):
            httpx.AsyncClient(transport=httpx.MockTransport(respond))
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
        with pytest.raises(transport.TransportRejectedError, match="proxy_environment"):
            httpx.Client()
    finally:
        restore()
    assert len(seen) == 2
    assert [request.extensions["timeout"]["read"] for request in seen] == [37, 41]
    assert all(json.loads(request.content) == payload for request in seen)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"OPENSQUILLA_LIVE_TRANSPORT": "0"}, "disabled"),
        ({"TOKENRHYTHM_API_KEY": "synthetic-secret"}, "must_stay_in_relay"),
        ({"HTTPS_PROXY": "http://example.com"}, "proxy_environment"),
        ({"OPENSQUILLA_LIVE_RELAY_URL": "https://example.com/v1"}, "invalid_loopback"),
        ({"OPENSQUILLA_LIVE_RELAY_CLIENT_KEY": "synthetic-secret"}, "invalid_placeholder"),
    ],
)
def test_explicit_isolated_installation_is_required(changes, reason) -> None:
    with pytest.raises(transport.TransportRejectedError, match=reason):
        transport.install_from_env({**_env(), **changes})


async def test_loopback_relay_preserves_real_http_bytes_and_accounts_every_request(
    tmp_path,
) -> None:
    ledger = BudgetLedger(tmp_path / "ledger.sqlite", enabled=True)
    ledger.select_phase(bucket="probe", variant="baseline", case_id="synthetic-case")
    bound = ModelCostBound(
        model="synthetic-model", input_limit=1000, output_limit=1000,
        input_cny_per_token=Decimal("0.001"), output_cny_per_token=Decimal("0.001"),
        valid_until=time.time() + 60, catalog_sha256="a" * 64,
    )
    received = []
    wire_response = (
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}\n\n'
        b'data: [DONE]\n\n'
        b'data: {"billing_pending":false,"cost_cny":"0.03"}\n\n'
    )

    def upstream(request):
        received.append(request)
        return httpx.Response(
            200, content=wire_response,
            headers={"Content-Type": "text/event-stream", "X-Request-ID": "synthetic-response"},
        )

    relay = BudgetRelay(
        ledger, {bound.model: bound}, api_key="synthetic-upstream-only",
        transport=httpx.MockTransport(upstream),
    )
    try:
        target = transport.RelayTarget(relay.start(), relay.client_key)
        async with httpx.AsyncClient(
            mounts={"all://": transport.BudgetedAsyncTransport(target)},
            trust_env=False, timeout=10,
        ) as client:
            body = b'{"model":"synthetic-model","max_tokens":100,"messages":[],"stream":true}'
            response = await client.post(
                "https://tokenrhythm.studio/v1/chat/completions", content=body,
                headers={
                    "Authorization": "Bearer " + relay.client_key,
                    "X-Request-ID": "synthetic-correlation",
                },
            )
        assert response.status_code == 200
        assert response.content == wire_response
        assert response.headers["X-Request-ID"] == "synthetic-response"
        assert str(response.request.url) == "https://tokenrhythm.studio/v1/chat/completions"
        assert len(received) == 1
        assert received[0].content == body
        assert received[0].headers["Authorization"] == "Bearer synthetic-upstream-only"
        assert received[0].headers["X-Request-ID"] == "synthetic-correlation"
        assert received[0].headers["Host"] == "tokenrhythm.studio"
        rows = ledger.snapshot()["requests"]
        assert len(rows) == 1
        assert rows[0]["charged_nanos"] == 30_000_000
        assert rows[0]["input_tokens"] == 10
        assert rows[0]["output_tokens"] == 20
        assert "synthetic-upstream-only" not in json.dumps(ledger.snapshot())
    finally:
        relay.close()
