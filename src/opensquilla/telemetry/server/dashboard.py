"""Authenticated Starlette application for the isolated telemetry v2 preview."""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from opensquilla.telemetry.server.auth import DashboardAuth, DashboardSession
from opensquilla.telemetry.server.dashboard_queries import (
    DashboardDataError,
    DashboardQueries,
    UtcCohortWindow,
)
from opensquilla.telemetry.server.legacy_installations import LegacyInstallationQueries

DEFAULT_PREVIEW_PATH = "/telemetry-v2-preview"
SESSION_COOKIE_NAME = "__Secure-opensquilla_telemetry_preview"
LOGIN_CSRF_COOKIE_NAME = "__Secure-opensquilla_telemetry_preview_login_csrf"
_MAX_FORM_BYTES = 4096
_TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
log = logging.getLogger(__name__)


def create_dashboard_app(
    *,
    reliability_db_path: str | Path,
    growth_db_path: str | Path,
    legacy_install_db_path: str | Path | None = None,
    credential: str,
    session_secret: bytes,
    preview_path: str = DEFAULT_PREVIEW_PATH,
    public_origin: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Starlette:
    """Build a standalone preview app; it never mutates collector databases."""

    base_path = _normalize_preview_path(preview_path)
    configured_origin = _normalize_public_origin(public_origin)
    utc_clock = clock or (lambda: datetime.now(UTC))
    auth = DashboardAuth(
        credential=credential,
        session_secret=session_secret,
        clock=lambda: _clock_utc(utc_clock).timestamp(),
    )
    queries = DashboardQueries(
        reliability_db_path=reliability_db_path,
        growth_db_path=growth_db_path,
    )
    legacy_queries = (
        LegacyInstallationQueries(legacy_install_db_path)
        if legacy_install_db_path is not None
        else None
    )
    login_path = f"{base_path}/login"

    async def login_page(request: Request) -> Response:
        if _session(request, auth) is not None:
            return _redirect(base_path)
        return _login_response(
            request,
            auth=auth,
            base_path=base_path,
            status_code=200,
            error=None,
        )

    async def login(request: Request) -> Response:
        origin_valid = _same_origin_or_absent(
            request,
            public_origin=configured_origin,
        ) or _same_site_document_navigation(request)
        if not origin_valid:
            log.warning(
                "telemetry preview login rejected: origin_present=%s "
                "origin_valid=false same_site_navigation=false",
                request.headers.get("origin") is not None,
            )
            return _error_response(request, 403, "请求校验失败。")
        form = await _read_urlencoded_form(request, expected={"password", "csrf_token"})
        csrf_valid = form is not None and auth.verify_login_csrf(
            request.cookies.get(LOGIN_CSRF_COOKIE_NAME),
            form.get("csrf_token") if form else None,
        )
        if form is None or not csrf_valid:
            log.warning(
                "telemetry preview login rejected: origin_valid=true "
                "form_valid=%s csrf_cookie_present=%s csrf_valid=%s",
                form is not None,
                LOGIN_CSRF_COOKIE_NAME in request.cookies,
                csrf_valid,
            )
            return _error_response(request, 403, "请求校验失败。")
        if not auth.authenticate(form["password"]):
            return _login_response(
                request,
                auth=auth,
                base_path=base_path,
                status_code=401,
                error="面板密码不正确。",
            )

        response = _redirect(base_path)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            auth.issue_session(),
            max_age=auth.session_ttl_seconds,
            path=base_path,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        _delete_cookie(response, LOGIN_CSRF_COOKIE_NAME, path=base_path)
        return _secure_response(response)

    async def dashboard(request: Request) -> Response:
        session = _session(request, auth)
        if session is None:
            return _redirect(login_path)
        try:
            window, start_date, end_date = _request_window(request, utc_clock)
        except ValueError:
            return _error_response(request, 400, "UTC 统计日期无效。")
        try:
            summary, available_scopes = await _partial_summary(
                queries,
                window,
                legacy_queries=legacy_queries,
            )
        except DashboardDataError:  # pragma: no cover - helper contract guard
            return _error_response(request, 503, "预览数据暂不可用。")
        if available_scopes == 0:
            return _error_response(request, 503, "预览数据暂不可用。")
        response = _template_response(
            request,
            "dashboard.html",
            {
                "base_path": base_path,
                "start_date": start_date,
                "end_date": end_date,
                "summary": summary,
                "logout_csrf": auth.session_csrf_token(session, action="logout"),
            },
        )
        return response

    async def api_summary(request: Request) -> Response:
        session = _session(request, auth)
        if session is None:
            return _json_error(401, "authentication_required")
        try:
            window, _, _ = _request_window(request, utc_clock)
            summary, available_scopes = await _partial_summary(
                queries,
                window,
                legacy_queries=legacy_queries,
            )
        except ValueError:
            return _json_error(400, "invalid_utc_cohort")
        except DashboardDataError:
            return _json_error(503, "preview_data_unavailable")
        if available_scopes == 0:
            return _json_error(503, "preview_data_unavailable")
        return _secure_response(JSONResponse(summary))

    async def api_reliability(request: Request) -> Response:
        session = _session(request, auth)
        if session is None:
            return _json_error(401, "authentication_required")
        try:
            window, _, _ = _request_window(request, utc_clock)
            result = await run_in_threadpool(queries.reliability, window)
        except ValueError:
            return _json_error(400, "invalid_utc_cohort")
        except DashboardDataError:
            return _json_error(503, "preview_data_unavailable")
        return _secure_response(
            JSONResponse({"cohort": window.public_dict(), "reliability": result})
        )

    async def api_growth(request: Request) -> Response:
        session = _session(request, auth)
        if session is None:
            return _json_error(401, "authentication_required")
        try:
            window, _, _ = _request_window(request, utc_clock)
            result = await run_in_threadpool(queries.growth, window)
        except ValueError:
            return _json_error(400, "invalid_utc_cohort")
        except DashboardDataError:
            return _json_error(503, "preview_data_unavailable")
        return _secure_response(JSONResponse({"cohort": window.public_dict(), "growth": result}))

    async def api_legacy_installations(request: Request) -> Response:
        if _session(request, auth) is None:
            return _json_error(401, "authentication_required")
        if legacy_queries is None:
            return _json_error(404, "legacy_installations_not_configured")
        try:
            window, _, _ = _request_window(request, utc_clock)
            result = await run_in_threadpool(legacy_queries.summary, window)
        except ValueError:
            return _json_error(400, "invalid_utc_cohort")
        except DashboardDataError:
            return _json_error(503, "legacy_installations_unavailable")
        return _secure_response(
            JSONResponse({"cohort": window.public_dict(), "legacyInstallation": result})
        )

    async def logout(request: Request) -> Response:
        session = _session(request, auth)
        if session is None:
            return _json_error(401, "authentication_required")
        if not _same_origin_or_absent(request, public_origin=configured_origin):
            return _error_response(request, 403, "请求校验失败。")
        form = await _read_urlencoded_form(request, expected={"csrf_token"})
        if form is None or not auth.verify_session_csrf(
            form.get("csrf_token") if form else None,
            session,
            action="logout",
        ):
            return _error_response(request, 403, "请求校验失败。")
        response = _redirect(login_path)
        _delete_cookie(response, SESSION_COOKIE_NAME, path=base_path)
        return _secure_response(response)

    routes = [
        Route(login_path, login_page, methods=["GET"]),
        Route(login_path, login, methods=["POST"]),
        Route(f"{base_path}/logout", logout, methods=["POST"]),
        Route(f"{base_path}/api/summary", api_summary, methods=["GET"]),
        Route(f"{base_path}/api/reliability", api_reliability, methods=["GET"]),
        Route(f"{base_path}/api/growth", api_growth, methods=["GET"]),
        Route(
            f"{base_path}/api/legacy-installations",
            api_legacy_installations,
            methods=["GET"],
        ),
        Route(base_path, dashboard, methods=["GET"]),
        Route(f"{base_path}/", dashboard, methods=["GET"]),
    ]
    return Starlette(debug=False, routes=routes)


def _session(request: Request, auth: DashboardAuth) -> DashboardSession | None:
    return auth.verify_session(request.cookies.get(SESSION_COOKIE_NAME))


def _login_response(
    request: Request,
    *,
    auth: DashboardAuth,
    base_path: str,
    status_code: int,
    error: str | None,
) -> Response:
    csrf_token = auth.issue_login_csrf()
    response = _template_response(
        request,
        "login.html",
        {
            "base_path": base_path,
            "csrf_token": csrf_token,
            "error": error,
        },
        status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE_NAME,
        csrf_token,
        max_age=10 * 60,
        path=base_path,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


def _template_response(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    nonce = secrets.token_urlsafe(18)
    response = _TEMPLATES.TemplateResponse(
        request,
        template_name,
        {**context, "csp_nonce": nonce},
        status_code=status_code,
    )
    return _secure_response(response, csp_nonce=nonce)


def _error_response(request: Request, status_code: int, message: str) -> Response:
    return _template_response(
        request,
        "error.html",
        {"status_code": status_code, "message": message},
        status_code=status_code,
    )


def _json_error(status_code: int, code: str) -> Response:
    return _secure_response(JSONResponse({"error": code}, status_code=status_code))


async def _partial_summary(
    queries: DashboardQueries,
    window: UtcCohortWindow,
    *,
    legacy_queries: LegacyInstallationQueries | None = None,
) -> tuple[dict[str, Any], int]:
    reliability: dict[str, Any] | None
    growth: dict[str, Any] | None
    try:
        reliability = await run_in_threadpool(queries.reliability, window)
    except DashboardDataError:
        reliability = None
    try:
        growth = await run_in_threadpool(queries.growth, window)
    except DashboardDataError:
        growth = None
    legacy_installation: dict[str, Any] | None
    if legacy_queries is None:
        legacy_installation = None
    else:
        try:
            legacy_installation = await run_in_threadpool(legacy_queries.summary, window)
        except DashboardDataError:
            legacy_installation = None
    availability = {
        "legacyInstallation": {"available": legacy_installation is not None},
        "reliability": {"available": reliability is not None},
        "growth": {"available": growth is not None},
    }
    available_scopes = sum(
        int(value is not None) for value in (legacy_installation, reliability, growth)
    )
    return (
        {
            "cohort": window.public_dict(),
            "scopeAvailability": availability,
            "legacyInstallation": legacy_installation,
            "reliability": reliability,
            "growth": growth,
        },
        available_scopes,
    )


def _redirect(location: str) -> Response:
    return _secure_response(RedirectResponse(location, status_code=303))


def _secure_response(response: Response, *, csp_nonce: str | None = None) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    style_source = f"'nonce-{csp_nonce}'" if csp_nonce is not None else "'none'"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"style-src {style_source}; "
        f"script-src {style_source}; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def _delete_cookie(response: Response, name: str, *, path: str) -> None:
    response.delete_cookie(
        name,
        path=path,
        secure=True,
        httponly=True,
        samesite="strict",
    )


async def _read_urlencoded_form(
    request: Request,
    *,
    expected: set[str],
) -> dict[str, str] | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_length = int(content_length, 10)
            if parsed_length < 0 or parsed_length > _MAX_FORM_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    body_size = 0
    try:
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > _MAX_FORM_BYTES:
                return None
            chunks.append(chunk)
    except ClientDisconnect:
        return None
    body = b"".join(chunks)
    try:
        text = body.decode("utf-8", errors="strict")
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(expected),
        )
    except (UnicodeDecodeError, ValueError):
        return None
    result: dict[str, str] = {}
    for key, value in pairs:
        if key not in expected or key in result:
            return None
        result[key] = value
    return result if set(result) == expected else None


def _same_origin_or_absent(request: Request, *, public_origin: str | None) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    expected = public_origin or f"{request.url.scheme.lower()}://{request.url.netloc.lower()}"
    try:
        supplied = _canonical_origin(parsed)
    except ValueError:
        return False
    return hmac.compare_digest(supplied, expected)


def _same_site_document_navigation(request: Request) -> bool:
    """Accept the Fetch Metadata signal used by sandboxed embedded browsers.

    Chromium may serialize a nonstandard Origin for a sandboxed application
    webview even though the form navigation is same-origin.  Fetch Metadata is
    browser-controlled and the signed form token remains mandatory, so this
    fallback does not accept cross-site form submissions.
    """

    return (
        request.method == "POST"
        and request.headers.get("sec-fetch-site", "").lower() == "same-origin"
        and request.headers.get("sec-fetch-mode", "").lower() == "navigate"
        and request.headers.get("sec-fetch-dest", "").lower() == "document"
    )


def _request_window(
    request: Request,
    clock: Callable[[], datetime],
) -> tuple[UtcCohortWindow, str, str]:
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    if (start is None) != (end is None):
        raise ValueError("both cohort dates are required")
    if start is None or end is None:
        today = _clock_utc(clock).date()
        start_date = today - timedelta(days=29)
        end_date = today
        start, end = start_date.isoformat(), end_date.isoformat()
    return UtcCohortWindow.from_dates(start, end), start, end


def _clock_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("dashboard clock must return timezone-aware UTC")
    return value.astimezone(UTC)


def _normalize_preview_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(character in path for character in "?#;\r\n")
    ):
        raise ValueError("dashboard preview path is invalid")
    normalized = path.rstrip("/")
    if not normalized or any(segment in {"", ".", ".."} for segment in normalized.split("/")[1:]):
        raise ValueError("dashboard preview path is invalid")
    return normalized


def _normalize_public_origin(origin: str | None) -> str | None:
    if origin is None:
        return None
    if not isinstance(origin, str) or len(origin) > 512:
        raise ValueError("dashboard public origin is invalid")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise ValueError("dashboard public origin is invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("dashboard public origin must be an HTTPS origin")
    try:
        return _canonical_origin(parsed)
    except ValueError:
        raise ValueError("dashboard public origin is invalid") from None


def _canonical_origin(parsed: Any) -> str:
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("origin host is missing")
    port = parsed.port
    if port is None or (scheme == "https" and port == 443):
        authority = f"[{host}]" if ":" in host else host
    else:
        authority_host = f"[{host}]" if ":" in host else host
        authority = f"{authority_host}:{port}"
    return f"{scheme}://{authority}"


__all__ = [
    "DEFAULT_PREVIEW_PATH",
    "LOGIN_CSRF_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "create_dashboard_app",
]
