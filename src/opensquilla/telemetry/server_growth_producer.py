"""Strict adapter for website, CDN, and account-service growth producers.

The adapter deliberately does not own a retry queue.  A service should persist
its event and batch identifiers in the same transaction as its authoritative
business fact, then call :meth:`ServerGrowthProducer.send` until it receives an
accepted result.  This keeps retries idempotent without pretending an in-memory
HTTP helper can provide durable delivery.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import httpx
from pydantic import UUID4, BaseModel, ConfigDict, Field, ValidationError

from opensquilla.env import trust_env
from opensquilla.telemetry.contracts.batch import GrowthEventBatch
from opensquilla.telemetry.contracts.canonical import canonical_json_bytes
from opensquilla.telemetry.contracts.common import EventSource
from opensquilla.telemetry.contracts.growth import GrowthEvent
from opensquilla.telemetry.server.producer_auth import (
    SERVER_OWNED_GROWTH_SOURCES,
    sign_producer_request,
)

_GROWTH_PATH = "/v1/growth/events"
_MAX_RECEIPT_BYTES = 16 * 1024


class ProducerDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PreparedGrowthBatch:
    """Canonical body whose IDs and timestamp must remain stable across retries."""

    source: EventSource
    batch_id: str
    event_count: int
    body: bytes


@dataclass(frozen=True, slots=True)
class ProducerDeliveryResult:
    status: ProducerDeliveryStatus
    batch_id: str
    event_count: int
    http_status: int | None


class _Receipt(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    ok: Literal[True]
    batch_id: UUID4
    accepted: int = Field(strict=True, ge=0)
    duplicates: int = Field(strict=True, ge=0)


def prepare_server_growth_batch(
    *,
    source: EventSource,
    batch_id: UUID,
    sent_at_utc: datetime,
    events: Sequence[GrowthEvent],
) -> PreparedGrowthBatch:
    """Validate and canonicalize one homogeneous server-authoritative batch."""

    if source not in SERVER_OWNED_GROWTH_SOURCES:
        raise ValueError("source must be a server-owned growth producer")
    frozen_events = tuple(events)
    if not frozen_events or any(event.source is not source for event in frozen_events):
        raise ValueError("batch must contain only the configured producer source")
    batch = GrowthEventBatch(
        batch_version=1,
        batch_id=batch_id,
        sent_at_utc=sent_at_utc,
        events=frozen_events,
    )
    return PreparedGrowthBatch(
        source=source,
        batch_id=str(batch.batch_id),
        event_count=len(batch.events),
        body=canonical_json_bytes(batch),
    )


class ServerGrowthProducer:
    """Send canonical server-owned growth batches to the shared growth endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        source: EventSource,
        secret: bytes,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if source not in SERVER_OWNED_GROWTH_SOURCES:
            raise ValueError("source must be a server-owned growth producer")
        self._source = source
        self._secret = bytes(secret)
        # Validate the credential before retaining it.
        sign_producer_request(
            secret=self._secret,
            producer=source,
            timestamp=0,
            body=b"",
        )
        self._endpoint = _endpoint_url(base_url)
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=trust_env(),
        )
        self._closed = False

    async def __aenter__(self) -> ServerGrowthProducer:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def send(self, prepared: PreparedGrowthBatch) -> ProducerDeliveryResult:
        """Make one attempt; the caller owns durable retry scheduling."""

        self._ensure_open()
        if not isinstance(prepared, PreparedGrowthBatch):
            raise TypeError("prepared must be PreparedGrowthBatch")
        if prepared.source is not self._source:
            raise ValueError("prepared batch source does not match producer")
        headers = {
            "Content-Type": "application/json",
            **sign_producer_request(
                secret=self._secret,
                producer=self._source,
                timestamp=int(self._clock()),
                body=prepared.body,
            ),
        }
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                content=prepared.body,
                headers=headers,
            ) as response:
                status_code = response.status_code
                if status_code == 202:
                    receipt = await _read_receipt(response)
                    status = (
                        ProducerDeliveryStatus.ACCEPTED
                        if _receipt_matches(receipt, prepared)
                        else ProducerDeliveryStatus.RETRYABLE
                    )
                elif status_code in {401, 403, 409, 415, 422}:
                    status = ProducerDeliveryStatus.REJECTED
                elif status_code == 429 or status_code >= 500:
                    status = ProducerDeliveryStatus.RETRYABLE
                else:
                    status = ProducerDeliveryStatus.REJECTED
        except httpx.RequestError:
            status_code = None
            status = ProducerDeliveryStatus.RETRYABLE
        return ProducerDeliveryResult(
            status=status,
            batch_id=prepared.batch_id,
            event_count=prepared.event_count,
            http_status=status_code,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("server growth producer is closed")


def _endpoint_url(base_url: str) -> httpx.URL:
    try:
        base = httpx.URL(base_url)
    except (TypeError, ValueError) as exc:
        raise ValueError("telemetry base URL is invalid") from exc
    if (
        base.scheme != "https"
        or not base.host
        or base.username
        or base.password
        or base.query
        or base.fragment
    ):
        raise ValueError("telemetry base URL must be an HTTPS origin without credentials")
    return base.copy_with(path=_GROWTH_PATH, query=None, fragment=None)


async def _read_receipt(response: httpx.Response) -> _Receipt | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_RECEIPT_BYTES:
            return None
    try:
        return _Receipt.model_validate_json(bytes(body), strict=True)
    except (ValidationError, ValueError):
        return None


def _receipt_matches(
    receipt: _Receipt | None,
    prepared: PreparedGrowthBatch,
) -> bool:
    return bool(
        receipt is not None
        and receipt.ok is True
        and str(receipt.batch_id) == prepared.batch_id
        and receipt.accepted + receipt.duplicates == prepared.event_count
    )


__all__ = [
    "PreparedGrowthBatch",
    "ProducerDeliveryResult",
    "ProducerDeliveryStatus",
    "ServerGrowthProducer",
    "prepare_server_growth_batch",
]
