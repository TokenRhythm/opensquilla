"""Regression tests: Control UI static assets carry correct Content-Types even
when the host OS MIME database is wrong.

On some Windows machines third-party installers rewrite the
``HKEY_CLASSES_ROOT\\.js`` registry entry to ``text/plain``. Python's
``mimetypes`` seeds itself from that registry, and Starlette's ``FileResponse``
picks its Content-Type via ``mimetypes.guess_type`` — so the gateway serves
every ``.js`` file as ``text/plain``. Chromium's strict MIME check then refuses
to run the Vite ``<script type="module">`` entry and the console renders as a
white screen, even though gateway boot and health checks all pass.

``_CachedStaticFiles`` therefore pins the Content-Type for the asset extensions
it ships instead of trusting the environment. These tests simulate the polluted
host by patching the ``guess_type`` symbol that ``starlette.responses`` binds at
import time — the same effect a corrupt registry has on a real deployment.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import starlette.responses as starlette_responses
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.gateway import control_ui
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui import _PINNED_CONTENT_TYPES, create_control_ui_routes

# One synthetic asset per extension in _PINNED_CONTENT_TYPES, with the
# Content-Type the browser needs. A drift-guard test asserts this map covers
# every pinned extension so new pins cannot ship untested.
#
# Note on .map: the pinned application/json deliberately differs from what a
# healthy host serves today (CPython's mimetypes has no .map entry, so Starlette
# falls back to text/plain). Do not "correct" this expectation back.
_ASSETS: dict[str, tuple[bytes, str]] = {
    "app.js": (b"export {};\n", "text/javascript"),
    "upper.JS": (b"export {};\n", "text/javascript"),  # extension match is case-insensitive
    "chunk.mjs": (b"export {};\n", "text/javascript"),
    "app.css": (b"body{}\n", "text/css"),
    "logo.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>\n", "image/svg+xml"),
    "manifest.json": (b"{}\n", "application/json"),
    "app.js.map": (b"{}\n", "application/json"),
    "mod.wasm": (b"\x00asm\x01\x00\x00\x00", "application/wasm"),
    "font.woff2": (b"wOF2", "font/woff2"),
    "font.woff": (b"wOFF", "font/woff"),
    "font.ttf": (b"\x00\x01\x00\x00", "font/ttf"),
    "page.html": (b"<!doctype html>\n", "text/html"),
    "page.htm": (b"<!doctype html>\n", "text/html"),
    "icon.png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    "favicon.ico": (b"\x00\x00\x01\x00", "image/x-icon"),
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    for name, (body, _mime) in _ASSETS.items():
        (static_dir / name).write_bytes(body)
    (static_dir / "readme.unknownext").write_bytes(b"hello")
    dist_dir = static_dir / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        """<!doctype html>
<link rel="stylesheet" href="./assets/control-ui.css">
<script type="module" src="./assets/control-ui.js"></script>
""",
        encoding="utf-8",
    )
    (dist_dir / "assets" / "control-ui.js").write_bytes(b"export {};\n")
    (dist_dir / "assets" / "control-ui.css").write_bytes(b"body{}\n")
    monkeypatch.setattr(control_ui, "_STATIC_DIR", static_dir)
    monkeypatch.setattr(control_ui, "_DIST_DIR", dist_dir)
    monkeypatch.delenv("OPENSQUILLA_STATIC_NO_CACHE", raising=False)

    config = GatewayConfig()
    config.control_ui.enabled = True
    app = Starlette(routes=create_control_ui_routes(config))
    return TestClient(app)


def _pollute_mime_db(monkeypatch: pytest.MonkeyPatch, mime: str = "text/plain") -> None:
    """Make guess_type behave like a corrupt Windows registry: everything the
    registry knows about comes back as `mime` (text/plain in the wild)."""

    def polluted(path: object) -> tuple[str | None, str | None]:
        return (mime, None)

    monkeypatch.setattr(starlette_responses, "guess_type", polluted)


def test_assets_cover_every_pinned_extension() -> None:
    # Drift guard: every extension in _PINNED_CONTENT_TYPES must have a test
    # asset asserting its exact pinned value, so a typo'd or dropped pin can
    # never ship silently.
    covered = {PurePosixPath(name).suffix.lower() for name in _ASSETS}
    assert covered == set(_PINNED_CONTENT_TYPES)
    for name, (_body, mime) in _ASSETS.items():
        assert _PINNED_CONTENT_TYPES[PurePosixPath(name).suffix.lower()] == mime, name


@pytest.mark.parametrize(("name", "expected"), [(n, m) for n, (_b, m) in _ASSETS.items()])
def test_pinned_content_type_survives_polluted_mime_db(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, name: str, expected: str
) -> None:
    _pollute_mime_db(monkeypatch)

    response = client.get(f"/control/static/{name}")

    assert response.status_code == 200, response.text
    content_type = response.headers.get("content-type", "")
    assert content_type.split(";")[0].strip() == expected, content_type


def test_text_types_keep_charset_parity(client: TestClient) -> None:
    # Starlette appends "; charset=utf-8" to text/* media types it derives
    # itself; the pinned headers must follow the same convention so text
    # responses on healthy hosts keep their charset.
    for name, charset_expected in (("app.js", True), ("app.css", True), ("icon.png", False)):
        content_type = client.get(f"/control/static/{name}").headers["content-type"]
        assert ("charset=utf-8" in content_type) is charset_expected, (name, content_type)


def test_unknown_extension_still_uses_environment_guess(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    # Only extensions the Control UI knows are pinned; anything else keeps
    # flowing through the environment's MIME database, polluted or not. The
    # sentinel value doubles as a canary that the pollution patch actually
    # reaches the serving path (text/plain would be indistinguishable from
    # Starlette's own unknown-extension fallback).
    _pollute_mime_db(monkeypatch, mime="application/x-corrupt-registry")

    response = client.get("/control/static/readme.unknownext")

    assert response.status_code == 200
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/x-corrupt-registry"), content_type


def test_pin_survives_no_cache_debug_knob(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    # OPENSQUILLA_STATIC_NO_CACHE=1 disables the Cache-Control header, and is
    # exactly the knob an operator debugging a white screen would reach for —
    # the Content-Type pin must not be tied to it.
    monkeypatch.setenv("OPENSQUILLA_STATIC_NO_CACHE", "1")
    _pollute_mime_db(monkeypatch)

    response = client.get("/control/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/javascript")
    assert "max-age=2592000" not in response.headers.get("Cache-Control", "")


def test_pinned_type_coexists_with_cache_control(client: TestClient) -> None:
    response = client.get("/control/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/javascript")
    assert "max-age=2592000" in response.headers.get("Cache-Control", "")


def test_missing_asset_still_404s(client: TestClient) -> None:
    response = client.get("/control/static/does-not-exist.js")

    assert response.status_code == 404


def test_vite_entrypoint_is_pinned_on_polluted_host(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    # End-to-end against a synthetic static tree: the Vite module entry (the
    # asset whose text/plain mislabel white-screens the Vue console). Keeping the
    # fixture synthetic lets source-only checkouts run this regression test
    # without a generated Control UI bundle.
    _pollute_mime_db(monkeypatch)

    vite_js_url, _css_urls = control_ui._read_vite_assets("/control")
    assert vite_js_url.startswith("/control/static/dist/assets/"), vite_js_url

    response = client.get(vite_js_url)
    assert response.status_code == 200, vite_js_url
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("text/javascript"), (vite_js_url, content_type)
