"""Side conversation capability boundary shared by Gateway RPC entry points."""

from __future__ import annotations

import time
from typing import Any

from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.keys import is_sidechat_key
from opensquilla.session.sidechat import sidechat_metadata


def _rejected(code: str, message: str) -> Exception:
    from opensquilla.gateway.rpc.registry import RpcHandlerError

    return RpcHandlerError(code, message, accepted=False, retryable=False)


async def enforce_sidechat_command(method: str, params: Any, ctx: Any) -> Any:
    if not isinstance(params, dict):
        return params
    # ``sessions.sidechat.create`` names its source in ``parentKey`` rather
    # than ``key``.  Include it here so a side conversation cannot smuggle a
    # nested fork through a differently shaped lifecycle request.
    key = (
        params.get("key")
        or params.get("sessionKey")
        or params.get("session_key")
        or params.get("parentKey")
        or params.get("parent_key")
    )
    if not isinstance(key, str) or not is_sidechat_key(key):
        return params

    if not ctx.principal.is_owner:
        raise _rejected("OWNER_REQUIRED", "Side conversations require an owner.")
    storage = get_session_storage(ctx.session_manager)
    node = await storage.get_session(key) if storage is not None else None
    metadata = sidechat_metadata(node)
    if storage is None or metadata is None:
        raise _rejected("NOT_FOUND", "Side conversation not found.")
    prohibited = (
        method.startswith("goals.")
        or method.startswith("plans.")
        or method.startswith("sessions.plan")
        or method
        in {
            "sessions.fork",
            "sessions.forkThroughTurn",
            "sessions.reset",
            "sessions.truncate",
            "sessions.sidechat.create",
        }
    )
    if prohibited:
        raise _rejected("INVALID_REQUEST", "This operation is unavailable in a side conversation.")
    if method in {
        "chat.send", "sessions.send", "sessions.steer.v2",
        "sessions.pending_inputs.enqueue", "sessions.pending_inputs.dispatch",
        "sessions.pending_inputs.steer",
    }:
        if params.get("intent", "continue") != "continue":
            raise _rejected("INVALID_REQUEST", "Side conversations cannot reset or create chats.")
        if params.get("collaborationMode", params.get("collaboration_mode")) == "plan":
            raise _rejected("INVALID_REQUEST", "Plan runs are unavailable in a side conversation.")
        parent = await storage.get_session(metadata["parentKey"])
        if (
            metadata.get("state") != "ready"
            or parent is None
            or int(metadata.get("leaseExpiresAt", 0)) <= int(time.time() * 1000)
            or parent.session_id != metadata["parentSessionId"]
            or parent.epoch != metadata["parentEpoch"]
        ):
            raise _rejected("INVALID_REQUEST", "Side conversation is closed or its parent changed.")
        source = params.get("_source")
        source = dict(source) if isinstance(source, dict) else {}
        if any(
            source.get(name) or params.get(name)
            for name in (
                "forkBeforeMessageId",
                "fork_before_message_id",
                "retryTurnId",
                "retry_turn_id",
            )
        ):
            raise _rejected(
                "INVALID_REQUEST", "Side conversations cannot edit or retry parent turns."
            )
        source["noMemoryCapture"] = True
        source["no_memory_capture"] = True
        params = {**params, "_source": source, "noMemoryCapture": True, "no_memory_capture": True}
    return params
