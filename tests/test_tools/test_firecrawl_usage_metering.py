from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider.metered_http import response_measurement
from opensquilla.session.storage import SessionStorage


@pytest.mark.parametrize("credits", [0, 1, "1.5"])
def test_firecrawl_receipt_keeps_credits_without_assuming_money(credits):
    meters, settlement = response_measurement(
        provider="firecrawl",
        operation="web_fetch",
        payload={
            "success": True,
            "data": {
                "markdown": "private extracted content",
                "metadata": {"creditsUsed": credits, "sourceURL": "https://private.invalid"},
            },
        },
    )
    [meter] = meters
    assert (meter.metric, meter.unit, meter.quantity, meter.known) == (
        "scrape_credits", "credit", str(credits), True,
    )
    assert meter.dimensions == {"operation": "web_fetch"}
    assert settlement is None


@pytest.mark.parametrize("credits", [None, True, -1, "NaN", "Infinity", {}])
def test_firecrawl_invalid_credits_are_unknown(credits):
    meters, settlement = response_measurement(
        provider="firecrawl",
        operation="web_fetch",
        payload={"data": {"metadata": {"creditsUsed": credits}}},
    )
    [meter] = meters
    assert meter.metric == "scrape_credits"
    assert meter.quantity is None and not meter.known
    assert settlement is None


@pytest.mark.parametrize("document", [None, {}, {"metadata": None}, {"metadata": {}}])
def test_firecrawl_absent_credits_keep_observed_request_without_zero_price(document):
    meters, settlement = response_measurement(
        provider="firecrawl", operation="web_fetch", payload={"data": document},
    )
    [meter] = meters
    assert (meter.metric, meter.quantity, meter.source) == ("requests", "1", "observed")
    assert settlement is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "http_error", "network_error", "cancelled"])
async def test_firecrawl_dispatch_binds_owner_and_records_one_physical_call(
    monkeypatch, tmp_path, outcome,
):
    module = importlib.import_module("opensquilla.tools.builtin.web_fetch")
    module._cache.clear()
    storage = SessionStorage(str(tmp_path / "firecrawl.db"))
    await storage.connect()
    client_type = httpx.AsyncClient
    physical_calls = []

    async def handler(request):
        physical_calls.append(request)
        [started] = await storage.query_usage_events(None, None, statuses=("started",))
        assert started.session_id == "side-chat-owner"
        assert started.status == "started"
        if outcome == "network_error":
            raise httpx.ConnectError("synthetic network error", request=request)
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return httpx.Response(
            403 if outcome == "http_error" else 200,
            json={
                "success": True,
                "data": {
                    "markdown": "private extracted content",
                    "metadata": {"title": "Example", "creditsUsed": 1},
                },
            },
        )

    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(module, "managed_network_httpx_kwargs", lambda: {"trust_env": False})
    monkeypatch.setattr(module, "_check_ssrf", lambda url: ["93.184.216.34"])
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic-key")
    accounting_scope = UsageAccountingScope(
        sink=SessionUsageEventSink(storage),
        context=UsageExecutionContext(
            execution_id="side-chat-run",
            agent_run_id="side-chat-run",
            session_id="side-chat-owner",
        ),
    )
    try:
        with bind_usage_accounting_scope(accounting_scope):
            if outcome == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await module.run_web_fetch_payload(
                        "https://example.test", extractor="firecrawl",
                    )
            else:
                result = await module.run_web_fetch_payload(
                    "https://example.test", extractor="firecrawl",
                )
                if outcome == "success":
                    assert result["extractor"] == "firecrawl"
                    assert "private extracted content" in result["text"]
                    # A cache hit has no physical provider call and no second charge.
                    await module.run_web_fetch_payload(
                        "https://example.test", extractor="firecrawl",
                    )
                else:
                    assert result["error"]
        assert len(physical_calls) == 1
        [event] = await storage.query_usage_events(
            None, None, statuses=("finalized", "unknown", "started"),
        )
        assert event.session_id == "side-chat-owner"
        assert event.provider == "firecrawl"
        assert event.model == "web_fetch"
        assert event.status == ("finalized" if outcome == "success" else "unknown")
        if outcome == "success":
            [item] = await storage.query_usage_item_metering([event.event_id])
            [meter] = item["meters"]
            assert (meter["metric"], meter["quantity"]) == ("scrape_credits", "1")
            [settlement] = await storage.query_usage_settlements([event.event_id])
            assert settlement["authority"] == "unknown"
            assert settlement["native_amount"] is None and settlement["usd_amount"] is None
            assert "private extracted content" not in json.dumps(item)
            assert "synthetic-key" not in json.dumps(item)
    finally:
        module._cache.clear()
        await storage.close()
