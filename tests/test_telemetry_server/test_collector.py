from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Scope

from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.contracts.common import ConsentScope, EventSource
from opensquilla.telemetry.contracts.manifest import (
    MAX_GROWTH_BATCH_BYTES,
    MAX_RELIABILITY_BATCH_BYTES,
)
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.producer_auth import sign_producer_request
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.server.storage import TelemetryIngestStorage

_EVENT_ID = "00000000-0000-4000-8000-000000000011"
_SESSION_ID = "00000000-0000-4000-8000-000000000012"
_BATCH_ID = "00000000-0000-4000-8000-000000000013"
_SECOND_BATCH_ID = "00000000-0000-4000-8000-000000000014"
_ACQUISITION_ID = "00000000-0000-4000-8000-000000000015"
_ANALYTICS_USER_ID = "00000000-0000-4000-8000-000000000016"
_WEBSITE_SECRET = b"w" * 32


def _event(*, duration_ms: int = 120) -> dict[str, object]:
    return {
        "event_name": "app_start_result",
        "event_version": 1,
        "event_id": _EVENT_ID,
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "macos",
        "outcome": "success",
        "error_code": None,
        "duration_ms": duration_ms,
        "consent_scope": "reliability",
        "notice_version": "reliability-v1",
        "sample_rate": 1.0,
        "app_session_id": _SESSION_ID,
        "failure_stage": None,
    }


def _batch(*, batch_id: str = _BATCH_ID, duration_ms: int = 120) -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": batch_id,
        "sent_at_utc": "2026-09-01T01:03:00.000Z",
        "events": [_event(duration_ms=duration_ms)],
    }


def _turn_v3_batch() -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": _BATCH_ID,
        "sent_at_utc": "2026-09-01T01:03:00.000Z",
        "events": [
            {
                "event_name": "turn_result",
                "event_version": 3,
                "event_id": _EVENT_ID,
                "occurred_at_utc": "2026-09-01T01:02:03.456Z",
                "source": "gateway",
                "app_version": "1.2.3",
                "platform": "macos",
                "outcome": "timeout",
                "error_code": "provider_timeout",
                "duration_ms": 31_000,
                "consent_scope": "reliability",
                "notice_version": "reliability-v1",
                "sample_rate": 1.0,
                "app_session_id": _SESSION_ID,
                "ttft_ms": 400,
                "stall_count": 1,
                "stall_threshold_ms": 15_000,
                "surface": "tui",
                "execution_mode": "gateway",
                "failure_stage": "agent_execution",
            }
        ],
    }


def _growth_batch() -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": _BATCH_ID,
        "sent_at_utc": "2026-09-01T01:03:00.000Z",
        "events": [
            {
                "event_name": "landing_view",
                "event_version": 1,
                "event_id": _EVENT_ID,
                "occurred_at_utc": "2026-09-01T01:02:03.456Z",
                "source": "website",
                "app_version": None,
                "platform": "macos",
                "outcome": None,
                "error_code": None,
                "duration_ms": None,
                "consent_scope": "growth",
                "notice_version": "growth-v1",
                "sample_rate": 1,
                "acquisition_id": _ACQUISITION_ID,
            }
        ],
    }


def _client_growth_batch() -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": _BATCH_ID,
        "sent_at_utc": "2026-09-01T01:03:00.000Z",
        "events": [
            {
                "event_name": "first_app_ready",
                "event_version": 1,
                "event_id": _EVENT_ID,
                "occurred_at_utc": "2026-09-01T01:02:03.456Z",
                "source": "desktop",
                "app_version": "1.2.3",
                "platform": "macos",
                "outcome": None,
                "error_code": None,
                "duration_ms": None,
                "consent_scope": "growth",
                "notice_version": "growth-v1",
                "sample_rate": 1,
                "analytics_user_id": _ANALYTICS_USER_ID,
            }
        ],
    }


def _body(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _settings(tmp_path: Path) -> CollectorSettings:
    return CollectorSettings(
        scope=ConsentScope.RELIABILITY,
        database_path=tmp_path / "reliability.sqlite3",
    )


def _growth_settings(tmp_path: Path) -> CollectorSettings:
    return CollectorSettings(
        scope=ConsentScope.GROWTH,
        database_path=tmp_path / "growth.sqlite3",
        producer_secrets={EventSource.WEBSITE: _WEBSITE_SECRET},
    )


def _signed_growth_headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        **sign_producer_request(
            secret=_WEBSITE_SECRET,
            producer=EventSource.WEBSITE,
            timestamp=int(time.time()),
            body=body,
        ),
    }


def _counts(database: Path) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("SELECT count(*) FROM ingest_batches").fetchone()[0],
            connection.execute("SELECT count(*) FROM events").fetchone()[0],
        )


def test_settings_are_derived_from_manifest_and_scope(tmp_path: Path) -> None:
    reliability = _settings(tmp_path)
    growth = CollectorSettings(
        scope=ConsentScope.GROWTH,
        database_path=tmp_path / "growth.sqlite3",
    )

    assert reliability.endpoint_path == "/v1/reliability/events"
    assert reliability.max_body_bytes == MAX_RELIABILITY_BATCH_BYTES
    assert growth.endpoint_path == "/v1/growth/events"
    assert growth.max_body_bytes == MAX_GROWTH_BATCH_BYTES


def test_healthz_is_ready_without_disclosing_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "scope": "reliability",
        "schema_version": 1,
        "protocol_fingerprint": TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    }
    assert str(settings.database_path) not in response.text


def test_only_the_exact_scope_route_is_registered(tmp_path: Path) -> None:
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        assert client.post("/v1/growth/events", json={}).status_code == 404
        assert client.post("/v1/reliability/events/", json={}).status_code == 404
        assert client.get("/v1/reliability/events").status_code == 405
        assert client.get("/healthz/").status_code == 404


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/problem+json", "text/json"],
)
def test_rejects_non_json_content_type_before_parsing(
    tmp_path: Path, content_type: str | None
) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/reliability/events",
            content=_body(_batch()),
            headers=headers,
        )

    assert response.status_code == 415
    assert response.json() == {"ok": False, "error": "unsupported_media_type"}


def test_accepts_json_content_type_parameters_and_returns_strict_receipt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        first = client.post(
            settings.endpoint_path,
            content=_body(_batch()),
            headers={"Content-Type": "Application/JSON; Charset=UTF-8"},
        )
        retry = client.post(
            settings.endpoint_path,
            content=_body(_batch()),
            headers={"Content-Type": "application/json"},
        )

    assert first.status_code == 202
    assert first.json() == {
        "ok": True,
        "batch_id": _BATCH_ID,
        "accepted": 1,
        "duplicates": 0,
    }
    assert retry.status_code == 202
    assert retry.json() == {
        "ok": True,
        "batch_id": _BATCH_ID,
        "accepted": 0,
        "duplicates": 1,
    }
    assert _counts(settings.database_path) == (1, 1)


def test_reliability_collector_accepts_turn_v3_failure_stage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=_body(_turn_v3_batch()),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 202
    with sqlite3.connect(settings.database_path) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT payload_json FROM events WHERE event_id = ?",
                (_EVENT_ID,),
            ).fetchone()[0]
        )
    assert stored["failure_stage"] == "agent_execution"
    assert stored["error_code"] == "provider_timeout"


def test_growth_process_accepts_only_growth_contract(tmp_path: Path) -> None:
    settings = _growth_settings(tmp_path)
    body = _body(_growth_batch())
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=body,
            headers=_signed_growth_headers(body),
        )

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "batch_id": _BATCH_ID,
        "accepted": 1,
        "duplicates": 0,
    }
    assert _counts(settings.database_path) == (1, 1)


def test_unsigned_client_owned_growth_source_is_accepted(tmp_path: Path) -> None:
    settings = _growth_settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=_body(_client_growth_batch()),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 202
    assert _counts(settings.database_path) == (1, 1)


def test_unsigned_server_owned_growth_source_is_rejected(tmp_path: Path) -> None:
    settings = _growth_settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=_body(_growth_batch()),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "producer_unauthorized"}
    assert _counts(settings.database_path) == (0, 0)


def test_server_owned_signature_is_bound_to_exact_body_and_source(tmp_path: Path) -> None:
    settings = _growth_settings(tmp_path)
    signed_body = _body(_growth_batch())
    tampered = _growth_batch()
    tampered["batch_id"] = _SECOND_BATCH_ID
    with TestClient(create_collector_app(settings)) as client:
        body_mismatch = client.post(
            settings.endpoint_path,
            content=_body(tampered),
            headers=_signed_growth_headers(signed_body),
        )
        source_mismatch = client.post(
            settings.endpoint_path,
            content=_body(_client_growth_batch()),
            headers=_signed_growth_headers(_body(_client_growth_batch())),
        )

    assert body_mismatch.status_code == 401
    assert body_mismatch.json() == {"ok": False, "error": "producer_unauthorized"}
    assert source_mismatch.status_code == 403
    assert source_mismatch.json() == {
        "ok": False,
        "error": "producer_source_mismatch",
    }
    assert _counts(settings.database_path) == (0, 0)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"batch_version":1}',
        b'{"batch_version":1,"batch_version":1}',
    ],
)
def test_maps_every_wire_or_schema_rejection_to_422(tmp_path: Path, body: bytes) -> None:
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/reliability/events",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "schema_invalid"}


def test_enforces_streaming_limit_without_buffering_past_boundary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def oversized_chunks() -> Iterator[bytes]:
        yield b"{"
        yield b" " * (settings.max_body_bytes - 1)
        yield b"x"

    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=oversized_chunks(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "body_too_large"}


def test_rejects_oversized_declared_length_before_schema_parsing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(settings.max_body_bytes + 1),
            },
        )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "body_too_large"}


@pytest.mark.parametrize("content_length", ["-1", "invalid", " 12", "+12"])
def test_rejects_invalid_content_length(tmp_path: Path, content_length: str) -> None:
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/reliability/events",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": content_length,
            },
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_content_length"}


@pytest.mark.asyncio
async def test_client_disconnect_is_400_not_body_too_large(tmp_path: Path) -> None:
    app = create_collector_app(_settings(tmp_path))
    sent: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/reliability/events",
        "raw_path": b"/v1/reliability/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 123),
        "server": ("test", 80),
        "state": {},
    }

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert start["status"] == 400
    assert json.loads(body) == {"ok": False, "error": "client_disconnected"}


def test_accepts_a_valid_body_exactly_at_manifest_limit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    body = _body(_batch())
    body += b" " * (settings.max_body_bytes - len(body))

    with TestClient(create_collector_app(settings)) as client:
        response = client.post(
            settings.endpoint_path,
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert len(body) == settings.max_body_bytes
    assert response.status_code == 202


@pytest.mark.parametrize("conflict_kind", ["batch", "event"])
def test_hash_conflicts_return_409_and_do_not_add_rows(
    tmp_path: Path, conflict_kind: str
) -> None:
    settings = _settings(tmp_path)
    first = _batch()
    conflicting = deepcopy(first)
    conflicting["events"][0]["duration_ms"] = 999  # type: ignore[index]
    if conflict_kind == "event":
        conflicting["batch_id"] = _SECOND_BATCH_ID

    with TestClient(create_collector_app(settings)) as client:
        accepted = client.post(
            settings.endpoint_path,
            content=_body(first),
            headers={"Content-Type": "application/json"},
        )
        rejected = client.post(
            settings.endpoint_path,
            content=_body(conflicting),
            headers={"Content-Type": "application/json"},
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 409
    assert rejected.json() == {"ok": False, "error": "identifier_conflict"}
    assert _counts(settings.database_path) == (1, 1)


def test_rejections_never_log_or_echo_payload_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "PROMPT-SHOULD-NEVER-APPEAR-42"
    payload = _batch()
    payload["events"][0]["prompt"] = secret  # type: ignore[index]

    caplog.set_level(logging.INFO, logger="opensquilla.telemetry.server.collector")
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/reliability/events",
            content=_body(payload),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text


def test_internal_exception_text_is_never_logged_or_echoed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "STORAGE-EXCEPTION-SHOULD-NEVER-APPEAR-42"

    async def fail_ingest(
        _storage: TelemetryIngestStorage, _batch: object
    ) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(TelemetryIngestStorage, "ingest", fail_ingest)
    caplog.set_level(logging.INFO, logger="opensquilla.telemetry.server.collector")
    with TestClient(create_collector_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/reliability/events",
            content=_body(_batch()),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "internal_error"}
    assert secret not in response.text
    assert secret not in caplog.text
