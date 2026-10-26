from __future__ import annotations

import asyncio
import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider.media_jobs import (
    _key,
    accounted_video_submission,
    job_record,
    observe_video_job,
)
from opensquilla.provider.metered_http import (
    accounted_request,
    audio_duration_seconds,
    response_measurement,
)
from opensquilla.session.storage import SessionStorage


def scope(sink, owner="recording-owner"):
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(execution_id=owner, agent_run_id=owner, session_id=owner),
    )


@pytest.mark.parametrize("amount", ["0", "0.0000000001", "0.2123456789"])
def test_image_money_is_independent_of_token_receipt(amount):
    meters, settlement = response_measurement(
        provider="openrouter",
        operation="image_generation",
        base_url="https://openrouter.ai/api/v1",
        raw_json='{"usage":{"cost":' + amount + '},"choices":[{"message":{"images":[{}]}}]}',
    )
    assert settlement.usd_amount == amount
    assert settlement.authority == "provider_confirmed"
    assert [(m.metric, m.quantity) for m in meters] == [("output_images", "1")]


def test_tokenrhythm_native_precision_and_pending_are_not_confirmed():
    _, confirmed = response_measurement(
        provider="tokenrhythm",
        operation="image_generation",
        base_url="https://tokenrhythm.studio/v1",
        raw_json='{"billing_pending":false,"cost_cny":0.2000000001}',
    )
    assert confirmed.native_amount == "0.2000000001"
    _, pending = response_measurement(
        provider="tokenrhythm",
        operation="image_generation",
        base_url="https://tokenrhythm.studio/v1",
        payload={"billing_pending": True, "cost_cny": "0.2"},
    )
    assert (pending.authority, pending.status, pending.native_amount, pending.usd_amount) == (
        "unknown",
        "pending",
        "0.2",
        None,
    )
    _, spoof = response_measurement(
        provider="tokenrhythm",
        operation="image_generation",
        base_url="https://proxy.invalid/v1",
        payload={"billing_pending": False, "cost_cny": 9},
    )
    assert spoof is None


def test_valid_image_charge_survives_invalid_measurement():
    meters, settlement = response_measurement(
        provider="openrouter",
        operation="image_generation",
        base_url="https://openrouter.ai/api/v1",
        payload={"usage": {"cost": "0.2", "image_count": "NaN"}},
    )
    assert settlement.usd_amount == "0.2"
    assert meters[0].quantity is None and not meters[0].known


@pytest.mark.parametrize("images", [None, 1, "malformed", {}])
def test_valid_image_charge_survives_malformed_image_list(images):
    meters, settlement = response_measurement(
        provider="openrouter",
        operation="image_generation",
        base_url="https://openrouter.ai/api/v1",
        payload={
            "usage": {"cost": "0.2"},
            "choices": [{"message": {"images": images}}],
        },
    )
    assert settlement.usd_amount == "0.2"
    assert settlement.authority == "provider_confirmed"
    assert meters[0].metric == "output_images"
    assert meters[0].quantity is None and not meters[0].known


@pytest.mark.asyncio
async def test_video_json_keeps_exact_money_and_ordinary_catalog_types():
    from opensquilla.provider.video_generation import read_video_json

    response = httpx.Response(
        200,
        content=b'{"usage":{"cost":0.123456789012345678},"duration":2.5,"data":[{"pricing_skus":{"duration_seconds":0.0000000001},"supported_durations":[2,4]}]}',
    )
    payload = await read_video_json(response, max_bytes=4096, label="fixture")
    _, settlement = response_measurement(
        provider="openrouter",
        operation="video_generation",
        base_url="https://openrouter.ai/api/v1",
        payload=payload,
    )
    assert settlement.native_amount == "0.123456789012345678"
    assert type(payload["duration"]) is float
    assert payload["data"][0]["supported_durations"] == [2, 4]


def test_search_credits_and_estimated_dollars_do_not_claim_actual_charge():
    meters, settlement = response_measurement(
        provider="tavily", operation="web_search", payload={"usage": {"credits": 2}}
    )
    assert meters[0].quantity == "2" and settlement is None
    _, exa = response_measurement(
        provider="exa",
        operation="web_search",
        base_url="https://api.exa.ai",
        payload={"costDollars": {"total": 0.005}},
    )
    assert exa.authority == "provider_estimate"
    assert exa.cost_fields()["billed_cost_nanos"] == 0


@pytest.mark.parametrize(
    "amount,pending", [("1.20000000", False), ("0.00000000", False), ("1.20000000", True)]
)
def test_tokenrhythm_video_live_receipt_shape_uses_total_once(amount, pending):
    payload = {
        "status": "success",
        "billing": {
            "pending": pending,
            "total_cost_cny": amount,
            "output_cost_cny": amount,
            "input_video_cost_cny": "0",
            "input_image_cost_cny": "0",
            "input_audio_cost_cny": "0",
        },
        "usage": {"output_seconds": 2, "input_seconds": 0, "input_image_count": 0},
    }
    meters, settlement = response_measurement(
        provider="tokenrhythm",
        operation="video_generation",
        base_url="https://tokenrhythm.studio/v1",
        payload=payload,
    )
    assert settlement.native_amount == amount
    assert settlement.status == ("pending" if pending else "final")
    assert settlement.authority == ("unknown" if pending else "provider_confirmed")
    assert {m.metric: m.quantity for m in meters} == {
        "video_seconds": "2",
        "input_video_seconds": "0",
        "input_images": "0",
    }


def test_xai_total_ticks_not_added_to_tools():
    meters, settlement = response_measurement(
        provider="xai",
        operation="video_generation",
        base_url="https://api.x.ai/v1",
        payload={"usage": {"cost_in_usd_ticks": 1, "server_side_tool_usage": {"web_search": 3}}},
    )
    assert settlement.usd_amount == "0.0000000001"
    assert len(meters) == 1


def test_audio_headers_and_real_media_duration():
    data = io.BytesIO()
    with wave.open(data, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\x00\x00" * 12000)
    assert audio_duration_seconds(data.getvalue()) == "1.5"
    meters, settlement = response_measurement(
        provider="elevenlabs",
        operation="text_to_speech",
        headers={"character-cost": "23", "request-id": "synthetic"},
    )
    assert meters[0].quantity == "23"
    assert meters[0].dimensions["operation"] == "text_to_speech"
    assert settlement is None


def test_job_store_isolated_by_ledger_and_payer(tmp_path):
    first = SessionUsageEventSink(SimpleNamespace(_db_path=str(tmp_path / "a.db")))
    second = SessionUsageEventSink(SimpleNamespace(_db_path=str(tmp_path / "b.db")))
    with bind_usage_accounting_scope(scope(first)):
        job_record("test", "job", {"owner": "first"})
        original_key = _key("tokenrhythm", "https://tokenrhythm.studio/v1", "same-job", "payer-a")
        assert original_key != _key(
            "tokenrhythm", "https://tokenrhythm.studio/v1", "same-job", "payer-b"
        )
    with bind_usage_accounting_scope(scope(second)):
        assert job_record("test", "job") is None
    with bind_usage_accounting_scope(scope(SessionUsageEventSink(first._storage))):
        assert job_record("test", "job") == {"owner": "first"}
        assert (
            _key("tokenrhythm", "https://tokenrhythm.studio/v1", "same-job", "payer-a")
            == original_key
        )


@pytest.mark.asyncio
async def test_video_charge_commits_before_download_failure(monkeypatch, tmp_path):
    from opensquilla.provider.tokenrhythm_video_generation import generate_tokenrhythm_video
    from opensquilla.provider.video_generation import VideoGenerationError

    storage = SessionStorage(str(tmp_path / "before-download.db"))
    await storage.connect()
    original_client = httpx.AsyncClient

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "charged-job", "status": "queued"})
        return httpx.Response(
            200,
            json={
                "id": "charged-job",
                "status": "success",
                "billing": {"pending": False, "total_cost_cny": "1.2"},
                "usage": {"output_seconds": 2, "input_seconds": 0, "input_image_count": 0},
                "result": {"url": "https://media.example.invalid/video.mp4"},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def fail_download(**kwargs):
        [event] = await storage.query_usage_events(None, None)
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert settlement["native_amount"] == "1.2"
        assert settlement["authority"] == "provider_confirmed"
        raise VideoGenerationError("synthetic download failure", job_id="charged-job")

    monkeypatch.setattr("opensquilla.provider.video_download.download_video_url", fail_download)
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage))):
            with pytest.raises(VideoGenerationError, match="download failure"):
                await generate_tokenrhythm_video(
                    base_url="https://tokenrhythm.studio/v1",
                    api_key="synthetic",
                    model="wan3.0-video",
                    prompt="synthetic",
                    duration=2,
                    aspect_ratio="16:9",
                    resolution="720p",
                    output_path=tmp_path / "video.mp4",
                    poll_interval_seconds=0.01,
                )
        assert len(await storage.query_usage_events(None, None)) == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_search_request_starts_before_dispatch_and_records_missing_price(tmp_path):
    storage = SessionStorage(str(tmp_path / "usage.db"))
    await storage.connect()
    sink = SessionUsageEventSink(storage)

    async def handler(request):
        rows = await storage.query_usage_events(None, None, statuses=("started",))
        assert len(rows) == 1 and rows[0].status == "started"
        return httpx.Response(200, json={"usage": {"credits": 1}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with bind_usage_accounting_scope(scope(sink)):
                await accounted_request(
                    client,
                    "POST",
                    "https://api.tavily.com/search",
                    provider="tavily",
                    operation="web_search",
                    json={"query": "secret query not retained"},
                )
        [event] = await storage.query_usage_events(None, None)
        [item] = await storage.query_usage_item_metering([event.event_id])
        assert item["meters"][0]["metric"] == "search_credits"
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert settlement["authority"] == "unknown"
        assert "secret query" not in json.dumps(item)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_video_restart_repeated_poll_revises_original_owner_once(tmp_path):
    storage = SessionStorage(str(tmp_path / "usage.db"))
    await storage.connect()
    sink = SessionUsageEventSink(storage)

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("job-1")
        return "job-1"

    try:
        with bind_usage_accounting_scope(scope(sink, "original-owner")):
            await submit(api_root="https://tokenrhythm.studio/v1", model="wan3.0-video", duration=5)
        # Simulate a new sink and caller after restart, preserving only files.
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "new-owner")):
            payload = {"billing_pending": False, "cost_cny": "1.5", "usage": {"video_seconds": 5}}
            await asyncio.gather(
                *(
                    observe_video_job(
                        "tokenrhythm", "https://tokenrhythm.studio/v1", "job-1", payload
                    )
                    for _ in range(3)
                )
            )
            await observe_video_job("tokenrhythm", "https://tokenrhythm.studio/v1", "job-1", {})
        [event] = await storage.query_usage_events(None, None)
        assert event.session_id == "original-owner"
        [item] = await storage.query_usage_item_metering([event.event_id])
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert settlement["native_amount"] == "1.5"
        revisions = await storage.query_usage_settlements([event.event_id], history=True)
        assert len(revisions) == 2
        assert {m["metric"] for m in item["meters"]} >= {"requested_video_seconds", "video_seconds"}
    finally:
        await storage.close()


@pytest.fixture
def pending_catalog(monkeypatch):
    from opensquilla.provider import media_pricing

    monkeypatch.setattr(media_pricing, "_FEEDS", {})
    base = "https://tokenrhythm.studio/v1"
    media_pricing.register_media_catalog(
        "tokenrhythm",
        [
            {
                "id": "fixture-image",
                "currency": "CNY",
                "billingMode": "per_image",
                "pricePerImage": "0.2",
            },
            {
                "id": "fixture-video",
                "videoPricing": {
                    "pricingScheme": "wan_total_seconds",
                    "outputPerSecondCny": {"720P": "0.6"},
                    "inputVideoPerSecondCny": {"720P": "0.6"},
                    "inputImagePerImageCny": "0",
                    "inputAudioPerSecondCny": "0",
                },
            },
        ],
        base_url=base,
    )
    return base


@pytest.mark.asyncio
async def test_pending_image_receipt_keeps_catalog_quote_and_native_candidate(
    tmp_path, pending_catalog
):
    from opensquilla.engine.usage_http import reserve_direct_usage_call

    storage = SessionStorage(str(tmp_path / "pending-image.db"))
    await storage.connect()
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage))):
            reservation = await reserve_direct_usage_call(
                provider="tokenrhythm",
                model="fixture-image",
                base_url=pending_catalog,
                operation_kind="image_generation",
            )
            await reservation.finalize_openai_response(
                {"billing_pending": True, "cost_cny": "0.19", "data": [{}]}
            )
        [event] = await storage.query_usage_events(None, None)
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert settlement["authority"] == "local_estimate" and settlement["status"] == "pending"
        assert settlement["native_amount"] == "0.2"
        assert settlement["provider_pending_native_amount"] == "0.19"
        assert settlement["provider_pending_currency"] == "CNY"
        assert settlement["billed_cost_nanos"] == 0
        assert settlement["estimated_cost_nanos"] > 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_pending_video_quote_then_confirmed_bill_replaces_once(tmp_path, pending_catalog):
    storage = SessionStorage(str(tmp_path / "pending-video.db"))
    await storage.connect()

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("job-pending")
        return "job-pending"

    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage))):
            await submit(
                api_root=pending_catalog, model="fixture-video", duration=2, resolution="720p"
            )
            pending = {
                "billing": {"pending": True, "total_cost_cny": "1.1"},
                "usage": {"output_seconds": 2},
            }
            await observe_video_job("tokenrhythm", pending_catalog, "job-pending", pending)
            [event] = await storage.query_usage_events(None, None)
            [quote] = await storage.query_usage_settlements([event.event_id])
            assert (quote["authority"], quote["status"], quote["native_amount"]) == (
                "local_estimate",
                "pending",
                "1.2",
            )
            assert quote["provider_pending_native_amount"] == "1.1"
            confirmed = {
                "billing": {"pending": False, "total_cost_cny": "1.15"},
                "usage": {"output_seconds": 2},
            }
            await observe_video_job("tokenrhythm", pending_catalog, "job-pending", confirmed)
            await observe_video_job("tokenrhythm", pending_catalog, "job-pending", pending)
        assert len(await storage.query_usage_events(None, None)) == 1
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert (
            settlement["native_amount"] == "1.15"
            and settlement["authority"] == "provider_confirmed"
        )
        assert len(await storage.query_usage_settlements([event.event_id], history=True)) == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_video_estimate_survives_restart_without_catalog(
    tmp_path, pending_catalog, monkeypatch
):
    from opensquilla.provider import media_pricing

    storage = SessionStorage(str(tmp_path / "estimated-before-restart.db"))
    await storage.connect()

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("estimated-job")

    pending = {
        "billing": {"pending": True, "total_cost_cny": "1.1"},
        "usage": {"output_seconds": 2},
    }
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "original-owner")):
            await submit(
                api_root=pending_catalog, model="fixture-video", duration=2, resolution="720p"
            )
            await observe_video_job("tokenrhythm", pending_catalog, "estimated-job", pending)
        [event] = await storage.query_usage_events(None, None)
        [before] = await storage.query_usage_settlements([event.event_id])
        monkeypatch.setattr(media_pricing, "_FEEDS", {})
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "after-restart")):
            await observe_video_job("tokenrhythm", pending_catalog, "estimated-job", pending)
        assert await storage.query_usage_settlements([event.event_id]) == [before]
        assert before["authority"] == "local_estimate" and before["native_amount"] == "1.2"
        assert len(await storage.query_usage_settlements([event.event_id], history=True)) == 2
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_video_poll_recovers_lost_initial_ledger_commit_after_restart(tmp_path):
    storage = SessionStorage(str(tmp_path / "lost-commit.db"))
    await storage.connect()

    class LostFinalizationSink(SessionUsageEventSink):
        async def finalize(self, call, result):
            raise OSError("synthetic permanent initial ledger failure")

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("job-recover")
        return "job-recover"

    base = "https://tokenrhythm.studio/v1"
    try:
        with bind_usage_accounting_scope(scope(LostFinalizationSink(storage), "original-owner")):
            await submit(api_root=base, model="fixture-video", duration=2)
        [started] = await storage.query_usage_events(None, None, statuses=("started",))
        assert not await storage.query_usage_settlements([started.event_id])
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "new-owner")):
            await observe_video_job(
                "tokenrhythm",
                base,
                "job-recover",
                {"billing": {"pending": False, "total_cost_cny": "1.2"}},
            )
        [event] = await storage.query_usage_events(None, None)
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert event.session_id == "original-owner" and event.event_id == started.event_id
        assert (
            settlement["native_amount"] == "1.2" and settlement["authority"] == "provider_confirmed"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["confirmed", "pending", "unavailable", "foreign_job"])
async def test_delivered_video_status_refreshes_pending_bill_without_regeneration(
    tmp_path, monkeypatch, response_kind
):
    from opensquilla.gateway.config import VideoGenerationConfig
    from opensquilla.provider.media_jobs import credential_fingerprint
    from opensquilla.provider.video_generation_credentials import VideoGenerationCredential
    from opensquilla.tools.builtin import media
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    storage = SessionStorage(str(tmp_path / "delivered-video.db"))
    await storage.connect()
    base = "https://tokenrhythm.studio/v1"
    key, job, session = "synthetic-key", "delivered-job", "webchat:original-owner"
    cached_payload = json.dumps({"status": "ok", "job_id": job})
    requests = []
    original_client = httpx.AsyncClient

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"](job)
        return job

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == f"/v1/videos/{job}"
        if response_kind == "unavailable":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "id": "another-job" if response_kind == "foreign_job" else job,
                "status": "success",
                "billing": {"pending": response_kind == "pending", "total_cost_cny": "1.2"},
                "usage": {"output_seconds": 2},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    config = VideoGenerationConfig(enabled=True, provider="tokenrhythm", primary="wan3.0-video")
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (config, VideoGenerationCredential(available=True, api_key=key)),
    )
    monkeypatch.setattr(media, "_video_job_sessions", {})

    def unexpected_adapter(_provider):
        raise AssertionError("A delivered video must not be generated or downloaded again")

    monkeypatch.setattr(media, "_video_adapter", unexpected_adapter)
    token = current_tool_context.set(ToolContext(caller_kind=CallerKind.WEB, session_key=session))
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "original-owner")):
            await submit(api_root=base, api_key=key, model="wan3.0-video", duration=2)
            await observe_video_job(
                "tokenrhythm",
                base,
                job,
                {
                    "billing": {"pending": True, "total_cost_cny": "1.2"},
                    "usage": {"output_seconds": 2},
                },
                api_key=key,
            )
            media._video_job_sessions[job] = media._VideoJobReceipt(
                session_key=session,
                credential_fingerprint=credential_fingerprint(key),
                provider="tokenrhythm",
                model="wan3.0-video",
                base_url=base,
                native_job_id=job,
                completed_payload=cached_payload,
            )
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage), "later-turn")):
            assert await media.video_status(job) == cached_payload
            [event] = await storage.query_usage_events(None, None)
            first_revisions = await storage.query_usage_settlements([event.event_id], history=True)
            assert await media.video_status(job) == cached_payload
        assert event.session_id == "original-owner"
        assert len(await storage.query_usage_events(None, None)) == 1
        assert (
            await storage.query_usage_settlements([event.event_id], history=True) == first_revisions
        )
        [latest] = await storage.query_usage_settlements([event.event_id])
        if response_kind == "confirmed":
            assert len(requests) == 1
            assert latest["authority"] == "provider_confirmed" and latest["native_amount"] == "1.2"
            assert len(first_revisions) == 3
        else:
            assert len(requests) == 2
            assert latest["authority"] == "unknown" and latest["status"] == "pending"
            assert len(first_revisions) == 2
    finally:
        current_tool_context.reset(token)
        await storage.close()


def test_catalog_quote_does_not_override_provider_estimate():
    from opensquilla.provider.media_pricing import media_settlement
    from opensquilla.usage_metering import UsageSettlementRevision

    estimate = UsageSettlementRevision(
        "initial", "local_estimate", "final", native_amount="2", usd_amount="2"
    )
    provider = UsageSettlementRevision(
        "initial", "provider_estimate", "final", native_amount="1", usd_amount="1"
    )
    assert media_settlement(provider, estimate) is provider


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["image_generation", "video_generation"])
@pytest.mark.parametrize("pending", [False, True])
async def test_media_bill_survives_overflowing_catalog(
    tmp_path, pending_catalog, operation, pending
):
    from opensquilla.engine.usage_http import reserve_direct_usage_call
    from opensquilla.provider.media_pricing import register_media_catalog

    register_media_catalog(
        "tokenrhythm",
        [
            {
                "id": "fixture-image",
                "currency": "CNY",
                "billingMode": "per_image",
                "pricePerImage": "1e24",
            },
            {
                "id": "fixture-video",
                "videoPricing": {
                    "pricingScheme": "wan_total_seconds",
                    "outputPerSecondCny": {"720P": "1e24"},
                    "inputVideoPerSecondCny": {"720P": "0"},
                    "inputImagePerImageCny": "0",
                    "inputAudioPerSecondCny": "0",
                },
            },
        ],
        base_url=pending_catalog,
    )
    storage = SessionStorage(str(tmp_path / "confirmed-media.db"))
    await storage.connect()
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage))):
            if operation == "image_generation":
                reservation = await reserve_direct_usage_call(
                    provider="tokenrhythm",
                    model="fixture-image",
                    base_url=pending_catalog,
                    operation_kind=operation,
                )
                await reservation.finalize_openai_response(
                    {
                        "billing_pending": pending,
                        "cost_cny": "0.2",
                        "usage": {"image_count": 100},
                    }
                )
            else:
                @accounted_video_submission("tokenrhythm")
                async def submit(**kwargs):
                    kwargs["on_job_accepted"]("confirmed-job")

                await submit(
                    api_root=pending_catalog, model="fixture-video", duration=2, resolution="720p"
                )
                await observe_video_job(
                    "tokenrhythm",
                    pending_catalog,
                    "confirmed-job",
                    {
                        "billing": {"pending": pending, "total_cost_cny": "0.2"},
                        "usage": {"output_seconds": 100},
                    },
                )
        [event] = await storage.query_usage_events(None, None)
        [latest] = await storage.query_usage_settlements([event.event_id])
        assert latest["authority"] == ("unknown" if pending else "provider_confirmed")
        assert latest["status"] == ("pending" if pending else "final")
        assert latest["native_amount"] == "0.2"
        assert latest["price_snapshot_id"] is None
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["provider_confirmed", "provider_estimate"])
async def test_provider_valuation_does_not_capture_unused_catalog(
    tmp_path, pending_catalog, authority
):
    from opensquilla.engine.usage_http import reserve_direct_usage_call
    from opensquilla.usage_metering import UsageMeter, UsageSettlementRevision

    storage = SessionStorage(str(tmp_path / "provider-valuation.db"))
    await storage.connect()
    try:
        with bind_usage_accounting_scope(scope(SessionUsageEventSink(storage))):
            reservation = await reserve_direct_usage_call(
                provider="tokenrhythm",
                model="fixture-image",
                base_url=pending_catalog,
                operation_kind="image_generation",
            )
            await reservation.finalize_measurement(
                meters=(UsageMeter("output_images", "image", "1"),),
                settlement=UsageSettlementRevision(
                    "initial", authority, "final", native_amount="0.1", usd_amount="0.1"
                ),
            )
        [event] = await storage.query_usage_events(None, None)
        [latest] = await storage.query_usage_settlements([event.event_id])
        [item] = await storage.query_usage_item_metering([event.event_id])
        assert latest["authority"] == authority and latest["native_amount"] == "0.1"
        assert item["price_snapshot_id"] is None
    finally:
        await storage.close()
