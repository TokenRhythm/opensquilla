"""Starlette ingress for one strictly isolated telemetry consent scope."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opensquilla.telemetry.contracts.batch import TelemetryBatch
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.contracts.wire import (
    TelemetryWireError,
    TelemetryWireErrorCode,
    TelemetryWireTarget,
    parse_telemetry_wire,
)
from opensquilla.telemetry.server.producer_auth import (
    CLIENT_OWNED_GROWTH_SOURCES,
    SERVER_OWNED_GROWTH_SOURCES,
    GrowthProducerAuthenticator,
    ProducerCredentialError,
)
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.server.storage import (
    SCHEMA_VERSION,
    BatchConflictError,
    EventConflictError,
    StorageScopeError,
    TelemetryIngestStorage,
)

_LOGGER = logging.getLogger(__name__)


def _json_response(content: dict[str, object], *, status_code: int) -> JSONResponse:
    return JSONResponse(
        content,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _error_response(status_code: int, error: str) -> JSONResponse:
    return _json_response(
        {"ok": False, "error": error},
        status_code=status_code,
    )


def _is_json_content_type(request: Request) -> bool:
    content_type = request.headers.get("content-type")
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return media_type == "application/json"


class _BodyTooLargeError(Exception):
    pass


class _ClientDisconnectedError(Exception):
    pass


def _declared_body_error(request: Request, *, max_body_bytes: int) -> str | None:
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    if not declared or not declared.isascii() or not declared.isdigit():
        return "invalid_content_length"
    normalized = declared.lstrip("0") or "0"
    maximum = str(max_body_bytes)
    if len(normalized) > len(maximum):
        return "body_too_large"
    if len(normalized) == len(maximum) and normalized > maximum:
        return "body_too_large"
    return None


async def _read_bounded_body(request: Request, *, max_body_bytes: int) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > max_body_bytes - len(body):
                raise _BodyTooLargeError
            body.extend(chunk)
    except ClientDisconnect:
        raise _ClientDisconnectedError from None
    return bytes(body)


def create_collector_app(settings: CollectorSettings) -> Starlette:
    """Create a collector app exposing only one scope-specific POST route."""

    if not isinstance(settings, CollectorSettings):
        raise TypeError("settings must be CollectorSettings")
    producer_authenticator = GrowthProducerAuthenticator(settings.producer_secrets)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        storage = await TelemetryIngestStorage.open(
            settings.database_path,
            settings.scope,
            protocol_fingerprint=settings.protocol_fingerprint,
        )
        app.state.telemetry_storage = storage
        try:
            yield
        finally:
            await storage.close()

    async def healthz(request: Request) -> Response:
        del request
        storage: TelemetryIngestStorage = app.state.telemetry_storage
        try:
            await storage.ping()
        except Exception:
            _LOGGER.error(
                "telemetry_collector_health_failed",
                extra={"telemetry_scope": settings.scope.value},
            )
            return _error_response(503, "not_ready")
        return _json_response(
            {
                "ok": True,
                "scope": settings.scope.value,
                "schema_version": SCHEMA_VERSION,
                "protocol_fingerprint": settings.protocol_fingerprint,
            },
            status_code=200,
        )

    async def ingest(request: Request) -> Response:
        if not _is_json_content_type(request):
            return _error_response(415, "unsupported_media_type")
        declared_error = _declared_body_error(
            request,
            max_body_bytes=settings.max_body_bytes,
        )
        if declared_error == "body_too_large":
            return _error_response(413, "body_too_large")
        if declared_error is not None:
            return _error_response(400, declared_error)

        try:
            body = await _read_bounded_body(
                request,
                max_body_bytes=settings.max_body_bytes,
            )
        except _BodyTooLargeError:
            return _error_response(413, "body_too_large")
        except _ClientDisconnectedError:
            return _error_response(400, "client_disconnected")

        try:
            authenticated_producer = producer_authenticator.authenticate(
                headers=request.headers,
                body=body,
                method=request.method,
                path=request.url.path,
            )
        except ProducerCredentialError:
            return _error_response(401, "producer_unauthorized")

        try:
            batch: TelemetryBatch
            if settings.scope is ConsentScope.RELIABILITY:
                batch = parse_telemetry_wire(
                    body,
                    target=TelemetryWireTarget.RELIABILITY_BATCH,
                )
            else:
                batch = parse_telemetry_wire(
                    body,
                    target=TelemetryWireTarget.GROWTH_BATCH,
                )
        except TelemetryWireError as exc:
            if exc.code is TelemetryWireErrorCode.BODY_TOO_LARGE:
                return _error_response(413, "body_too_large")
            return _error_response(422, "schema_invalid")

        if settings.scope is ConsentScope.GROWTH:
            event_sources = frozenset(event.source for event in batch.events)
            if authenticated_producer is None:
                if not event_sources <= CLIENT_OWNED_GROWTH_SOURCES:
                    return _error_response(401, "producer_unauthorized")
            elif event_sources != {authenticated_producer}:
                return _error_response(403, "producer_source_mismatch")
            elif authenticated_producer not in SERVER_OWNED_GROWTH_SOURCES:
                return _error_response(403, "producer_source_mismatch")

        storage: TelemetryIngestStorage = app.state.telemetry_storage
        try:
            receipt = await storage.ingest(batch)
        except (BatchConflictError, EventConflictError):
            return _error_response(409, "identifier_conflict")
        except StorageScopeError:
            _LOGGER.error(
                "telemetry_collector_scope_invariant_failed",
                extra={"telemetry_scope": settings.scope.value},
            )
            return _error_response(500, "internal_error")
        except Exception:
            # Deliberately omit exception text and traceback: lower layers may
            # contain identifiers, filesystem paths, or rejected wire values.
            _LOGGER.error(
                "telemetry_collector_ingest_failed",
                extra={"telemetry_scope": settings.scope.value},
            )
            return _error_response(500, "internal_error")

        return _json_response(
            {
                "ok": True,
                "batch_id": receipt.batch_id,
                "accepted": receipt.accepted,
                "duplicates": receipt.duplicates,
            },
            status_code=202,
        )

    app = Starlette(
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route(settings.endpoint_path, endpoint=ingest, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app


__all__ = ["create_collector_app"]
