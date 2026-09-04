from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.server.dashboard_queries import (
    DashboardDataError,
    DashboardQueries,
    UtcCohortWindow,
)
from opensquilla.telemetry.server.storage import TelemetryIngestStorage

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


def _insert(
    path: Path,
    *,
    sequence: int,
    event_name: str,
    occurred_at: str,
    event_version: int = 1,
    app_version: str | None = "1.0.0",
    outcome: str | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    sample_rate: float = 1.0,
    app_session_id: str | None = None,
    acquisition_id: str | None = None,
    analytics_user_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    values: dict[str, object] = {
        "event_id": f"event-{sequence:04d}",
        "payload_sha256": "a" * 64,
        "event_name": event_name,
        "event_version": event_version,
        "occurred_at_utc": occurred_at,
        "source": "desktop",
        "app_version": app_version,
        "platform": "macos",
        "outcome": outcome,
        "error_code": error_code,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "notice_version": "test-v1",
        "app_session_id": app_session_id,
        "acquisition_id": acquisition_id,
        "analytics_user_id": analytics_user_id,
        "payload_json": json.dumps(payload or {}, separators=(",", ":")),
        "first_batch_id": None,
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
    add("first_app_ready", "2026-09-02T04:30:00.000Z", analytics="analytics-y")

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
