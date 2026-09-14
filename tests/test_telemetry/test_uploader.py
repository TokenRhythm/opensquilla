from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.telemetry.consent import (
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.consent_transition import (
    global_network_observability_transition,
)
from opensquilla.telemetry.contracts import (
    CURRENT_NOTICE_VERSION_BY_SCOPE,
    TELEMETRY_EVENT_ADAPTER,
    telemetry_protocol_manifest,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.outbox import OutboxLimits, TelemetryOutbox
from opensquilla.telemetry.uploader import TelemetryUploader, UploadStatus

_PROTOCOL_MANIFEST = telemetry_protocol_manifest()


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


@dataclass
class FakeClock:
    now_ms: int = 1_788_224_400_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def _turn_event(
    number: int = 1,
    *,
    notice: str = CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"],
    app_version: str = "1.2.3",
):
    payload = {
        "event_name": "turn_result",
        "event_version": 1,
        "event_id": _uuid(number),
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "gateway",
        "app_version": app_version,
        "platform": "linux",
        "outcome": "success",
        "error_code": None,
        "duration_ms": 120,
        "consent_scope": "reliability",
        "notice_version": notice,
        "sample_rate": 1.0,
        "app_session_id": _uuid(900),
        "ttft_ms": 40,
        "stall_count": 0,
        "stall_threshold_ms": 15_000,
    }
    return TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)


def _growth_event(number: int = 1):
    payload = {
        "event_name": "first_app_ready",
        "event_version": 1,
        "event_id": _uuid(number),
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "windows",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": CURRENT_NOTICE_VERSION_BY_SCOPE["growth"],
        "sample_rate": 1,
        "analytics_user_id": _uuid(901),
    }
    return TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)


class SequenceConsent:
    def __init__(self, *values: ScopeConsentState) -> None:
        if not values:
            raise ValueError("at least one consent state is required")
        self._values = list(values)
        self._fallback = values[-1]
        self.calls = 0

    async def __call__(self, scope: TelemetryScope) -> ScopeConsentState:
        assert scope in {TelemetryScope.RELIABILITY, TelemetryScope.GROWTH}
        self.calls += 1
        return self._values.pop(0) if self._values else self._fallback


def _consent_state(
    *,
    scope: TelemetryScope = TelemetryScope.RELIABILITY,
    notice: str | None = None,
    decision: ConsentDecision = ConsentDecision.GRANTED,
) -> ScopeConsentState:
    current_notice = CURRENT_NOTICE_VERSION_BY_SCOPE[scope.value]
    resolved_notice = current_notice if notice is None else notice
    granted = decision is ConsentDecision.GRANTED
    return ScopeConsentState(
        scope=scope,
        decision=decision,
        notice_version=resolved_notice,
        consented_at_utc="2026-09-02T01:00:00Z" if granted else None,
        record_complete=granted,
        notice_current=resolved_notice == current_notice,
    )


def _live_config(tmp_path: Path, *, global_disabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        privacy=SimpleNamespace(
            disable_network_observability=global_disabled,
            reliability_diagnostics_enabled=True,
            reliability_notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"],
            reliability_consented_at_utc="2026-09-02T01:00:00Z",
            product_analytics_enabled=True,
            product_analytics_notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE["growth"],
            product_analytics_consented_at_utc="2026-09-02T01:00:00Z",
        ),
    )


def _accepted_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(
        202,
        json={
            "ok": True,
            "batch_id": payload["batch_id"],
            "accepted": len(payload["events"]),
            "duplicates": 0,
        },
    )


async def _uploader(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    scope: TelemetryScope = TelemetryScope.RELIABILITY,
    consent: Callable[
        [TelemetryScope],
        ScopeConsentState | Awaitable[ScopeConsentState],
    ]
    | None = None,
    clock: FakeClock | None = None,
    limits: OutboxLimits | None = None,
    random_value: Callable[[], float] = lambda: 0.5,
) -> tuple[TelemetryOutbox, TelemetryUploader, httpx.AsyncClient]:
    outbox = await TelemetryOutbox.open(tmp_path, scope, clock=clock, limits=limits)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = SimpleNamespace()
    scope_consent_coordinator_for(
        config,
        state_provider=consent or (lambda requested: _consent_state(scope=requested)),
    )
    uploader = TelemetryUploader(
        outbox,
        base_url="https://telemetry.invalid",
        config=config,
        http_client=client,
        clock=clock,
        random_value=random_value,
        base_backoff_seconds=2.0,
        max_backoff_seconds=3_600.0,
        max_retry_after_seconds=3_600.0,
    )
    return outbox, uploader, client


async def test_202_accepted_deletes_reliability_batch_and_uses_scope_endpoint(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _accepted_response(request)

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event())

        result = await uploader.upload_once()

        assert result.status is UploadStatus.UPLOADED
        assert result.event_count == 1
        assert (await outbox.stats()).pending_events == 0
        assert seen[0].url.path == "/v1/reliability/events"
        assert seen[0].headers["content-type"] == "application/json"
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_202_duplicates_are_acknowledged(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "ok": True,
                "batch_id": payload["batch_id"],
                "accepted": 0,
                "duplicates": len(payload["events"]),
            },
        )

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event())
        assert (await uploader.upload_once()).status is UploadStatus.UPLOADED
        assert (await outbox.stats()).pending_events == 0
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_malformed_202_receipt_retries_without_deleting_events(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={"ok": True, "batch_id": _uuid(999), "accepted": 1, "duplicates": 0},
        )

    outbox, uploader, client = await _uploader(tmp_path, handler, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.RETRY_SCHEDULED
        assert (await outbox.stats()).pending_events == 1
        assert await outbox.claim_batch() is None
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


@pytest.mark.parametrize(
    "variant",
    ["duplicate_key", "extra_field", "deep", "nan", "infinity", "oversized"],
)
async def test_ambiguous_or_unbounded_202_receipt_never_acknowledges(
    tmp_path: Path,
    variant: str,
) -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        batch_id = json.loads(request.content)["batch_id"]
        if variant == "duplicate_key":
            content = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                '"accepted":0,"accepted":1,"duplicates":0}'
            ).encode()
        elif variant == "extra_field":
            content = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                '"accepted":1,"duplicates":0,"unexpected":0}'
            ).encode()
        elif variant == "deep":
            nested = "[" * 17 + "0" + "]" * 17
            content = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                f'"accepted":1,"duplicates":0,"unexpected":{nested}}}'
            ).encode()
        elif variant == "nan":
            content = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                '"accepted":1,"duplicates":0,"unexpected":NaN}'
            ).encode()
        elif variant == "infinity":
            content = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                '"accepted":1,"duplicates":0,"unexpected":Infinity}'
            ).encode()
        else:
            valid = (
                f'{{"ok":true,"batch_id":"{batch_id}",'
                '"accepted":1,"duplicates":0}'
            ).encode()
            content = valid + (b" " * (17 * 1024))
        return httpx.Response(202, content=content)

    outbox, uploader, client = await _uploader(tmp_path, handler, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.RETRY_SCHEDULED
        assert result.http_status == 202
        assert (await outbox.stats()).pending_events == 1
        assert await outbox.claim_batch() is None
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_consent_is_checked_before_claim_and_immediately_before_http(tmp_path: Path) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    consent = SequenceConsent(
        _consent_state(),
        _consent_state(decision=ConsentDecision.DECLINED),
    )
    outbox, uploader, client = await _uploader(tmp_path, handler, consent=consent)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.CONSENT_DISABLED
        assert consent.calls == 2
        assert requests == 0
        reclaimed = await outbox.claim_batch()
        assert reclaimed is not None
        assert reclaimed.events[0].attempt_count == 1
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_boolean_true_is_not_accepted_as_consent(tmp_path: Path) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = SimpleNamespace()
    scope_consent_coordinator_for(
        config,
        state_provider=lambda _scope: True,  # type: ignore[arg-type,return-value]
    )
    uploader = TelemetryUploader(
        outbox,
        base_url="https://telemetry.invalid",
        config=config,
        http_client=client,
    )
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.CONSENT_DISABLED
        assert requests == 0
        stats = await outbox.stats()
        assert stats.pending_events == 1
        assert stats.leased_events == 0
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_disabled_consent_does_not_claim_or_send(tmp_path: Path) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    outbox, uploader, client = await _uploader(
        tmp_path,
        handler,
        consent=SequenceConsent(_consent_state(decision=ConsentDecision.DECLINED)),
    )
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.CONSENT_DISABLED
        assert requests == 0
        assert (await outbox.stats()).leased_events == 0
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_structured_consent_must_match_scope_and_current_notice(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    for state in (
        _consent_state(notice="reliability-older"),
        _consent_state(scope=TelemetryScope.GROWTH),
    ):
        outbox, uploader, client = await _uploader(
            tmp_path,
            handler,
            consent=lambda _scope, state=state: state,
        )
        try:
            await outbox.enqueue(_turn_event())
            result = await uploader.upload_once()
            assert result.status is UploadStatus.CONSENT_DISABLED
            assert (await outbox.stats()).leased_events == 0
        finally:
            await uploader.close()
            await client.aclose()
            await outbox.close()
    assert requests == 0


@pytest.mark.parametrize(("status_code", "reason"), [(409, "conflict"), (422, "contract")])
async def test_permanent_status_quarantines_without_payload(
    tmp_path: Path,
    status_code: int,
    reason: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="synthetic-secret-response-body")

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.QUARANTINED
        assert result.http_status == status_code
        assert (await outbox.stats()).pending_events == 0
        rejected = await outbox.list_rejections()
        assert rejected[0].reason == reason
        assert not hasattr(rejected[0], "payload")
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_429_honors_retry_after_and_keeps_payload_for_retry(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    outbox, uploader, client = await _uploader(tmp_path, handler, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.RETRY_SCHEDULED
        assert result.retry_after_ms == 30_000
        assert await outbox.claim_batch() is None
        clock.advance(30_000)
        assert await outbox.claim_batch() is not None
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_5xx_uses_exponential_equal_jitter(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    outbox, uploader, client = await _uploader(
        tmp_path,
        handler,
        clock=clock,
        random_value=lambda: 0.5,
    )
    try:
        await outbox.enqueue(_turn_event())
        first = await uploader.upload_once()
        assert first.retry_after_ms == 1_500

        clock.advance(1_500)
        second = await uploader.upload_once()
        assert second.retry_after_ms == 3_000
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_network_error_is_retried(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport failure", request=request)

    outbox, uploader, client = await _uploader(tmp_path, handler, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        result = await uploader.upload_once()

        assert result.status is UploadStatus.RETRY_SCHEDULED
        assert result.retry_after_ms == 1_500
        assert (await outbox.stats()).pending_events == 1
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_payload_and_response_are_never_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="synthetic-secret-response")

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event(app_version="synthetic-secret-payload"))
        with caplog.at_level(logging.DEBUG):
            await uploader.upload_once()

        rendered = caplog.text
        assert "synthetic-secret-payload" not in rendered
        assert "synthetic-secret-response" not in rendered
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_stale_notice_is_deleted_without_request_and_current_peer_is_released(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _accepted_response(request)

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event(1, notice="reliability-older"))
        await outbox.enqueue(_turn_event(2))

        first = await uploader.upload_once()
        assert first.status is UploadStatus.STALE_NOTICE_DROPPED
        assert first.event_count == 1
        assert requests == 0
        stats = await outbox.stats()
        assert stats.pending_events == 1
        assert stats.leased_events == 0

        second = await uploader.upload_once()
        assert second.status is UploadStatus.UPLOADED
        assert second.event_count == 1
        assert requests == 1
        assert (await outbox.stats()).pending_events == 0
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_tampered_database_payload_is_deleted_without_http_request(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event())
        with sqlite3.connect(outbox.database_path) as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET payload = ? WHERE event_id = ?",
                (b'{"private":"must-never-leave-device"}', _uuid(1)),
            )

        result = await uploader.upload_once()

        assert result.status is UploadStatus.IDLE
        assert requests == 0
        assert (await outbox.stats()).pending_events == 0
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_async_http_does_not_block_event_loop(tmp_path: Path) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return _accepted_response(request)

    outbox, uploader, client = await _uploader(tmp_path, handler)
    try:
        await outbox.enqueue(_turn_event())
        upload_task = asyncio.create_task(uploader.upload_once())
        await asyncio.wait_for(request_started.wait(), timeout=1)

        event_loop_progressed = False

        async def marker() -> None:
            nonlocal event_loop_progressed
            await asyncio.sleep(0)
            event_loop_progressed = True

        await marker()
        assert event_loop_progressed
        assert not upload_task.done()

        release_request.set()
        assert (await upload_task).status is UploadStatus.UPLOADED
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_revoke_transition_waits_for_already_started_request(
    tmp_path: Path,
) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    current = _consent_state()
    config = SimpleNamespace()
    coordinator = scope_consent_coordinator_for(
        config,
        state_provider=lambda _scope: current,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return _accepted_response(request)

    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    uploader = TelemetryUploader(
        outbox,
        base_url="https://telemetry.invalid",
        config=config,
        http_client=client,
    )
    try:
        await outbox.enqueue(_turn_event())
        upload_task = asyncio.create_task(uploader.upload_once())
        await asyncio.wait_for(request_started.wait(), timeout=1)

        async def revoke() -> None:
            nonlocal current
            async with coordinator.transition(TelemetryScope.RELIABILITY):
                current = _consent_state(decision=ConsentDecision.DECLINED)
                await outbox.clear_scope()

        revoke_task = asyncio.create_task(revoke())
        await asyncio.sleep(0)
        assert not revoke_task.done()
        release_request.set()
        assert (await upload_task).status is UploadStatus.UPLOADED
        await revoke_task
        assert (await outbox.stats()).pending_events == 0
        assert coordinator.revision(TelemetryScope.RELIABILITY) == 1
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


async def test_global_veto_waits_for_started_send_and_preserves_new_queue(
    tmp_path: Path,
) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    config = _live_config(tmp_path)
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return _accepted_response(request)

    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    uploader = TelemetryUploader(
        outbox,
        base_url="https://telemetry.invalid",
        config=config,
        http_client=client,
    )
    candidate = _live_config(tmp_path, global_disabled=True)

    async def enable_global_veto() -> None:
        async with global_network_observability_transition(config, candidate):
            config.privacy.disable_network_observability = True

    try:
        await outbox.enqueue(_turn_event())
        upload_task = asyncio.create_task(uploader.upload_once())
        await asyncio.wait_for(request_started.wait(), timeout=1)
        veto_task = asyncio.create_task(enable_global_veto())
        await asyncio.sleep(0)
        assert not veto_task.done()

        release_request.set()
        assert (await upload_task).status is UploadStatus.UPLOADED
        await veto_task

        await outbox.enqueue(_turn_event(2))
        assert (await uploader.upload_once()).status is UploadStatus.CONSENT_DISABLED
        assert (await outbox.stats()).pending_events == 1
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()


def test_uploader_constructor_has_no_private_consent_provider_escape_hatch() -> None:
    parameters = inspect.signature(TelemetryUploader).parameters

    assert "config" in parameters
    assert "consent_check" not in parameters
    assert "coordinator" not in parameters


@pytest.mark.parametrize(
    ("scope", "event_factory", "expected_count", "max_bytes", "expected_path"),
    [
        (
            TelemetryScope.RELIABILITY,
            _turn_event,
            _PROTOCOL_MANIFEST["batch_limits"]["reliability"]["max_events"],
            _PROTOCOL_MANIFEST["batch_limits"]["reliability"]["max_bytes"],
            "/v1/reliability/events",
        ),
        (
            TelemetryScope.GROWTH,
            _growth_event,
            _PROTOCOL_MANIFEST["batch_limits"]["growth"]["max_events"],
            _PROTOCOL_MANIFEST["batch_limits"]["growth"]["max_bytes"],
            "/v1/growth/events",
        ),
    ],
)
async def test_scope_batch_limits_are_enforced_on_wire(
    tmp_path: Path,
    scope: TelemetryScope,
    event_factory: Callable[[int], object],
    expected_count: int,
    max_bytes: int,
    expected_path: str,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _accepted_response(request)

    outbox, uploader, client = await _uploader(tmp_path, handler, scope=scope)
    try:
        for number in range(1, expected_count + 2):
            await outbox.enqueue(event_factory(number))  # type: ignore[arg-type]

        result = await uploader.upload_once()

        assert result.status is UploadStatus.UPLOADED
        assert result.event_count == expected_count
        assert len(seen[0].content) <= max_bytes
        assert seen[0].url.path == expected_path
        assert (await outbox.stats()).pending_events == 1
    finally:
        await uploader.close()
        await client.aclose()
        await outbox.close()
