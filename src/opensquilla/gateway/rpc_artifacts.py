"""Read-only RPC handlers for session-scoped generated artifacts."""

from __future__ import annotations

import asyncio
from typing import Any

from opensquilla.application.artifact_workbench import ArtifactCatalogQuery, ArtifactIdentity
from opensquilla.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    artifact_cursor,
    artifact_payload,
    validate_artifact_cursor,
)
from opensquilla.gateway.adapters.artifact_workbench import (
    GatewayArtifactWorkbenchAdapter,
)
from opensquilla.gateway.adapters.artifact_workbench_contract import (
    register_artifact_workbench_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.protocol import ERROR_NOT_FOUND
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import (
    SessionServiceUnavailableError,
    session_id_for_key,
)
from opensquilla.paths import media_root_from_config
from opensquilla.session.keys import canonicalize_session_key

_d = get_dispatcher()

def _empty_artifact_page(limit: int) -> dict[str, Any]:
    return {
        "artifacts": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "total_count": 0,
        "page_size": limit,
    }


class _ArtifactCatalogRuntimePort:
    """Resolve session scope and storage behind typed catalog commands."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    async def list_artifacts(self, query: ArtifactCatalogQuery) -> dict[str, Any]:
        session_key = canonicalize_session_key(query.session_key)
        before = validate_artifact_cursor(query.before) if query.before is not None else None
        try:
            session_id = await session_id_for_key(self._ctx.session_manager, session_key)
        except SessionServiceUnavailableError as exc:
            raise RpcUnavailableError(str(exc)) from exc
        if session_id is None:
            return _empty_artifact_page(query.limit)
        store = ArtifactStore(media_root_from_config(self._ctx.config))
        try:
            page = await asyncio.to_thread(
                store.list_refs,
                session_id=session_id,
                limit=query.limit,
                before=before,
            )
        except OSError as exc:
            raise RpcUnavailableError("Artifact storage is temporarily unavailable.") from exc
        return {
            "artifacts": [artifact_payload(ref) for ref in page.refs],
            "has_more": page.has_more,
            "oldest_cursor": artifact_cursor(page.refs[0]) if page.refs else None,
            "newest_cursor": artifact_cursor(page.refs[-1]) if page.refs else None,
            "total_count": page.total_count,
            "page_size": query.limit,
        }

    async def get_artifact(self, identity: ArtifactIdentity) -> dict[str, Any]:
        session_key = canonicalize_session_key(identity.session_key)
        artifact_id = validate_artifact_cursor(identity.artifact_id)
        try:
            session_id = await session_id_for_key(self._ctx.session_manager, session_key)
        except SessionServiceUnavailableError as exc:
            raise RpcUnavailableError(str(exc)) from exc
        if session_id is None:
            raise RpcHandlerError(
                ERROR_NOT_FOUND,
                "Artifact not found",
                details={"sessionKey": session_key, "artifactId": artifact_id},
            )
        store = ArtifactStore(media_root_from_config(self._ctx.config))
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
            raise RpcUnavailableError("Artifact storage is temporarily unavailable.") from exc
        return {"artifact": artifact_payload(ref)}


_handle_artifacts_list = GatewayArtifactWorkbenchAdapter.bind(
    "artifacts.list", _ArtifactCatalogRuntimePort
)
_handle_artifacts_get = GatewayArtifactWorkbenchAdapter.bind(
    "artifacts.get", _ArtifactCatalogRuntimePort
)

for _artifact_method, _artifact_implementation in (
    ("artifacts.list", _handle_artifacts_list),
    ("artifacts.get", _handle_artifacts_get),
):
    register_artifact_workbench_contract(
        _d,
        _artifact_method,
        _artifact_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
