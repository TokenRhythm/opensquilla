"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import uuid4

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
from opensquilla.gateway.compaction_target import (
    effective_session_model,
    resolve_gateway_compaction_target,
    resolve_selected_compaction_provider,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.context_overflow import apply_context_overflow_policy
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import build_compaction_config_from_provider
from opensquilla.session.compaction_lifecycle import new_compaction_id

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




def _effective_compaction_model(session: object | None) -> str | None:
    return effective_session_model(session)


def _resolve_compaction_provider(ctx: RpcContext, session: object | None) -> object | None:
    return resolve_selected_compaction_provider(ctx, session)


async def _build_context_overflow_compaction_config(ctx: RpcContext, session_key: str):
    session = None
    storage = getattr(getattr(ctx, "session_manager", None), "_storage", None)
    if storage is not None:
        try:
            session = await storage.get_session(session_key)
        except Exception:  # noqa: BLE001
            session = None
    compaction_target = resolve_gateway_compaction_target(ctx, session)
    return build_compaction_config_from_provider(
        compaction_target.provider,
        model_override=compaction_target.model or _effective_compaction_model(session),
        compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
        compaction_plan=compaction_target.plan,
    )


async def _enforce_context_overflow(
    ctx: RpcContext,
    session_key: str,
    message: str,
    *,
    restricted_turn: bool = False,
) -> dict | None:
    """Apply the configured context-overflow policy before a turn runs.

    Returns a stable error envelope when the policy is REFUSE and the
    payload exceeds the budget; returns ``None`` for every other path
    (policy consults pass, HARD_TRUNCATE dropped some history in place,
    AUTO_SUMMARIZE kicked off a compaction). The caller short-circuits
    on a non-None return.
    """

    config = ctx.config if isinstance(ctx.config, GatewayConfig) else GatewayConfig()

    transcript: list = []
    if ctx.session_manager is not None:
        try:
            transcript = list(await ctx.session_manager.get_transcript(session_key))
        except Exception:  # noqa: BLE001 — missing transcript just means "no history"
            transcript = []

    # Per-session context-budget overrides are independent from runtime/request
    # timeout resolution, which happens in TurnRunner.
    # A session-scoped context_budget_tokens override is supported via
    # ctx.session_manager.get_config(session_key) if present.
    budget_override = None
    policy_override = None
    if ctx.session_manager is not None and hasattr(ctx.session_manager, "get_session_config"):
        try:
            session_cfg = await ctx.session_manager.get_session_config(session_key)
            if session_cfg is not None:
                budget_override = getattr(session_cfg, "context_budget_tokens", None)
                policy_override = getattr(session_cfg, "context_overflow_policy", None)
        except Exception:  # noqa: BLE001
            pass

    from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
    from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

    usage_scope = await build_session_usage_scope(
        getattr(ctx, "usage_event_sink", None),
        ctx.session_manager,
        session_key,
        run_kind="session_compaction",
    )
    root_operation_id = new_compaction_id()
    provider_request_correlation = None
    if not provider_request_correlation_disabled(config=config):
        try:
            session = await ctx.session_manager.get_session(session_key)
        except Exception:  # noqa: BLE001 - observability is best-effort
            session = None
        durable_session_id = getattr(session, "session_id", None)
        if isinstance(durable_session_id, str) and durable_session_id:
            provider_request_correlation = ProviderRequestCorrelation(
                session_id=durable_session_id,
                turn_id=root_operation_id,
                execution_id=uuid4().hex,
                call_kind="auxiliary.compaction",
            )
    with bind_usage_accounting_scope(usage_scope):
        outcome = await apply_context_overflow_policy(
            config=config,
            message=message,
            transcript=transcript,
            session_key=session_key,
            session_manager=ctx.session_manager,
            compaction_config=await _build_context_overflow_compaction_config(ctx, session_key),
            flush_service=getattr(ctx, "flush_service", None),
            compaction_marker=getattr(ctx, "turn_runner", None),
            policy_override=policy_override,
            budget_override=budget_override,
            provider_request_correlation=provider_request_correlation,
            root_operation_id=root_operation_id,
            restricted_turn=restricted_turn,
        )

    if outcome.refusal is not None:
        log.warning(
            "chat_send.context_overflow_refused",
            session_key=session_key,
            estimated_tokens=outcome.estimated_tokens,
            budget_tokens=outcome.budget_tokens,
        )
        return outcome.refusal

    if outcome.compacted_this_turn:
        marker = getattr(ctx, "turn_runner", None)
        mark = getattr(marker, "mark_compacted_this_turn", None)
        if callable(mark):
            mark(session_key)

    return None


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
