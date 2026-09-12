"""Consent-gated asynchronous uploader for telemetry outboxes.

The uploader deliberately treats an HTTP request as the only point of no
return: consent is checked both before claiming and immediately before the
request starts.  Response bodies and queued payloads are never logged.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

import httpx
from pydantic import UUID4, BaseModel, ConfigDict, Field, ValidationError

from opensquilla.env import trust_env
from opensquilla.http_retry import parse_retry_after
from opensquilla.telemetry.consent import ConsentCheckpoint, TelemetryScope
from opensquilla.telemetry.contracts.manifest import (
    CURRENT_NOTICE_VERSION_BY_SCOPE,
    MAX_TELEMETRY_NESTING_DEPTH,
)
from opensquilla.telemetry.coordination import (
    scope_consent_coordinator_for,
)
from opensquilla.telemetry.outbox import ClaimedBatch, QuarantineReason, TelemetryOutbox

_ENDPOINT_PATHS = {
    TelemetryScope.RELIABILITY: "/v1/reliability/events",
    TelemetryScope.GROWTH: "/v1/growth/events",
}
_MAX_RECEIPT_BYTES = 16 * 1024


class _UploadReceipt(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    ok: Literal[True]
    batch_id: UUID4
    accepted: Annotated[int, Field(strict=True, ge=0)]
    duplicates: Annotated[int, Field(strict=True, ge=0)]


class _ReceiptDecodeError(ValueError):
    pass


class UploadStatus(StrEnum):
    IDLE = "idle"
    CONSENT_DISABLED = "consent_disabled"
    UPLOADED = "uploaded"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    STALE_NOTICE_DROPPED = "stale_notice_dropped"


@dataclass(frozen=True)
class UploadResult:
    status: UploadStatus
    event_count: int = 0
    http_status: int | None = None
    retry_after_ms: int | None = None


class TelemetryUploader:
    """Upload one scope's leased batches without blocking the event loop."""

    def __init__(
        self,
        outbox: TelemetryOutbox,
        *,
        base_url: str,
        config: object,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] | None = None,
        random_value: Callable[[], float] | None = None,
        base_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 3_600.0,
        max_retry_after_seconds: float = 3_600.0,
    ) -> None:
        self._outbox = outbox
        self._config = config
        self._coordinator = scope_consent_coordinator_for(config)
        self._clock = clock or _now_ms
        self._random_value = random_value or __import__("random").random
        self._base_backoff_seconds = _positive_finite(
            base_backoff_seconds,
            name="base_backoff_seconds",
        )
        self._max_backoff_seconds = _positive_finite(
            max_backoff_seconds,
            name="max_backoff_seconds",
        )
        self._max_retry_after_seconds = _positive_finite(
            max_retry_after_seconds,
            name="max_retry_after_seconds",
        )
        self._endpoint = _endpoint_url(base_url, outbox.scope)
        self._required_notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[outbox.scope.value]
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=trust_env(),
        )
        self._upload_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> TelemetryUploader:
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

    async def upload_once(self) -> UploadResult:
        """Attempt one bounded batch and durably resolve its lease."""

        self._ensure_open()
        async with self._upload_lock:
            if not await self._send_allowed_now():
                return UploadResult(UploadStatus.CONSENT_DISABLED)

            batch = await self._outbox.claim_batch()
            if batch is None:
                return UploadResult(UploadStatus.IDLE)

            stale_event_ids = tuple(
                event.event_id
                for event in batch.events
                if event.notice_version != self._required_notice_version
            )
            if stale_event_ids:
                removed = await self._outbox.discard_claimed_events(
                    batch.lease_id,
                    stale_event_ids,
                    release_remaining=True,
                )
                return UploadResult(
                    UploadStatus.STALE_NOTICE_DROPPED,
                    event_count=removed,
                )

            async with self._coordinator.authorized(
                self._outbox.scope,
                checkpoint=ConsentCheckpoint.SEND,
                notice_version=self._required_notice_version,
            ) as permit:
                if permit is None:
                    await self._outbox.release_unattempted(batch.lease_id)
                    return UploadResult(
                        UploadStatus.CONSENT_DISABLED,
                        event_count=len(batch.events),
                    )

                try:
                    async with self._client.stream(
                        "POST",
                        self._endpoint,
                        content=batch.body,
                        headers={"Content-Type": "application/json"},
                    ) as response:
                        return await self._handle_response(batch, response)
                except asyncio.CancelledError:
                    # The request may have reached the server.  Keep the lease so
                    # expiry recovers it with the same event IDs for deduplication.
                    raise
                except httpx.RequestError:
                    return await self._schedule_retry(batch, http_status=None)

    async def _handle_response(
        self,
        batch: ClaimedBatch,
        response: httpx.Response,
    ) -> UploadResult:
        status_code = response.status_code
        event_count = len(batch.events)
        if status_code == 202:
            receipt = await _read_receipt(response)
            if _valid_receipt(receipt, batch):
                await self._outbox.acknowledge(batch.lease_id)
                return UploadResult(
                    UploadStatus.UPLOADED,
                    event_count=event_count,
                    http_status=status_code,
                )
            return await self._schedule_retry(batch, http_status=status_code)

        if status_code in {409, 422}:
            reason = QuarantineReason.CONFLICT if status_code == 409 else QuarantineReason.CONTRACT
            await self._outbox.quarantine(
                batch.lease_id,
                status_code=status_code,
                reason=reason,
            )
            return UploadResult(
                UploadStatus.QUARANTINED,
                event_count=event_count,
                http_status=status_code,
            )

        retry_after = (
            response.headers.get("Retry-After")
            if status_code == 429 or status_code >= 500
            else None
        )
        return await self._schedule_retry(
            batch,
            http_status=status_code,
            retry_after=retry_after,
        )

    async def _schedule_retry(
        self,
        batch: ClaimedBatch,
        *,
        http_status: int | None,
        retry_after: str | None = None,
    ) -> UploadResult:
        attempt = max(event.attempt_count for event in batch.events)
        exponent = min(attempt - 1, 62)
        cap = min(
            self._max_backoff_seconds,
            self._base_backoff_seconds * (2**exponent),
        )
        random_fraction = self._random_value()
        if not isinstance(random_fraction, (int, float)) or not math.isfinite(random_fraction):
            random_fraction = 0.5
        random_fraction = min(max(float(random_fraction), 0.0), 1.0)
        delay_seconds = (cap / 2.0) + (cap / 2.0 * random_fraction)

        parsed_retry_after = parse_retry_after(
            retry_after,
            now_utc=datetime.fromtimestamp(self._clock() / 1_000, tz=UTC),
        )
        if parsed_retry_after is not None:
            delay_seconds = max(
                delay_seconds,
                min(parsed_retry_after, self._max_retry_after_seconds),
            )

        delay_ms = max(1, round(delay_seconds * 1_000))
        await self._outbox.release_for_retry(
            batch.lease_id,
            next_attempt_at_ms=self._clock() + delay_ms,
        )
        return UploadResult(
            UploadStatus.RETRY_SCHEDULED,
            event_count=len(batch.events),
            http_status=http_status,
            retry_after_ms=delay_ms,
        )

    async def _send_allowed_now(self) -> bool:
        async with self._coordinator.authorized(
            self._outbox.scope,
            checkpoint=ConsentCheckpoint.SEND,
            notice_version=self._required_notice_version,
        ) as permit:
            return permit is not None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("telemetry uploader is closed")


async def _read_receipt(response: httpx.Response) -> _UploadReceipt | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_RECEIPT_BYTES:
            return None
    raw = bytes(body)
    if raw.startswith(codecs.BOM_UTF8):
        return None
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_receipt_duplicate_keys,
            parse_constant=_reject_receipt_constant,
            parse_float=_parse_receipt_finite_float,
        )
        _require_receipt_nesting_limit(value)
        normalized = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        return _UploadReceipt.model_validate_json(normalized, strict=True)
    except (
        _ReceiptDecodeError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        return None


def _valid_receipt(receipt: _UploadReceipt | None, batch: ClaimedBatch) -> bool:
    if receipt is None or str(receipt.batch_id) != batch.batch_id:
        return False
    return receipt.accepted + receipt.duplicates == len(batch.events)


def _reject_receipt_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ReceiptDecodeError
        result[key] = value
    return result


def _reject_receipt_constant(_token: str) -> None:
    raise _ReceiptDecodeError


def _parse_receipt_finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise _ReceiptDecodeError
    return value


def _require_receipt_nesting_limit(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, container_depth = stack.pop()
        if isinstance(current, dict):
            next_depth = container_depth + 1
            if next_depth > MAX_TELEMETRY_NESTING_DEPTH:
                raise _ReceiptDecodeError
            stack.extend((child, next_depth) for child in current.values())
        elif isinstance(current, list):
            next_depth = container_depth + 1
            if next_depth > MAX_TELEMETRY_NESTING_DEPTH:
                raise _ReceiptDecodeError
            stack.extend((child, next_depth) for child in current)


def _endpoint_url(base_url: str, scope: TelemetryScope) -> httpx.URL:
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
    return base.copy_with(path=_ENDPOINT_PATHS[scope], query=None, fragment=None)


def _positive_finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)


__all__ = ["TelemetryUploader", "UploadResult", "UploadStatus"]
