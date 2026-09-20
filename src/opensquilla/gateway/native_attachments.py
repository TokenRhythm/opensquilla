"""Private, single-use Desktop file selections over the existing owner channel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import stat
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import request_origin_allowed
from opensquilla.gateway.uploads import (
    UploadStore,
    UploadStoreError,
    _authorization_token_matches,
)
from opensquilla.sandbox.types import SandboxBackendError
from opensquilla.tools.types import SafeToolError
from opensquilla.workspace_files import probe_file_access, session_workspace_binding

_SIGNING_CONTEXT = b"opensquilla-native-attachment-v1\n"
_WINDOWS = os.name == "nt"


def _selected_identity(info: os.stat_result) -> tuple[str, ...]:
    # libuv ctime is ChangeTime on Windows, while Python 3.12 ctime is
    # CreationTime. Birth time is the common, exact nanosecond identity there.
    timestamp = "st_birthtime_ns" if _WINDOWS else "st_ctime_ns"
    return tuple(
        str(getattr(info, name))
        for name in ("st_dev", "st_ino", "st_mtime_ns", timestamp, "st_size")
    )


def _selected_bytes(selection: dict[str, Any], limit: int) -> bytes:
    path = Path(selection["path"])
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("selected file path changed")
    before = path.lstat()

    timestamp = "birthtimeNs" if _WINDOWS else "ctimeNs"
    expected = tuple(str(selection[name]) for name in ("dev", "ino", "mtimeNs", timestamp, "size"))
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > limit
        or getattr(path, "is_junction", lambda: False)()
        or _selected_identity(before) != expected
    ):
        raise ValueError("selected file changed or exceeds the upload limit")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _selected_identity(opened) != expected:
            raise ValueError("selected file changed while opening")
        payload = stream.read(limit + 1)
        if _selected_identity(os.fstat(stream.fileno())) != expected:
            raise ValueError("selected file changed while reading")
    if path.resolve(strict=True) != path or _selected_identity(path.lstat()) != expected:
        raise ValueError("selected file path changed while reading")
    if len(payload) > limit or len(payload) != before.st_size:
        raise ValueError("selected file size changed")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), selection["sha256"]):
        raise ValueError("selected file content changed")
    return payload


async def native_selection_context(config: Any, manager: Any, key: str) -> tuple[Any, Any, Any]:
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.gateway.project_workspace_runtime import authoritative_project_run_context
    from opensquilla.gateway.session_services import get_session_storage
    from opensquilla.sandbox.policy_store import pin_sandbox_policy
    from opensquilla.tools.types import CallerKind, ToolContext

    session = await manager.get_session(key)
    if session is None:
        raise ValueError("selected file session is unavailable")
    storage = get_session_storage(manager)
    if storage is None:
        raise ValueError("native attachment session storage is unavailable")
    workspace = resolve_agent_workspace_dir(getattr(session, "agent_id", None) or "main", config)
    context, _ = await authoritative_project_run_context(
        storage=storage,
        session_manager=manager,
        session=session,
        config=config,
        default_workspace=str(workspace) if workspace else None,
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        session_key=key,
        workspace_dir=context.workspace,
        run_mode=context.run_mode.value,
        sandbox_run_context=context,
        sandbox_mounts=context.to_origin_payload()["mounts"],
        session_id=session.session_id,
        session_epoch=session.epoch,
    )
    pin_sandbox_policy(tool_context, config)
    return session, storage, tool_context


def register_native_attachment_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    store: UploadStore,
    session_manager: Any,
) -> None:
    # Ephemeral replay protection, bounded by the native selection lifetime.
    spent: dict[str, float] = {}

    async def native_import(request: Request) -> JSONResponse:
        owner = getattr(request.app.state, "desktop_gateway_ownership", None)
        if (
            owner is None
            or not getattr(owner, "instance_id", "")
            or not _authorization_token_matches(config, request)
            or not request_origin_allowed(request, config)
        ):
            return JSONResponse({"code": "NATIVE_SELECTION_FORBIDDEN"}, status_code=403)
        if session_manager is None:
            return JSONResponse({"code": "NATIVE_SESSION_UNAVAILABLE"}, status_code=503)
        owner_identity = (owner.instance_id, owner.instance_nonce)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 16384:
                    raise ValueError("selection payload too large")
                body.extend(chunk)
            packet = json.loads(body)
            encoded = packet["selection"]
            if not isinstance(encoded, str) or len(encoded) > 12000:
                raise ValueError("invalid selected file capability")
            expected = hmac.new(
                owner.instance_nonce.encode("ascii"),
                _SIGNING_CONTEXT + encoded.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(
                expected, request.headers.get("x-opensquilla-native-signature", "")
            ):
                raise PermissionError("invalid selected file capability")
            selection = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            now = time.time() * 1000
            expiry = selection["expiresAt"]
            UUID(selection["id"])
            if (
                selection["v"] != 1
                or selection["instanceId"] != owner.instance_id
                or type(expiry) not in {int, float}
                or not now < expiry <= now + 120_000
                or not isinstance(selection["sessionKey"], str)
                or type(selection["senderId"]) is not int
                or selection["senderId"] <= 0
                or type(selection["size"]) is not int
                or selection["size"] < 0
            ):
                raise ValueError("selected file capability expired or binding changed")
            for identity, until in list(spent.items()):
                if until <= now:
                    del spent[identity]
            if selection["id"] in spent or len(spent) >= 2048:
                raise ValueError("selected file capability was consumed")
            spent[selection["id"]] = expiry
            key = selection["sessionKey"]
            session, storage, context = await native_selection_context(config, session_manager, key)
            if (
                selection.get("sessionId") != session.session_id
                or type(selection.get("sessionEpoch")) is not int
                or selection["sessionEpoch"] != session.epoch
            ):
                raise ValueError("selected file session generation changed")
            snapshot = (
                session.session_id,
                session.epoch,
                session.workspace_id,
                deepcopy(session.execution_workspace),
                deepcopy(session.origin),
            )
            path = Path(selection["path"])
            await probe_file_access(path, context)
            payload = await asyncio.to_thread(_selected_bytes, selection, store.max_file_bytes)

            async def check_binding() -> None:
                current = await session_manager.get_session(key)
                if (
                    current is None
                    or current.session_id != snapshot[0]
                    or current.epoch != snapshot[1]
                    or current.workspace_id != snapshot[2]
                    or current.execution_workspace != snapshot[3]
                    or current.origin != snapshot[4]
                    or getattr(request.app.state, "desktop_gateway_ownership", None) is not owner
                    or (getattr(owner, "instance_id", None), getattr(owner, "instance_nonce", None))
                    != owner_identity
                    or time.time() * 1000 >= expiry
                ):
                    raise ValueError("selected file session or gateway changed")

            await check_binding()
            from opensquilla.contracts.attachments import (
                IMAGE_ATTACHMENT_MIMES,
                attachment_size_limit_for_mime,
                normalize_attachment_mime,
            )
            from opensquilla.contracts.image_validation import validate_image_bytes

            mime = normalize_attachment_mime(selection["mime"])
            if len(payload) > attachment_size_limit_for_mime(mime):
                raise ValueError("selected file exceeds format size limit")
            if mime in IMAGE_ATTACHMENT_MIMES:
                validate_image_bytes(payload, mime)
            # Project inputs retain live identity, including current-value semantics.
            if session.workspace_id or session.execution_workspace is not None:
                from opensquilla.workspace_files import validate_workspace_files

                identity, root = await session_workspace_binding(session, storage)
                if path.is_relative_to(root):
                    ref = {
                        "workspaceId": identity,
                        "relativePath": path.relative_to(root).as_posix(),
                        "name": selection["name"],
                        "mime": selection["mime"],
                        "size": len(payload),
                    }
                    await validate_workspace_files(
                        [ref],
                        session=session,
                        storage=storage,
                        tool_context=context,
                    )
                    await check_binding()
                    return JSONResponse({"workspaceFile": ref})
            from opensquilla.application.artifact_workbench import (
                AttachmentStage,
                AttachmentStagingApplication,
                AttachmentStagingPolicy,
            )
            from opensquilla.gateway.adapters.artifact_content import (
                GatewayAttachmentMimePolicy,
                GatewayAttachmentStagingPort,
            )

            staging = AttachmentStagingApplication(
                GatewayAttachmentStagingPort(store),
                AttachmentStagingPolicy(
                    accept_opaque=bool(config.attachments.accept_opaque),
                    opaque_max_bytes=config.attachments.opaque_max_bytes,
                ),
                GatewayAttachmentMimePolicy(),
            )
            staged = await staging.stage(
                AttachmentStage(selection["name"], selection["mime"], payload)
            )
            try:
                await check_binding()
            except BaseException:
                await asyncio.shield(store.evict(staged.file_uuid))
                raise
            return JSONResponse(
                {
                    "file_uuid": staged.file_uuid,
                    "filename": staged.filename,
                    "name": staged.filename,
                    "mime": staged.mime,
                    "size": staged.size,
                    "expires_at": staged.expires_at,
                    "ttl_seconds": store.ttl_seconds,
                }
            )
        except (PermissionError, SafeToolError, SandboxBackendError):
            return JSONResponse({"code": "NATIVE_FILE_ACCESS_DENIED"}, status_code=403)
        except (KeyError, TypeError, ValueError, OSError, UploadStoreError):
            return JSONResponse({"code": "NATIVE_SELECTION_INVALID"}, status_code=409)

    app.router.routes.append(Route("/api/v1/files/native-import", native_import, methods=["POST"]))
