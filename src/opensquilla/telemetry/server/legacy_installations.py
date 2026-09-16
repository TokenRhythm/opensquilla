"""Read-only compatibility adapter for the telemetry v1 installation history."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from opensquilla.telemetry.server.dashboard_queries import (
    DashboardDataError,
    UtcCohortWindow,
)

_REQUIRED_EVENT_COLUMNS: Final = frozenset(
    {
        "received_at",
        "event",
        "install_hash",
        "opensquilla_version",
        "install_method",
        "os",
        "ci_environment",
    }
)

_COUNTED_INSTALLS: Final = (
    "event = 'install' AND COALESCE(ci_environment, 0) = 0 AND install_method = 'desktop'"
)


class LegacyInstallationQueries:
    """Expose v1 installation aggregates without exposing legacy identifiers."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path).expanduser()

    def summary(self, window: UtcCohortWindow) -> dict[str, Any]:
        with self._open() as connection:
            row = connection.execute(
                f"""
                WITH ranked_installs AS MATERIALIZED (
                    SELECT
                        received_at AS first_received_at,
                        CASE
                            WHEN trim(opensquilla_version) = '' THEN '未知版本'
                            ELSE trim(opensquilla_version)
                        END AS version,
                        CASE
                            WHEN lower(trim(os)) IN ('darwin', 'macos', 'mac os', 'osx')
                                THEN 'macOS'
                            WHEN lower(trim(os)) LIKE 'win%' THEN 'Windows'
                            WHEN lower(trim(os)) LIKE 'linux%' THEN 'Linux'
                            ELSE COALESCE(NULLIF(trim(os), ''), '未知系统')
                        END AS operating_system,
                        ROW_NUMBER() OVER (
                            PARTITION BY install_hash
                            ORDER BY received_at, rowid
                        ) AS record_number
                    FROM events
                    WHERE {_COUNTED_INSTALLS}
                ), devices AS MATERIALIZED (
                    SELECT first_received_at, version, operating_system
                    FROM ranked_installs
                    WHERE record_number = 1
                      AND first_received_at >= ?
                      AND first_received_at < ?
                ), trend AS (
                    SELECT
                        strftime('%Y-%m-%d', first_received_at) AS utc_day,
                        COUNT(*) AS installations
                    FROM devices
                    GROUP BY utc_day
                ), versions AS (
                    SELECT version AS dimension, COUNT(*) AS installations
                    FROM devices
                    GROUP BY version
                ), operating_systems AS (
                    SELECT operating_system AS dimension, COUNT(*) AS installations
                    FROM devices
                    GROUP BY operating_system
                )
                SELECT
                    (SELECT COUNT(*) FROM devices) AS installations,
                    (
                        SELECT MAX(received_at)
                        FROM events
                        WHERE received_at >= ?
                          AND received_at < ?
                          AND {_COUNTED_INSTALLS}
                    ) AS watermark,
                    (
                        SELECT json_group_array(
                            json_object('date', utc_day, 'installations', installations)
                        )
                        FROM trend
                    ) AS trend,
                    (
                        SELECT json_group_array(
                            json_object('dimension', dimension, 'installations', installations)
                        )
                        FROM versions
                    ) AS versions,
                    (
                        SELECT json_group_array(
                            json_object('dimension', dimension, 'installations', installations)
                        )
                        FROM operating_systems
                    ) AS operating_systems
                """,
                (*window.sql_params, *window.sql_params),
            ).fetchone()
            if row is None:  # pragma: no cover - aggregate queries always return one row
                raise DashboardDataError("legacy installation data is unavailable")
            total = int(row["installations"])
            return {
                "asOfReceivedUtc": _string_or_none(row["watermark"]),
                "sourceVersion": "telemetry-v1",
                "deduplicationUnit": "installation",
                "timezone": "UTC",
                "installations": total,
                "dailyTrend": _zero_filled_trend(row["trend"], window),
                "versions": _coverage_rows(row["versions"], total),
                "operatingSystems": _coverage_rows(row["operating_systems"], total),
            }

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        try:
            resolved = self._path.resolve(strict=True)
            if not resolved.is_file():
                raise OSError
            connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except (OSError, sqlite3.Error):
            raise DashboardDataError("legacy installation data is unavailable") from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=5000")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or query_only[0] != 1:
                raise DashboardDataError("legacy installation database is not read-only")
            connection.execute("BEGIN")
            self._validate_schema(connection)
            yield connection
        except DashboardDataError:
            raise
        except sqlite3.Error:
            raise DashboardDataError("legacy installation data is unavailable") from None
        finally:
            if connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
        except sqlite3.Error:
            raise DashboardDataError("legacy installation schema is incompatible") from None
        if not _REQUIRED_EVENT_COLUMNS.issubset(columns):
            raise DashboardDataError("legacy installation schema is incompatible")


def _zero_filled_trend(value: object, window: UtcCohortWindow) -> list[dict[str, Any]]:
    rows = json.loads(str(value)) if value else []
    counts = {str(row["date"]): int(row["installations"]) for row in rows if isinstance(row, dict)}
    day_count = (window.end_exclusive.date() - window.start.date()).days
    return [
        {
            "date": (window.start + timedelta(days=offset)).date().isoformat(),
            "installations": counts.get(
                (window.start + timedelta(days=offset)).date().isoformat(),
                0,
            ),
        }
        for offset in range(day_count)
    ]


def _coverage_rows(value: object, total: int) -> list[dict[str, Any]]:
    rows = json.loads(str(value)) if value else []
    result: list[dict[str, Any]] = [
        {
            "dimension": str(row["dimension"]),
            "installations": int(row["installations"]),
            "share": round(int(row["installations"]) / total, 6) if total else None,
        }
        for row in rows
        if isinstance(row, dict)
    ]
    result.sort(key=lambda row: (-int(row["installations"]), str(row["dimension"]).casefold()))
    return result


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["LegacyInstallationQueries"]
