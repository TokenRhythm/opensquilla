from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.server import storage as storage_module
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.dashboard_queries import DashboardQueries, UtcCohortWindow
from opensquilla.telemetry.server.settings import CollectorSettings

_TIMESTAMP = "2026-09-01T01:00:00.000Z"
_SOURCE_COMMIT = "a" * 40


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _event(number: int, scope: ConsentScope, platform: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_name": "app_start_result"
        if scope is ConsentScope.RELIABILITY
        else "first_app_ready",
        "event_version": 1,
        "event_id": _uuid(number),
        "occurred_at_utc": _TIMESTAMP,
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": platform,
        "outcome": "success" if scope is ConsentScope.RELIABILITY else None,
        "error_code": None,
        "duration_ms": 120 if scope is ConsentScope.RELIABILITY else None,
        "consent_scope": scope.value,
        "notice_version": "reliability-v1" if scope is ConsentScope.RELIABILITY else "growth-v2",
        "sample_rate": 1,
    }
    if scope is ConsentScope.RELIABILITY:
        payload.update(app_session_id=_uuid(900), failure_stage=None)
    else:
        payload["analytics_user_id"] = _uuid(901)
    return payload


def _batch(number: int, events: list[dict[str, object]]) -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": _uuid(number),
        "sent_at_utc": "2026-09-01T02:00:00.000Z",
        "events": events,
    }


def _rows(path: Path, table: str) -> list[tuple]:
    assert table in {"events", "ingest_batches"}
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def _legacy_database(settings: CollectorSettings, batch: dict[str, object]) -> None:
    # Seed a v1 event through the existing canonical writer, then restore the
    # exact older schema/metadata that predate the client-launch unique index.
    with TestClient(create_collector_app(settings)) as client:
        response = client.post(settings.endpoint_path, json=batch)
        assert response.status_code == 202
        assert response.json()["accepted"] == 1
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("DROP INDEX idx_events_client_launch_user_surface_day")
        connection.execute(
            "UPDATE meta SET protocol_fingerprint = ? WHERE singleton = 1",
            (storage_module._LEGACY_PROTOCOL_FINGERPRINT_SHA256,),
        )
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        assert objects == set(storage_module._LEGACY_EXPECTED_SCHEMA_SQL)


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
def test_legacy_collector_upgrade_preserves_history_and_accepts_current_metrics(
    tmp_path: Path,
    platform: str,
) -> None:
    settings = {
        scope: CollectorSettings(scope=scope, database_path=tmp_path / f"{scope.value}.sqlite3")
        for scope in ConsentScope
    }
    legacy_batches = {
        scope: _batch(100 + index, [_event(1 + index, scope, platform)])
        for index, scope in enumerate(ConsentScope)
    }
    historical_rows = {}
    for scope, config in settings.items():
        _legacy_database(config, legacy_batches[scope])
        historical_rows[scope] = _rows(config.database_path, "events")

    growth_events = []
    for number, name in enumerate(["metaskill_usage", "coding_mode_usage"], start=10):
        event = _event(number, ConsentScope.GROWTH, platform)
        event.update(event_name=name, source="runtime")
        growth_events.append(event)
    for number, (surface, entrypoint) in enumerate([("tui", "chat"), ("cli", "agent")], start=12):
        event = _event(number, ConsentScope.GROWTH, platform)
        event.update(
            event_name="client_launch",
            source="gateway",
            surface=surface,
            entrypoint=entrypoint,
            execution_mode="gateway",
        )
        growth_events.append(event)
    turn = _event(20, ConsentScope.RELIABILITY, platform)
    turn.update(
        event_name="turn_result",
        event_version=3,
        source="gateway",
        app_version=f"2.0.0+source.{_SOURCE_COMMIT}",
        outcome="timeout",
        error_code="provider_timeout",
        duration_ms=31_000,
        ttft_ms=400,
        stall_count=1,
        stall_threshold_ms=15_000,
        surface="tui",
        execution_mode="gateway",
        failure_stage="agent_execution",
    )
    current_events = {ConsentScope.GROWTH: growth_events, ConsentScope.RELIABILITY: [turn]}

    for scope, config in settings.items():
        with TestClient(create_collector_app(config)) as client:
            health = client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["protocol_fingerprint"] == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
            assert _rows(config.database_path, "events") == historical_rows[scope]
            with sqlite3.connect(config.database_path) as connection:
                assert connection.execute(
                    "SELECT schema_version, scope, protocol_fingerprint FROM meta"
                ).fetchone() == (1, scope.value, TELEMETRY_PROTOCOL_FINGERPRINT_SHA256)
                assert (
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = ?",
                        ("idx_events_client_launch_user_surface_day",),
                    ).fetchone()
                    is not None
                )

            batch = _batch(200, current_events[scope])
            first = client.post(config.endpoint_path, json=batch)
            assert first.status_code == 202
            assert (first.json()["accepted"], first.json()["duplicates"]) == (
                len(current_events[scope]),
                0,
            )
            for retry in [batch, {**batch, "batch_id": _uuid(201)}, legacy_batches[scope]]:
                receipt = client.post(config.endpoint_path, json=retry)
                assert receipt.status_code == 202
                assert (receipt.json()["accepted"], receipt.json()["duplicates"]) == (
                    0,
                    len(retry["events"]),
                )

            if scope is ConsentScope.GROWTH:
                repeated_launches = [
                    {**event, "event_id": _uuid(30 + index)}
                    for index, event in enumerate(growth_events[2:])
                ]
                receipt = client.post(config.endpoint_path, json=_batch(202, repeated_launches))
                assert receipt.status_code == 202
                assert (receipt.json()["accepted"], receipt.json()["duplicates"]) == (0, 2)

            before = {
                table: _rows(config.database_path, table) for table in ["events", "ingest_batches"]
            }
            invalid = deepcopy(batch)
            invalid["batch_id"] = _uuid(203)
            invalid["events"][0]["unexpected_attribute"] = "synthetic"
            rejected = client.post(config.endpoint_path, json=invalid)
            assert rejected.status_code == 422
            assert rejected.json() == {"ok": False, "error": "schema_invalid"}
            assert {table: _rows(config.database_path, table) for table in before} == before
            assert len(before["events"]) == 1 + len(current_events[scope])
            assert historical_rows[scope][0] in before["events"]

    queries = DashboardQueries(
        reliability_db_path=settings[ConsentScope.RELIABILITY].database_path,
        growth_db_path=settings[ConsentScope.GROWTH].database_path,
    )
    summary = queries.summary(UtcCohortWindow.from_dates("2026-09-01", "2026-09-01"))
    reliability = summary["reliability"]
    assert reliability["appStart"]["estimatedEvents"] == 1
    assert reliability["turns"]["estimatedEvents"] == 1
    assert reliability["turns"]["byFailureStage"] == [
        {"dimension": "agent_execution", "estimatedEvents": 1},
    ]
    assert reliability["turns"]["byErrorCode"] == [
        {"dimension": "provider_timeout", "estimatedEvents": 1},
    ]
    assert reliability["byVersion"] == [
        {
            "appVersion": "1.2.3",
            "sourceCommitId": None,
            "estimatedEvents": 1,
            "estimatedIssues": 0,
            "issueRate": 0.0,
        },
        {
            "appVersion": "2.0.0",
            "sourceCommitId": _SOURCE_COMMIT,
            "estimatedEvents": 1,
            "estimatedIssues": 1,
            "issueRate": 1.0,
        },
    ]
    growth = summary["growth"]
    assert growth["metaskillUsage"]["totalUses"] == 1
    assert growth["codingModeUsage"]["totalUses"] == 1
    assert growth["clientUsage"]["totals"]["tuiUsers"] == 1
    assert growth["clientUsage"]["totals"]["cliUsers"] == 1
    assert growth["clientUsage"]["totals"]["terminalUsers"] == 1
