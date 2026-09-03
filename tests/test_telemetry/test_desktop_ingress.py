from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.desktop_ingress import (
    DEFAULT_PROCESSING_STALE_AFTER,
    EARLY_SPOOL_MAX_BYTES,
    drain_desktop_early_spool,
)
from opensquilla.telemetry.outbox import EnqueueResult
from opensquilla.telemetry.recorder import TelemetryRecorder

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
EVENT_ID = "00000000-0000-4000-8000-000000000001"
SECOND_EVENT_ID = "00000000-0000-4000-8000-000000000002"
APP_SESSION_ID = "00000000-0000-4000-8000-000000000003"
ANALYTICS_USER_ID = "00000000-0000-4000-8000-000000000004"


class _RecorderOutbox:
    def __init__(
        self,
        scope: TelemetryScope,
        callback: Callable[[object], object | Awaitable[object]],
    ) -> None:
        self.scope = scope
        self._callback = callback

    async def enqueue(self, event: object, *, priority: object = None) -> EnqueueResult:
        del priority
        result = self._callback(event)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, EnqueueResult):
            return result
        return EnqueueResult.ENQUEUED


def _recorders(
    config: SimpleNamespace,
    callback: Callable[[object], object | Awaitable[object]],
) -> dict[TelemetryScope, TelemetryRecorder]:
    try:
        scope_consent_coordinator_for(
            config,
            state_provider=lambda scope: resolve_scope_consent(
                scope,
                config=config,
                env={},
            ),
        )
    except ValueError:
        pass
    return {
        scope: TelemetryRecorder(  # type: ignore[arg-type]
            _RecorderOutbox(scope, callback),
            config=config,
        )
        for scope in TelemetryScope
    }


def _config(
    *,
    reliability: bool | None = True,
    growth: bool | None = True,
    global_disabled: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        privacy=SimpleNamespace(
            disable_network_observability=global_disabled,
            reliability_diagnostics_enabled=reliability,
            reliability_notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
            reliability_consented_at_utc="2026-09-01T08:00:00Z",
            product_analytics_enabled=growth,
            product_analytics_notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
            product_analytics_consented_at_utc="2026-09-01T08:00:00Z",
        )
    )


def _reliability_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_name": "app_start_result",
        "event_version": 1,
        "event_id": EVENT_ID,
        "occurred_at_utc": "2026-09-01T08:00:00.000Z",
        "source": "desktop",
        "app_version": "0.5.3",
        "platform": "macos",
        "outcome": "success",
        "error_code": None,
        "duration_ms": 120,
        "consent_scope": "reliability",
        "notice_version": CURRENT_RELIABILITY_NOTICE_VERSION,
        "sample_rate": 1.0,
        "app_session_id": APP_SESSION_ID,
        "failure_stage": None,
    }
    payload.update(changes)
    return payload


def _growth_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_name": "first_app_ready",
        "event_version": 1,
        "event_id": SECOND_EVENT_ID,
        "occurred_at_utc": "2026-09-01T08:00:00.000Z",
        "source": "desktop",
        "app_version": "0.5.3",
        "platform": "windows",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        "sample_rate": 1,
        "analytics_user_id": ANALYTICS_USER_ID,
    }
    payload.update(changes)
    return payload


def _write_ready(
    root: Path,
    scope: str,
    payload: dict[str, object],
    *,
    raw: bytes | None = None,
    filename_event_id: str | None = None,
    modified_at: datetime | None = None,
) -> Path:
    directory = root / scope
    directory.mkdir(parents=True, exist_ok=True)
    event_id = filename_event_id or str(payload["event_id"])
    path = directory / f"{event_id}.ready"
    path.write_bytes(
        raw
        if raw is not None
        else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if modified_at is not None:
        timestamp = modified_at.timestamp()
        os.utime(path, (timestamp, timestamp))
    return path


async def test_valid_scope_is_claimed_enqueued_then_deleted(tmp_path: Path) -> None:
    reliability_path = _write_ready(tmp_path, "reliability", _reliability_payload())
    growth_path = _write_ready(tmp_path, "growth", _growth_payload())
    enqueued: list[object] = []

    async def _enqueue(event: object) -> None:
        event_id = str(getattr(event, "event_id"))
        scope = str(getattr(event, "consent_scope"))
        assert not (tmp_path / scope / f"{event_id}.ready").exists()
        assert list((tmp_path / scope).glob(f"{event_id}.processing.*"))
        enqueued.append(event)

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, _enqueue),
        env={},
        now=NOW,
    )

    assert stats.enqueued == 2
    assert stats.claimed == 2
    assert stats.rejected == 0
    assert len(enqueued) == 2
    assert not reliability_path.exists()
    assert not growth_path.exists()
    assert not list(tmp_path.rglob("*.processing.*"))


@pytest.mark.parametrize(
    ("config", "payload"),
    [
        (_config(reliability=False), _reliability_payload()),
        (_config(reliability=None), _reliability_payload()),
        (_config(growth=False), _growth_payload()),
        (
            _config(),
            _reliability_payload(notice_version="reliability-older"),
        ),
    ],
)
async def test_persistent_or_invalid_consent_deletes_event(
    tmp_path: Path,
    config: SimpleNamespace,
    payload: dict[str, object],
) -> None:
    scope = str(payload["consent_scope"])
    ready = _write_ready(tmp_path, scope, payload)
    received: list[object] = []

    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, received.append),
        env={},
        now=NOW,
    )

    assert stats.rejected == 1
    assert stats.retried == 0
    assert received == []
    assert not ready.exists()
    assert not list((tmp_path / scope).glob("*.processing.*"))


@pytest.mark.parametrize(
    ("config", "env", "transient_forced_off"),
    [
        (_config(), {"CI": "true"}, False),
        (_config(global_disabled=True), {}, False),
        (_config(), {}, True),
    ],
)
async def test_temporary_forced_off_restores_valid_authorized_event(
    tmp_path: Path,
    config: SimpleNamespace,
    env: dict[str, str],
    transient_forced_off: bool,
) -> None:
    ready = _write_ready(tmp_path, "reliability", _reliability_payload())
    received: list[object] = []

    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, received.append),
        env=env,
        now=NOW,
        transient_forced_off=transient_forced_off,
    )

    assert stats.retried == 1
    assert stats.rejected == 0
    assert received == []
    assert ready.exists()
    assert not list((tmp_path / "reliability").glob("*.processing.*"))


async def test_enqueue_failure_restores_ready_for_idempotent_retry(tmp_path: Path) -> None:
    ready = _write_ready(tmp_path, "reliability", _reliability_payload())

    def _fail(_event: object) -> None:
        raise RuntimeError("synthetic enqueue failure")

    failed_config = _config()
    failed = await drain_desktop_early_spool(
        tmp_path,
        config=failed_config,
        recorders=_recorders(failed_config, _fail),
        env={},
        now=NOW,
    )

    assert failed.retried == 1
    assert failed.enqueued == 0
    assert ready.exists()
    received: list[object] = []
    retried_config = _config()
    retried = await drain_desktop_early_spool(
        tmp_path,
        config=retried_config,
        recorders=_recorders(retried_config, received.append),
        env={},
        now=NOW + timedelta(seconds=1),
    )
    assert retried.enqueued == 1
    assert len(received) == 1
    assert not ready.exists()


async def test_capacity_eviction_restores_ready_for_idempotent_retry(tmp_path: Path) -> None:
    ready = _write_ready(tmp_path, "growth", _growth_payload())
    attempts = 0

    def _enqueue(_event: object) -> EnqueueResult:
        nonlocal attempts
        attempts += 1
        return EnqueueResult.EVICTED

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders={TelemetryScope.GROWTH: _recorders(config, _enqueue)[TelemetryScope.GROWTH]},
        env={},
        now=NOW,
    )

    assert attempts == 1
    assert stats.enqueued == 0
    assert stats.retried == 1
    assert ready.exists()


async def test_subset_recorder_drains_only_that_scope(tmp_path: Path) -> None:
    reliability = _write_ready(tmp_path, "reliability", _reliability_payload())
    growth = _write_ready(tmp_path, "growth", _growth_payload())
    received: list[object] = []
    config = _config()
    all_recorders = _recorders(config, received.append)

    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders={TelemetryScope.GROWTH: all_recorders[TelemetryScope.GROWTH]},
        env={},
        now=NOW,
    )

    assert stats.enqueued == 1
    assert [str(getattr(event, "consent_scope")) for event in received] == ["growth"]
    assert reliability.exists()
    assert not growth.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"event_name":"app_start_result","event_name":"app_start_result"}',
        json.dumps(_reliability_payload(event_name="future_event")).encode(),
        json.dumps(_reliability_payload(event_version=2)).encode(),
        b"not-json",
    ],
)
async def test_duplicate_keys_invalid_json_and_unknown_versions_are_rejected(
    tmp_path: Path,
    raw: bytes,
) -> None:
    ready = _write_ready(tmp_path, "reliability", _reliability_payload(), raw=raw)

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, lambda _event: None),
        env={},
        now=NOW,
    )

    assert stats.rejected == 1
    assert stats.enqueued == 0
    assert not ready.exists()


@pytest.mark.parametrize(
    ("scope", "payload", "filename_event_id"),
    [
        ("growth", _reliability_payload(), None),
        ("reliability", _reliability_payload(), SECOND_EVENT_ID),
    ],
)
async def test_scope_and_filename_identity_mismatches_are_rejected(
    tmp_path: Path,
    scope: str,
    payload: dict[str, object],
    filename_event_id: str | None,
) -> None:
    ready = _write_ready(
        tmp_path,
        scope,
        payload,
        filename_event_id=filename_event_id,
    )

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, lambda _event: None),
        env={},
        now=NOW,
    )

    assert stats.rejected == 1
    assert not ready.exists()


async def test_oversized_and_expired_ready_files_are_deleted(tmp_path: Path) -> None:
    oversized = _write_ready(
        tmp_path,
        "reliability",
        _reliability_payload(),
        raw=b"x" * (EARLY_SPOOL_MAX_BYTES + 1),
    )
    expired_payload = _growth_payload()
    expired = _write_ready(
        tmp_path,
        "growth",
        expired_payload,
        modified_at=NOW - timedelta(days=8),
    )

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, lambda _event: None),
        env={},
        now=NOW,
    )

    assert stats.rejected == 2
    assert not oversized.exists()
    assert not expired.exists()


async def test_stale_processing_is_recovered_but_live_claim_is_untouched(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "reliability"
    directory.mkdir()
    raw = json.dumps(_reliability_payload(), separators=(",", ":")).encode()
    stale = directory / f"{EVENT_ID}.processing.99991"
    stale.write_bytes(raw)
    stale_time = NOW - DEFAULT_PROCESSING_STALE_AFTER - timedelta(seconds=1)
    os.utime(stale, (stale_time.timestamp(), stale_time.timestamp()))
    live_payload = _reliability_payload(event_id=SECOND_EVENT_ID)
    live = directory / f"{SECOND_EVENT_ID}.processing.99992"
    live.write_text(json.dumps(live_payload), encoding="utf-8")
    live_time = NOW - timedelta(seconds=1)
    os.utime(live, (live_time.timestamp(), live_time.timestamp()))
    received: list[object] = []

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(config, received.append),
        env={},
        now=NOW,
    )

    assert stats.recovered_stale == 1
    assert stats.enqueued == 1
    assert len(received) == 1
    assert not stale.exists()
    assert live.exists()


async def test_symlink_event_is_rejected_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(_reliability_payload()), encoding="utf-8")
    directory = tmp_path / "reliability"
    directory.mkdir()
    link = directory / f"{EVENT_ID}.ready"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not available")

    config = _config()
    stats = await drain_desktop_early_spool(
        tmp_path,
        config=config,
        recorders=_recorders(
            config,
            lambda _event: pytest.fail("symlink target was enqueued"),
        ),
        env={},
        now=NOW,
    )

    assert stats.rejected == 1
    assert target.exists()
    assert not link.exists()


async def test_symlink_scope_directory_fails_closed_without_crossing_boundary(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "spool"
    root.mkdir()
    try:
        (root / "reliability").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")

    config = _config()
    stats = await drain_desktop_early_spool(
        root,
        config=config,
        recorders=_recorders(config, lambda _event: None),
        env={},
        now=NOW,
    )

    assert stats.rejected == 1
    assert list(outside.iterdir()) == []


def test_desktop_ingress_has_no_raw_enqueue_escape_hatch() -> None:
    parameters = inspect.signature(drain_desktop_early_spool).parameters

    assert "recorders" in parameters
    assert "enqueue" not in parameters
