from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.server.dashboard_queries import (
    DashboardDataError,
    DashboardQueries,
    UtcCohortWindow,
)
from opensquilla.telemetry.server.storage import (
    _COMPATIBLE_PREVIOUS_PROTOCOL_FINGERPRINTS,
    _LEGACY_EXPECTED_SCHEMA_SQL,
    _LEGACY_PROTOCOL_FINGERPRINT_SHA256,
    TelemetryIngestStorage,
)

_EVENT_COLUMNS = (
    "event_id",
    "payload_sha256",
    "event_name",
    "event_version",
    "occurred_at_utc",
    "source",
    "app_version",
    "platform",
    "outcome",
    "error_code",
    "duration_ms",
    "sample_rate",
    "notice_version",
    "app_session_id",
    "acquisition_id",
    "analytics_user_id",
    "payload_json",
    "first_batch_id",
    "received_at_utc",
)
_LEGACY_BATCH_ID = "00000000-0000-4000-8000-000000000100"


def _database(tmp_path: Path, scope: TelemetryScope) -> Path:
    path = tmp_path / f"{scope.value}.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=1;
        CREATE TABLE meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            schema_version INTEGER NOT NULL,
            scope TEXT NOT NULL,
            protocol_fingerprint TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE ingest_batches (
            batch_id TEXT PRIMARY KEY,
            body_sha256 TEXT NOT NULL,
            sent_at_utc TEXT NOT NULL,
            received_at_utc TEXT NOT NULL,
            accepted_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            payload_sha256 TEXT NOT NULL,
            event_name TEXT NOT NULL,
            event_version INTEGER NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            source TEXT NOT NULL,
            app_version TEXT,
            platform TEXT NOT NULL,
            outcome TEXT,
            error_code TEXT,
            duration_ms INTEGER,
            sample_rate REAL NOT NULL,
            notice_version TEXT NOT NULL,
            app_session_id TEXT,
            acquisition_id TEXT,
            analytics_user_id TEXT,
            payload_json TEXT NOT NULL,
            first_batch_id TEXT,
            received_at_utc TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO meta(
            singleton, schema_version, scope, protocol_fingerprint, created_at_utc
        ) VALUES (1, 1, ?, ?, '2026-09-01T00:00:00.000Z')
        """,
        (scope.value, TELEMETRY_PROTOCOL_FINGERPRINT_SHA256),
    )
    connection.commit()
    connection.close()
    return path


def _legacy_database(tmp_path: Path, scope: TelemetryScope) -> Path:
    path = tmp_path / f"legacy-{scope.value}.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in _LEGACY_EXPECTED_SCHEMA_SQL.values():
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            """
            INSERT INTO meta(singleton, schema_version, scope, protocol_fingerprint,
                             created_at_utc)
            VALUES (1, 1, ?, ?, '2026-09-01T00:00:00.000Z')
            """,
            (scope.value, _LEGACY_PROTOCOL_FINGERPRINT_SHA256),
        )
        connection.execute(
            """
            INSERT INTO ingest_batches(batch_id, body_sha256, sent_at_utc,
                                       received_at_utc, accepted_count, duplicate_count)
            VALUES (?, ?, '2026-09-01T00:00:00.000Z', '2026-09-01T00:00:00.000Z', 0, 0)
            """,
            (_LEGACY_BATCH_ID, "a" * 64),
        )
    return path


def _insert(
    path: Path,
    *,
    sequence: int,
    event_id: str | None = None,
    first_batch_id: str | None = None,
    event_name: str,
    occurred_at: str,
    event_version: int = 1,
    source: str = "desktop",
    app_version: str | None = "1.0.0",
    outcome: str | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    sample_rate: float = 1.0,
    app_session_id: str | None = None,
    acquisition_id: str | None = None,
    analytics_user_id: str | None = None,
    notice_version: str = "test-v1",
    payload: dict[str, Any] | None = None,
) -> None:
    values: dict[str, object] = {
        "event_id": event_id or f"event-{sequence:04d}",
        "payload_sha256": "a" * 64,
        "event_name": event_name,
        "event_version": event_version,
        "occurred_at_utc": occurred_at,
        "source": source,
        "app_version": app_version,
        "platform": "macos",
        "outcome": outcome,
        "error_code": error_code,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "notice_version": notice_version,
        "app_session_id": app_session_id,
        "acquisition_id": acquisition_id,
        "analytics_user_id": analytics_user_id,
        "payload_json": json.dumps(payload or {}, separators=(",", ":")),
        "first_batch_id": first_batch_id,
        "received_at_utc": occurred_at,
    }
    placeholders = ",".join("?" for _ in _EVENT_COLUMNS)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO events({','.join(_EVENT_COLUMNS)}) VALUES ({placeholders})",
            tuple(values[column] for column in _EVENT_COLUMNS),
        )


def _queries(tmp_path: Path) -> tuple[DashboardQueries, Path, Path]:
    reliability = _database(tmp_path, TelemetryScope.RELIABILITY)
    growth = _database(tmp_path, TelemetryScope.GROWTH)
    return (
        DashboardQueries(
            reliability_db_path=reliability,
            growth_db_path=growth,
        ),
        reliability,
        growth,
    )


def _window() -> UtcCohortWindow:
    return UtcCohortWindow.from_dates("2026-09-01", "2026-09-30")


def _assert_no_sensitive_output(value: object) -> None:
    serialized = json.dumps(value, sort_keys=True)
    for forbidden in (
        "event_id",
        "app_session_id",
        "acquisition_id",
        "analytics_user_id",
        "payload_json",
        "event-",
        "session-",
        "acquisition-",
        "analytics-",
    ):
        assert forbidden not in serialized


def test_product_activity_deduplicates_surfaces_and_repeated_days(tmp_path: Path) -> None:
    queries, _, growth = _queries(tmp_path)
    events = [
        ("2026-09-01", "analytics-a", "desktop"),
        ("2026-09-01", "analytics-a", "web"),
        ("2026-09-01", "analytics-a", "tui"),
        ("2026-09-01", "analytics-a", "cli"),
        ("2026-09-01", "analytics-a", "cli"),
        ("2026-09-02", "analytics-a", "web"),
        ("2026-09-02", "analytics-b", "cli"),
        ("2026-09-03", "analytics-b", "desktop"),
    ]
    for sequence, (day, user, surface) in enumerate(events, start=1):
        _insert(
            growth, sequence=sequence, event_name="product_active",
            occurred_at=f"{day}T01:00:00.000Z", source="gateway",
            analytics_user_id=user, notice_version="growth-v2", payload={"surface": surface},
        )

    result = queries.growth(UtcCohortWindow.from_dates("2026-09-01", "2026-09-03"))[
        "productActivity"
    ]

    assert result == {
        "asOfDate": "2026-09-03",
        "mauStartDate": "2026-08-05",
        "dau": 1,
        "mau": 2,
        "dailyTrend": [
            {"period": "2026-09-01", "dau": 1, "mau": 1},
            {"period": "2026-09-02", "dau": 2, "mau": 2},
            {"period": "2026-09-03", "dau": 1, "mau": 2},
        ],
    }
    _assert_no_sensitive_output(result)


def test_product_activity_rolling_window_includes_history_before_selected_start(
    tmp_path: Path,
) -> None:
    queries, _, growth = _queries(tmp_path)
    events = [
        ("2026-08-30T23:59:59.999Z", "analytics-too-old"),
        ("2026-08-31T00:00:00.000Z", "analytics-expiring"),
        ("2026-09-01T00:00:00.000Z", "analytics-earliest"),
        ("2026-09-29T00:00:00.000Z", "analytics-returning"),
        ("2026-09-30T00:00:00.000Z", "analytics-returning"),
        ("2026-09-30T23:59:59.999Z", "analytics-new"),
        ("2026-10-01T00:00:00.000Z", "analytics-future"),
    ]
    for sequence, (occurred_at, user) in enumerate(events, start=1):
        _insert(
            growth, sequence=sequence, event_name="product_active", occurred_at=occurred_at,
            source="gateway", analytics_user_id=user, notice_version="growth-v2",
            payload={"surface": "desktop"},
        )

    result = queries.growth(UtcCohortWindow.from_dates("2026-09-29", "2026-09-30"))[
        "productActivity"
    ]
    assert result["dailyTrend"] == [
        {"period": "2026-09-29", "dau": 1, "mau": 3},
        {"period": "2026-09-30", "dau": 2, "mau": 3},
    ]
    assert (result["dau"], result["mau"]) == (2, 3)
    assert result["mauStartDate"] == "2026-09-01"
    one_day = queries.growth(UtcCohortWindow.from_dates("2026-09-30", "2026-09-30"))[
        "productActivity"
    ]
    assert (one_day["dau"], one_day["mau"]) == (2, 3)
    assert one_day["dailyTrend"] == result["dailyTrend"][-1:]
    _assert_no_sensitive_output(result)


@pytest.mark.parametrize("legacy_rows", [False, True])
def test_product_activity_is_zero_filled_without_new_activity_events(
    tmp_path: Path, legacy_rows: bool,
) -> None:
    queries, _, growth = _queries(tmp_path)
    if legacy_rows:
        for sequence, name in enumerate(
            ["client_launch", "first_app_ready", "first_turn_started", "first_turn_result",
             "metaskill_usage", "coding_mode_usage"], start=1,
        ):
            _insert(
                growth, sequence=sequence, event_name=name,
                occurred_at="2026-09-30T01:00:00.000Z", source="gateway",
                analytics_user_id="analytics-legacy", notice_version="growth-v2",
                payload={"surface": "cli", "entrypoint": "agent"},
            )
    result = queries.growth(UtcCohortWindow.from_dates("2026-09-29", "2026-09-30"))
    assert result["productActivity"] == {
        "asOfDate": "2026-09-30",
        "mauStartDate": "2026-09-01",
        "dau": 0,
        "mau": 0,
        "dailyTrend": [
            {"period": "2026-09-29", "dau": 0, "mau": 0},
            {"period": "2026-09-30", "dau": 0, "mau": 0},
        ],
    }
    assert result["clientUsage"]["totals"]["cliUsers"] == int(legacy_rows)


def test_database_connections_are_uri_read_only_and_query_only(tmp_path: Path) -> None:
    queries, _, _ = _queries(tmp_path)

    with queries._open(TelemetryScope.RELIABILITY) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write(value TEXT)")


async def test_queries_open_the_locked_collector_schema_without_migration(
    tmp_path: Path,
) -> None:
    reliability_path = tmp_path / "locked-reliability.sqlite3"
    growth_path = tmp_path / "locked-growth.sqlite3"
    reliability = await TelemetryIngestStorage.open(
        reliability_path,
        ConsentScope.RELIABILITY,
    )
    growth = await TelemetryIngestStorage.open(growth_path, ConsentScope.GROWTH)
    await reliability.close()
    await growth.close()
    reliability_before = reliability_path.read_bytes()
    growth_before = growth_path.read_bytes()

    result = DashboardQueries(
        reliability_db_path=reliability_path,
        growth_db_path=growth_path,
    ).summary(_window())

    assert result["reliability"]["appStart"]["estimatedEvents"] == 0
    assert result["growth"]["acquisition"]["stages"][0]["deduplicatedCount"] == 0
    assert reliability_path.read_bytes() == reliability_before
    assert growth_path.read_bytes() == growth_before


def test_legacy_collector_schema_aggregates_both_scopes_without_migration(tmp_path: Path) -> None:
    reliability = _legacy_database(tmp_path, TelemetryScope.RELIABILITY)
    growth = _legacy_database(tmp_path, TelemetryScope.GROWTH)
    for sequence, outcome in enumerate(("success", "fail"), start=1):
        _insert(
            reliability,
            sequence=sequence,
            event_id=str(UUID(int=sequence)),
            first_batch_id=_LEGACY_BATCH_ID,
            event_name="app_start_result",
            occurred_at="2026-09-01T01:00:00.000Z",
            outcome=outcome,
            app_session_id="synthetic-session",
            duration_ms=100,
            sample_rate=0.5 if outcome == "success" else 1,
        )
    for sequence, (event_name, outcome) in enumerate(
        (
            ("onboarding_result", "completed"),
            ("first_app_ready", None),
            ("first_turn_started", None),
            ("first_turn_result", "success"),
        ),
        start=1,
    ):
        _insert(
            growth,
            sequence=sequence,
            event_id=str(UUID(int=sequence)),
            first_batch_id=_LEGACY_BATCH_ID,
            event_name=event_name,
            occurred_at=f"2026-09-01T01:0{sequence}:00.000Z",
            outcome=outcome,
            analytics_user_id="synthetic-user",
        )
    # Older stores have no per-day launch index. Repeated launches must still
    # count the same user once for each terminal in read-only aggregation.
    for sequence, surface in enumerate(("tui", "cli", "cli"), start=5):
        _insert(
            growth,
            sequence=sequence,
            event_id=str(UUID(int=sequence)),
            first_batch_id=_LEGACY_BATCH_ID,
            event_name="client_launch",
            occurred_at="2026-09-01T01:05:00.000Z",
            analytics_user_id="synthetic-user",
            payload={"surface": surface, "entrypoint": "chat"},
        )
    before = {path: path.read_bytes() for path in (reliability, growth)}

    result = DashboardQueries(
        reliability_db_path=reliability,
        growth_db_path=growth,
    ).summary(_window())

    assert result["reliability"]["appStart"]["estimatedEvents"] == 3
    assert result["reliability"]["appStart"]["estimatedSuccesses"] == 2
    activation = result["growth"]["activation"]
    assert [stage["deduplicatedCount"] for stage in activation["stages"]] == [1, 1, 1, 1]
    assert [transition["dropoffRate"] for transition in activation["transitions"]] == [0, 0, 0]
    totals = result["growth"]["clientUsage"]["totals"]
    assert (totals["tuiUsers"], totals["cliUsers"]) == (1, 1)
    assert result["growth"]["metaskillUsage"]["totalUses"] == 0
    assert result["growth"]["codingModeUsage"]["totalUses"] == 0
    for path in (reliability, growth):
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT protocol_fingerprint FROM meta").fetchone() == (
                _LEGACY_PROTOCOL_FINGERPRINT_SHA256,
            )
            assert connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name = 'idx_events_client_launch_user_surface_day'"
            ).fetchone() is None
        assert path.read_bytes() == before[path]
    _assert_no_sensitive_output(result)


@pytest.mark.parametrize("scope", (TelemetryScope.RELIABILITY, TelemetryScope.GROWTH))
@pytest.mark.parametrize("mismatch", ("scope", "fingerprint", "version", "column"))
def test_legacy_collector_compatibility_keeps_schema_checks(
    tmp_path: Path, scope: TelemetryScope, mismatch: str
) -> None:
    reliability = _legacy_database(tmp_path, TelemetryScope.RELIABILITY)
    growth = _legacy_database(tmp_path, TelemetryScope.GROWTH)
    path = reliability if scope is TelemetryScope.RELIABILITY else growth
    with sqlite3.connect(path) as connection:
        if mismatch == "scope":
            other_scope = "growth" if scope is TelemetryScope.RELIABILITY else "reliability"
            connection.execute("UPDATE meta SET scope = ?", (other_scope,))
        elif mismatch == "fingerprint":
            connection.execute("UPDATE meta SET protocol_fingerprint = ?", ("f" * 64,))
        elif mismatch == "version":
            connection.execute("PRAGMA user_version=2")
        else:
            connection.execute("ALTER TABLE events RENAME COLUMN duration_ms TO wrong_duration")
    before = path.read_bytes()
    queries = DashboardQueries(reliability_db_path=reliability, growth_db_path=growth)

    with pytest.raises(DashboardDataError, match="incompatible"):
        queries.summary(_window())

    assert path.read_bytes() == before


def test_each_scope_query_uses_one_consistent_read_snapshot(tmp_path: Path) -> None:
    queries, reliability, _ = _queries(tmp_path)
    with sqlite3.connect(reliability) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

    with queries._open(TelemetryScope.RELIABILITY) as reader:
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        _insert(
            reliability,
            sequence=999,
            event_name="app_start_result",
            occurred_at="2026-09-10T00:00:00.000Z",
            outcome="success",
            duration_ms=100,
            app_session_id="concurrent-session",
        )
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    refreshed = queries.reliability(_window())
    assert refreshed["appStart"]["estimatedEvents"] == 1
    assert refreshed["asOfReceivedUtc"] == "2026-09-10T00:00:00.000Z"


@pytest.mark.parametrize("mismatch", ("scope", "fingerprint", "version"))
def test_database_scope_fingerprint_and_schema_are_fail_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    with sqlite3.connect(reliability) as connection:
        if mismatch == "scope":
            connection.execute("UPDATE meta SET scope = 'growth'")
        elif mismatch == "fingerprint":
            connection.execute("UPDATE meta SET protocol_fingerprint = ?", ("f" * 64,))
        else:
            connection.execute("PRAGMA user_version=2")

    with pytest.raises(DashboardDataError, match="incompatible"):
        queries.reliability(_window())


@pytest.mark.parametrize(
    "compatible_fingerprint",
    sorted(_COMPATIBLE_PREVIOUS_PROTOCOL_FINGERPRINTS),
)
def test_compatible_previous_fingerprint_remains_readable_without_migration(
    tmp_path: Path,
    compatible_fingerprint: str,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    with sqlite3.connect(reliability) as connection:
        connection.execute(
            "UPDATE meta SET protocol_fingerprint = ? WHERE singleton = 1",
            (compatible_fingerprint,),
        )
    before = reliability.read_bytes()

    result = queries.reliability(_window())

    assert result["appStart"]["estimatedEvents"] == 0
    assert reliability.read_bytes() == before


def test_reliability_queries_return_weighted_aggregates_only(tmp_path: Path) -> None:
    queries, reliability, _ = _queries(tmp_path)
    _insert(
        reliability,
        sequence=1,
        event_name="app_start_result",
        occurred_at="2026-09-01T01:00:00.000Z",
        outcome="success",
        duration_ms=100,
        sample_rate=0.5,
        app_session_id="session-one",
    )
    _insert(
        reliability,
        sequence=2,
        event_name="app_start_result",
        occurred_at="2026-09-01T02:00:00.000Z",
        outcome="fail",
        duration_ms=900,
        app_session_id="session-two",
    )
    _insert(
        reliability,
        sequence=3,
        event_name="app_crash_detected",
        occurred_at="2026-09-01T03:00:00.000Z",
        outcome="detected",
        app_session_id="session-two",
    )
    _insert(
        reliability,
        sequence=4,
        event_name="turn_result",
        occurred_at="2026-09-02T01:00:00.000Z",
        outcome="success",
        duration_ms=500,
        app_session_id="session-one",
        payload={"ttft_ms": 50, "stall_count": 0},
    )
    _insert(
        reliability,
        sequence=5,
        event_name="turn_result",
        occurred_at="2026-09-02T02:00:00.000Z",
        outcome="fail",
        error_code="internal_error",
        duration_ms=2_000,
        app_session_id="session-two",
        payload={"ttft_ms": 200, "stall_count": 2},
    )
    _insert(
        reliability,
        sequence=6,
        event_name="tool_call_result",
        occurred_at="2026-09-02T03:00:00.000Z",
        outcome="success",
        duration_ms=40,
        app_session_id="session-one",
        payload={"tool_category": "web"},
    )
    _insert(
        reliability,
        sequence=7,
        event_name="performance_summary",
        occurred_at="2026-09-02T04:00:00.000Z",
        outcome="success",
        duration_ms=3_000,
        app_session_id="session-one",
        payload={
            "turn_count": 10,
            "stalled_turn_count": 2,
            "monitored_request_count": 20,
            "slow_request_count": 1,
        },
    )
    _insert(
        reliability,
        sequence=8,
        event_name="turn_result",
        event_version=3,
        occurred_at="2026-09-02T05:00:00.000Z",
        outcome="fail",
        error_code="provider_unavailable",
        duration_ms=1_500,
        sample_rate=0.5,
        app_session_id="session-two",
        payload={
            "ttft_ms": None,
            "stall_count": 0,
            "failure_stage": "agent_execution",
        },
    )

    result = queries.reliability(_window())

    assert result["appStart"] == {
        "estimatedEvents": 3,
        "estimatedSuccesses": 2,
        "successRate": pytest.approx(2 / 3),
        "p95DurationMs": 900,
    }
    assert result["crashFreeSessions"] == {
        "sessions": 2,
        "crashedSessions": 1,
        "crashFreeRate": 0.5,
    }
    assert result["turns"]["successRate"] == 0.25
    assert result["turns"]["p95TtftMs"] == 200
    assert result["turns"]["stalledTurnRate"] == 0.25
    assert result["turns"]["byFailureStage"] == [
        {"dimension": "agent_execution", "estimatedEvents": 2},
        {"dimension": "unclassified", "estimatedEvents": 1},
    ]
    assert result["turns"]["byErrorCode"] == [
        {"dimension": "provider_unavailable", "estimatedEvents": 2},
        {"dimension": "internal_error", "estimatedEvents": 1},
    ]
    assert result["tools"]["byCategory"] == [
        {"dimension": "web", "estimatedEvents": 1, "successRate": 1.0}
    ]
    assert result["performance"] == {
        "summaries": 1,
        "stalledTurnRate": 0.2,
        "slowRequestRate": 0.05,
    }
    assert len(result["dailyTrend"]) == 30
    assert result["dailyTrend"][0] == {
        "date": "2026-09-01",
        "estimatedEvents": 4,
        "estimatedIssues": 2,
    }
    assert result["dailyTrend"][1] == {
        "date": "2026-09-02",
        "estimatedEvents": 6,
        "estimatedIssues": 3,
    }
    assert result["dailyTrend"][-1] == {
        "date": "2026-09-30",
        "estimatedEvents": 0,
        "estimatedIssues": 0,
    }
    _assert_no_sensitive_output(result)


def test_hourly_reliability_trend_is_weighted_zero_filled_and_half_open(
    tmp_path: Path,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    window = UtcCohortWindow.from_dates("2026-09-01", "2026-09-02")
    events = (
        (20, "app_start_result", "2026-08-31T23:59:59.999Z", "fail", 0.1),
        (21, "app_start_result", "2026-09-01T00:00:00.000Z", "success", 0.5),
        (22, "tool_call_result", "2026-09-01T00:59:59.999Z", "denied", 0.25),
        (23, "app_crash_detected", "2026-09-02T00:15:00.000Z", "detected", 0.5),
        (24, "app_start_result", "2026-09-02T12:00:00.000Z", "detected", 0.5),
        (25, "turn_result", "2026-09-02T23:59:59.999Z", "cancel", 0.2),
        (26, "turn_result", "2026-09-03T00:00:00.000Z", "fail", 0.1),
    )
    for sequence, event_name, occurred_at, outcome, sample_rate in events:
        _insert(
            reliability,
            sequence=sequence,
            event_name=event_name,
            occurred_at=occurred_at,
            outcome=outcome,
            sample_rate=sample_rate,
        )

    result = queries.reliability(window)
    trend = result["hourlyTrend"]

    assert [point["hourUtc"] for point in trend] == list(range(24))
    assert trend[0] == {
        "hourUtc": 0,
        "estimatedEvents": 8,
        "estimatedIssues": 6,
    }
    assert trend[1] == {
        "hourUtc": 1,
        "estimatedEvents": 0,
        "estimatedIssues": 0,
    }
    assert trend[12] == {
        "hourUtc": 12,
        "estimatedEvents": 2,
        "estimatedIssues": 0,
    }
    assert trend[23] == {
        "hourUtc": 23,
        "estimatedEvents": 5,
        "estimatedIssues": 5,
    }
    assert sum(point["estimatedEvents"] for point in trend) == 15
    assert sum(point["estimatedIssues"] for point in trend) == 11
    assert sum(point["estimatedEvents"] for point in trend) == sum(
        point["estimatedEvents"] for point in result["dailyTrend"]
    )
    assert sum(point["estimatedIssues"] for point in trend) == sum(
        point["estimatedIssues"] for point in result["dailyTrend"]
    )
    _assert_no_sensitive_output(trend)


def test_reliability_is_grouped_by_version_and_controlled_source_commit(
    tmp_path: Path,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    source_sha1 = "a" * 40
    events = (
        (30, f"1.0.0+source.{source_sha1}", "success", 0.5),
        (31, f"1.0.0+source.g{source_sha1}", "fail", 1.0),
        (32, "1.0.0", "success", 1.0),
        (33, "1.0.0", "timeout", 0.5),
        (34, "2.0.0", "cancel", 0.25),
    )
    for sequence, app_version, outcome, sample_rate in events:
        _insert(
            reliability,
            sequence=sequence,
            event_name="turn_result",
            occurred_at=f"2026-09-03T{sequence - 30:02d}:00:00.000Z",
            app_version=app_version,
            outcome=outcome,
            sample_rate=sample_rate,
        )

    result = queries.reliability(_window())

    assert result["byVersion"] == [
        {
            "appVersion": "2.0.0",
            "sourceCommitId": None,
            "estimatedEvents": 4,
            "estimatedIssues": 4,
            "issueRate": 1.0,
        },
        {
            "appVersion": "1.0.0",
            "sourceCommitId": None,
            "estimatedEvents": 3,
            "estimatedIssues": 2,
            "issueRate": pytest.approx(2 / 3),
        },
        {
            "appVersion": "1.0.0",
            "sourceCommitId": source_sha1,
            "estimatedEvents": 3,
            "estimatedIssues": 1,
            "issueRate": pytest.approx(1 / 3),
        },
    ]
    assert sum(item["estimatedEvents"] for item in result["byVersion"]) == sum(
        point["estimatedEvents"] for point in result["dailyTrend"]
    )
    _assert_no_sensitive_output(result["byVersion"])


def test_reliability_version_breakdown_is_bounded_and_preserves_tail_metrics(
    tmp_path: Path,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    for sequence in range(30):
        _insert(
            reliability,
            sequence=100 + sequence,
            event_name="turn_result",
            occurred_at=f"2026-09-03T00:00:{sequence:02d}.000Z",
            app_version=f"1.0.{sequence}",
            outcome="fail" if sequence % 3 == 0 else "success",
            sample_rate=0.3,
        )

    result = queries.reliability(_window())
    breakdown = result["byVersion"]

    assert len(breakdown) == 24
    assert breakdown[-1]["collapsedDimensions"] == 7
    assert breakdown[-1]["estimatedEvents"] == pytest.approx(23.333)
    assert breakdown[-1]["estimatedIssues"] == pytest.approx(10)
    assert all(0 <= item["estimatedIssues"] <= item["estimatedEvents"] for item in breakdown)
    assert all(0 <= item["issueRate"] <= 1 for item in breakdown)
    _assert_no_sensitive_output(breakdown)


def test_reliability_version_tail_never_inherits_rounding_residual(
    tmp_path: Path,
) -> None:
    queries, reliability, _ = _queries(tmp_path)
    for sequence in range(23):
        _insert(
            reliability,
            sequence=200 + sequence,
            event_name="turn_result",
            occurred_at=f"2026-09-03T00:00:{sequence:02d}.000Z",
            app_version=f"2.0.{sequence}",
            outcome="fail",
            sample_rate=0.6,
        )
    for sequence in range(2):
        _insert(
            reliability,
            sequence=300 + sequence,
            event_name="turn_result",
            occurred_at=f"2026-09-03T00:01:{sequence:02d}.000Z",
            app_version=f"3.0.{sequence}",
            outcome="success",
        )

    tail = queries.reliability(_window())["byVersion"][-1]

    assert tail["collapsedDimensions"] == 2
    assert tail["estimatedEvents"] == 2
    assert tail["estimatedIssues"] == 0
    assert tail["issueRate"] == 0


def test_growth_funnels_keep_identifiers_separate_and_use_fixed_windows(
    tmp_path: Path,
) -> None:
    queries, _, growth = _queries(tmp_path)
    sequence = 100

    def add(
        name: str,
        timestamp: str,
        *,
        acquisition: str | None = None,
        analytics: str | None = None,
        outcome: str | None = None,
    ) -> None:
        nonlocal sequence
        sequence += 1
        _insert(
            growth,
            sequence=sequence,
            event_name=name,
            occurred_at=timestamp,
            acquisition_id=acquisition,
            analytics_user_id=analytics,
            outcome=outcome,
        )

    # Acquisition A completes the acquisition segment and supplies the only
    # legitimate acquisition -> analytics bridge.
    add("landing_view", "2026-09-01T00:00:00.000Z", acquisition="acquisition-a")
    add("download_click", "2026-09-01T01:00:00.000Z", acquisition="acquisition-a")
    add(
        "download_served",
        "2026-09-01T02:00:00.000Z",
        acquisition="acquisition-a",
        outcome="success",
    )
    add("install_started", "2026-09-01T03:00:00.000Z", acquisition="acquisition-a")
    add(
        "install_result",
        "2026-09-01T04:00:00.000Z",
        acquisition="acquisition-a",
        outcome="success",
    )
    add("registration_started", "2026-09-01T05:00:00.000Z", acquisition="acquisition-a")
    add(
        "registration_result",
        "2026-09-01T06:00:00.000Z",
        acquisition="acquisition-a",
        analytics="analytics-x",
        outcome="success",
    )

    # B downloads too late for the 24-hour transition, but its independent
    # installation remains in the linked-install coverage denominator.
    add("landing_view", "2026-09-02T00:00:00.000Z", acquisition="acquisition-b")
    add("download_click", "2026-09-03T02:00:00.000Z", acquisition="acquisition-b")
    add(
        "install_result",
        "2026-09-02T04:00:00.000Z",
        acquisition="acquisition-b",
        outcome="success",
    )
    add("landing_view", "2026-09-03T00:00:00.000Z", acquisition="acquisition-c")

    # A repeated landing inside the cohort does not pull an older journey into
    # the cohort: cohort membership uses the first-ever anchor event.
    add("landing_view", "2026-08-01T00:00:00.000Z", acquisition="acquisition-old")
    add("landing_view", "2026-09-04T00:00:00.000Z", acquisition="acquisition-old")

    add("first_app_ready", "2026-09-01T04:30:00.000Z", analytics="analytics-x")
    add(
        "onboarding_result",
        "2026-09-01T07:00:00.000Z",
        analytics="analytics-x",
        outcome="completed",
    )
    add("first_turn_started", "2026-09-01T08:00:00.000Z", analytics="analytics-x")
    add("first_turn_result", "2026-09-01T09:00:00.000Z", analytics="analytics-x", outcome="success")
    add(
        "onboarding_result", "2026-09-02T04:30:00.000Z",
        analytics="analytics-y", outcome="completed",
    )
    add("first_app_ready", "2026-09-02T04:30:00.000Z", analytics="analytics-ready-only")

    result = queries.growth(_window())

    acquisition_counts = [stage["deduplicatedCount"] for stage in result["acquisition"]["stages"]]
    activation_counts = [stage["deduplicatedCount"] for stage in result["activation"]["stages"]]
    assert result["acquisition"]["deduplicationUnit"] == "acquisition journey"
    assert acquisition_counts == [3, 1, 1, 1, 1, 1, 1]
    assert result["acquisition"]["transitions"][0] == {
        "from": "landing_view",
        "to": "download_click",
        "windowHours": 24,
        "dropoffRate": pytest.approx(2 / 3),
    }
    assert result["activation"]["deduplicationUnit"] == "analytics user"
    assert activation_counts == [2, 1, 1, 1]
    assert result["linkedInstallToReady"] == {
        "eligibleInstallations": 2,
        "linkableInstallations": 1,
        "unlinkedInstallations": 1,
        "readyWithin24Hours": 1,
        "linkCoverageRate": 0.5,
        "conversionRateAmongLinked": 1.0,
    }
    _assert_no_sensitive_output(result)


@pytest.mark.parametrize(
    ("ready_minutes", "turn_minutes", "success_minutes", "counts"),
    [
        (1, 2, 3, [1, 1, 1, 1]),
        (-1, 2, 3, [1, 1, 1, 1]),
        (2, 1, 3, [1, 1, 0, 0]),
        (-2, -1, 3, [1, 1, 0, 0]),
        (10080, 10081, 10082, [1, 1, 1, 1]),
        (10081, 10082, 10083, [1, 0, 0, 0]),
        (-10080, 2, 3, [1, 1, 1, 1]),
        (-10081, 2, 3, [1, 0, 0, 0]),
        (1, 10082, 10083, [1, 1, 0, 0]),
        (1, 2, 10083, [1, 1, 1, 0]),
    ],
)
def test_activation_follows_desktop_order_and_accepts_legacy_readiness(
    tmp_path: Path,
    ready_minutes: int,
    turn_minutes: int,
    success_minutes: int,
    counts: list[int],
) -> None:
    queries, _, growth = _queries(tmp_path)
    anchor = datetime(2026, 9, 1, tzinfo=UTC)

    def timestamp(minutes: int) -> str:
        return (anchor + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Delivery order is deliberately reversed; occurrence time determines the
    # journey, while completed onboarding determines cohort membership.
    events = [
        ("first_turn_result", timestamp(success_minutes), "success"),
        ("first_turn_started", timestamp(turn_minutes), None),
        ("first_app_ready", timestamp(ready_minutes), None),
        ("onboarding_result", timestamp(0), "completed"),
    ]
    for sequence, (name, occurred_at, outcome) in enumerate(events, start=1):
        _insert(
            growth, sequence=sequence, event_name=name, occurred_at=occurred_at,
            analytics_user_id="synthetic-user", outcome=outcome,
        )
    activation = queries.growth(UtcCohortWindow.from_dates("2026-09-01", "2026-09-01"))[
        "activation"
    ]
    assert [stage["deduplicatedCount"] for stage in activation["stages"]] == counts
    assert [stage["stage"] for stage in activation["stages"]] == [
        "onboarding_completed", "first_app_ready", "first_turn_started", "first_turn_succeeded",
    ]
    assert [transition["windowHours"] for transition in activation["transitions"]] == [168] * 3
    if counts == [1, 1, 1, 1]:
        assert [transition["dropoffRate"] for transition in activation["transitions"]] == [0] * 3


def test_activation_cohort_uses_first_completed_onboarding_and_distinct_users(
    tmp_path: Path,
) -> None:
    queries, _, growth = _queries(tmp_path)
    events = [
        ("onboarding_result", "2026-08-31T23:59:00.000Z", "old-user", "completed"),
        ("onboarding_result", "2026-09-01T00:00:00.000Z", "old-user", "completed"),
        ("first_app_ready", "2026-09-01T00:01:00.000Z", "old-user", None),
        ("onboarding_result", "2026-09-01T23:58:00.000Z", "new-user", "completed"),
        ("onboarding_result", "2026-09-01T23:59:00.000Z", "new-user", "completed"),
        ("first_app_ready", "2026-09-02T00:00:00.000Z", "new-user", None),
        ("first_turn_started", "2026-09-02T00:01:00.000Z", "new-user", None),
        ("first_turn_result", "2026-09-02T00:02:00.000Z", "new-user", "success"),
        ("first_turn_result", "2026-09-02T00:03:00.000Z", "new-user", "success"),
        ("onboarding_result", "2026-09-01T00:00:00.000Z", "not-ready", "completed"),
        ("first_turn_started", "2026-09-01T00:01:00.000Z", "not-ready", None),
        ("first_turn_result", "2026-09-01T00:02:00.000Z", "not-ready", "success"),
        ("onboarding_result", "2026-09-01T00:00:00.000Z", "cancelled-user", "cancelled"),
        ("first_app_ready", "2026-09-01T00:01:00.000Z", "ready-only", None),
    ]
    for sequence, (name, occurred_at, user, outcome) in enumerate(events, start=1):
        _insert(
            growth, sequence=sequence, event_name=name, occurred_at=occurred_at,
            analytics_user_id=user, outcome=outcome,
        )

    activation = queries.growth(UtcCohortWindow.from_dates("2026-09-01", "2026-09-01"))[
        "activation"
    ]
    assert [stage["deduplicatedCount"] for stage in activation["stages"]] == [2, 1, 1, 1]
    assert [transition["dropoffRate"] for transition in activation["transitions"]] == [0.5, 0, 0]
    _assert_no_sensitive_output(activation)
    following_day = queries.growth(UtcCohortWindow.from_dates("2026-09-02", "2026-09-02"))[
        "activation"
    ]
    assert [stage["deduplicatedCount"] for stage in following_day["stages"]] == [0, 0, 0, 0]


def test_utc_cohort_dates_are_strict_and_bounded() -> None:
    window = UtcCohortWindow.from_dates("2026-09-01", "2026-09-02")
    assert window.public_dict() == {
        "startUtc": "2026-09-01T00:00:00.000Z",
        "endExclusiveUtc": "2026-09-03T00:00:00.000Z",
        "timezone": "UTC",
    }
    with pytest.raises(ValueError):
        UtcCohortWindow.from_dates("09/01/2026", "2026-09-02")
    with pytest.raises(ValueError):
        UtcCohortWindow(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end_exclusive=datetime(2028, 1, 1, tzinfo=UTC),
        )


def test_client_usage_counts_distinct_users_and_terminal_overlap(tmp_path: Path) -> None:
    queries, _, growth = _queries(tmp_path)
    launches = [
        (201, "analytics-a", "tui", "chat", "2026-09-01T01:00:00.000Z"),
        (202, "analytics-a", "tui", "chat", "2026-09-02T01:00:00.000Z"),
        (203, "analytics-a", "cli", "agent", "2026-09-02T02:00:00.000Z"),
        (204, "analytics-b", "tui", "chat", "2026-09-08T01:00:00.000Z"),
        (205, "analytics-c", "cli", "gateway_run", "2026-09-08T02:00:00.000Z"),
    ]
    for sequence, user_id, surface, entrypoint, occurred_at in launches:
        _insert(
            growth,
            sequence=sequence,
            event_name="client_launch",
            occurred_at=occurred_at,
            analytics_user_id=user_id,
            payload={
                "surface": surface,
                "entrypoint": entrypoint,
                "execution_mode": "gateway",
            },
        )

    result = queries.growth(_window())["clientUsage"]

    assert result["totals"] == {
        "tuiUsers": 2,
        "cliUsers": 2,
        "terminalUsers": 3,
        "tuiOnly": 1,
        "cliOnly": 1,
        "both": 1,
    }
    assert result["dailyTrend"][0] == {
        "period": "2026-09-01",
        "tuiUsers": 1,
        "cliUsers": 0,
        "terminalUsers": 1,
    }
    assert result["weeklyTrend"] == [
        {"period": "2026-08-31", "tuiUsers": 1, "cliUsers": 1, "terminalUsers": 1},
        {"period": "2026-09-07", "tuiUsers": 1, "cliUsers": 1, "terminalUsers": 2},
    ]
    assert result["monthlyTrend"] == [
        {"period": "2026-09", "tuiUsers": 2, "cliUsers": 2, "terminalUsers": 3}
    ]
    assert result["entrypoints"] == [
        {"entrypoint": "agent", "users": 1},
        {"entrypoint": "chat", "users": 2},
        {"entrypoint": "gateway_run", "users": 1},
    ]
    assert "不代表全部实际" in result["observablePopulationNote"]
    _assert_no_sensitive_output(result)


def test_metaskill_usage_counts_runs_and_zero_fills_daily_trend(tmp_path: Path) -> None:
    queries, _, growth = _queries(tmp_path)
    _insert(
        growth,
        sequence=301,
        event_name="metaskill_usage",
        occurred_at="2026-09-01T01:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-a",
        notice_version="growth-v2",
    )
    _insert(
        growth,
        sequence=302,
        event_name="metaskill_usage",
        occurred_at="2026-09-01T02:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-a",
        notice_version="growth-v2",
    )
    _insert(
        growth,
        sequence=303,
        event_name="metaskill_usage",
        occurred_at="2026-09-03T02:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-b",
        notice_version="growth-v2",
    )
    # Malformed/other-source rows are not part of the v1 usage contract.
    _insert(
        growth,
        sequence=304,
        event_name="metaskill_usage",
        occurred_at="2026-09-03T03:00:00.000Z",
        source="gateway",
        analytics_user_id="analytics-b",
        notice_version="growth-v2",
    )
    # Feature-use events require the notice that disclosed ongoing usage counts.
    _insert(
        growth,
        sequence=305,
        event_name="metaskill_usage",
        occurred_at="2026-09-03T04:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-b",
        notice_version="growth-v1",
    )

    result = queries.growth(_window())["metaskillUsage"]

    assert result["totalUses"] == 3
    assert result["dailyTrend"][0] == {"period": "2026-09-01", "uses": 2}
    assert result["dailyTrend"][1] == {"period": "2026-09-02", "uses": 0}
    assert result["dailyTrend"][2] == {"period": "2026-09-03", "uses": 1}
    _assert_no_sensitive_output(result)


def test_coding_mode_usage_counts_started_runs_and_zero_fills_daily_trend(
    tmp_path: Path,
) -> None:
    queries, _, growth = _queries(tmp_path)
    _insert(
        growth,
        sequence=311,
        event_name="coding_mode_usage",
        occurred_at="2026-09-01T01:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-a",
        notice_version="growth-v2",
    )
    _insert(
        growth,
        sequence=312,
        event_name="coding_mode_usage",
        occurred_at="2026-09-03T02:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-b",
        notice_version="growth-v2",
    )
    # Other sources do not satisfy the runtime event contract.
    _insert(
        growth,
        sequence=313,
        event_name="coding_mode_usage",
        occurred_at="2026-09-03T03:00:00.000Z",
        source="gateway",
        analytics_user_id="analytics-b",
        notice_version="growth-v2",
    )
    _insert(
        growth,
        sequence=314,
        event_name="coding_mode_usage",
        occurred_at="2026-09-03T04:00:00.000Z",
        source="runtime",
        analytics_user_id="analytics-b",
        notice_version="growth-v1",
    )

    result = queries.growth(_window())["codingModeUsage"]

    assert result["totalUses"] == 2
    assert result["dailyTrend"][0] == {"period": "2026-09-01", "uses": 1}
    assert result["dailyTrend"][1] == {"period": "2026-09-02", "uses": 0}
    assert result["dailyTrend"][2] == {"period": "2026-09-03", "uses": 1}
    assert "仅开启模式不计数" in result["note"]
    _assert_no_sensitive_output(result)
