from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scripts import live_usage_audio as probe

BASE = "https://openrouter.ai/api/v1"
AUDIO = (
    b'data: {"choices":[{"delta":{"audio":{"data":"UklGRg==",'
    b'"transcript":"private audio"}}}]}\r\n\r\n'
)
FOOTER = (
    b'data: {"model":"fixture","choices":[],"usage":{"prompt_tokens":2,'
    b'"completion_tokens":3,"cost":0.123456789012345678}}\n\n'
)
DONE = b"data: [DONE]\n\n"


class FakeReservation:
    def __init__(self):
        self.finalized = []
        self.unknown = []

    async def finalize_openai_response(self, payload, *, raw_json):
        self.finalized.append((payload, raw_json))

    async def mark_unknown(self, reason):
        self.unknown.append(reason)


@pytest.fixture
def reservation(monkeypatch):
    value = FakeReservation()
    value.starts = []

    async def reserve(**kwargs):
        value.starts.append(kwargs)
        return value

    monkeypatch.setattr(probe, "reserve_direct_usage_call", reserve)
    return value


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


@pytest.mark.asyncio
async def test_stream_is_one_post_with_start_barrier_and_exact_footer(reservation):
    calls = []

    def handle(request):
        assert len(reservation.starts) == 1
        calls.append(request)
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        data = AUDIO + FOOTER + DONE
        return httpx.Response(
            200, stream=Chunks(*(data[i : i + 7] for i in range(0, len(data), 7)))
        )

    body = {"model": "fixture", "stream": False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", body)
    assert len(calls) == len(reservation.finalized) == 1
    assert reservation.starts[0]["operation_kind"] == "audio_output"
    assert result["functional"] and result["terminal"] and result["usageObserved"]
    assert result["audioChunks"] == 1 and result["audioEncodedBytes"] == 8
    assert "0.123456789012345678" in reservation.finalized[0][1]
    assert not reservation.unknown
    assert body == {"model": "fixture", "stream": False}
    assert "private audio" not in json.dumps(result) and "UklGRg" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail,reason", [(DONE, "audio_usage_footer_missing"), (b"", "audio_stream_interrupted")]
)
async def test_missing_receipt_never_becomes_free(reservation, tail, reason):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=AUDIO + tail))
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert reservation.unknown == [reason]
    assert not reservation.finalized
    assert result["functional"] == bool(tail)


@pytest.mark.asyncio
async def test_http_error_only_exposes_redacted_allowlist(reservation):
    payload = {
        "error": {
            "code": 403,
            "message": "Denied synthetic-key",
            "metadata": {
                "error_type": "budget_exceeded",
                "limit_source": "key",
                "raw": "private payload",
                "api_key": "synthetic-key",
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, json=payload))
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert result["httpStatus"] == 403 and result["failureKind"] == "http_403"
    assert result["errorMetadata"]["limit_source"] == "key"
    assert "synthetic-key" not in json.dumps(result) and "private payload" not in json.dumps(result)
    assert reservation.unknown == ["audio_http_error"]


@pytest.mark.asyncio
async def test_cancellation_marks_unknown_and_propagates(reservation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Chunks(AUDIO, asyncio.CancelledError()))
        )
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert reservation.unknown == ["audio_stream_cancelled"]


@pytest.mark.asyncio
async def test_size_limit_stops_stream_without_retry(monkeypatch, reservation):
    monkeypatch.setattr(probe, "MAX_SSE_BYTES", 64)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks(b"x" * 65)))
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert result["failureKind"] == "response_limit"
    assert reservation.unknown == ["audio_stream_interrupted"]
    assert len(reservation.starts) == 1


@pytest.mark.asyncio
async def test_observed_bill_survives_transport_failure(reservation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Chunks(AUDIO, FOOTER, httpx.ReadError("closed")))
        )
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert result["failureKind"] == "transport" and not result["functional"]
    assert len(reservation.finalized) == 1 and not reservation.unknown


@pytest.mark.asyncio
async def test_embedded_error_is_not_audio_success(reservation):
    error = b'data: {"error":{"code":503,"message":"unavailable"}}\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=AUDIO + error + DONE))
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert not result["functional"] and result["audioObserved"]
    assert result["failureKind"] == "provider_stream_error"
    assert reservation.unknown == ["audio_stream_interrupted"]


@pytest.mark.asyncio
async def test_http_200_json_error_keeps_metadata(reservation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}})
        )
    ) as client:
        result = await probe.stream_audio_case(client, BASE, "synthetic-key", {"model": "fixture"})
    assert result["failureKind"] == "provider_stream_error"
    assert result["errorMetadata"]["code"] == "502"
    assert reservation.unknown == ["audio_stream_interrupted"]


@pytest.mark.asyncio
async def test_real_ledger_records_single_precise_audio_footer(tmp_path):
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage(str(tmp_path / "audio.sqlite3"))
    await storage.connect()
    sink = SessionUsageEventSink(storage)

    async def handle(request):
        assert len(await storage.query_usage_events(None, None, statuses=("started",))) == 1
        return httpx.Response(200, content=AUDIO + FOOTER + DONE)

    try:
        scope = UsageAccountingScope(
            sink=sink,
            context=UsageExecutionContext(
                execution_id="audio", agent_run_id="audio", session_id="audio-owner"
            ),
        )
        with bind_usage_accounting_scope(scope):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                result = await probe.stream_audio_case(
                    client, BASE, "synthetic-key", {"model": "fixture"}
                )
        await sink.close(drain_timeout=5)
        [event] = await storage.query_usage_events(None, None)
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert result["functional"]
        assert settlement["native_amount"] == "0.123456789012345678"
        assert settlement["authority"] == "provider_confirmed"
        assert event.session_id == "audio-owner"
    finally:
        await storage.close()
