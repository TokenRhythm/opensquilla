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

from opensquilla.application.artifact_workbench import (
    AttachmentClaimError,
    AttachmentOpaqueOversizeError,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import request_origin_allowed
from opensquilla.gateway.uploads import (
    UploadOversizeError,
    UploadStore,
    UploadStoreError,
    UploadStoreFullError,
    UploadUnsupportedMimeError,
    _authorization_token_matches,
)
from opensquilla.sandbox.types import SandboxBackendError
from opensquilla.tools.types import SafeToolError
from opensquilla.workspace_files import probe_file_access, session_workspace_binding

_SIGNING_CONTEXT = b"opensquilla-native-attachment-v1\n"
_WINDOWS = os.name == "nt"


class _NativeSelectionError(ValueError):
    """A fixed, user-safe diagnostic; never wrap raw paths or capability data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _selected_identity(info: os.stat_result) -> tuple[str, ...]:
    # libuv ctime is ChangeTime on Windows, while Python 3.12 ctime is
    # CreationTime. Birth time is the common, exact nanosecond identity there.
    timestamp = "st_birthtime_ns" if _WINDOWS else "st_ctime_ns"
    return tuple(
        str(getattr(info, name))
        for name in ("st_dev", "st_ino", "st_mtime_ns", timestamp, "st_size")
    )


def _desktop_identity(info: os.stat_result) -> tuple[str, ...]:
    identity = _selected_identity(info)
    # libuv exposes the volume serial's LowPart, while Python >=3.12 exposes
    # all 64 bits. Normalize only this cross-runtime comparison. Python's own
    # before/open/after checks must retain the full device and file identity.
    return (str(info.st_dev & 0xFFFFFFFF), *identity[1:]) if _WINDOWS else identity


def _selected_bytes(selection: dict[str, Any], limit: int) -> bytes:
    path = Path(selection["path"])
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise _NativeSelectionError(
            "NATIVE_FILE_CHANGED", "Selected file path changed; select the file again."
        )
    before = path.lstat()
    identity = _selected_identity(before)

    timestamp = "birthtimeNs" if _WINDOWS else "ctimeNs"
    expected = tuple(str(selection[name]) for name in ("dev", "ino", "mtimeNs", timestamp, "size"))
    if before.st_size > limit:
        raise _NativeSelectionError(
            "NATIVE_FILE_TOO_LARGE", "Selected file exceeds the attachment size limit."
        )
    if (
        not stat.S_ISREG(before.st_mode)
        or getattr(path, "is_junction", lambda: False)()
        or _desktop_identity(before) != expected
    ):
        raise _NativeSelectionError(
            "NATIVE_FILE_CHANGED", "Selected file identity changed; select the file again."
        )
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _selected_identity(opened) != identity:
            raise _NativeSelectionError(
                "NATIVE_FILE_CHANGED", "Selected file changed while opening; select it again."
            )
        payload = stream.read(limit + 1)
        if _selected_identity(os.fstat(stream.fileno())) != identity:
            raise _NativeSelectionError(
                "NATIVE_FILE_CHANGED", "Selected file changed while reading; select it again."
            )
    if path.resolve(strict=True) != path or _selected_identity(path.lstat()) != identity:
        raise _NativeSelectionError(
            "NATIVE_FILE_CHANGED", "Selected file path changed while reading; select it again."
        )
    if len(payload) > limit or len(payload) != before.st_size:
        raise _NativeSelectionError(
            "NATIVE_FILE_CHANGED", "Selected file size changed; select the file again."
        )
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), selection["sha256"]):
        raise _NativeSelectionError(
            "NATIVE_FILE_CHANGED", "Selected file content changed; select the file again."
        )
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
            return JSONResponse(
                {
                    "code": "NATIVE_SELECTION_FORBIDDEN",
                    "error": "Attachment selection is not authorized for this Gateway.",
                },
                status_code=403,
            )
        if session_manager is None:
            return JSONResponse(
                {
                    "code": "NATIVE_SESSION_UNAVAILABLE",
                    "error": "Attachment session is unavailable; reconnect and try again.",
                },
                status_code=503,
            )
        owner_identity = (owner.instance_id, owner.instance_nonce)
        failure = (
            "NATIVE_SELECTION_INVALID",
            "Invalid attachment selection; select the file again.",
        )
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
                raise _NativeSelectionError(
                    "NATIVE_SELECTION_EXPIRED",
                    "Attachment selection expired or its Gateway changed; select the file again.",
                )
            for identity, until in list(spent.items()):
                if until <= now:
                    del spent[identity]
            if selection["id"] in spent or len(spent) >= 2048:
                raise _NativeSelectionError(
                    "NATIVE_SELECTION_CONSUMED",
                    "Attachment selection was already used or too many selections are pending; "
                    "select it again.",
                )
            spent[selection["id"]] = expiry
            key = selection["sessionKey"]
            failure = (
                "NATIVE_SESSION_CHANGED",
                "Attachment session changed or is unavailable; select the file again.",
            )
            session, storage, context = await native_selection_context(config, session_manager, key)
            if (
                selection.get("sessionId") != session.session_id
                or type(selection.get("sessionEpoch")) is not int
                or selection["sessionEpoch"] != session.epoch
            ):
                raise _NativeSelectionError(*failure)
            snapshot = (
                session.session_id,
                session.epoch,
                session.workspace_id,
                deepcopy(session.execution_workspace),
                deepcopy(session.origin),
            )
            path = Path(selection["path"])
            failure = (
                "NATIVE_FILE_UNAVAILABLE",
                "Selected file is unavailable or could not be read; select it again.",
            )
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
                    raise _NativeSelectionError(
                        "NATIVE_SESSION_CHANGED",
                        "Attachment session or Gateway changed; select the file again.",
                    )

            await check_binding()
            from opensquilla.contracts.attachments import (
                IMAGE_ATTACHMENT_MIMES,
                attachment_category,
                attachment_size_limit_for_mime,
                can_stage_attachment_mime,
                normalize_attachment_mime,
            )
            from opensquilla.contracts.image_validation import validate_image_bytes

            mime = normalize_attachment_mime(selection["mime"])
            if mime in IMAGE_ATTACHMENT_MIMES:
                if len(payload) > attachment_size_limit_for_mime(mime):
                    raise _NativeSelectionError(
                        "NATIVE_FILE_TOO_LARGE", "Selected image exceeds the image size limit."
                    )
                try:
                    validate_image_bytes(payload, mime)
                except ValueError as exc:
                    raise _NativeSelectionError(
                        "NATIVE_IMAGE_INVALID",
                        "Selected image is corrupt, unreadable, or does not match its format; "
                        "choose a valid image.",
                    ) from exc
            # Project inputs retain live identity, including current-value semantics.
            if session.workspace_id or session.execution_workspace is not None:
                from opensquilla.workspace_files import validate_workspace_files

                failure = (
                    "NATIVE_WORKSPACE_CHANGED",
                    "Attachment workspace changed or is unavailable; select the file again.",
                )
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
            # Match UploadStore's snapshot policy. Strict deployments retain
            # the legacy stageable set; text/email must not gain a larger cap.
            # Authorized live references above are not copies and do not use
            # accept_opaque as a filesystem permission switch.
            staged_mime = can_stage_attachment_mime(mime) and (
                store.accept_opaque or attachment_category(mime) in {"pdf", "image", "office"}
            )
            if len(payload) > attachment_size_limit_for_mime(mime, staged=staged_mime):
                raise _NativeSelectionError(
                    "NATIVE_FILE_TOO_LARGE", "Selected file exceeds the size limit for its format."
                )
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
            failure = (
                "NATIVE_STAGING_FAILED",
                "Attachment could not be prepared; select the file and try again.",
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
        except _NativeSelectionError as exc:
            return JSONResponse({"code": exc.code, "error": str(exc)}, status_code=409)
        except (PermissionError, SafeToolError, SandboxBackendError):
            return JSONResponse(
                {
                    "code": "NATIVE_FILE_ACCESS_DENIED",
                    "error": "File access was denied by the current permissions.",
                },
                status_code=403,
            )
        except (AttachmentOpaqueOversizeError, UploadOversizeError):
            return JSONResponse(
                {
                    "code": "NATIVE_FILE_TOO_LARGE",
                    "error": "Selected file exceeds the configured attachment size limit.",
                },
                status_code=409,
            )
        except (AttachmentClaimError, UploadUnsupportedMimeError):
            return JSONResponse(
                {
                    "code": "NATIVE_FORMAT_UNSUPPORTED",
                    "error": "Selected file format is not supported by the attachment policy.",
                },
                status_code=409,
            )
        except UploadStoreFullError:
            return JSONResponse(
                {
                    "code": "NATIVE_UPLOAD_STORE_FULL",
                    "error": "Attachment storage is full; wait for earlier uploads to expire "
                    "and try again.",
                },
                status_code=409,
            )
        except (KeyError, TypeError, ValueError, OSError, UploadStoreError):
            return JSONResponse({"code": failure[0], "error": failure[1]}, status_code=409)

    app.router.routes.append(Route("/api/v1/files/native-import", native_import, methods=["POST"]))
