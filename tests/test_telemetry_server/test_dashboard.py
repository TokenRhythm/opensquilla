from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from opensquilla.telemetry.contracts import TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
from opensquilla.telemetry.server.auth import hash_dashboard_password
from opensquilla.telemetry.server.dashboard import (
    LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    _read_urlencoded_form,
    create_dashboard_app,
)

_BASE = "/telemetry-v2-preview"
_CLOCK = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_RAW_EVENT_ID = "00000000-0000-4000-8000-000000000001"
_RAW_SESSION_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def dashboard_credential() -> str:
    return hash_dashboard_password("preview password", salt=b"d" * 16)


def _database(tmp_path: Path, scope: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{scope}.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version=1;
            CREATE TABLE meta (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                scope TEXT NOT NULL,
                protocol_fingerprint TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
            CREATE TABLE ingest_batches (
                batch_id TEXT PRIMARY KEY,
                body_sha256 TEXT,
                sent_at_utc TEXT,
                received_at_utc TEXT,
                accepted_count INTEGER,
                duplicate_count INTEGER
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
            INSERT INTO meta VALUES (
                1, 1, ?, ?, '2026-09-01T00:00:00.000Z'
            )
            """,
            (scope, TELEMETRY_PROTOCOL_FINGERPRINT_SHA256),
        )
        if scope == "reliability":
            connection.execute(
                """
                INSERT INTO events VALUES (
                    ?, ?, 'app_start_result', 1, '2026-09-02T00:00:00.000Z',
                    'desktop', '1.2.3', 'macos', 'success', NULL, 125, 1.0,
                    'reliability-v1', ?, NULL, NULL, ?, NULL,
                    '2026-09-02T00:00:01.000Z'
                )
                """,
                (
                    _RAW_EVENT_ID,
                    "a" * 64,
                    _RAW_SESSION_ID,
                    json.dumps({"failure_stage": None}),
                ),
            )
            connection.execute(
                """
                INSERT INTO events VALUES (
                    ?, ?, 'turn_result', 3, '2026-09-02T00:05:00.000Z',
                    'gateway', '1.2.3', 'macos', 'timeout', 'provider_timeout',
                    30000, 1.0, 'reliability-v1', ?, NULL, NULL, ?, NULL,
                    '2026-09-02T00:05:01.000Z'
                )
                """,
                (
                    "00000000-0000-4000-8000-000000000003",
                    "b" * 64,
                    _RAW_SESSION_ID,
                    json.dumps(
                        {
                            "failure_stage": "agent_execution",
                            "ttft_ms": 500,
                            "stall_count": 1,
                        }
                    ),
                ),
            )
    return path


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
            INSERT INTO events VALUES (
                1, '2026-09-02T00:00:00Z', 'install',
                'private-legacy-install-id', '1.2.3', 'desktop', 'darwin', 0
            );
            """
        )
    return path


def _app(tmp_path: Path, credential: str):
    return create_dashboard_app(
        reliability_db_path=_database(tmp_path, "reliability"),
        growth_db_path=_database(tmp_path, "growth"),
        legacy_install_db_path=_legacy_database(tmp_path),
        credential=credential,
        session_secret=b"h" * 32,
        clock=lambda: _CLOCK,
    )


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([A-Za-z0-9._-]+)"', html)
    assert match is not None
    return match.group(1)


def _login(client: TestClient) -> None:
    page = client.get(f"{_BASE}/login")
    response = client.post(
        f"{_BASE}/login",
        data={"csrf_token": _csrf(page.text), "password": "preview password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _cookie_header(response, name: str) -> str:
    cookies = response.headers.get_list("set-cookie")
    return next(cookie for cookie in cookies if cookie.startswith(f"{name}="))


def _stream_request(messages: list[dict[str, object]]) -> Request:
    queue = list(messages)

    async def receive() -> dict[str, object]:
        return queue.pop(0)

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": f"{_BASE}/login",
            "raw_path": f"{_BASE}/login".encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("127.0.0.1", 12345),
            "server": ("preview.test", 443),
        },
        receive,
    )


def test_unauthenticated_pages_and_apis_never_reveal_metrics(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(_BASE, follow_redirects=False)
        api = client.get(f"{_BASE}/api/summary")

    assert page.status_code == 303
    assert page.headers["location"] == f"{_BASE}/login"
    assert _RAW_EVENT_ID not in page.text
    assert api.status_code == 401
    assert api.json() == {"error": "authentication_required"}
    assert _RAW_EVENT_ID not in api.text


def test_login_uses_scoped_secure_cookie_and_csrf(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        csrf_cookie = _cookie_header(page, LOGIN_CSRF_COOKIE_NAME)
        assert "Secure" in csrf_cookie
        assert "HttpOnly" in csrf_cookie
        assert "SameSite=strict" in csrf_cookie
        assert f"Path={_BASE}" in csrf_cookie

        missing_csrf = client.post(
            f"{_BASE}/login",
            data={"csrf_token": "missing", "password": "preview password"},
        )
        assert missing_csrf.status_code == 403

        refreshed = client.get(f"{_BASE}/login")
        wrong = client.post(
            f"{_BASE}/login",
            data={"csrf_token": _csrf(refreshed.text), "password": "wrong"},
        )
        assert wrong.status_code == 401
        assert not any(
            cookie.startswith(f"{SESSION_COOKIE_NAME}=")
            for cookie in wrong.headers.get_list("set-cookie")
        )

        accepted = client.post(
            f"{_BASE}/login",
            data={"csrf_token": _csrf(wrong.text), "password": "preview password"},
            follow_redirects=False,
        )
        session_cookie = _cookie_header(accepted, SESSION_COOKIE_NAME)

    assert accepted.status_code == 303
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert f"Path={_BASE}" in session_cookie


def test_same_origin_login_accepts_signed_token_when_embedded_cookie_is_absent(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        token = _csrf(page.text)
        client.cookies.clear()
        accepted = client.post(
            f"{_BASE}/login",
            data={"csrf_token": token, "password": "preview password"},
            headers={"origin": "https://preview.test"},
            follow_redirects=False,
        )

    assert accepted.status_code == 303
    assert _cookie_header(accepted, SESSION_COOKIE_NAME)


def test_login_treats_explicit_default_https_port_as_same_origin(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        accepted = client.post(
            f"{_BASE}/login",
            data={
                "csrf_token": _csrf(page.text),
                "password": "preview password",
            },
            headers={"origin": "https://preview.test:443"},
            follow_redirects=False,
        )

    assert accepted.status_code == 303


def test_embedded_same_site_navigation_accepts_signed_login_token(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        accepted = client.post(
            f"{_BASE}/login",
            data={
                "csrf_token": _csrf(page.text),
                "password": "preview password",
            },
            headers={
                "origin": "null",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            },
            follow_redirects=False,
        )

    assert accepted.status_code == 303


def test_embedded_cross_site_navigation_is_rejected(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        rejected = client.post(
            f"{_BASE}/login",
            data={
                "csrf_token": _csrf(page.text),
                "password": "preview password",
            },
            headers={
                "origin": "https://attacker.invalid",
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            },
        )

    assert rejected.status_code == 403


def test_authenticated_page_and_api_contain_aggregates_but_no_telemetry_ids(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        _login(client)
        page = client.get(_BASE)
        api = client.get(f"{_BASE}/api/summary")

    assert page.status_code == 200
    assert "稳定性质量" in page.text
    assert "Agent 出错步骤" in page.text
    assert "Agent 执行" in page.text
    assert "模型服务超时" in page.text
    assert "macOS" in page.text
    assert "darwin" not in page.text
    assert "安装与版本演进" in page.text
    assert 'id="install-trend"' in page.text
    assert 'data-installations="1"' in page.text
    assert "每日稳定性趋势" in page.text
    assert 'id="reliability-trend"' in page.text
    assert 'data-events="2"' in page.text
    assert "landing_view" not in page.text
    assert "first_app_ready" not in page.text
    assert '<progress' in page.text
    assert api.status_code == 200
    assert api.json()["reliability"]["appStart"]["estimatedEvents"] == 1
    assert api.json()["legacyInstallation"]["installations"] == 1
    combined = page.text + api.text
    for forbidden in (
        _RAW_EVENT_ID,
        _RAW_SESSION_ID,
        "event_id",
        "app_session_id",
        "acquisition_id",
        "analytics_user_id",
        "payload_json",
        "private-legacy-install-id",
    ):
        assert forbidden not in combined
    assert page.headers["cache-control"].startswith("no-store")
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "script-src 'nonce-" in page.headers["content-security-policy"]
    assert "<script src=" not in page.text


def test_logout_requires_session_bound_csrf_and_same_origin(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        _login(client)
        page = client.get(_BASE)
        token = _csrf(page.text)

        cross_origin = client.post(
            f"{_BASE}/logout",
            data={"csrf_token": token},
            headers={"origin": "https://attacker.invalid"},
        )
        assert cross_origin.status_code == 403
        assert client.get(f"{_BASE}/api/summary").status_code == 200

        invalid = client.post(
            f"{_BASE}/logout",
            data={"csrf_token": token + "x"},
        )
        assert invalid.status_code == 403

        logged_out = client.post(
            f"{_BASE}/logout",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        deletion = _cookie_header(logged_out, SESSION_COOKIE_NAME)
        assert client.get(f"{_BASE}/api/summary").status_code == 401

    assert logged_out.status_code == 303
    assert "Max-Age=0" in deletion
    assert "Secure" in deletion
    assert "HttpOnly" in deletion
    assert "SameSite=strict" in deletion
    assert f"Path={_BASE}" in deletion


def test_invalid_cohort_and_missing_databases_are_sanitized(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path / "valid", dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        _login(client)
        invalid = client.get(f"{_BASE}/api/summary?start=bad&end=2026-09-30")
        assert invalid.status_code == 400
        assert invalid.json() == {"error": "invalid_utc_cohort"}

    missing_path = tmp_path / "private-do-not-leak.sqlite3"
    app = create_dashboard_app(
        reliability_db_path=missing_path,
        growth_db_path=missing_path,
        credential=dashboard_credential,
        session_secret=b"h" * 32,
        clock=lambda: _CLOCK,
    )
    with TestClient(app, base_url="https://preview.test") as client:
        _login(client)
        unavailable = client.get(f"{_BASE}/api/summary")

    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "preview_data_unavailable"}
    assert str(missing_path) not in unavailable.text


def test_one_scope_failure_does_not_hide_the_healthy_scope(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    reliability = _database(tmp_path, "reliability")
    missing_growth = tmp_path / "missing-growth.sqlite3"
    app = create_dashboard_app(
        reliability_db_path=reliability,
        growth_db_path=missing_growth,
        credential=dashboard_credential,
        session_secret=b"h" * 32,
        clock=lambda: _CLOCK,
    )
    with TestClient(app, base_url="https://preview.test") as client:
        _login(client)
        page = client.get(_BASE)
        api = client.get(f"{_BASE}/api/summary")

    assert page.status_code == 200
    assert 'id="reliability-trend"' in page.text
    assert 'data-events="2"' in page.text
    assert "用户增长数据暂不可用" in page.text
    assert api.status_code == 200
    assert api.json()["reliability"]["appStart"]["estimatedEvents"] == 1
    assert api.json()["growth"] is None
    assert api.json()["scopeAvailability"] == {
        "legacyInstallation": {"available": False},
        "reliability": {"available": True},
        "growth": {"available": False},
    }
    assert str(missing_growth) not in page.text + api.text


def test_cross_origin_login_is_rejected_even_with_valid_csrf(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    with TestClient(
        _app(tmp_path, dashboard_credential),
        base_url="https://preview.test",
    ) as client:
        page = client.get(f"{_BASE}/login")
        response = client.post(
            f"{_BASE}/login",
            data={"csrf_token": _csrf(page.text), "password": "preview password"},
            headers={"origin": "https://attacker.invalid"},
        )

    assert response.status_code == 403
    assert not any(
        cookie.startswith(f"{SESSION_COOKIE_NAME}=")
        for cookie in response.headers.get_list("set-cookie")
    )


async def test_chunked_form_reader_is_bounded_and_handles_disconnect() -> None:
    exact_body = b"value=" + (b"x" * (4096 - len(b"value=")))
    exact = _stream_request(
        [{"type": "http.request", "body": exact_body, "more_body": False}]
    )
    assert await _read_urlencoded_form(exact, expected={"value"}) == {
        "value": "x" * (4096 - len(b"value="))
    }

    oversized = _stream_request(
        [
            {"type": "http.request", "body": b"value=" + (b"x" * 3000), "more_body": True},
            {"type": "http.request", "body": b"x" * 2000, "more_body": False},
        ]
    )
    assert await _read_urlencoded_form(oversized, expected={"value"}) is None

    disconnected = _stream_request([{"type": "http.disconnect"}])
    assert await _read_urlencoded_form(disconnected, expected={"value"}) is None


def test_explicit_https_public_origin_supports_a_tls_reverse_proxy(
    tmp_path: Path,
    dashboard_credential: str,
) -> None:
    app = create_dashboard_app(
        reliability_db_path=_database(tmp_path, "reliability"),
        growth_db_path=_database(tmp_path, "growth"),
        credential=dashboard_credential,
        session_secret=b"h" * 32,
        public_origin="https://preview.example/",
        clock=lambda: _CLOCK,
    )
    with TestClient(app, base_url="https://internal-proxy.test") as client:
        page = client.get(f"{_BASE}/login")
        response = client.post(
            f"{_BASE}/login",
            data={"csrf_token": _csrf(page.text), "password": "preview password"},
            headers={"origin": "https://preview.example"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with pytest.raises(ValueError, match="HTTPS origin"):
        create_dashboard_app(
            reliability_db_path=tmp_path / "unused-a.sqlite3",
            growth_db_path=tmp_path / "unused-b.sqlite3",
            credential=dashboard_credential,
            session_secret=b"h" * 32,
            public_origin="http://preview.example",
        )
