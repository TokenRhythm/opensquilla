"""Native structured decision requests never use chat completion semantics."""

from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.provider.jev_decision import JEV_MODEL, NativeJevClient, is_jev_model
from opensquilla.provider.selector import ProviderConfig


@pytest.mark.parametrize(
    "value",
    [
        JEV_MODEL,
        "neohorse-jev",
        "NeoHorse-JEV-4B",
        "neohorse-4b-jev",
        "TokenRhythm/NeoHorse-Jev-4B",
    ],
)
def test_canonical_and_prior_jev_aliases(value):
    assert is_jev_model(value)
    assert not is_jev_model("synthetic-chat")


def client(**changes):
    return NativeJevClient(
        ProviderConfig(
            provider="tokenrhythm",
            model=JEV_MODEL,
            api_key="synthetic-test-key",
            **changes,
        )
    )


def install_http(monkeypatch, handler):
    captures = []

    def create(api_key, **options):
        captures.append((api_key, options.copy()))
        options.pop("proxy", None)
        options["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**options)

    monkeypatch.setattr("opensquilla.desktop_account.account_http_client", create)
    return captures


@pytest.mark.parametrize(
    ("base_url", "url"),
    [
        ("", "https://tokenrhythm.studio/v1/decision"),
        ("https://gateway.example/v1/", "https://gateway.example/v1/decision"),
        ("https://gateway.example", "https://gateway.example/v1/decision"),
        ("http://127.0.0.1:54321/v1", "http://127.0.0.1:54321/v1/decision"),
    ],
)
async def test_request_uses_native_json_endpoint_and_resolved_proxy(monkeypatch, base_url, url):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"model": JEV_MODEL, "answers": {}})

    captures = install_http(monkeypatch, respond)
    payload = {"model": JEV_MODEL, "state": "Synthetic task", "questions": {}}
    result = await client(base_url=base_url, proxy="http://127.0.0.1:3128").decide(
        payload,
        timeout_seconds=0.5,
    )
    assert result["model"] == JEV_MODEL
    assert len(requests) == 1 and str(requests[0].url) == url
    assert requests[0].headers["authorization"] == "Bearer synthetic-test-key"
    assert requests[0].headers["accept"] == "application/json"
    assert json.loads(requests[0].content) == payload
    assert captures[0][1]["follow_redirects"] is False
    assert captures[0][1]["proxy"] == "http://127.0.0.1:3128"


@pytest.mark.parametrize("status", [301, 401, 403, 422, 429, 502, 503, 504])
async def test_error_response_is_never_retried_or_redirected(monkeypatch, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"code": "SYNTHETIC_FAILURE"},
            headers={"location": "https://untrusted.example/decision", "retry-after": "1"},
        )

    install_http(monkeypatch, respond)
    with pytest.raises(httpx.HTTPStatusError):
        await client().decide({"model": JEV_MODEL}, timeout_seconds=0.5)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"[]", b"null", b"broken", b"x" * (64 * 1024 + 1)])
async def test_malformed_or_unbounded_body_is_rejected(monkeypatch, body):
    install_http(monkeypatch, lambda _: httpx.Response(200, content=body))
    with pytest.raises(ValueError):
        await client().decide({"model": JEV_MODEL}, timeout_seconds=0.5)


async def test_request_body_is_bounded_before_network(monkeypatch):
    install_http(monkeypatch, lambda _: pytest.fail("must not send"))
    with pytest.raises(ValueError, match="request_too_large"):
        await client().decide({"state": "x" * (1024 * 1024)}, timeout_seconds=0.5)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://key@example.test/v1",
        "https://example.test/v1?target=x",
        "https://example.test/v1#key",
        "http://example.test/v1",
        "file:///tmp/native",
    ],
)
def test_invalid_or_insecure_endpoint_is_rejected(base_url):
    with pytest.raises(ValueError):
        client(base_url=base_url)
