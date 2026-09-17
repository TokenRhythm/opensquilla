"""Fail-closed local launch configuration for isolated telemetry v2 services.

Recommended invocation::

    python -m opensquilla.telemetry.server reliability
    python -m opensquilla.telemetry.server growth
    python -m opensquilla.telemetry.server preview

Every process receives its own explicit environment.  No Gateway credential,
database path, listener, or port is inferred or reused.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import ipaddress
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from opensquilla.telemetry.contracts.common import ConsentScope, EventSource
from opensquilla.telemetry.server.auth import (
    DashboardCredentialError,
    ScryptCredential,
)
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.dashboard import (
    DEFAULT_PREVIEW_PATH,
    create_dashboard_app,
)
from opensquilla.telemetry.server.settings import CollectorSettings

ENV_RELIABILITY_DB_PATH: Final = "OPENSQUILLA_TELEMETRY_RELIABILITY_DB_PATH"
ENV_RELIABILITY_HOST: Final = "OPENSQUILLA_TELEMETRY_RELIABILITY_HOST"
ENV_RELIABILITY_PORT: Final = "OPENSQUILLA_TELEMETRY_RELIABILITY_PORT"

ENV_GROWTH_DB_PATH: Final = "OPENSQUILLA_TELEMETRY_GROWTH_DB_PATH"
ENV_GROWTH_HOST: Final = "OPENSQUILLA_TELEMETRY_GROWTH_HOST"
ENV_GROWTH_PORT: Final = "OPENSQUILLA_TELEMETRY_GROWTH_PORT"
ENV_GROWTH_WEBSITE_SECRET_B64: Final = "OPENSQUILLA_TELEMETRY_GROWTH_WEBSITE_SECRET_B64"
ENV_GROWTH_CDN_SECRET_B64: Final = "OPENSQUILLA_TELEMETRY_GROWTH_CDN_SECRET_B64"
ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64: Final = (
    "OPENSQUILLA_TELEMETRY_GROWTH_ACCOUNT_SERVICE_SECRET_B64"
)

ENV_PREVIEW_RELIABILITY_DB_PATH: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_RELIABILITY_DB_PATH"
ENV_PREVIEW_GROWTH_DB_PATH: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_GROWTH_DB_PATH"
ENV_PREVIEW_LEGACY_INSTALL_DB_PATH: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_LEGACY_INSTALL_DB_PATH"
ENV_PREVIEW_CREDENTIAL: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_CREDENTIAL"
ENV_PREVIEW_SESSION_SECRET_B64: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_SESSION_SECRET_B64"
ENV_PREVIEW_HOST: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_HOST"
ENV_PREVIEW_PORT: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_PORT"
ENV_PREVIEW_PATH: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_PATH"
ENV_PREVIEW_PUBLIC_ORIGIN: Final = "OPENSQUILLA_TELEMETRY_PREVIEW_PUBLIC_ORIGIN"

RELIABILITY_PORT: Final = 8786
PREVIEW_PORT: Final = 8789
GROWTH_PORT: Final = 8791

_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_MIN_SESSION_SECRET_BYTES = 32
_MAX_SESSION_SECRET_BYTES = 64
_MAX_ENV_VALUE_CHARS = 4096


class RuntimeEnvironmentError(ValueError):
    """A required service environment value is missing or unsafe."""


class TelemetryService(StrEnum):
    RELIABILITY = "reliability"
    GROWTH = "growth"
    PREVIEW = "preview"


@dataclass(frozen=True, slots=True)
class CollectorRuntimeSettings:
    service: TelemetryService
    host: str
    port: int
    database_path: Path
    producer_secrets: Mapping[EventSource, bytes]


@dataclass(frozen=True, slots=True)
class PreviewRuntimeSettings:
    service: TelemetryService
    host: str
    port: int
    reliability_db_path: Path
    growth_db_path: Path
    legacy_install_db_path: Path | None
    credential: str
    session_secret: bytes
    preview_path: str
    public_origin: str | None


type RuntimeSettings = CollectorRuntimeSettings | PreviewRuntimeSettings


class _LoopbackOnlyMiddleware:
    """Reject direct network clients even if uvicorn is accidentally misbound."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _client_is_loopback(scope.get("client")):
            response = JSONResponse(
                {"error": "loopback_required"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _client_is_loopback(client: tuple[str, int] | None) -> bool:
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ENV_VALUE_CHARS
        or value != value.strip()
        or "\x00" in value
    ):
        raise RuntimeEnvironmentError(f"{name} is required and must be canonical")
    return value


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    if name not in environ:
        return None
    return _required(environ, name)


def _loopback_host(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise RuntimeEnvironmentError(f"{name} must be an explicit loopback IP address") from None
    if not address.is_loopback:
        raise RuntimeEnvironmentError(f"{name} must be an explicit loopback IP address")
    return str(address)


def _reserved_port(environ: Mapping[str, str], name: str, *, expected: int) -> int:
    value = _required(environ, name)
    if not value.isascii() or not value.isdigit() or int(value, 10) != expected:
        raise RuntimeEnvironmentError(f"{name} must be explicitly set to {expected}")
    return expected


def _contains_symlink(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _database_path(
    environ: Mapping[str, str],
    name: str,
    *,
    must_exist: bool,
) -> Path:
    value = _required(environ, name)
    path = Path(value)
    if (
        not path.is_absolute()
        or not path.name
        or any(part in {".", ".."} for part in path.parts)
        or _contains_symlink(path)
    ):
        raise RuntimeEnvironmentError(f"{name} must be an absolute non-symlink file path")
    if must_exist:
        if not path.is_file():
            raise RuntimeEnvironmentError(f"{name} must identify an existing database file")
    else:
        if not path.parent.is_dir() or (path.exists() and not path.is_file()):
            raise RuntimeEnvironmentError(
                f"{name} parent must exist and the target must be a regular file"
            )
    return path


def _preview_credential(environ: Mapping[str, str]) -> str:
    value = _required(environ, ENV_PREVIEW_CREDENTIAL)
    try:
        ScryptCredential.parse(value)
    except DashboardCredentialError:
        raise RuntimeEnvironmentError(
            f"{ENV_PREVIEW_CREDENTIAL} must be a dedicated scrypt credential"
        ) from None
    return value


def _bounded_secret(environ: Mapping[str, str], name: str) -> bytes:
    value = _required(environ, name)
    if any(character not in _BASE64URL_CHARS for character in value):
        raise RuntimeEnvironmentError(f"{name} must be canonical unpadded base64url")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        raise RuntimeEnvironmentError(f"{name} must be canonical unpadded base64url") from None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not _MIN_SESSION_SECRET_BYTES <= len(
        decoded
    ) <= _MAX_SESSION_SECRET_BYTES or not hmac.compare_digest(value, canonical):
        raise RuntimeEnvironmentError(f"{name} must encode 32 to 64 random bytes")
    return decoded


def _preview_session_secret(environ: Mapping[str, str]) -> bytes:
    return _bounded_secret(environ, ENV_PREVIEW_SESSION_SECRET_B64)


def _preview_path(environ: Mapping[str, str]) -> str:
    value = _optional(environ, ENV_PREVIEW_PATH)
    if value is None:
        return DEFAULT_PREVIEW_PATH
    if (
        len(value) > 128
        or not value.startswith("/")
        or value.startswith("//")
        or value.endswith("/")
        or any(
            not segment
            or segment in {".", ".."}
            or not all(
                character.isascii() and (character.isalnum() or character in "-._~")
                for character in segment
            )
            for segment in value.split("/")[1:]
        )
    ):
        raise RuntimeEnvironmentError(f"{ENV_PREVIEW_PATH} is not a canonical URL path")
    return value


def _public_origin(environ: Mapping[str, str]) -> str | None:
    value = _optional(environ, ENV_PREVIEW_PUBLIC_ORIGIN)
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RuntimeEnvironmentError(
            f"{ENV_PREVIEW_PUBLIC_ORIGIN} must be an HTTPS origin"
        ) from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeEnvironmentError(f"{ENV_PREVIEW_PUBLIC_ORIGIN} must be an HTTPS origin")
    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = "" if port is None or port == 443 else f":{port}"
    return f"https://{rendered_host}{rendered_port}"


def load_reliability_runtime(environ: Mapping[str, str]) -> CollectorRuntimeSettings:
    return CollectorRuntimeSettings(
        service=TelemetryService.RELIABILITY,
        host=_loopback_host(environ, ENV_RELIABILITY_HOST),
        port=_reserved_port(environ, ENV_RELIABILITY_PORT, expected=RELIABILITY_PORT),
        database_path=_database_path(
            environ,
            ENV_RELIABILITY_DB_PATH,
            must_exist=False,
        ),
        producer_secrets={},
    )


def load_growth_runtime(environ: Mapping[str, str]) -> CollectorRuntimeSettings:
    producer_secrets = {
        EventSource.WEBSITE: _bounded_secret(environ, ENV_GROWTH_WEBSITE_SECRET_B64),
        EventSource.CDN: _bounded_secret(environ, ENV_GROWTH_CDN_SECRET_B64),
        EventSource.ACCOUNT_SERVICE: _bounded_secret(
            environ,
            ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64,
        ),
    }
    if len(set(producer_secrets.values())) != len(producer_secrets):
        raise RuntimeEnvironmentError("growth producer secrets must be distinct")
    return CollectorRuntimeSettings(
        service=TelemetryService.GROWTH,
        host=_loopback_host(environ, ENV_GROWTH_HOST),
        port=_reserved_port(environ, ENV_GROWTH_PORT, expected=GROWTH_PORT),
        database_path=_database_path(
            environ,
            ENV_GROWTH_DB_PATH,
            must_exist=False,
        ),
        producer_secrets=producer_secrets,
    )


def load_preview_runtime(environ: Mapping[str, str]) -> PreviewRuntimeSettings:
    reliability_db_path = _database_path(
        environ,
        ENV_PREVIEW_RELIABILITY_DB_PATH,
        must_exist=True,
    )
    growth_db_path = _database_path(
        environ,
        ENV_PREVIEW_GROWTH_DB_PATH,
        must_exist=True,
    )
    legacy_install_db_path = (
        _database_path(
            environ,
            ENV_PREVIEW_LEGACY_INSTALL_DB_PATH,
            must_exist=True,
        )
        if _optional(environ, ENV_PREVIEW_LEGACY_INSTALL_DB_PATH) is not None
        else None
    )
    if reliability_db_path == growth_db_path:
        raise RuntimeEnvironmentError("preview database paths must be distinct")
    if legacy_install_db_path in {reliability_db_path, growth_db_path}:
        raise RuntimeEnvironmentError("preview database paths must be distinct")
    return PreviewRuntimeSettings(
        service=TelemetryService.PREVIEW,
        host=_loopback_host(environ, ENV_PREVIEW_HOST),
        port=_reserved_port(environ, ENV_PREVIEW_PORT, expected=PREVIEW_PORT),
        reliability_db_path=reliability_db_path,
        growth_db_path=growth_db_path,
        legacy_install_db_path=legacy_install_db_path,
        credential=_preview_credential(environ),
        session_secret=_preview_session_secret(environ),
        preview_path=_preview_path(environ),
        public_origin=_public_origin(environ),
    )


def _runtime_for_service(
    service: TelemetryService,
    environ: Mapping[str, str],
) -> RuntimeSettings:
    if service is TelemetryService.RELIABILITY:
        return load_reliability_runtime(environ)
    if service is TelemetryService.GROWTH:
        return load_growth_runtime(environ)
    if service is TelemetryService.PREVIEW:
        return load_preview_runtime(environ)
    raise RuntimeEnvironmentError("telemetry service is invalid")


def _build_app(settings: RuntimeSettings) -> Starlette:
    if isinstance(settings, CollectorRuntimeSettings):
        scope = (
            ConsentScope.RELIABILITY
            if settings.service is TelemetryService.RELIABILITY
            else ConsentScope.GROWTH
        )
        app = create_collector_app(
            CollectorSettings(
                scope=scope,
                database_path=settings.database_path,
                producer_secrets=settings.producer_secrets,
            )
        )
    else:
        app = create_dashboard_app(
            reliability_db_path=settings.reliability_db_path,
            growth_db_path=settings.growth_db_path,
            legacy_install_db_path=settings.legacy_install_db_path,
            credential=settings.credential,
            session_secret=settings.session_secret,
            preview_path=settings.preview_path,
            public_origin=settings.public_origin,
        )
    app.state.telemetry_service = settings.service.value
    app.state.telemetry_bind_host = settings.host
    app.state.telemetry_bind_port = settings.port
    app.add_middleware(_LoopbackOnlyMiddleware)
    return app


def _from_current_environment(service: TelemetryService) -> Starlette:
    return _build_app(_runtime_for_service(service, os.environ))


def create_reliability_collector_from_env() -> Starlette:
    """ASGI factory for the reliability-only collector."""

    return _from_current_environment(TelemetryService.RELIABILITY)


def create_growth_collector_from_env() -> Starlette:
    """ASGI factory for the growth-only collector."""

    return _from_current_environment(TelemetryService.GROWTH)


def create_preview_dashboard_from_env() -> Starlette:
    """ASGI factory for the authenticated, loopback-only preview dashboard."""

    return _from_current_environment(TelemetryService.PREVIEW)


def run_service(
    service: TelemetryService,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = uvicorn.run,
) -> None:
    """Run one configured service with one worker and privacy-safe logging defaults."""

    if not isinstance(service, TelemetryService):
        raise TypeError("service must be TelemetryService")
    settings = _runtime_for_service(service, os.environ if environ is None else environ)
    app = _build_app(settings)
    runner(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opensquilla.telemetry.server",
        description="Run one isolated OpenSquilla telemetry v2 service.",
    )
    parser.add_argument("service", choices=tuple(service.value for service in TelemetryService))
    arguments = parser.parse_args(argv)
    try:
        run_service(TelemetryService(arguments.service))
    except RuntimeEnvironmentError as exc:
        parser.error(str(exc))
    return 0


__all__ = [
    "ENV_GROWTH_ACCOUNT_SERVICE_SECRET_B64",
    "ENV_GROWTH_CDN_SECRET_B64",
    "ENV_GROWTH_DB_PATH",
    "ENV_GROWTH_HOST",
    "ENV_GROWTH_PORT",
    "ENV_GROWTH_WEBSITE_SECRET_B64",
    "ENV_PREVIEW_CREDENTIAL",
    "ENV_PREVIEW_GROWTH_DB_PATH",
    "ENV_PREVIEW_HOST",
    "ENV_PREVIEW_LEGACY_INSTALL_DB_PATH",
    "ENV_PREVIEW_PATH",
    "ENV_PREVIEW_PORT",
    "ENV_PREVIEW_PUBLIC_ORIGIN",
    "ENV_PREVIEW_RELIABILITY_DB_PATH",
    "ENV_PREVIEW_SESSION_SECRET_B64",
    "ENV_RELIABILITY_DB_PATH",
    "ENV_RELIABILITY_HOST",
    "ENV_RELIABILITY_PORT",
    "GROWTH_PORT",
    "PREVIEW_PORT",
    "RELIABILITY_PORT",
    "CollectorRuntimeSettings",
    "PreviewRuntimeSettings",
    "RuntimeEnvironmentError",
    "RuntimeSettings",
    "TelemetryService",
    "create_growth_collector_from_env",
    "create_preview_dashboard_from_env",
    "create_reliability_collector_from_env",
    "load_growth_runtime",
    "load_preview_runtime",
    "load_reliability_runtime",
    "main",
    "run_service",
]
