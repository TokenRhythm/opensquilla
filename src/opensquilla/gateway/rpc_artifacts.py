"""Read-only RPC handlers for session-scoped generated artifacts."""

from __future__ import annotations

import asyncio
from typing import Any

from opensquilla.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    artifact_cursor,
    artifact_payload,
    validate_artifact_cursor,
)
from opensquilla.gateway.protocol import ERROR_NOT_FOUND
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config
from opensquilla.session.keys import canonicalize_session_key

_d = get_dispatcher()

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200


def _require_string(params: dict[str, Any] | None, name: str) -> str:
    if not isinstance(params, dict) or name not in params:
        raise ValueError(f"params.{name} is required")
    value = params[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"params.{name} must be a non-empty string")
    return value.strip()


def _require_session_key(params: dict[str, Any] | None) -> str:
    return canonicalize_session_key(_require_string(params, "sessionKey"))


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        return _DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = _DEFAULT_LIMIT
    if parsed < 1:
        return _DEFAULT_LIMIT
    return min(parsed, _MAX_LIMIT)


def _optional_before(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict) or params.get("before") is None:
        return None
    value = params["before"]
    if not isinstance(value, str):
        raise ValueError("params.before must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("params.before must be a non-empty artifact cursor")
    return validate_artifact_cursor(stripped)


async def _session_id_for_key(ctx: RpcContext, session_key: str) -> str | None:
    manager = ctx.session_manager
    if manager is None:
        raise RpcUnavailableError("session manager is not wired")

    get_session = getattr(manager, "get_session", None)
    try:
        if callable(get_session):
            session = await get_session(session_key)
        else:
            storage = get_session_storage(manager)
            if storage is None:
                raise RpcUnavailableError("session storage is not wired")
            session = await storage.get_session(session_key)
    except KeyError:
        session = None
    if session is None:
        return None
    session_id = getattr(session, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id


def _empty_artifact_page(limit: int) -> dict[str, Any]:
    return {
        "artifacts": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "total_count": 0,
        "page_size": limit,
    }


@_d.method("artifacts.list", scope="operator.read")
async def _handle_artifacts_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """List one session's artifact metadata with backwards pagination."""

    session_key = _require_session_key(params)
    limit = _bounded_limit(params.get("limit") if isinstance(params, dict) else None)
    before = _optional_before(params)
    session_id = await _session_id_for_key(ctx, session_key)
    if session_id is None:
        return _empty_artifact_page(limit)
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        page = await asyncio.to_thread(
            store.list_refs,
            session_id=session_id,
            limit=limit,
            before=before,
        )
    except OSError as exc:
        raise RpcUnavailableError(
            "Artifact storage is temporarily unavailable."
        ) from exc
    return {
        "artifacts": [artifact_payload(ref) for ref in page.refs],
        "has_more": page.has_more,
        "oldest_cursor": artifact_cursor(page.refs[0]) if page.refs else None,
        "newest_cursor": artifact_cursor(page.refs[-1]) if page.refs else None,
        "total_count": page.total_count,
        "page_size": limit,
    }


@_d.method("artifacts.get", scope="operator.read")
async def _handle_artifacts_get(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Get one session-scoped artifact metadata record."""

    session_key = _require_session_key(params)
    artifact_id = validate_artifact_cursor(_require_string(params, "artifactId"))
    session_id = await _session_id_for_key(ctx, session_key)
    if session_id is None:
        raise RpcHandlerError(
            ERROR_NOT_FOUND,
            "Artifact not found",
            details={"sessionKey": session_key, "artifactId": artifact_id},
        )
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        ref = await asyncio.to_thread(
            store.get_ref,
            session_id=session_id,
            artifact_id=artifact_id,
        )
    except ArtifactNotFoundError:
        raise RpcHandlerError(
            ERROR_NOT_FOUND,
            "Artifact not found",
            details={"sessionKey": session_key, "artifactId": artifact_id},
        ) from None
    except OSError as exc:
        raise RpcUnavailableError(
            "Artifact storage is temporarily unavailable."
        ) from exc
    return {"artifact": artifact_payload(ref)}
