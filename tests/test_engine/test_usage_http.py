from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageAccountingUnavailableError,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.engine.usage_http import openai_compatible_done_event
from opensquilla.session.compaction import call_compaction_llm
from opensquilla.session.naming import call_naming_llm


@dataclass
class _Sink:
    starts: list = field(default_factory=list)
    finalized: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    fail_start: bool = False

    async def start(self, call) -> None:
        if self.fail_start:
            raise UsageAccountingUnavailableError("busy")
        self.starts.append(call)

    async def finalize(self, call, result) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call, reason: str) -> None:
        self.unknown.append((call, reason))


def _scope(sink: _Sink) -> UsageAccountingScope:
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="execution-1",
            agent_run_id="run-1",
            turn_id="turn-1",
            session_id="session-1",
            session_epoch=2,
            agent_id="main",
            run_kind="test_direct_http",
        ),
    )


def _response_payload(content: str = "summary") -> dict:
    return {
        "model": "provider/actual-model",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "prompt_tokens_details": {
                "cached_tokens": 3,
                "cache_write_tokens": 2,
            },
            "completion_tokens_details": {"reasoning_tokens": 4},
            "cost": "0.000000009",
        },
    }


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _client(payload: dict, calls: list[dict]):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return _Response(payload)

    return Client()


def test_openai_compatible_receipt_rejects_inconsistent_or_nonfinite_values() -> None:
    valid = openai_compatible_done_event(_response_payload(), default_model="fallback")
    assert valid.input_tokens == 11
    assert valid.output_tokens == 7
    assert valid.reasoning_tokens == 4
    assert valid.cached_tokens == 3
    assert valid.cache_write_tokens == 2
    assert valid.billed_cost == 0.000000009

    inconsistent = _response_payload()
    inconsistent["usage"]["total_tokens"] = 19
    with pytest.raises(ValueError, match="total"):
        openai_compatible_done_event(inconsistent, default_model="fallback")

    nonfinite = _response_payload()
    nonfinite["usage"]["cost"] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        openai_compatible_done_event(nonfinite, default_model="fallback")


@pytest.mark.asyncio
async def test_direct_naming_receipt_finalizes_once(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "opensquilla.session.naming.httpx.AsyncClient",
        lambda **kwargs: _client(_response_payload('"A useful title"'), calls),
    )
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        title = await call_naming_llm(
            "please title this",
            model="provider/requested-model",
            api_key="dummy",
            provider="openrouter",
        )

    assert title == "A useful title"
    assert len(calls) == len(sink.starts) == len(sink.finalized) == 1
    assert sink.unknown == []
    call, result = sink.finalized[0]
    assert call.call_index == 1
    assert call.run_kind == "test_direct_http"
    assert result.billed_cost_nanos == 9
    assert result.input_tokens == 11


@pytest.mark.asyncio
async def test_direct_tokenrhythm_receipt_uses_native_cny_policy(monkeypatch) -> None:
    payload = _response_payload('"A native title"')
    payload["usage"].pop("cost")
    payload["billing_pending"] = False
    payload["cost_cny"] = 0.000021
    monkeypatch.setattr(
        "opensquilla.session.naming.httpx.AsyncClient",
        lambda **kwargs: _client(payload, []),
    )
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        title = await call_naming_llm(
            "please title this",
            model="deepseek-v4-flash",
            api_key="dummy",
            base_url="https://tokenrhythm.studio",
            provider="tokenrhythm",
        )

    assert title == "A native title"
    [(_call, result)] = sink.finalized
    assert result.cost_source == "provider_billed"
    assert result.billed_cost_nanos == 3_011
    [item] = result.items
    assert item.billing_receipt is not None
    assert item.billing_receipt.currency == "CNY"
    assert item.billing_receipt.amount_nanos == 21_000
    assert item.billing_receipt.usd_equivalent_nanos == 3_011


@pytest.mark.asyncio
async def test_direct_start_failure_prevents_http_dispatch(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "opensquilla.session.naming.httpx.AsyncClient",
        lambda **kwargs: _client(_response_payload(), calls),
    )
    sink = _Sink(fail_start=True)

    with bind_usage_accounting_scope(_scope(sink)):
        with pytest.raises(UsageAccountingUnavailableError):
            await call_naming_llm(
                "please title this",
                model="model",
                api_key="dummy",
            )

    assert calls == []
    assert sink.finalized == []
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_missing_receipt_is_unknown_but_keeps_successful_output(monkeypatch) -> None:
    payload = _response_payload('"Fallback title"')
    payload.pop("usage")
    monkeypatch.setattr(
        "opensquilla.session.naming.httpx.AsyncClient",
        lambda **kwargs: _client(payload, []),
    )
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        title = await call_naming_llm("hello", model="model", api_key="dummy")

    assert title == "Fallback title"
    assert len(sink.starts) == 1
    assert sink.finalized == []
    assert [reason for _call, reason in sink.unknown] == [
        "missing_or_invalid_usage_receipt"
    ]


@pytest.mark.asyncio
async def test_each_compaction_chunk_gets_one_distinct_event(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: _client(_response_payload("compact summary"), calls),
    )
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        for chunk in ("old chunk one", "old chunk two"):
            assert await call_compaction_llm(
                chunk,
                "",
                "provider/requested-model",
                "dummy",
                provider="openrouter",
            ) == "compact summary"

    assert len(calls) == len(sink.starts) == len(sink.finalized) == 2
    assert [call.call_index for call in sink.starts] == [1, 2]
    assert {call.event_id for call in sink.starts} == {
        call.event_id for call, _result in sink.finalized
    }
    assert sink.unknown == []
