from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from opensquilla.telemetry.server.dashboard_queries import (
    DashboardDataError,
    UtcCohortWindow,
)
from opensquilla.telemetry.server.legacy_installations import LegacyInstallationQueries


def _legacy_database(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                received_at TEXT NOT NULL,
                event TEXT NOT NULL,
                install_hash TEXT NOT NULL,
                opensquilla_version TEXT NOT NULL,
                install_method TEXT NOT NULL,
                os TEXT NOT NULL,
                ci_environment INTEGER
            );
            """
        )
    return path


def _insert(
    path: Path,
    *,
    sequence: int,
    received_at: str,
    install_hash: str,
    version: str = "1.0.0",
    operating_system: str = "darwin",
    event: str = "install",
    install_method: str = "desktop",
    ci_environment: int = 0,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO events(
                id, received_at, event, install_hash, opensquilla_version,
                install_method, os, ci_environment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                received_at,
                event,
                install_hash,
                version,
                install_method,
                operating_system,
                ci_environment,
            ),
        )


def _window() -> UtcCohortWindow:
    return UtcCohortWindow.from_dates("2026-09-01", "2026-09-30")


def test_legacy_installations_preserve_v1_first_install_definition(tmp_path: Path) -> None:
    path = _legacy_database(tmp_path)
    _insert(
        path,
        sequence=1,
        received_at="2026-09-01T01:00:00Z",
        install_hash="install-one",
        version="1.0.0",
        operating_system="Darwin",
    )
    _insert(
        path,
        sequence=2,
        received_at="2026-09-02T01:00:00Z",
        install_hash="install-one",
        version="1.1.0",
        operating_system="Darwin",
    )
    _insert(
        path,
        sequence=3,
        received_at="2026-09-02T02:00:00Z",
        install_hash="install-two",
        version="",
        operating_system="Windows 11",
    )
    _insert(
        path,
        sequence=4,
        received_at="2026-08-31T23:00:00Z",
        install_hash="installed-before-cohort",
    )
    _insert(
        path,
        sequence=5,
        received_at="2026-09-03T01:00:00Z",
        install_hash="installed-before-cohort",
    )
    _insert(
        path,
        sequence=6,
        received_at="2026-09-03T02:00:00Z",
        install_hash="ci-install",
        ci_environment=1,
    )
    _insert(
        path,
        sequence=7,
        received_at="2026-09-03T03:00:00Z",
        install_hash="non-desktop",
        install_method="pip",
    )
    _insert(
        path,
        sequence=8,
        received_at="2026-09-03T04:00:00Z",
        install_hash="version-signal",
        event="version_seen",
    )

    result = LegacyInstallationQueries(path).summary(_window())

    assert result["installations"] == 2
    assert result["sourceVersion"] == "telemetry-v1"
    assert result["timezone"] == "UTC"
    assert result["asOfReceivedUtc"] == "2026-09-03T01:00:00Z"
    assert len(result["dailyTrend"]) == 30
    assert result["dailyTrend"][:3] == [
        {"date": "2026-09-01", "installations": 1},
        {"date": "2026-09-02", "installations": 1},
        {"date": "2026-09-03", "installations": 0},
    ]
    assert result["versions"] == [
        {"dimension": "1.0.0", "installations": 1, "share": 0.5},
        {"dimension": "未知版本", "installations": 1, "share": 0.5},
    ]
    assert result["operatingSystems"] == [
        {"dimension": "macOS", "installations": 1, "share": 0.5},
        {"dimension": "Windows", "installations": 1, "share": 0.5},
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    for private_value in (
        "install-one",
        "install-two",
        "installed-before-cohort",
        "install_hash",
    ):
        assert private_value not in serialized


def test_legacy_database_is_opened_read_only(tmp_path: Path) -> None:
    queries = LegacyInstallationQueries(_legacy_database(tmp_path))

    with queries._open() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write(value TEXT)")


def test_legacy_schema_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "incompatible.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events(received_at TEXT)")

    with pytest.raises(DashboardDataError, match="incompatible"):
        LegacyInstallationQueries(path).summary(_window())
