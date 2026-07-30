"""Passive update polling endpoint contracts."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from opensquilla import __version__
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import AuthConfig, GatewayConfig, RateLimitConfig
from opensquilla.observability import update_check


def test_update_endpoint_returns_fixed_uncached_shape(monkeypatch) -> None:
    starts: list[str] = []
    calls: list[str] = []
    info = update_check.UpdateCheckInfo(
        current_version="0.5.0rc4",
        latest_version="0.5.0rc5",
        update_available=True,
        release_url="https://example.test/rc5",
        checked_at="2026-07-13T00:00:00Z",
    )
    monkeypatch.setattr(
        update_check,
        "start_background_update_check",
        lambda **kwargs: (
            calls.append("refresh"),
            starts.append(str(kwargs["version"])),
        ),
    )
    monkeypatch.setattr(
        update_check,
        "get_cached_update_info",
        lambda **_kwargs: (calls.append("snapshot"), info)[1],
    )

    with TestClient(create_gateway_app(GatewayConfig())) as client:
        response = client.get("/api/system/update")

    assert response.status_code == 200
    assert response.json() == {
        "current": "0.5.0rc4",
        "latest": "0.5.0rc5",
        "available": True,
        "url": "https://example.test/rc5",
        "checkedAt": "2026-07-13T00:00:00Z",
    }
    assert response.headers["cache-control"] == "no-store"
    assert starts == [__version__]
    assert calls == ["snapshot", "refresh"]


def test_update_endpoint_returns_empty_fixed_shape(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "start_background_update_check", lambda **_kwargs: None)
    monkeypatch.setattr(update_check, "get_cached_update_info", lambda **_kwargs: None)

    with TestClient(create_gateway_app(GatewayConfig())) as client:
        response = client.get("/api/system/update")

    expected = update_check.default_update_info(version=__version__)
    assert response.status_code == 200
    assert response.json() == {
        "current": __version__,
        "latest": None,
        "available": False,
        "url": expected.release_url,
        "checkedAt": None,
    }
    assert response.headers["cache-control"] == "no-store"


def test_update_endpoint_masks_internal_failures(monkeypatch) -> None:
    def fail(**_kwargs):
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(update_check, "get_cached_update_info", fail)
    monkeypatch.setattr(update_check, "start_background_update_check", fail)

    with TestClient(create_gateway_app(GatewayConfig())) as client:
        response = client.get("/api/system/update")

    expected = update_check.default_update_info(version=__version__).to_public_dict()
    assert response.status_code == 200
    assert response.json() == expected
    assert "sensitive internal detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_update_endpoint_does_not_wait_for_background_release_lookup(
    tmp_path,
    monkeypatch,
) -> None:
    for name in (
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        update_check.UPDATE_CHECK_DISABLED_ENV,
        update_check.TELEMETRY_DISABLED_ENV,
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        update_check.TELEMETRY_TESTING_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    monkeypatch.setattr(update_check, "_REFRESH_LOCKS", {})
    monkeypatch.setattr(update_check, "_BACKGROUND_THREADS", {})
    entered = threading.Event()
    release = threading.Event()

    def blocking_fetch(endpoint: str, current_version: str, *, timeout: float):
        entered.set()
        assert release.wait(timeout=5)
        return None, None, None

    monkeypatch.setattr(update_check, "_fetch_latest_release", blocking_fetch)
    config = GatewayConfig(state_dir=str(tmp_path))

    try:
        with TestClient(create_gateway_app(config)) as client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                response_future = pool.submit(client.get, "/api/system/update")
                assert entered.wait(timeout=2), "background release lookup did not start"
                # The HTTP response must complete while the external lookup is
                # still deliberately blocked.
                try:
                    response = response_future.result(timeout=1)
                finally:
                    release.set()
    finally:
        release.set()
        for thread in list(update_check._BACKGROUND_THREADS.values()):
            thread.join(timeout=5)

    assert response.status_code == 200
    assert response.json()["available"] is False


def test_update_endpoint_inherits_gateway_auth(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "start_background_update_check", lambda **_kwargs: None)
    monkeypatch.setattr(update_check, "get_cached_update_info", lambda **_kwargs: None)
    config = GatewayConfig(auth=AuthConfig(mode="token", token="secret"))

    with TestClient(create_gateway_app(config)) as client:
        unauthorized = client.get("/api/system/update")
        authorized = client.get(
            "/api/system/update",
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_update_endpoint_is_not_exempt_from_normal_rate_limiting(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "start_background_update_check", lambda **_kwargs: None)
    monkeypatch.setattr(update_check, "get_cached_update_info", lambda **_kwargs: None)
    config = GatewayConfig(
        rate_limit=RateLimitConfig(enabled=True, max_requests=1, window_seconds=60)
    )

    with TestClient(create_gateway_app(config)) as client:
        first = client.get("/api/system/update")
        limited = client.get("/api/system/update")

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"
