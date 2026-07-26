"""Smoke tests for Cache-Control on /control/static/* responses.

The Control UI serves generated Vue assets through a `_CachedStaticFiles` subclass
(see ``opensquilla.gateway.control_ui``). These tests pin the header semantics
so a refactor that drops the subclass — or breaks the env-rollback knob —
shows up immediately.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.gateway import control_ui
from opensquilla.gateway.config import ControlUiConfig, GatewayConfig
from opensquilla.gateway.control_ui import create_control_ui_routes

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_vite_static(static_dir: Path) -> Path:
    dist_dir = static_dir / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>'
        '<link rel="stylesheet" crossorigin href="./assets/index.css">',
        encoding="utf-8",
    )
    (assets_dir / "index.js").write_text("export {};\n", encoding="utf-8")
    (assets_dir / "index.css").write_text("body{}\n", encoding="utf-8")
    return dist_dir


@pytest.fixture
def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Starlette:
    monkeypatch.delenv("OPENSQUILLA_STATIC_NO_CACHE", raising=False)
    static_dir = tmp_path / "static"
    dist_dir = _write_vite_static(static_dir)
    monkeypatch.setattr(control_ui, "_STATIC_DIR", static_dir)
    monkeypatch.setattr(control_ui, "_DIST_DIR", dist_dir)
    config = GatewayConfig()
    config.control_ui.enabled = True
    routes = create_control_ui_routes(config)
    return Starlette(routes=routes)


def test_static_asset_carries_long_cache_control(_app: Starlette) -> None:
    client = TestClient(_app)
    response = client.get("/control/static/dist/assets/index.js")
    assert response.status_code == 200, response.text
    cache = response.headers.get("Cache-Control", "")
    assert "max-age=2592000" in cache, cache
    assert "public" in cache, cache


def test_control_ui_bootstrap_includes_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_ui, "_DIST_DIR", _write_vite_static(tmp_path / "static"))
    config = GatewayConfig()
    config.config_path = str(tmp_path / "OpenSquilla Config.toml")
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)

    response = client.get("/control/")

    assert response.status_code == 200
    assert 'data-config-path="' in response.text
    assert str(config.config_path) in response.text


def test_control_ui_vite_asset_urls_use_configured_base_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>'
        '<link rel="stylesheet" crossorigin href="./assets/index.css">',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)

    js_url, css_urls = control_ui._read_vite_assets("/ops")

    assert js_url == "/ops/static/dist/assets/index.js"
    assert css_urls == ["/ops/static/dist/assets/index.css"]


def test_read_vite_assets_extracts_every_stylesheet(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Vite can emit more than one entry stylesheet (e.g. a shared Icon chunk
    # before the main bundle); all must be returned in document order, else the
    # page renders unstyled.
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>'
        '<link rel="stylesheet" crossorigin href="./assets/Icon-abc.css">'
        '<link rel="stylesheet" crossorigin href="./assets/index-def.css">',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)

    js_url, css_urls = control_ui._read_vite_assets("/ops")

    assert js_url == "/ops/static/dist/assets/index.js"
    assert css_urls == [
        "/ops/static/dist/assets/Icon-abc.css",
        "/ops/static/dist/assets/index-def.css",
    ]


def test_control_ui_rebases_hard_coded_vite_base_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" src="/control/static/dist/assets/index.js"></script>'
        '<link rel="stylesheet" href="/control/static/dist/assets/index.css">',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)

    js_url, css_urls = control_ui._read_vite_assets("/custom")

    assert js_url == "/custom/static/dist/assets/index.js"
    assert css_urls == ["/custom/static/dist/assets/index.css"]


def test_control_ui_defaults_to_vue_bootstrap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>'
        '<link rel="stylesheet" crossorigin href="./assets/index.css">',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)
    config = GatewayConfig()
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)

    response = client.get("/control/")

    assert response.status_code == 200
    assert "/control/static/dist/assets/index.js" in response.text
    assert "/control/static/js/app.js" not in response.text


def test_control_ui_explains_how_to_build_missing_vue_assets(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)
    config = GatewayConfig()
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))

    response = TestClient(app).get("/control/")

    assert response.status_code == 200
    assert "Control UI assets are unavailable" in response.text
    assert "npm ci &amp;&amp; npm run build" in response.text
    assert "data-webui-artifact-missing" in response.text
    assert '<div id="app"></div>' not in response.text


def test_control_ui_startup_logs_warning_when_vue_assets_missing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Headless operators never see the in-page notice, so the missing-artifact
    # diagnostic must also reach the gateway log at startup.
    import structlog

    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path / "dist")
    config = GatewayConfig()
    config.control_ui.enabled = True

    with structlog.testing.capture_logs() as captured:
        create_control_ui_routes(config)

    events = [e for e in captured if e["event"] == "control_ui.webui_assets_missing"]
    assert events, captured
    assert events[0]["log_level"] == "warning"
    assert "npm ci && npm run build" in events[0]["detail"]
    assert events[0]["dist_dir"] == str(tmp_path / "dist")


def test_control_ui_startup_warning_absent_when_vue_assets_present(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    monkeypatch.setattr(control_ui, "_DIST_DIR", _write_vite_static(tmp_path / "static"))
    config = GatewayConfig()
    config.control_ui.enabled = True

    with structlog.testing.capture_logs() as captured:
        create_control_ui_routes(config)

    assert not [e for e in captured if e["event"] == "control_ui.webui_assets_missing"]


def test_control_ui_startup_warning_absent_when_control_ui_disabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path / "dist")
    config = GatewayConfig()
    config.control_ui.enabled = False

    with structlog.testing.capture_logs() as captured:
        assert create_control_ui_routes(config) == []

    assert not [e for e in captured if e["event"] == "control_ui.webui_assets_missing"]


def test_missing_vue_asset_recovery_is_in_troubleshooting_guide() -> None:
    troubleshooting = (REPO_ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    normalized = " ".join(troubleshooting.split())

    assert "## Control UI Assets Are Unavailable" in troubleshooting
    assert "npm ci\nnpm run build" in troubleshooting
    assert "Direct VCS URL installs" in troubleshooting
    assert "official release wheel" in normalized


def test_control_ui_legacy_frontend_compat_input_serves_vue(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>'
        '<link rel="stylesheet" crossorigin href="./assets/index.css">',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)
    with pytest.warns(DeprecationWarning, match="Vue is always served"):
        control_config = ControlUiConfig(frontend="legacy")
    config = GatewayConfig(control_ui=control_config)
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)

    response = client.get("/control/")

    assert response.status_code == 200
    assert control_config.frontend == "vue"
    assert "/control/static/dist/assets/index.js" in response.text
    assert "/control/static/js/" not in response.text


def test_control_ui_frontend_reads_env_override(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_CONTROL_UI_FRONTEND", "legacy")

    with caplog.at_level(logging.WARNING, logger="opensquilla.gateway.config"):
        with pytest.warns(DeprecationWarning, match="no longer selects"):
            config = GatewayConfig()

    assert config.control_ui.frontend == "vue"
    assert "Vue is always served" in caplog.text
    assert "Remove this setting or set it to 'vue'" in caplog.text


def test_control_ui_frontend_reads_toml_config(tmp_path) -> None:
    config_path = tmp_path / "opensquilla.toml"
    config_path.write_text(
        '[control_ui]\nfrontend = "legacy"\n',
        encoding="utf-8",
    )

    with pytest.warns(DeprecationWarning, match="Remove this setting"):
        config = GatewayConfig.load_from_toml(config_path)

    assert config.control_ui.frontend == "vue"


@pytest.mark.parametrize("value", ["vue", " VUE "])
def test_control_ui_frontend_accepts_vue(value: str) -> None:
    assert ControlUiConfig(frontend=value).frontend == "vue"


def test_control_ui_frontend_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        ControlUiConfig(frontend="retro")


def test_control_ui_legacy_frontend_compat_uses_configured_base_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./assets/index.js"></script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path)
    with pytest.warns(DeprecationWarning, match="Vue is always served"):
        control_config = ControlUiConfig(base_path="/ops", frontend="legacy")
    config = GatewayConfig(control_ui=control_config)
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)

    response = client.get("/ops/")

    assert response.status_code == 200
    assert "/ops/static/dist/assets/index.js" in response.text
    assert "/control/static/dist/assets/index.js" not in response.text
    assert "/ops/static/js/" not in response.text


def test_control_ui_bootstrap_ws_url_uses_client_reachable_wildcard_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_ui, "_DIST_DIR", _write_vite_static(tmp_path / "static"))
    config = GatewayConfig()
    config.host = "0.0.0.0"
    config.port = 20002
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)

    response = client.get("/control/")

    assert response.status_code == 200
    assert 'data-ws-url="ws://127.0.0.1:20002/ws"' in response.text
    assert 'data-ws-url="ws://0.0.0.0:20002/ws"' not in response.text


def test_env_rollback_disables_cache_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OPENSQUILLA_STATIC_NO_CACHE=1 must completely skip the Cache-Control
    # header so a release with a static-cache problem can be defused without
    # a redeploy.
    monkeypatch.setenv("OPENSQUILLA_STATIC_NO_CACHE", "1")
    static_dir = tmp_path / "static"
    dist_dir = _write_vite_static(static_dir)
    monkeypatch.setattr(control_ui, "_STATIC_DIR", static_dir)
    monkeypatch.setattr(control_ui, "_DIST_DIR", dist_dir)
    config = GatewayConfig()
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))
    client = TestClient(app)
    response = client.get("/control/static/dist/assets/index.js")
    assert response.status_code == 200
    # Either header is absent or it does not advertise our long max-age.
    cache = response.headers.get("Cache-Control", "")
    assert "max-age=2592000" not in cache


def test_nonexistent_path_does_not_add_header(_app: Starlette) -> None:
    client = TestClient(_app)
    response = client.get("/control/static/dist/assets/does-not-exist-12345.js")
    # 404 must not be tagged with a long-cache header — clients would otherwise
    # remember a "missing" asset for 30 days.
    assert response.status_code == 404
    assert "max-age=2592000" not in response.headers.get("Cache-Control", "")


def _cleanup_env() -> None:
    os.environ.pop("OPENSQUILLA_STATIC_NO_CACHE", None)
