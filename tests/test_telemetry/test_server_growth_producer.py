from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from opensquilla.telemetry.contracts.common import EventSource
from opensquilla.telemetry.contracts.growth import LandingView
from opensquilla.telemetry.server.producer_auth import GrowthProducerAuthenticator
from opensquilla.telemetry.server_growth_producer import (
    ProducerDeliveryStatus,
    ServerGrowthProducer,
    prepare_server_growth_batch,
)

_NOW = 1_788_230_400
_EVENT_ID = UUID("00000000-0000-4000-8000-000000000011")
_ACQUISITION_ID = UUID("00000000-0000-4000-8000-000000000012")
_BATCH_ID = UUID("00000000-0000-4000-8000-000000000013")
_SECRET = b"w" * 32


def _event() -> LandingView:
    return LandingView.model_validate_json(
        b"""
        {
          "event_name": "landing_view",
          "event_version": 1,
          "event_id": "00000000-0000-4000-8000-000000000011",
          "occurred_at_utc": "2026-09-01T01:02:03.456Z",
          "source": "website",
          "app_version": null,
          "platform": "unknown",
          "outcome": null,
          "error_code": null,
          "duration_ms": null,
          "consent_scope": "growth",
          "notice_version": "growth-v1",
          "sample_rate": 1,
          "acquisition_id": "00000000-0000-4000-8000-000000000012"
        }
        """,
        strict=True,
    )


def _prepared():
    return prepare_server_growth_batch(
        source=EventSource.WEBSITE,
        batch_id=_BATCH_ID,
        sent_at_utc=datetime(2026, 9, 1, 1, 3, tzinfo=UTC),
        events=(_event(),),
    )


@pytest.mark.asyncio
async def test_adapter_posts_canonical_signed_batch_and_validates_receipt() -> None:
    prepared = _prepared()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        authenticated = GrowthProducerAuthenticator(
            {EventSource.WEBSITE: _SECRET},
            clock=lambda: float(_NOW),
        ).authenticate(
            headers=request.headers,
            body=body,
            method=request.method,
            path=request.url.path,
        )
        assert authenticated is EventSource.WEBSITE
        assert request.url == httpx.URL("https://telemetry.example/v1/growth/events")
        assert body == prepared.body
        return httpx.Response(
            202,
            json={
                "ok": True,
                "batch_id": prepared.batch_id,
                "accepted": 1,
                "duplicates": 0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        producer = ServerGrowthProducer(
            base_url="https://telemetry.example",
            source=EventSource.WEBSITE,
            secret=_SECRET,
            http_client=client,
            clock=lambda: float(_NOW),
        )
        result = await producer.send(prepared)
        await producer.close()

    assert result.status is ProducerDeliveryStatus.ACCEPTED
    assert result.batch_id == str(_BATCH_ID)
    assert result.event_count == 1
    assert result.http_status == 202


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "response_json", "expected"),
    [
        (202, {"ok": True}, ProducerDeliveryStatus.RETRYABLE),
        (
            202,
            {
                "ok": False,
                "batch_id": str(_BATCH_ID),
                "accepted": 1,
                "duplicates": 0,
            },
            ProducerDeliveryStatus.RETRYABLE,
        ),
        (401, {"ok": False}, ProducerDeliveryStatus.REJECTED),
        (422, {"ok": False}, ProducerDeliveryStatus.REJECTED),
        (429, {"ok": False}, ProducerDeliveryStatus.RETRYABLE),
        (503, {"ok": False}, ProducerDeliveryStatus.RETRYABLE),
    ],
)
async def test_adapter_classifies_receipts_without_automatic_retry(
    status_code: int,
    response_json: dict[str, object],
    expected: ProducerDeliveryStatus,
) -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, json=response_json)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        producer = ServerGrowthProducer(
            base_url="https://telemetry.example/ignored",
            source=EventSource.WEBSITE,
            secret=_SECRET,
            http_client=client,
            clock=lambda: float(_NOW),
        )
        result = await producer.send(_prepared())

    assert result.status is expected
    assert attempts == 1


@pytest.mark.asyncio
async def test_network_ambiguity_is_retryable_with_same_prepared_batch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        producer = ServerGrowthProducer(
            base_url="https://telemetry.example",
            source=EventSource.WEBSITE,
            secret=_SECRET,
            http_client=client,
            clock=lambda: float(_NOW),
        )
        prepared = _prepared()
        result = await producer.send(prepared)

    assert result.status is ProducerDeliveryStatus.RETRYABLE
    assert result.http_status is None
    assert prepared.batch_id == str(_BATCH_ID)


def test_prepared_batch_is_strict_homogeneous_and_canonical() -> None:
    first = _prepared()
    second = _prepared()
    assert first == second
    assert first.body.startswith(b'{"batch_id"')
    assert b"prompt" not in first.body

    with pytest.raises(ValueError, match="server-owned"):
        prepare_server_growth_batch(
            source=EventSource.DESKTOP,
            batch_id=_BATCH_ID,
            sent_at_utc=datetime.now(UTC),
            events=(_event(),),
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://telemetry.example",
        "https://user:password@telemetry.example",
        "https://telemetry.example?query=1",
    ],
)
def test_adapter_requires_credential_free_https_origin(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        ServerGrowthProducer(
            base_url=base_url,
            source=EventSource.WEBSITE,
            secret=_SECRET,
        )


@pytest.mark.asyncio
async def test_adapter_rejects_cross_producer_prepared_batch() -> None:
    producer = ServerGrowthProducer(
        base_url="https://telemetry.example",
        source=EventSource.CDN,
        secret=b"c" * 32,
    )
    try:
        with pytest.raises(ValueError, match="does not match"):
            await producer.send(_prepared())
    finally:
        await producer.close()
