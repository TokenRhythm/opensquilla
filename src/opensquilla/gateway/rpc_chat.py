"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import structlog

from opensquilla.application.conversation_ancillary import (
    ClarificationSubmissionPort,
    ClarificationSubmissionResult,
    SubmitClarification,
)
from opensquilla.gateway.adapters.conversation_ancillary import (
    GatewayConversationAncillaryAdapter,
)
from opensquilla.gateway.adapters.conversation_ancillary_contract import (
    register_conversation_ancillary_contract,
)
from opensquilla.gateway.adapters.session_history_projection import read_chat_history_v4
from opensquilla.gateway.adapters.session_read_contract import (
    register_chat_history_contract,
)
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    webchat_session_key,
)
from opensquilla.gateway.adapters.turn_admission_contract import (
    register_turn_admission_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)

_d = get_dispatcher()
log = structlog.get_logger(__name__)

type TurnAdmissionAdapterFactory = Callable[
    [RpcContext],
    GatewayTurnAdmissionAdapter,
]
_turn_admission_adapter_factory: TurnAdmissionAdapterFactory | None = None


def bind_turn_admission_adapter_factory(
    factory: TurnAdmissionAdapterFactory,
) -> None:
    """Wire the fixed TurnAdmission composition after session RPC import."""
    global _turn_admission_adapter_factory
    _turn_admission_adapter_factory = factory




def _canonical_webchat_session_key(value: object = None) -> str:
    """Map legacy WebChat defaults onto the canonical WebChat session."""
    return webchat_session_key(value)




def _chat_turn_admission_adapter(ctx: RpcContext) -> GatewayTurnAdmissionAdapter:
    factory = _turn_admission_adapter_factory
    if factory is None:
        raise RuntimeError("TurnAdmission composition is not initialized")
    return factory(ctx)


async def _handle_chat_send(params: dict | None, ctx: RpcContext) -> dict:
    return await _chat_turn_admission_adapter(ctx).admit(
        params,
        surface="webchat",
    )


async def _handle_chat_abort(params: dict | None, ctx: RpcContext) -> dict:
    return await _chat_turn_admission_adapter(ctx).cancel(
        params,
        surface="webchat",
    )


async def _handle_chat_history(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return await read_chat_history_v4(params, ctx)




_handle_chat_history_contract = register_chat_history_contract(
    _d,
    _handle_chat_history,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


def _clarify_fields_to_text(fields: dict[str, object]) -> str:
    """Serialize a clarify form into the existing text reply protocol."""
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


async def _submit_clarification(
    request: SubmitClarification | dict | None,
    ctx: RpcContext,
) -> dict:
    """Resolve or admit one typed command, retaining the legacy helper shape."""

    if isinstance(request, SubmitClarification):
        command = request
    else:
        if not isinstance(request, dict):
            raise ValueError("params required: sessionKey, fields")
        fields = request.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("params.fields must be a non-empty mapping")
        raw_request_id = request.get("request_id", request.get("requestId"))
        request_id = str(raw_request_id).strip() if raw_request_id is not None else None
        if request_id == "":
            raise ValueError("params.request_id must be a non-empty string")
        run_id = request.get("run_id")
        command = SubmitClarification(
            session_key=_canonical_webchat_session_key(request.get("sessionKey")),
            fields=fields,
            request_id=request_id,
            run_id=run_id if isinstance(run_id, str) else None,
        )
    fields = dict(command.fields)
    session_key = _canonical_webchat_session_key(command.session_key)
    if command.request_id is not None:
        request_id = command.request_id
        task_runtime = getattr(ctx, "task_runtime", None)
        resolve_user_input = getattr(task_runtime, "resolve_user_input", None)
        if not callable(resolve_user_input):
            raise RpcUnavailableError("Deferred user-input resolution is not available")
        result = await resolve_user_input(
            session_key=session_key,
            request_id=request_id,
            fields=fields,
        )
        log.info(
            "chat.clarify_submit.deferred",
            session_key=session_key,
            request_id=request_id,
            field_count=len(fields),
            replayed=bool(result.get("replayed")),
        )
        return {"sessionKey": session_key, **result}

    text = _clarify_fields_to_text(fields)
    run_id = command.run_id
    log.info(
        "chat.clarify_submit.params",
        session_key=session_key,
        field_count=len(fields),
        run_id=run_id if isinstance(run_id, str) and run_id else None,
    )
    send_params: dict[str, Any] = {
        "message": text,
        "sessionKey": session_key,
        "inputProvenance": {"kind": "clarify_form", "source": "webui"},
    }
    if isinstance(run_id, str) and run_id:
        send_params["_source"] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
            "channel_id": f"webchat:{session_key}",
            "source_kind": "webui",
            "source_name": "WebChat",
            "clarify_run_id": run_id,
        }
    return cast(
        dict,
        await _chat_turn_admission_adapter(ctx).admit(send_params, surface="webchat"),
    )


@_d.method("chat.inject", scope="operator.admin")
async def _handle_chat_inject(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, role, content")
    for field in ("sessionKey", "role", "content"):
        if field not in params:
            raise ValueError(f"params.{field} is required")

    role = params["role"]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role}")

    session_key = _canonical_webchat_session_key(params["sessionKey"])

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = getattr(ctx.session_manager, "_storage", None)
    if storage is not None:
        existing = await storage.get_session(session_key)
        if existing is None:
            raise KeyError(f"Session not found: {session_key}")

    await ctx.session_manager.append_message(session_key, role=role, content=params["content"])
    return {"ok": True, "sessionKey": session_key}


async def _handle_chat_send_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _handle_chat_send(params, ctx)


async def _handle_chat_abort_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _handle_chat_abort(params, ctx)


_handle_chat_send_generated_contract = register_turn_admission_contract(
    _d,
    "chat.send",
    _handle_chat_send_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_chat_abort_generated_contract = register_turn_admission_contract(
    _d,
    "chat.abort",
    _handle_chat_abort_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


class _GatewayClarificationSubmissionPort(ClarificationSubmissionPort):
    def __init__(self, context: RpcContext) -> None:
        self._context = context

    async def submit(self, command: SubmitClarification) -> ClarificationSubmissionResult:
        return cast(
            ClarificationSubmissionResult,
            await _submit_clarification(command, self._context),
        )


async def _handle_chat_clarify_submit_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    adapter = GatewayConversationAncillaryAdapter(
        clarification=_GatewayClarificationSubmissionPort(ctx)
    )
    return await adapter.submit_clarification(params)


_handle_chat_clarify_submit_generated_contract = (
    register_conversation_ancillary_contract(
        _d,
        "chat.clarify_submit",
        _handle_chat_clarify_submit_contract,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
)
