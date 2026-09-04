"""Read-only, aggregate-only queries for the isolated telemetry preview dashboard."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.contracts.reliability import (
    FileType,
    ToolCategory,
    TurnErrorCode,
    TurnFailureStage,
    UpdateStage,
)

_SCHEMA_VERSION: Final = 1
_MAX_COHORT_DAYS: Final = 366
_REQUIRED_EVENT_COLUMNS: Final = frozenset(
    {
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
    }
)


class DashboardDataError(RuntimeError):
    """Preview data is missing, incompatible, or cannot be queried safely."""


@dataclass(frozen=True)
class UtcCohortWindow:
    """One bounded, half-open UTC cohort interval."""

    start: datetime
    end_exclusive: datetime

    def __post_init__(self) -> None:
        start = _require_utc(self.start)
        end = _require_utc(self.end_exclusive)
        if start >= end or end - start > timedelta(days=_MAX_COHORT_DAYS):
            raise ValueError("dashboard cohort window is invalid")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end_exclusive", end)

    @classmethod
    def from_dates(cls, start: str, end_inclusive: str) -> UtcCohortWindow:
        """Parse strict ISO dates and make the final date inclusive."""

        try:
            start_date = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
            end_date = datetime.strptime(end_inclusive, "%Y-%m-%d").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            raise ValueError("dashboard dates must use YYYY-MM-DD") from None
        return cls(start=start_date, end_exclusive=end_date + timedelta(days=1))

    @property
    def sql_params(self) -> tuple[str, str]:
        return (_wire_timestamp(self.start), _wire_timestamp(self.end_exclusive))

    def public_dict(self) -> dict[str, str]:
        return {
            "startUtc": _wire_timestamp(self.start),
            "endExclusiveUtc": _wire_timestamp(self.end_exclusive),
            "timezone": "UTC",
        }


@dataclass(frozen=True)
class _FunnelStage:
    key: str
    event_name: str
    outcome: str | None = None
    window_hours_from_previous: int | None = None


_ACQUISITION_STAGES: Final = (
    _FunnelStage("landing_view", "landing_view"),
    _FunnelStage("download_click", "download_click", window_hours_from_previous=24),
    _FunnelStage("download_served", "download_served", "success", 24),
    _FunnelStage("install_started", "install_started", window_hours_from_previous=7 * 24),
    _FunnelStage("install_succeeded", "install_result", "success", 7 * 24),
    _FunnelStage(
        "registration_started",
        "registration_started",
        window_hours_from_previous=7 * 24,
    ),
    _FunnelStage("registration_succeeded", "registration_result", "success", 7 * 24),
)

_ACTIVATION_STAGES: Final = (
    _FunnelStage("first_app_ready", "first_app_ready"),
    _FunnelStage("onboarding_completed", "onboarding_result", "completed", 7 * 24),
    _FunnelStage("first_turn_started", "first_turn_started", window_hours_from_previous=7 * 24),
    _FunnelStage("first_turn_succeeded", "first_turn_result", "success", 7 * 24),
)


class DashboardQueries:
    """Query two scope-isolated collector databases without write capability."""

    def __init__(
        self,
        *,
        reliability_db_path: str | Path,
        growth_db_path: str | Path,
        protocol_fingerprint: str = TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    ) -> None:
        self._paths = {
            TelemetryScope.RELIABILITY: Path(reliability_db_path).expanduser(),
            TelemetryScope.GROWTH: Path(growth_db_path).expanduser(),
        }
        if (
            not isinstance(protocol_fingerprint, str)
            or len(protocol_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in protocol_fingerprint)
        ):
            raise ValueError("telemetry protocol fingerprint is invalid")
        self._protocol_fingerprint = protocol_fingerprint

    def summary(self, window: UtcCohortWindow) -> dict[str, Any]:
        return {
            "cohort": window.public_dict(),
            "reliability": self.reliability(window),
            "growth": self.growth(window),
        }

    def reliability(self, window: UtcCohortWindow) -> dict[str, Any]:
        with self._open(TelemetryScope.RELIABILITY) as connection:
            watermark = self._received_watermark(connection)
            app_start = self._outcome_metric(connection, window, "app_start_result")
            gateway_start = self._outcome_metric(connection, window, "gateway_start_result")
            turns = self._outcome_metric(connection, window, "turn_result")
            turns.update(
                {
                    "p95TtftMs": self._weighted_p95(
                        connection,
                        window,
                        event_name="turn_result",
                        value_expression=(
                            "CAST(json_extract(payload_json, '$.ttft_ms') AS INTEGER)"
                        ),
                        extra_predicate=(
                            "json_type(payload_json, '$.ttft_ms') = 'integer' "
                            "AND json_extract(payload_json, '$.ttft_ms') >= 0"
                        ),
                    ),
                    "stalledTurnRate": self._json_counter_rate(
                        connection,
                        window,
                        event_name="turn_result",
                        numerator_field="stall_count",
                        denominator_mode="event",
                    ),
                    "byFailureStage": self._turn_failure_stages(connection, window),
                    "byErrorCode": self._turn_error_codes(connection, window),
                }
            )
            tools = self._outcome_metric(connection, window, "tool_call_result")
            tools["byCategory"] = self._dimension_outcomes(
                connection,
                window,
                event_name="tool_call_result",
                json_field="tool_category",
                allowed_values=tuple(category.value for category in ToolCategory),
            )
            file_parsing = self._outcome_metric(connection, window, "file_parse_result")
            file_parsing["byType"] = self._dimension_outcomes(
                connection,
                window,
                event_name="file_parse_result",
                json_field="file_type",
                allowed_values=tuple(file_type.value for file_type in FileType),
            )
            updates = self._outcome_metric(connection, window, "update_result")
            updates["byStage"] = self._dimension_outcomes(
                connection,
                window,
                event_name="update_result",
                json_field="update_stage",
                allowed_values=tuple(stage.value for stage in UpdateStage),
            )
            return {
                "asOfReceivedUtc": watermark,
                "dailyTrend": self._daily_reliability_trend(connection, window),
                "hourlyTrend": self._hourly_reliability_trend(connection, window),
                "appStart": app_start,
                "gatewayStart": gateway_start,
                "crashFreeSessions": self._crash_free_sessions(connection, window),
                "turns": turns,
                "tools": tools,
                "fileParsing": file_parsing,
                "updates": updates,
                "performance": self._performance_summary(connection, window),
            }

    def _daily_reliability_trend(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> list[dict[str, Any]]:
        """Return a zero-filled daily volume and issue series for charting."""

        rows = connection.execute(
            """
            SELECT
                substr(occurred_at_utc, 1, 10) AS utc_day,
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_events,
                COALESCE(SUM(
                    CASE
                        WHEN event_name = 'app_crash_detected'
                          OR outcome IN ('fail', 'timeout', 'cancel', 'denied')
                        THEN 1.0 / sample_rate
                        ELSE 0
                    END
                ), 0.0) AS estimated_issues
            FROM events
            WHERE occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            GROUP BY substr(occurred_at_utc, 1, 10)
            """,
            window.sql_params,
        ).fetchall()
        by_day = {
            str(row["utc_day"]): (
                _public_count(float(row["estimated_events"])),
                _public_count(float(row["estimated_issues"])),
            )
            for row in rows
        }
        day_count = (window.end_exclusive.date() - window.start.date()).days
        result: list[dict[str, Any]] = []
        for offset in range(day_count):
            day = (window.start + timedelta(days=offset)).date().isoformat()
            events, issues = by_day.get(day, (0, 0))
            result.append(
                {
                    "date": day,
                    "estimatedEvents": events,
                    "estimatedIssues": issues,
                }
            )
        return result

    def _hourly_reliability_trend(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> list[dict[str, Any]]:
        """Return a zero-filled UTC hour-of-day volume and issue series."""

        rows = connection.execute(
            """
            SELECT
                substr(occurred_at_utc, 12, 2) AS utc_hour,
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_events,
                COALESCE(SUM(
                    CASE
                        WHEN event_name = 'app_crash_detected'
                          OR outcome IN ('fail', 'timeout', 'cancel', 'denied')
                        THEN 1.0 / sample_rate
                        ELSE 0
                    END
                ), 0.0) AS estimated_issues
            FROM events
            WHERE occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            GROUP BY substr(occurred_at_utc, 12, 2)
            """,
            window.sql_params,
        ).fetchall()
        by_hour = {
            str(row["utc_hour"]): (
                _public_count(float(row["estimated_events"])),
                _public_count(float(row["estimated_issues"])),
            )
            for row in rows
        }
        result: list[dict[str, Any]] = []
        for hour in range(24):
            events, issues = by_hour.get(f"{hour:02d}", (0, 0))
            result.append(
                {
                    "hourUtc": hour,
                    "estimatedEvents": events,
                    "estimatedIssues": issues,
                }
            )
        return result

    def growth(self, window: UtcCohortWindow) -> dict[str, Any]:
        with self._open(TelemetryScope.GROWTH) as connection:
            watermark = self._received_watermark(connection)
            acquisition = self._funnel(
                connection,
                window,
                id_column="acquisition_id",
                stages=_ACQUISITION_STAGES,
            )
            activation = self._funnel(
                connection,
                window,
                id_column="analytics_user_id",
                stages=_ACTIVATION_STAGES,
            )
            return {
                "asOfReceivedUtc": watermark,
                "populationExclusions": {
                    "verifiedByDashboard": False,
                    "requiredUpstream": (
                        "internal testing, automated traffic, and repeat installations"
                    ),
                    "fingerprintLinkageAllowed": False,
                },
                "acquisition": acquisition,
                "activation": activation,
                "linkedInstallToReady": self._linked_install_to_ready(connection, window),
                "clientUsage": self._client_usage(connection, window),
            }

    def _client_usage(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> dict[str, Any]:
        """Return consented observable terminal users, never event counts or IDs."""

        totals = connection.execute(
            """
            WITH users AS (
                SELECT analytics_user_id,
                       MAX(json_extract(payload_json, '$.surface') = 'tui') AS used_tui,
                       MAX(json_extract(payload_json, '$.surface') = 'cli') AS used_cli
                FROM events
                WHERE event_name = 'client_launch'
                  AND analytics_user_id IS NOT NULL
                  AND occurred_at_utc >= ? AND occurred_at_utc < ?
                GROUP BY analytics_user_id
            )
            SELECT COALESCE(SUM(used_tui), 0) AS tui_users,
                   COALESCE(SUM(used_cli), 0) AS cli_users,
                   COUNT(*) AS terminal_users,
                   COALESCE(SUM(used_tui = 1 AND used_cli = 0), 0) AS tui_only,
                   COALESCE(SUM(used_tui = 0 AND used_cli = 1), 0) AS cli_only,
                   COALESCE(SUM(used_tui = 1 AND used_cli = 1), 0) AS both_users
            FROM users
            WHERE used_tui = 1 OR used_cli = 1
            """,
            window.sql_params,
        ).fetchone()
        daily_rows = self._client_usage_trend(
            connection,
            window,
            "substr(occurred_at_utc, 1, 10)",
        )
        daily_by_period = {str(row["period"]): row for row in daily_rows}
        daily: list[dict[str, Any]] = []
        day_count = (window.end_exclusive.date() - window.start.date()).days
        for offset in range(day_count):
            day = (window.start + timedelta(days=offset)).date().isoformat()
            row = daily_by_period.get(day)
            daily.append(self._public_client_usage_trend_row(day, row))

        entrypoint_rows = connection.execute(
            """
            SELECT json_extract(payload_json, '$.entrypoint') AS entrypoint,
                   COUNT(DISTINCT analytics_user_id) AS users
            FROM events
            WHERE event_name = 'client_launch'
              AND analytics_user_id IS NOT NULL
              AND occurred_at_utc >= ? AND occurred_at_utc < ?
              AND json_extract(payload_json, '$.entrypoint')
                    IN ('chat', 'agent', 'gateway_run')
            GROUP BY json_extract(payload_json, '$.entrypoint')
            ORDER BY entrypoint
            """,
            window.sql_params,
        ).fetchall()
        return {
            "observablePopulationNote": (
                "仅统计当前隐私声明下明确同意产品与增长分析、且客户端可观测的用户；"
                "不代表全部实际 TUI/CLI 用户。"
            ),
            "totals": {
                "tuiUsers": int(totals["tui_users"]),
                "cliUsers": int(totals["cli_users"]),
                "terminalUsers": int(totals["terminal_users"]),
                "tuiOnly": int(totals["tui_only"]),
                "cliOnly": int(totals["cli_only"]),
                "both": int(totals["both_users"]),
            },
            "dailyTrend": daily,
            "weeklyTrend": [
                self._public_client_usage_trend_row(str(row["period"]), row)
                for row in self._client_usage_trend(
                    connection,
                    window,
                    "date(occurred_at_utc, '-' || "
                    "((CAST(strftime('%w', occurred_at_utc) AS INTEGER) + 6) % 7) "
                    "|| ' days')",
                )
            ],
            "monthlyTrend": [
                self._public_client_usage_trend_row(str(row["period"]), row)
                for row in self._client_usage_trend(
                    connection,
                    window,
                    "substr(occurred_at_utc, 1, 7)",
                )
            ],
            "entrypoints": [
                {"entrypoint": str(row["entrypoint"]), "users": int(row["users"])}
                for row in entrypoint_rows
            ],
        }

    @staticmethod
    def _client_usage_trend(
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        period_expression: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            f"""
            SELECT {period_expression} AS period,
                   COUNT(DISTINCT CASE
                       WHEN json_extract(payload_json, '$.surface') = 'tui'
                       THEN analytics_user_id END) AS tui_users,
                   COUNT(DISTINCT CASE
                       WHEN json_extract(payload_json, '$.surface') = 'cli'
                       THEN analytics_user_id END) AS cli_users,
                   COUNT(DISTINCT analytics_user_id) AS terminal_users
            FROM events
            WHERE event_name = 'client_launch'
              AND analytics_user_id IS NOT NULL
              AND occurred_at_utc >= ? AND occurred_at_utc < ?
              AND json_extract(payload_json, '$.surface') IN ('tui', 'cli')
            GROUP BY {period_expression}
            ORDER BY period
            """,
            window.sql_params,
        ).fetchall()

    @staticmethod
    def _public_client_usage_trend_row(
        period: str,
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        return {
            "period": period,
            "tuiUsers": 0 if row is None else int(row["tui_users"]),
            "cliUsers": 0 if row is None else int(row["cli_users"]),
            "terminalUsers": 0 if row is None else int(row["terminal_users"]),
        }

    @contextmanager
    def _open(self, scope: TelemetryScope) -> Iterator[sqlite3.Connection]:
        path = self._paths[scope]
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise OSError
            uri = f"{resolved.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error):
            raise DashboardDataError("telemetry preview data is unavailable") from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or query_only[0] != 1:
                raise DashboardDataError("telemetry preview database is not read-only")
            # The first schema SELECT below pins one WAL snapshot for every
            # aggregate issued by this scope query. Reliability and Growth are
            # intentionally separate databases and therefore separate snapshots.
            connection.execute("BEGIN")
            self._validate_schema(connection, scope)
            yield connection
        except DashboardDataError:
            raise
        except sqlite3.Error:
            raise DashboardDataError("telemetry preview data is unavailable") from None
        finally:
            if connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            connection.close()

    @staticmethod
    def _received_watermark(connection: sqlite3.Connection) -> str | None:
        row = connection.execute("SELECT MAX(received_at_utc) AS watermark FROM events").fetchone()
        value = row["watermark"]
        return value if isinstance(value, str) else None

    def _validate_schema(self, connection: sqlite3.Connection, scope: TelemetryScope) -> None:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        if version_row is None or version_row[0] != _SCHEMA_VERSION:
            raise DashboardDataError("telemetry preview schema is incompatible")
        try:
            rows = connection.execute(
                """
                SELECT schema_version, scope, protocol_fingerprint
                FROM meta
                WHERE singleton = 1
                """
            ).fetchall()
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
        except sqlite3.Error:
            raise DashboardDataError("telemetry preview schema is incompatible") from None
        if (
            len(rows) != 1
            or rows[0]["schema_version"] != _SCHEMA_VERSION
            or rows[0]["scope"] != scope.value
            or rows[0]["protocol_fingerprint"] != self._protocol_fingerprint
            or not _REQUIRED_EVENT_COLUMNS.issubset(columns)
        ):
            raise DashboardDataError("telemetry preview schema is incompatible")

    def _outcome_metric(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        event_name: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_total,
                COALESCE(
                    SUM(CASE WHEN outcome = 'success' THEN 1.0 / sample_rate ELSE 0 END),
                    0.0
                ) AS estimated_success
            FROM events
            WHERE event_name = ?
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            """,
            (event_name, *window.sql_params),
        ).fetchone()
        total = float(row["estimated_total"])
        success = float(row["estimated_success"])
        return {
            "estimatedEvents": _public_count(total),
            "estimatedSuccesses": _public_count(success),
            "successRate": _rate(success, total),
            "p95DurationMs": self._weighted_p95(
                connection,
                window,
                event_name=event_name,
                value_expression="duration_ms",
                extra_predicate="duration_ms IS NOT NULL AND duration_ms >= 0",
            ),
        }

    def _weighted_p95(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        *,
        event_name: str,
        value_expression: str,
        extra_predicate: str,
    ) -> int | None:
        # SQL fragments are private constants selected by callers in this module.
        row = connection.execute(
            f"""
            WITH grouped AS (
                SELECT {value_expression} AS value, SUM(1.0 / sample_rate) AS weight
                FROM events
                WHERE event_name = ?
                  AND occurred_at_utc >= ?
                  AND occurred_at_utc < ?
                  AND sample_rate > 0
                  AND {extra_predicate}
                GROUP BY {value_expression}
            ), ranked AS (
                SELECT
                    value,
                    SUM(weight) OVER (
                        ORDER BY value ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cumulative_weight,
                    SUM(weight) OVER () AS total_weight
                FROM grouped
            )
            SELECT MIN(value) AS p95
            FROM ranked
            WHERE cumulative_weight >= total_weight * 0.95
            """,
            (event_name, *window.sql_params),
        ).fetchone()
        value = row["p95"]
        return int(value) if value is not None else None

    def _json_counter_rate(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        *,
        event_name: str,
        numerator_field: str,
        denominator_mode: str,
    ) -> float | None:
        if denominator_mode != "event":  # pragma: no cover - internal invariant
            raise ValueError("unsupported denominator mode")
        json_path = f"$.{numerator_field}"
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS denominator,
                COALESCE(SUM(
                    CASE WHEN json_extract(payload_json, ?) > 0
                         THEN 1.0 / sample_rate ELSE 0 END
                ), 0.0) AS numerator
            FROM events
            WHERE event_name = ?
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            """,
            (json_path, event_name, *window.sql_params),
        ).fetchone()
        return _rate(float(row["numerator"]), float(row["denominator"]))

    def _dimension_outcomes(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        *,
        event_name: str,
        json_field: str,
        allowed_values: Sequence[str],
    ) -> list[dict[str, Any]]:
        json_path = f"$.{json_field}"
        rows = connection.execute(
            """
            SELECT
                json_extract(payload_json, ?) AS dimension,
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_total,
                COALESCE(SUM(
                    CASE WHEN outcome = 'success' THEN 1.0 / sample_rate ELSE 0 END
                ), 0.0) AS estimated_success
            FROM events
            WHERE event_name = ?
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            GROUP BY json_extract(payload_json, ?)
            """,
            (json_path, event_name, *window.sql_params, json_path),
        ).fetchall()
        allowed = frozenset(allowed_values)
        result: list[dict[str, Any]] = []
        for row in rows:
            dimension = row["dimension"]
            if not isinstance(dimension, str) or dimension not in allowed:
                continue
            total = float(row["estimated_total"])
            success = float(row["estimated_success"])
            result.append(
                {
                    "dimension": dimension,
                    "estimatedEvents": _public_count(total),
                    "successRate": _rate(success, total),
                }
            )
        result.sort(key=lambda item: str(item["dimension"]))
        return result

    def _turn_failure_stages(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT
                COALESCE(json_extract(payload_json, '$.failure_stage'), 'unclassified')
                    AS dimension,
                COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_total
            FROM events
            WHERE event_name = 'turn_result'
              AND outcome IN ('fail', 'timeout', 'cancel')
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            GROUP BY COALESCE(
                json_extract(payload_json, '$.failure_stage'),
                'unclassified'
            )
            """,
            window.sql_params,
        ).fetchall()
        allowed = {stage.value for stage in TurnFailureStage} | {"unclassified"}
        return self._public_failure_counts(rows, allowed_values=allowed)

    def _turn_error_codes(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT error_code AS dimension,
                   COALESCE(SUM(1.0 / sample_rate), 0.0) AS estimated_total
            FROM events
            WHERE event_name = 'turn_result'
              AND outcome IN ('fail', 'timeout', 'cancel')
              AND error_code IS NOT NULL
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
              AND sample_rate > 0
            GROUP BY error_code
            """,
            window.sql_params,
        ).fetchall()
        return self._public_failure_counts(
            rows,
            allowed_values={code.value for code in TurnErrorCode},
        )

    @staticmethod
    def _public_failure_counts(
        rows: Sequence[sqlite3.Row],
        *,
        allowed_values: set[str],
    ) -> list[dict[str, Any]]:
        result = [
            {
                "dimension": str(row["dimension"]),
                "estimatedEvents": _public_count(float(row["estimated_total"])),
            }
            for row in rows
            if isinstance(row["dimension"], str) and row["dimension"] in allowed_values
        ]
        result.sort(key=lambda item: (-float(item["estimatedEvents"]), item["dimension"]))
        return result

    def _crash_free_sessions(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            WITH sessions AS (
                SELECT app_session_id
                FROM events
                WHERE app_session_id IS NOT NULL
                  AND occurred_at_utc >= ?
                  AND occurred_at_utc < ?
                GROUP BY app_session_id
            ), crashed AS (
                SELECT app_session_id
                FROM events
                WHERE event_name = 'app_crash_detected'
                  AND app_session_id IS NOT NULL
                  AND occurred_at_utc >= ?
                  AND occurred_at_utc < ?
                GROUP BY app_session_id
            )
            SELECT
                COUNT(*) AS sessions,
                COALESCE(SUM(CASE WHEN crashed.app_session_id IS NOT NULL THEN 1 ELSE 0 END), 0)
                    AS crashed_sessions
            FROM sessions
            LEFT JOIN crashed USING (app_session_id)
            """,
            (*window.sql_params, *window.sql_params),
        ).fetchone()
        sessions = int(row["sessions"])
        crashed = int(row["crashed_sessions"])
        return {
            "sessions": sessions,
            "crashedSessions": crashed,
            "crashFreeRate": _rate(sessions - crashed, sessions),
        }

    def _performance_summary(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS summaries,
                COALESCE(SUM(json_extract(payload_json, '$.turn_count')), 0) AS turns,
                COALESCE(SUM(json_extract(payload_json, '$.stalled_turn_count')), 0)
                    AS stalled_turns,
                COALESCE(SUM(json_extract(payload_json, '$.monitored_request_count')), 0)
                    AS monitored_requests,
                COALESCE(SUM(json_extract(payload_json, '$.slow_request_count')), 0)
                    AS slow_requests
            FROM events
            WHERE event_name = 'performance_summary'
              AND occurred_at_utc >= ?
              AND occurred_at_utc < ?
            """,
            window.sql_params,
        ).fetchone()
        turns = int(row["turns"])
        stalled_turns = int(row["stalled_turns"])
        monitored = int(row["monitored_requests"])
        slow = int(row["slow_requests"])
        return {
            "summaries": int(row["summaries"]),
            "stalledTurnRate": _rate(stalled_turns, turns),
            "slowRequestRate": _rate(slow, monitored),
        }

    def _funnel(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
        *,
        id_column: str,
        stages: Sequence[_FunnelStage],
    ) -> dict[str, Any]:
        if id_column not in {"acquisition_id", "analytics_user_id"}:
            raise ValueError("unsupported funnel identifier")
        if not stages:
            raise ValueError("funnel requires stages")

        ctes: list[str] = []
        params: list[Any] = []
        first = stages[0]
        first_outcome = "AND outcome = ?" if first.outcome is not None else ""
        ctes.append(
            f"""
            first_stage AS (
                SELECT {id_column} AS journey_key, MIN(occurred_at_utc) AS reached_at
                FROM events
                WHERE {id_column} IS NOT NULL
                  AND event_name = ?
                  {first_outcome}
                GROUP BY {id_column}
            )
            """
        )
        params.append(first.event_name)
        if first.outcome is not None:
            params.append(first.outcome)
        ctes.append(
            """
            stage_0 AS (
                SELECT journey_key, reached_at
                FROM first_stage
                WHERE reached_at >= ? AND reached_at < ?
            )
            """
        )
        params.extend(window.sql_params)

        for index, stage in enumerate(stages[1:], start=1):
            previous = index - 1
            outcome_clause = "AND candidate.outcome = ?" if stage.outcome is not None else ""
            ctes.append(
                f"""
                stage_{index} AS (
                    SELECT
                        prior.journey_key,
                        MIN(candidate.occurred_at_utc) AS reached_at
                    FROM stage_{previous} AS prior
                    LEFT JOIN events AS candidate
                      ON candidate.{id_column} = prior.journey_key
                     AND prior.reached_at IS NOT NULL
                     AND candidate.event_name = ?
                     {outcome_clause}
                     AND candidate.occurred_at_utc >= prior.reached_at
                     AND julianday(candidate.occurred_at_utc)
                         <= julianday(prior.reached_at) + (? / 24.0)
                    GROUP BY prior.journey_key
                )
                """
            )
            params.append(stage.event_name)
            if stage.outcome is not None:
                params.append(stage.outcome)
            params.append(stage.window_hours_from_previous)

        count_expressions = [
            "(SELECT COUNT(*) FROM stage_0) AS stage_0",
            *(
                f"(SELECT COUNT(reached_at) FROM stage_{index}) AS stage_{index}"
                for index in range(1, len(stages))
            ),
        ]
        row = connection.execute(
            f"WITH {','.join(ctes)} SELECT {','.join(count_expressions)}",
            params,
        ).fetchone()
        counts = [int(row[f"stage_{index}"]) for index in range(len(stages))]
        public_stages = [
            {"stage": stage.key, "deduplicatedCount": count}
            for stage, count in zip(stages, counts, strict=True)
        ]
        transitions = []
        for index, stage in enumerate(stages[1:], start=1):
            current = counts[index - 1]
            reached = counts[index]
            transitions.append(
                {
                    "from": stages[index - 1].key,
                    "to": stage.key,
                    "windowHours": stage.window_hours_from_previous,
                    "dropoffRate": _rate(current - reached, current),
                }
            )
        return {
            "deduplicationUnit": (
                "acquisition journey" if id_column == "acquisition_id" else "analytics user"
            ),
            "stages": public_stages,
            "transitions": transitions,
        }

    def _linked_install_to_ready(
        self,
        connection: sqlite3.Connection,
        window: UtcCohortWindow,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            WITH first_install AS (
                SELECT acquisition_id, MIN(occurred_at_utc) AS installed_at
                FROM events
                WHERE acquisition_id IS NOT NULL
                  AND event_name = 'install_result'
                  AND outcome = 'success'
                GROUP BY acquisition_id
            ), cohort AS (
                SELECT acquisition_id, installed_at
                FROM first_install
                WHERE installed_at >= ? AND installed_at < ?
            ), linked AS (
                SELECT
                    cohort.acquisition_id,
                    cohort.installed_at,
                    (
                        SELECT registration.analytics_user_id
                        FROM events AS registration
                        WHERE registration.event_name = 'registration_result'
                          AND registration.outcome = 'success'
                          AND registration.acquisition_id = cohort.acquisition_id
                          AND registration.analytics_user_id IS NOT NULL
                          AND registration.occurred_at_utc >= cohort.installed_at
                          AND julianday(registration.occurred_at_utc)
                              <= julianday(cohort.installed_at) + 7
                        ORDER BY registration.occurred_at_utc
                        LIMIT 1
                    ) AS linked_analytics_key
                FROM cohort
            ), reached AS (
                SELECT
                    linked.acquisition_id,
                    linked.linked_analytics_key,
                    EXISTS (
                        SELECT 1
                        FROM events AS ready
                        WHERE ready.event_name = 'first_app_ready'
                          AND ready.analytics_user_id = linked.linked_analytics_key
                          AND ready.occurred_at_utc >= linked.installed_at
                          AND julianday(ready.occurred_at_utc)
                              <= julianday(linked.installed_at) + 1
                    ) AS ready_within_window
                FROM linked
            )
            SELECT
                COUNT(*) AS eligible_installations,
                COALESCE(SUM(linked_analytics_key IS NOT NULL), 0) AS linkable_installations,
                COALESCE(SUM(
                    linked_analytics_key IS NOT NULL AND ready_within_window
                ), 0) AS linked_ready
            FROM reached
            """,
            window.sql_params,
        ).fetchone()
        eligible = int(row["eligible_installations"])
        linkable = int(row["linkable_installations"])
        ready = int(row["linked_ready"])
        return {
            "eligibleInstallations": eligible,
            "linkableInstallations": linkable,
            "unlinkedInstallations": eligible - linkable,
            "readyWithin24Hours": ready,
            "linkCoverageRate": _rate(linkable, eligible),
            "conversionRateAmongLinked": _rate(ready, linkable),
        }


def _require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("dashboard cohort timestamps must be UTC")
    return value.astimezone(UTC)


def _wire_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rate(numerator: float | int, denominator: float | int) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _public_count(value: float) -> int | float:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return int(round(value))
    return round(value, 3)


__all__ = [
    "DashboardDataError",
    "DashboardQueries",
    "UtcCohortWindow",
]
