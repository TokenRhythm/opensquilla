"""Control UI route factory — serves embedded HTML console with SPA fallback."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path, PurePosixPath

import structlog
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from opensquilla import __version__
from opensquilla.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)

# Conservative max-age for static assets. 30 days is long enough that hot
# clients save roundtrips but short enough that any deploy without a version
# bump still becomes visible within a release cycle. Templates already append
# ?v={{ version }} to every asset URL so cache invalidation on actual code
# change is immediate — this header only saves repeat hits for unchanged
# bytes within the 30-day window.
#
# Skip when OPENSQUILLA_STATIC_NO_CACHE is set (debugging / forced refresh).
# Skip on non-200 responses so 206 Range and 304 conditional reuse stay
# untouched.
_STATIC_CACHE_CONTROL = "public, max-age=2592000"

# Content-Types for the static assets the Control UI ships, keyed by lowercase
# extension. Starlette derives Content-Type from ``mimetypes.guess_type``, which
# seeds itself from the host OS MIME database. On Windows machines whose
# ``HKEY_CLASSES_ROOT\\.js`` registry entry has been rewritten to ``text/plain``
# (a common side effect of some third-party installers), every ``.js`` asset is
# served as ``text/plain``; Chromium's strict MIME check then refuses to execute
# the Vite ``<script type="module">`` entry and the console renders blank even
# though gateway boot and health checks pass. We therefore pin the Content-Type
# for these extensions at the serving boundary rather than trusting the
# environment. Extensions not listed here keep flowing through Starlette's own
# guess.
#
# For most extensions the pinned value equals what a correctly configured host
# already produces. A few are deliberately more correct than a bare host would
# emit: ``.map``/``.woff``/``.woff2``/``.ttf`` are absent from CPython's built-in
# table (so a host with no OS MIME registry falls back to ``text/plain``), and
# ``.ico`` resolves to ``image/vnd.microsoft.icon`` in the stdlib. Pinning
# normalizes these to their standard types on every host. All values are
# browser-accepted, so the change is safe on clean machines.
_PINNED_CONTENT_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class _CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that attaches Cache-Control to 200 responses and
    pins Content-Type for known code assets.

    Source maps (.map) are excluded from long-term caching since they are
    only used for debugging and should not be aggressively cached.

    Content-Type is forced from ``_PINNED_CONTENT_TYPES`` for extensions the
    console depends on, so a host with a corrupt MIME database cannot mislabel
    JavaScript (which browsers refuse to execute under strict MIME checking).
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        if not os.environ.get("OPENSQUILLA_STATIC_NO_CACHE"):
            # Skip cache-control for source maps — debug files should not be
            # cached aggressively (or served in production at all).
            if not path.endswith(".map"):
                response.headers.setdefault("Cache-Control", _STATIC_CACHE_CONTROL)
        pinned = _PINNED_CONTENT_TYPES.get(PurePosixPath(path).suffix.lower())
        if pinned is not None:
            if pinned.startswith("text/"):
                # Match Starlette's charset convention for text/* types so
                # headers on healthy hosts stay byte-identical.
                pinned = f"{pinned}; charset=utf-8"
            response.headers["content-type"] = pinned
        return response


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"

_TEMPLATE_VERSION_SUFFIX = str(int(time.time()))

_jinja_env = None


def _get_jinja_env():
    global _jinja_env
    if _jinja_env is None:
        import jinja2

        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=True,
        )
        _jinja_env.filters["tojson"] = lambda v, **kw: json.dumps(v)
    return _jinja_env


def _request_ws_url(request: Request, config: GatewayConfig) -> str:
    """Build the browser-facing websocket URL from the current request."""
    host = request.headers.get("host") or f"{config.host}:{config.port}"
    if config.host in {"0.0.0.0", "::"} and host == "testserver":
        host = f"127.0.0.1:{config.port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{host}/ws"


_SUPPORTED_LOCALES = ("en", "zh-Hans", "ja", "fr", "de", "es")


def _locale_from_tag(tag: str) -> str | None:
    """Map a single BCP-47 Accept-Language tag to a supported locale, else None."""
    t = tag.strip().lower()
    if not t:
        return None
    if t.startswith("zh"):
        return "zh-Hans"
    for code in ("ja", "fr", "de", "es", "en"):
        if t == code or t.startswith(code + "-"):
            return code
    return None


def _resolve_locale(config: GatewayConfig, request: Request) -> str:
    """Resolve the first-paint locale rendered into <html lang> and #opensquilla-data.

    Honors the configured default. Only when that default is the baseline 'en'
    do we sniff Accept-Language (the first supported tag wins), so an operator
    who explicitly pins a default is never overridden. The browser's saved
    localStorage choice and the in-app switcher always win client-side.
    """
    default = getattr(config.control_ui, "default_locale", "en")
    if default in _SUPPORTED_LOCALES and default != "en":
        return default
    accept = request.headers.get("accept-language", "") or ""
    for part in accept.split(","):
        code = _locale_from_tag(part.split(";", 1)[0])
        if code:
            return code
    return "en"


def _update_payload(config: GatewayConfig) -> dict | None:
    """Cached update-availability info for the bootstrap context.

    Read-only and non-blocking: the actual GitHub check runs in a background
    thread (see start_background_update_check). Returns a small dict only when a
    newer release is known, so the front end can treat "presence" as "show the
    notice"; returns None otherwise.
    """
    try:
        from opensquilla.observability.update_check import get_cached_update_info

        info = get_cached_update_info(config=config, version=__version__)
    except Exception:  # pragma: no cover - defensive, never break page render
        return None
    if info is None or not info.update_available:
        return None
    return info.to_public_dict()


def _link_token_from_request(request: Request) -> str:
    """Return the optional operator token carried by a Control UI deep link."""
    try:
        token = request.query_params.get("token") or ""
    except Exception:
        return ""
    return str(token).strip()


def _build_bootstrap_context(config: GatewayConfig, request: Request) -> dict:
    """Build the template context for bootstrap config injection."""
    return {
        "version": f"{__version__}+{_TEMPLATE_VERSION_SUFFIX}",
        "ws_url": _request_ws_url(request, config),
        "auth_mode": config.auth.mode,
        "base_path": config.control_ui.base_path,
        "config_path": config.config_path or "",
        "locale": _resolve_locale(config, request),
        "update": _update_payload(config),
        "link_token": _link_token_from_request(request),
        "features": {
            "diagnostics": config.diagnostics_enabled,
        },
    }


def _vite_asset_url(raw_url: str, base_path: str) -> str:
    """Normalize a Vite asset URL to the configured Control UI base path."""
    if not raw_url:
        return ""
    if raw_url.startswith(("http://", "https://", "//")):
        return raw_url

    base = base_path.rstrip("/") or ""
    asset_prefix = f"{base}/static/dist/"
    if raw_url.startswith(asset_prefix):
        return raw_url

    marker = "/static/dist/"
    if raw_url.startswith("/") and marker in raw_url:
        return f"{asset_prefix}{raw_url.split(marker, 1)[1]}"
    if raw_url.startswith("./"):
        return f"{asset_prefix}{raw_url[2:]}"
    if raw_url.startswith("assets/"):
        return f"{asset_prefix}{raw_url}"
    return raw_url


def _read_vite_assets(base_path: str) -> tuple[str, list[str]]:
    """Read the Vite-generated index.html and extract the main JS module and
    every entry stylesheet.

    Returns (js_url, css_urls) relative to the static directory. Vite emits more
    than one entry stylesheet (e.g. a shared Icon chunk plus the main bundle),
    and their order in index.html is not stable — extracting only the first
    drops the main bundle and renders the page unstyled, so all of them must be
    injected.
    """
    dist_index = _DIST_DIR / "index.html"
    if not dist_index.exists():
        # The template turns this into an actionable diagnostic instead of a
        # blank Vue mount point. Standard distributions cannot reach this state
        # because the Hatch build hook validates the artifact fail-closed.
        return ("", [])

    html = dist_index.read_text(encoding="utf-8")

    # Extract the main JS module
    js_match = re.search(r'<script type="module"[^>]*src="([^"]+)"', html)
    js_url = _vite_asset_url(js_match.group(1) if js_match else "", base_path)

    # Extract every stylesheet link, preserving document (cascade) order.
    css_urls = [
        _vite_asset_url(href, base_path)
        for href in re.findall(r'<link rel="stylesheet"[^>]*href="([^"]+)"', html)
    ]

    return (js_url, css_urls)


def create_control_ui_routes(config: GatewayConfig) -> list[Route | Mount]:
    """Create routes for the Control UI. Returns empty list if disabled."""
    if not config.control_ui.enabled:
        return []

    if not (_DIST_DIR / "index.html").exists():
        # The served page already shows an actionable notice, but headless
        # operators only watch logs — surface the same guidance at startup.
        log.warning(
            "control_ui.webui_assets_missing",
            detail=(
                "The built Vue console was not found, so the Control UI will "
                "serve an 'assets are unavailable' notice instead of the "
                "console. From a source checkout, build it with "
                "`cd opensquilla-webui && npm ci && npm run build` "
                "(Node.js 22.12+ with npm) and restart or reload the page. "
                "Release-wheel and Desktop installs should reinstall an "
                "official package."
            ),
            dist_dir=str(_DIST_DIR),
        )

    base = config.control_ui.base_path
    template = _get_jinja_env().get_template("index.html")

    async def serve_index(request: Request) -> HTMLResponse:
        ctx = _build_bootstrap_context(config, request)
        # Re-read latest Vite assets on every request so rebuilds are picked up
        # without restarting the gateway.
        live_js, live_css_urls = _read_vite_assets(base)
        ctx["vite_js_url"] = live_js
        ctx["vite_css_urls"] = live_css_urls
        ctx["webui_artifact_missing"] = not live_js
        # Back-compat single URL (first) for any consumer expecting one.
        ctx["vite_css_url"] = live_css_urls[0] if live_css_urls else ""
        html = template.render(**ctx)
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    routes: list[Route | Mount] = [
        Mount(
            f"{base}/static",
            app=_CachedStaticFiles(directory=str(_STATIC_DIR)),
            name="control_ui_static",
        ),
        Route(f"{base}/{{path:path}}", serve_index, methods=["GET"]),
        Route(f"{base}/", serve_index, methods=["GET"]),
    ]
    return routes
