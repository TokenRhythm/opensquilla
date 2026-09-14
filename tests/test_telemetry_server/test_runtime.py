from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.telemetry.server.auth import hash_dashboard_password
from opensquilla.telemetry.server.dashboard import DEFAULT_PREVIEW_PATH
from opensquilla.telemetry.server.runtime import (
    ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64,
    ENV_GROWTH_CDN_SECRET_B64,
    ENV_GROWTH_DB_PATH,
    ENV_GROWTH_HOST,
    ENV_GROWTH_PORT,
    ENV_GROWTH_WEBSITE_SECRET_B64,
    ENV_PREVIEW_CREDENTIAL,
    ENV_PREVIEW_GROWTH_DB_PATH,
    ENV_PREVIEW_HOST,
    ENV_PREVIEW_LEGACY_INSTALL_DB_PATH,
    ENV_PREVIEW_PATH,
    ENV_PREVIEW_PORT,
    ENV_PREVIEW_PUBLIC_ORIGIN,
    ENV_PREVIEW_RELIABILITY_DB_PATH,
    ENV_PREVIEW_SESSION_SECRET_B64,
    ENV_RELIABILITY_DB_PATH,
    ENV_RELIABILITY_HOST,
    ENV_RELIABILITY_PORT,
    GROWTH_PORT,
    PREVIEW_PORT,
    RELIABILITY_PORT,
    RuntimeEnvironmentError,
    TelemetryService,
    create_growth_collector_from_env,
    create_preview_dashboard_from_env,
    create_reliability_collector_from_env,
    load_growth_runtime,
    load_preview_runtime,
    load_reliability_runtime,
    run_service,
)


def _collector_env(tmp_path: Path, service: TelemetryService) -> dict[str, str]:
    directory = tmp_path / service.value
    directory.mkdir(parents=True)
    if service is TelemetryService.RELIABILITY:
        return {
            ENV_RELIABILITY_DB_PATH: str(directory / "events.sqlite3"),
            ENV_RELIABILITY_HOST: "127.0.0.1",
            ENV_RELIABILITY_PORT: str(RELIABILITY_PORT),
        }
    if service is TelemetryService.GROWTH:
        return {
            ENV_GROWTH_DB_PATH: str(directory / "events.sqlite3"),
            ENV_GROWTH_HOST: "127.0.0.1",
            ENV_GROWTH_PORT: str(GROWTH_PORT),
            ENV_GROWTH_WEBSITE_SECRET_B64: _secret_b64(b"w"),
            ENV_GROWTH_CDN_SECRET_B64: _secret_b64(b"c"),
            ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64: _secret_b64(b"a"),
        }
    raise AssertionError("preview is not a collector")


def _secret_b64(byte: bytes = b"s") -> str:
    return base64.urlsafe_b64encode(byte * 32).rstrip(b"=").decode("ascii")


def _preview_env(tmp_path: Path, credential: str) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    reliability = tmp_path / "reliability.sqlite3"
    growth = tmp_path / "growth.sqlite3"
    reliability.touch()
    growth.touch()
    return {
        ENV_PREVIEW_RELIABILITY_DB_PATH: str(reliability),
        ENV_PREVIEW_GROWTH_DB_PATH: str(growth),
        ENV_PREVIEW_CREDENTIAL: credential,
        ENV_PREVIEW_SESSION_SECRET_B64: _secret_b64(),
        ENV_PREVIEW_HOST: "127.0.0.1",
        ENV_PREVIEW_PORT: str(PREVIEW_PORT),
    }


@pytest.fixture(scope="module")
def preview_credential() -> str:
    return hash_dashboard_password("preview password", salt=b"r" * 16)


@pytest.mark.parametrize(
    ("service", "loader", "missing_key"),
    [
        (TelemetryService.RELIABILITY, load_reliability_runtime, ENV_RELIABILITY_DB_PATH),
        (TelemetryService.RELIABILITY, load_reliability_runtime, ENV_RELIABILITY_HOST),
        (TelemetryService.RELIABILITY, load_reliability_runtime, ENV_RELIABILITY_PORT),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_DB_PATH),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_HOST),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_PORT),
        (
            TelemetryService.GROWTH,
            load_growth_runtime,
            ENV_GROWTH_WEBSITE_SECRET_B64,
        ),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_CDN_SECRET_B64),
        (
            TelemetryService.GROWTH,
            load_growth_runtime,
            ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64,
        ),
    ],
)
def test_collector_environment_requires_every_explicit_value(
    tmp_path: Path,
    service: TelemetryService,
    loader: Any,
    missing_key: str,
) -> None:
    environ = _collector_env(tmp_path, service)
    del environ[missing_key]

    with pytest.raises(RuntimeEnvironmentError, match=missing_key):
        loader(environ)


@pytest.mark.parametrize(
    "missing_key",
    [
        ENV_PREVIEW_RELIABILITY_DB_PATH,
        ENV_PREVIEW_GROWTH_DB_PATH,
        ENV_PREVIEW_CREDENTIAL,
        ENV_PREVIEW_SESSION_SECRET_B64,
        ENV_PREVIEW_HOST,
        ENV_PREVIEW_PORT,
    ],
)
def test_preview_environment_requires_dedicated_explicit_values(
    tmp_path: Path,
    preview_credential: str,
    missing_key: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    del environ[missing_key]
    environ["OPENSQUILLA_GATEWAY_TOKEN"] = "must-not-be-reused"
    environ["OPENSQUILLA_CONTROL_UI_PASSWORD"] = "must-not-be-reused"

    with pytest.raises(RuntimeEnvironmentError, match=missing_key):
        load_preview_runtime(environ)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "telemetry.example.com"])
def test_preview_rejects_every_non_literal_loopback_host(
    tmp_path: Path,
    preview_credential: str,
    host: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    environ[ENV_PREVIEW_HOST] = host

    with pytest.raises(RuntimeEnvironmentError, match=ENV_PREVIEW_HOST):
        load_preview_runtime(environ)


@pytest.mark.parametrize(
    ("service", "loader", "port_key", "invalid_port"),
    [
        (TelemetryService.RELIABILITY, load_reliability_runtime, ENV_RELIABILITY_PORT, "8787"),
        (TelemetryService.RELIABILITY, load_reliability_runtime, ENV_RELIABILITY_PORT, "8788"),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_PORT, "8790"),
        (TelemetryService.GROWTH, load_growth_runtime, ENV_GROWTH_PORT, "not-a-port"),
    ],
)
def test_collector_ports_cannot_collide_with_legacy_services(
    tmp_path: Path,
    service: TelemetryService,
    loader: Any,
    port_key: str,
    invalid_port: str,
) -> None:
    environ = _collector_env(tmp_path, service)
    environ[port_key] = invalid_port

    with pytest.raises(RuntimeEnvironmentError, match=port_key):
        loader(environ)


def test_preview_secret_error_never_echoes_secret(
    tmp_path: Path,
    preview_credential: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    secret = "SECRET-MUST-NOT-BE-ECHOED"
    environ[ENV_PREVIEW_SESSION_SECRET_B64] = secret

    with pytest.raises(RuntimeEnvironmentError) as captured:
        load_preview_runtime(environ)

    assert secret not in str(captured.value)


def test_preview_requires_distinct_existing_database_files(
    tmp_path: Path,
    preview_credential: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    environ[ENV_PREVIEW_GROWTH_DB_PATH] = environ[ENV_PREVIEW_RELIABILITY_DB_PATH]

    with pytest.raises(RuntimeEnvironmentError, match="distinct"):
        load_preview_runtime(environ)

    environ = _preview_env(tmp_path / "second", preview_credential)
    Path(environ[ENV_PREVIEW_GROWTH_DB_PATH]).unlink()
    with pytest.raises(RuntimeEnvironmentError, match=ENV_PREVIEW_GROWTH_DB_PATH):
        load_preview_runtime(environ)


@pytest.mark.parametrize(
    "origin",
    [
        "http://telemetry.example.com",
        "https://user@telemetry.example.com",
        "https://telemetry.example.com/path",
    ],
)
def test_preview_public_origin_is_optional_but_https_only(
    tmp_path: Path,
    preview_credential: str,
    origin: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    environ[ENV_PREVIEW_PUBLIC_ORIGIN] = origin

    with pytest.raises(RuntimeEnvironmentError, match=ENV_PREVIEW_PUBLIC_ORIGIN):
        load_preview_runtime(environ)


def test_preview_defaults_to_current_preview_path(
    tmp_path: Path,
    preview_credential: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    settings = load_preview_runtime(environ)
    assert settings.preview_path == DEFAULT_PREVIEW_PATH
    assert settings.public_origin is None
    assert settings.legacy_install_db_path is None

    environ[ENV_PREVIEW_PATH] = "/private-preview"
    environ[ENV_PREVIEW_PUBLIC_ORIGIN] = "https://telemetry.example.com"
    settings = load_preview_runtime(environ)
    assert settings.preview_path == "/private-preview"
    assert settings.public_origin == "https://telemetry.example.com"


def test_preview_accepts_one_distinct_read_only_legacy_database(
    tmp_path: Path,
    preview_credential: str,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    legacy = tmp_path / "legacy.sqlite3"
    legacy.touch()
    environ[ENV_PREVIEW_LEGACY_INSTALL_DB_PATH] = str(legacy)

    settings = load_preview_runtime(environ)

    assert settings.legacy_install_db_path == legacy

    environ[ENV_PREVIEW_LEGACY_INSTALL_DB_PATH] = environ[
        ENV_PREVIEW_RELIABILITY_DB_PATH
    ]
    with pytest.raises(RuntimeEnvironmentError, match="distinct"):
        load_preview_runtime(environ)


def _install_environ(monkeypatch: pytest.MonkeyPatch, environ: Mapping[str, str]) -> None:
    for key, value in environ.items():
        monkeypatch.setenv(key, value)


def test_collector_factories_register_only_their_own_scope_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reliability_env = _collector_env(tmp_path, TelemetryService.RELIABILITY)
    _install_environ(monkeypatch, reliability_env)
    with TestClient(
        create_reliability_collector_from_env(),
        client=("127.0.0.1", 50000),
    ) as client:
        assert client.post("/v1/growth/events", json={}).status_code == 404
        assert client.post("/v1/reliability/events", json={}).status_code == 422

    for key in reliability_env:
        monkeypatch.delenv(key)
    growth_env = _collector_env(tmp_path, TelemetryService.GROWTH)
    _install_environ(monkeypatch, growth_env)
    with TestClient(
        create_growth_collector_from_env(),
        client=("127.0.0.1", 50000),
    ) as client:
        assert client.post("/v1/reliability/events", json={}).status_code == 404
        assert client.post("/v1/growth/events", json={}).status_code == 422


def test_preview_factory_exposes_only_the_configured_loopback_preview(
    tmp_path: Path,
    preview_credential: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = _preview_env(tmp_path, preview_credential)
    _install_environ(monkeypatch, environ)
    app = create_preview_dashboard_from_env()

    with TestClient(
        app,
        base_url="https://preview.test",
        client=("127.0.0.1", 50000),
    ) as client:
        response = client.get(DEFAULT_PREVIEW_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"{DEFAULT_PREVIEW_PATH}/login"
    assert getattr(app.state, "telemetry_bind_host") == "127.0.0.1"

    with TestClient(
        app,
        base_url="https://preview.test",
        client=("203.0.113.10", 50000),
    ) as client:
        rejected = client.get(DEFAULT_PREVIEW_PATH, follow_redirects=False)
    assert rejected.status_code == 403
    assert rejected.json() == {"error": "loopback_required"}


@pytest.mark.parametrize(
    ("service", "expected_port"),
    [
        (TelemetryService.RELIABILITY, RELIABILITY_PORT),
        (TelemetryService.GROWTH, GROWTH_PORT),
        (TelemetryService.PREVIEW, PREVIEW_PORT),
    ],
)
def test_run_service_uses_one_local_worker_and_privacy_safe_uvicorn_options(
    tmp_path: Path,
    preview_credential: str,
    service: TelemetryService,
    expected_port: int,
) -> None:
    if service is TelemetryService.PREVIEW:
        environ = _preview_env(tmp_path, preview_credential)
    else:
        environ = _collector_env(tmp_path, service)
    calls: list[tuple[Starlette, dict[str, Any]]] = []

    def runner(app: Starlette, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    run_service(service, environ=environ, runner=runner)

    assert len(calls) == 1
    app, options = calls[0]
    assert isinstance(app, Starlette)
    assert options == {
        "host": "127.0.0.1",
        "port": expected_port,
        "workers": 1,
        "access_log": False,
        "proxy_headers": False,
        "server_header": False,
    }
