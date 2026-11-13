"""Authenticated, session-scoped JEV preference and feedback handlers."""

from __future__ import annotations

from enum import Enum
from typing import Any, cast

from opensquilla.gateway.adapters.jev_contract import register_jev_contract, validate_jev_params
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.jev_service import JevService, get_jev_service
from opensquilla.session.keys import canonicalize_session_key

_d = get_dispatcher()


def _service(ctx: RpcContext) -> JevService:
    return get_jev_service(ctx.config, ctx.session_manager, ctx.turn_runner)


def _key(values: dict[str, Any]) -> str:
    key = canonicalize_session_key(values["sessionKey"])
    if not key:
        raise ValueError("sessionKey must be non-empty")
    return cast(str, key)


async def _synchronize_session(ctx: RpcContext, key: str) -> JevService:
    from opensquilla.gateway.session_services import get_session_storage

    service = _service(ctx)
    storage = get_session_storage(ctx.session_manager)
    if storage is not None:
        session = await storage.get_session(key)
        if session is not None:
            await service.bind_session(
                key, str(getattr(session, "session_id", "") or ""),
                int(getattr(session, "epoch", 0) or 0),
            )
    return service


async def _snapshot(ctx: RpcContext, key: str) -> dict[str, Any]:
    from opensquilla.engine.steps.jev_router import resolve_jev_deployment

    service = await _synchronize_session(ctx, key)
    settings = await service.settings(key, ctx.config.jev_router)
    inherited = getattr(ctx.provider_selector, "current_config", None)
    resolution = resolve_jev_deployment(
        ctx.config, settings, inherited=inherited, session_key=key, acquire=False,
    )
    result = await service.snapshot(key, ctx.config.jev_router, available=resolution.ready)
    if result["enabled"] and getattr(ctx.config.jev_router, "ensemble_enabled", True):
        from opensquilla.engine.jev_ensemble import jev_ensemble_descriptor

        if inherited is not None:
            plan = jev_ensemble_descriptor(ctx.config, inherited=inherited, session_key=key)
            result["ensembleAvailable"] = plan is not None
            if plan is not None:
                result["ensemblePlan"] = plan
        else:
            result["ensembleAvailable"] = False
    return result


def _bind_notifications(ctx: RpcContext) -> JevService:
    service = _service(ctx)

    async def changed(key: str) -> None:
        from opensquilla.gateway.rpc_sessions import _emit_to_subscribers

        await _emit_to_subscribers(ctx, key, "jev.changed", await _snapshot(ctx, key))

    service.on_change = changed

    async def deadline(key: str, decision_id: str) -> None:
        await _handle_jev_respond({
            "sessionKey": key, "decisionId": decision_id, "accepted": False,
        }, ctx, automatic=True)

    service.on_deadline = deadline

    async def routing_changed(key: str) -> None:
        from opensquilla.gateway.rpc_sessions import (
            _emit_to_subscribers,
            _handle_sessions_routing_get,
        )

        routing = await _handle_sessions_routing_get({"sessionKey": key}, ctx)
        keepalive = getattr(ctx, "prompt_cache_keepalive_service", None)
        if keepalive is not None:
            keepalive.refresh_required(key, "session_deployment_changed")
        hold = getattr(ctx.turn_runner, "router_control_hold_store", None)
        advance = getattr(hold, "advance_routing_revision", None)
        if callable(advance):
            advance(key, routing["revision"])
        await _emit_to_subscribers(ctx, key, "sessions.routing.changed", routing)

    service.on_routing_change = routing_changed
    return service


async def _handle_jev_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    values = validate_jev_params("jev.get", params)
    key = _key(values)
    _bind_notifications(ctx)
    return await _snapshot(ctx, key)


async def _handle_jev_configure(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    values = validate_jev_params("jev.configure", params)
    key = _key(values)
    patch = {
        field: value.value if isinstance(value, Enum) else value
        for field, value in values.items() if field != "sessionKey"
    }
    service = _bind_notifications(ctx)
    await service.configure(key, patch, ctx.config.jev_router)
    await service.notify(key)
    return await _snapshot(ctx, key)


async def _handle_jev_preview(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Acknowledge an ephemeral draft; provider inference stays in a background task."""
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.steps.jev_router import (
        apply_jev_router,
        jev_input_fingerprint,
        resolve_jev_deployment,
    )
    from opensquilla.gateway.session_services import get_session_storage

    values = validate_jev_params("jev.preview", params)
    key = _key(values)
    text = values["text"].strip()
    service = _bind_notifications(ctx)
    await _synchronize_session(ctx, key)
    if not service.is_enabled(key, ctx.config.jev_router) or service.execution_owned(key):
        return await _snapshot(ctx, key)
    from opensquilla.gateway.session_services import get_session_lock

    lock = get_session_lock(ctx.turn_runner, key)
    has_work = getattr(getattr(ctx, "task_runtime", None), "has_session_work", None)
    if (lock is not None and lock.locked()) or (callable(has_work) and await has_work(key)):
        return await _snapshot(ctx, key)
    if not text:
        await service.cancel_checks(key, preserve_context_notices=True)
        service.set_draft_id(key, values["draftId"])
        await service.set_status(key, "ready")
        await service.notify(key)
        return await _snapshot(ctx, key)
    service.set_draft_id(key, values["draftId"])
    fingerprint = jev_input_fingerprint(text)

    async def evaluate(generation: int) -> TurnContext:
        storage = get_session_storage(ctx.session_manager)
        session = await storage.get_session(key) if storage is not None else None
        provider = str(getattr(session, "provider_override", None) or ctx.config.llm.provider)
        model = str(
            getattr(session, "model_override", None)
            or getattr(session, "model", None) or ctx.config.llm.model
        )
        inherited = getattr(ctx.provider_selector, "current_config", None)
        resolution = resolve_jev_deployment(
            ctx.config, {"provider": provider, "model": model},
            inherited=inherited, session_key=key,
        )
        if resolution.ready:
            inherited = resolution.provider_config
        context: dict[str, Any] = {}
        history_reader = getattr(ctx.turn_runner, "_router_previous_assistant_context", None)
        if session is not None and callable(history_reader):
            context = await history_reader(
                key, expected_session_id=session.session_id,
                expected_session_epoch=session.epoch, include_capacity=True,
            )
        session_mode = str(getattr(session, "model_routing_mode", None) or "").strip().lower()
        ensemble_active = session_mode == "ensemble" or (
            session_mode not in {"direct", "router", "ensemble"}
            and bool(getattr(ctx.config.llm_ensemble, "enabled", False))
        )
        history_has_images = context.get("history_has_recent_image") is True
        metadata = {
            "jev_execution_mode": "ensemble" if ensemble_active else "direct",
            "router_history_user_texts": context.get("history_user_texts", []),
            "router_prev_assistant_text": context.get("prev_assistant_text", ""),
            "jev_turn_count": context.get("jev_turn_count", 1),
            **({"jev_completed_conversation_turns": context["jev_completed_conversation_turns"]}
               if "jev_completed_conversation_turns" in context else {}),
            "image_context_has_images": history_has_images,
            "router_history_has_recent_image": history_has_images,
            "router_history_image_turn_count": context.get("history_image_turn_count", 0),
            "routing_history_capacity_estimated_tokens": context.get(
                "history_capacity_estimated_tokens", 0,
            ),
            "routing_history_capacity_message_count": context.get(
                "history_capacity_message_count", 0,
            ),
            "routing_history_capacity_estimate_complete": context.get(
                "history_capacity_estimate_complete",
            ) is True,
        }
        turn = TurnContext(
            message=text, session_key=key, config=ctx.config, provider=None,
            model=model, tool_defs=[], system_prompt="", metadata=metadata,
        )
        return await apply_jev_router(
            turn, service=service, input_id=fingerprint, inherited=inherited,
            routing_revision=int(getattr(session, "model_routing_revision", 0) or 0),
            assessment_stage="input", check_generation=generation,
        )

    if service.current_check(key, fingerprint) is None:
        await service.set_status(key, "checking")
        await service.start_check(key, fingerprint, evaluate)
    return await _snapshot(ctx, key)


async def _handle_jev_respond(
    params: dict | None, ctx: RpcContext, *, automatic: bool = False,
) -> dict[str, Any]:
    values = validate_jev_params("jev.respond", params)
    key = _key(values)
    decision_id = values["decisionId"]
    accepted = values["accepted"]
    service = _bind_notifications(ctx)
    # Summary generation is auxiliary work and must not hold the global model
    # decision mutex while other conversations reach their own deadlines.
    initial = await service.get_decision(key, decision_id)
    if initial is not None and initial.get("action") == "new_session":
        return await _respond_handoff(ctx, key, decision_id, accepted, automatic=automatic)
    async with service.response_lock:
        await _synchronize_session(ctx, key)
        decision = await service.get_decision(key, decision_id)
        if decision is None:
            raise KeyError("JEV decision not found")
        if automatic:
            if decision["status"] != "pending":
                return await _snapshot(ctx, key)
            deadline = decision.get("autoSwitchAt")
            if not isinstance(deadline, (float, int)) or service._clock() < deadline:
                return await _snapshot(ctx, key)
            settings = await service.settings(key, ctx.config.jev_router)
            if not settings.get("enabled", False):
                await service.expire(key, decision_id)
                await service.notify(key)
                return await _snapshot(ctx, key)
            accepted = settings.get("mode") == "trust"
        if decision["status"] in {"applied", "accepted"} and accepted:
            return await _snapshot(ctx, key)
        if decision["status"] == "dismissed" and not accepted:
            return await _snapshot(ctx, key)
        if decision["status"] != "pending":
            raise RpcHandlerError("CONFLICT", "This recommendation is no longer pending.")
        if accepted and service.execution_owned(key):
            # The running turn owns the safe request boundary and performs its
            # durable CAS there. Never run an idle-session mutation mid-stream.
            await service.respond(key, decision_id, True)
        elif accepted and (
            decision.get("target") is not None or decision.get("action") == "ensemble"
        ):
            from opensquilla.gateway.rpc_sessions import (
                _handle_sessions_routing_get,
                _handle_sessions_routing_set,
            )

            routing = await _handle_sessions_routing_get({"sessionKey": key}, ctx)
            revision = routing["revision"]
            expected = await service.routing_revision(key, decision_id)
            if expected is not None and expected != revision:
                await service.expire(key, decision_id)
                await service.notify(key)
                raise RpcHandlerError(
                    "CONFLICT", "The session model changed. Request a fresh recommendation.",
                )
            if decision.get("action") == "ensemble":
                from opensquilla.engine.jev_ensemble import prepare_jev_ensemble
                from opensquilla.engine.steps.jev_router import resolve_jev_deployment

                inherited = getattr(ctx.provider_selector, "current_config", None)
                current = resolve_jev_deployment(
                    ctx.config, decision["current"], inherited=inherited, session_key=key,
                )
                prepared = prepare_jev_ensemble(
                    ctx.config, inherited=current.provider_config, session_key=key,
                    expected_fingerprint=(decision.get("ensemble") or {}).get(
                        "configurationFingerprint",
                    ),
                ) if current.ready else None
                if prepared is None:
                    await service.expire(key, decision_id)
                    await service.notify(key)
                    raise RpcHandlerError("CONFLICT", "The configured ensemble is no longer ready.")
                result = await _handle_sessions_routing_set({
                    "sessionKey": key, "mode": "ensemble", "expectedRevision": revision,
                }, ctx)
            else:
                result = await _handle_sessions_routing_set({
                    "sessionKey": key, "mode": "direct", "expectedRevision": revision,
                    "modelSelection": decision["target"],
                }, ctx)
            # A failed/busy CAS leaves the decision pending and safely retryable.
            await service.mark_applied(key, decision_id, routing_revision=result["revision"])
        else:
            await service.respond(key, decision_id, accepted)
    await service.notify(key)
    return await _snapshot(ctx, key)


async def _respond_handoff(
    ctx: RpcContext, key: str, decision_id: str, accepted: bool, *, automatic: bool,
) -> dict[str, Any]:
    service = _bind_notifications(ctx)
    if accepted and not automatic:
        async with service.handoff_response(key, decision_id):
            try:
                return await _respond_handoff_operation(
                    ctx, key, decision_id, accepted, automatic=automatic,
                )
            except BaseException:
                async with service.response_lock:
                    await service.reopen_handoff(key, decision_id)
                await service.notify(key)
                raise
    return await _respond_handoff_operation(ctx, key, decision_id, accepted, automatic=automatic)


async def _respond_handoff_operation(
    ctx: RpcContext, key: str, decision_id: str, accepted: bool, *, automatic: bool,
) -> dict[str, Any]:
    service = _bind_notifications(ctx)
    async with service.response_lock:
        await _synchronize_session(ctx, key)
        decision = await service.get_decision(key, decision_id)
        if decision is None:
            raise KeyError("JEV decision not found")
        if automatic:
            return await _snapshot(ctx, key)
        if decision["status"] == "applied" and accepted:
            return await _snapshot(ctx, key)
        if decision["status"] == "dismissed" and not accepted:
            return await _snapshot(ctx, key)
        if decision["status"] not in {"pending", "accepted"}:
            raise RpcHandlerError("CONFLICT", "This reminder is no longer pending.")
        if not accepted:
            await service.respond(key, decision_id, False)
        else:
            from opensquilla.session.manager import _settle_workspace_operation

            settings = await service.settings(key, ctx.config.jev_router)
            if not settings.get("enabled") or not settings.get("session_restart_enabled", True):
                raise RpcHandlerError("CONFLICT", "Session handoff is disabled.")
            has_work = getattr(getattr(ctx, "task_runtime", None), "has_session_work", None)
            if service.execution_owned(key) or (callable(has_work) and await has_work(key)):
                raise RpcHandlerError("CONFLICT", "Wait for the current task to finish first.")
            await _settle_workspace_operation(service.respond(key, decision_id, True))
    if accepted:
        from opensquilla.gateway.jev_handoff import create_jev_handoff

        result = await create_jev_handoff(ctx, key, decision_id)
        async with service.response_lock:
            decision = await service.get_decision(key, decision_id)
            if decision is None or decision.get("status") not in {"accepted", "applied"}:
                raise RpcHandlerError("CONFLICT", "This handoff approval was withdrawn.")
            await service.mark_handoff_applied(key, decision_id, result["targetSessionKey"])
    await service.notify(key)
    return await _snapshot(ctx, key)


_JEV_IMPLEMENTATIONS = {
    "jev.get": _handle_jev_get,
    "jev.configure": _handle_jev_configure,
    "jev.respond": _handle_jev_respond,
    "jev.preview": _handle_jev_preview,
}

_JEV_CONTRACT_HANDLERS = {
    method: register_jev_contract(
        _d, method, implementation, internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _JEV_IMPLEMENTATIONS.items()
}
