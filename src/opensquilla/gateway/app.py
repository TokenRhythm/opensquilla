"""Starlette ASGI application factory with routes and middleware."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from opensquilla import __version__
from opensquilla.gateway.approval_events import build_approval_snapshot_item
from opensquilla.gateway.approval_queue import get_approval_queue
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui import create_control_ui_routes
from opensquilla.gateway.middleware import (
    AuthMiddleware,
    ErrorHandlingMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from opensquilla.gateway.origin_guard import (
    extract_http_token,
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import is_loopback_address, is_loopback_bind
from opensquilla.gateway.websocket import handle_ws_connection

log = structlog.get_logger(__name__)

_start_time = time.time()


def _human_actionable_approvals(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude internal automatic reviews from operator approval surfaces."""

    return [
        item
        for item in pending
        if not (
            isinstance(item.get("params"), dict)
            and item["params"].get("humanActionable") is False
        )
    ]


def create_gateway_app(
    config: GatewayConfig,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    # May be a manager instance OR a zero-arg callable resolving to one:
    # live channel reconcile can create the manager after boot, so contexts
    # must re-resolve it per request instead of freezing the boot-time value.
    channel_manager: Any = None,
    usage_tracker: Any = None,
    usage_event_sink: Any = None,
    meta_run_writer: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    flush_service: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    provider_stats: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    extra_routes: list[Route] | None = None,
) -> Starlette:
    """Build and return the Starlette ASGI application."""
    if diagnostics_state is None:
        from opensquilla.gateway.diagnostics import DiagnosticsState

        diagnostics_state = DiagnosticsState.from_config(config)

    dispatcher = get_dispatcher()

    def _resolve_channel_manager() -> Any:
        return channel_manager() if callable(channel_manager) else channel_manager

    def _rpc_status_code(result: Any, default: int = 500) -> int:
        if result.error is None:
            return default
        code = result.error.code
        if code == "INVALID_REQUEST":
            return 400
        if code == "UNAUTHORIZED":
            return 403
        if code in {"NOT_FOUND", "METHOD_NOT_FOUND"}:
            return 404
        if code in {
            "COLLECT_RACE",
            "IDEMPOTENCY_CONFLICT",
            "SESSION_CHANGED",
            "SESSION_CONFLICT",
        }:
            return 409
        if code == "QUEUE_FULL":
            return 429
        if code in {"UNAVAILABLE", "STORAGE_BUSY"}:
            return 503
        return default

    def _same_origin(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        """Wrap a state-changing handler with the shared same-origin guard.

        Rejects browser-mediated cross-origin requests (403 FORBIDDEN_ORIGIN)
        before the handler runs; requests without an Origin header (curl, the
        desktop client) and same-origin Web UI requests pass through. Origins
        explicitly listed in ``cors.allowed_origins`` are also accepted.
        """

        @functools.wraps(handler)
        async def guarded(request: Request) -> Response:
            if not request_origin_allowed(request, config):
                return forbidden_origin_response()
            return await handler(request)

        return guarded

    # ── HTTP endpoint handlers ───────────────────────────────────────────────

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "status": "live"})

    async def root(request: Request) -> RedirectResponse:
        return RedirectResponse(url=f"{config.control_ui.base_path}/")

    async def ready(request: Request) -> JSONResponse:
        uptime = int((time.time() - _start_time) * 1000)
        is_ready = bool(getattr(request.app.state, "gateway_ready", True))
        payload = {
            "ready": is_ready,
            "status": "ready" if is_ready else "starting",
            "uptime_ms": uptime,
        }
        return JSONResponse(payload, status_code=200 if is_ready else 503)

    async def api_config(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "config.get", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_sessions(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        params: dict[str, object] = {}
        raw_limit = request.query_params.get("limit")
        if raw_limit:
            try:
                params["limit"] = int(raw_limit)
            except ValueError:
                return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        view = request.query_params.get("view")
        if view:
            params["view"] = view
        result = await dispatcher.dispatch("_http", "sessions.list", params or None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"sessions": []})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_chat(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "chat.send", body, ctx)
        if result.ok:
            return JSONResponse({"ok": True, **(result.payload or {})})
        error = result.error
        error_payload = error.model_dump(exclude_none=True) if error is not None else {}
        return JSONResponse(
            {
                "error": error.message if error is not None else "error",
                **error_payload,
            },
            status_code=_rpc_status_code(result, default=400),
        )

    async def api_system_status(request: Request) -> JSONResponse:
        uptime = int((time.time() - _start_time) * 1000)
        provider_name = None
        if provider_selector is not None and getattr(provider_selector, "is_configured", True):
            # Report the *configured* provider id (e.g. "openrouter"), not the
            # wire-protocol backend class. OpenAI-compatible providers
            # (openrouter / deepseek / gemini) are all served by OpenAIProvider,
            # so introspecting the instance would mislabel them as "openai".
            provider_name = getattr(provider_selector, "active_provider_id", None)
            if not provider_name:
                try:
                    p = provider_selector.resolve()
                    provider_name = getattr(p, "name", None) or type(p).__name__
                except Exception:
                    pass
        return JSONResponse(
            {
                "version": __version__,
                "uptime_ms": uptime,
                "status": "running",
                "provider": provider_name,
                "auth_mode": config.auth.mode,
            }
        )

    async def api_system_update(request: Request) -> JSONResponse:
        """Return cached update state and ensure a passive refresh is running.

        The handler never waits for GitHub. Repeated callers share the update
        checker's in-process single-flight thread and its persisted TTL.
        """
        from opensquilla.observability.update_check import (
            default_update_info,
            get_cached_update_info,
            start_background_update_check,
        )

        try:
            info = get_cached_update_info(config=config, version=__version__)
        except Exception:  # pragma: no cover - never break the Control UI
            log.debug("gateway.update_check_cache_read_failed", exc_info=True)
            info = None
        try:
            start_background_update_check(config=config, version=__version__)
        except Exception:  # pragma: no cover - never break the Control UI
            log.debug("gateway.update_check_refresh_failed", exc_info=True)
        payload = (
            info.to_public_dict()
            if info is not None
            else default_update_info(version=__version__).to_public_dict()
        )
        response = JSONResponse(payload)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    async def api_system_shutdown(request: Request) -> JSONResponse:
        """Owner-only graceful shutdown trigger.

        Signals the run loop to run the full ``GatewayServer.close()`` drain
        (in-flight agent turns + background completions, then scheduler/channel
        teardown) and exit. This is the cross-platform shutdown path the CLI and
        desktop use where POSIX signals are unavailable or unreliable — notably
        Windows, which has no real ``SIGTERM`` (``os.kill`` / ``child.kill`` map
        to an immediate ``TerminateProcess`` that skips the drain).

        Gated on loopback-proven ownership so a remote peer can never stop the
        gateway. Returns 202 once the drain is requested (the response flushes
        before the server stops, since ``close()`` drains before unbinding), and
        503 when no run loop is attached (app built without a server — e.g. in
        tests or embedded ``run=False`` use).
        """
        ctx = _make_ctx(request)
        if not ctx.principal.is_owner:
            return JSONResponse({"error": "owner privileges required"}, status_code=403)
        request_shutdown = getattr(request.app.state, "request_shutdown", None)
        if request_shutdown is None:
            return JSONResponse(
                {"error": "graceful shutdown is not available in this mode"},
                status_code=503,
            )
        request_shutdown("api_shutdown")
        return JSONResponse({"status": "accepted"}, status_code=202)

    def _desktop_gateway_owner(request: Request) -> tuple[Any | None, JSONResponse | None]:
        """Resolve a Desktop ownership proof without exposing profile paths."""

        owner = getattr(request.app.state, "desktop_gateway_ownership", None)
        if owner is None:
            return None, JSONResponse({"error": "not found"}, status_code=404)
        peer_ip = request.client.host if request.client is not None else None
        if not is_loopback_bind(config.host) or not is_loopback_address(peer_ip):
            return None, JSONResponse(
                {"error": "desktop owner privileges required"},
                status_code=403,
            )
        return owner, None

    async def api_desktop_identity(request: Request) -> JSONResponse:
        """Prove that this listener is the Desktop instance recorded on disk."""

        from opensquilla.gateway.desktop_ownership import valid_desktop_challenge

        owner, error = _desktop_gateway_owner(request)
        if error is not None:
            return error
        assert owner is not None
        try:
            body = await request.json()
        except Exception:
            body = None
        challenge = body.get("challenge") if isinstance(body, dict) else None
        if not valid_desktop_challenge(challenge):
            return JSONResponse({"error": "invalid challenge"}, status_code=400)
        response = JSONResponse(owner.identity_response(challenge))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    async def api_desktop_shutdown(request: Request) -> JSONResponse:
        """Shut down only after a nonce proof binds the request to this instance."""

        owner, error = _desktop_gateway_owner(request)
        if error is not None:
            return error
        assert owner is not None
        try:
            body = await request.json()
        except Exception:
            body = None
        challenge = body.get("challenge") if isinstance(body, dict) else None
        proof = body.get("proof") if isinstance(body, dict) else None
        if not isinstance(challenge, str) or not isinstance(proof, str):
            return JSONResponse({"error": "invalid ownership proof"}, status_code=403)
        if not owner.verify_shutdown_proof(challenge, proof):
            return JSONResponse({"error": "invalid ownership proof"}, status_code=403)
        request_shutdown = getattr(request.app.state, "request_shutdown", None)
        if request_shutdown is None:
            return JSONResponse(
                {"error": "graceful shutdown is not available in this mode"},
                status_code=503,
            )
        request_shutdown("desktop_api_shutdown")
        response = JSONResponse({"status": "accepted"}, status_code=202)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def api_usage(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "usage.status", None, ctx)
        if result.ok:
            # Merge breakdown from usage.cost into the status response
            cost_result = await dispatcher.dispatch("_http", "usage.cost", None, ctx)
            payload = result.payload or {}
            if cost_result.ok and cost_result.payload:
                payload["breakdown"] = cost_result.payload.get("breakdown", [])
                payload["totalSessions"] = payload.get("totalSessions", 0)
            return JSONResponse(payload)
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    def _make_ctx(request: Request | None = None, role_claim: str = "operator") -> RpcContext:
        from opensquilla.gateway.auth import Principal, resolve_auth

        auth_params: dict[str, str] = {}
        token = extract_http_token(request)
        if token:
            auth_params["token"] = token
        peer_ip = request.client.host if request is not None and request.client else None
        principal = resolve_auth(
            config,
            auth_params=auth_params,
            role_claim=role_claim,
            peer_ip=peer_ip,
        )
        if principal is None:
            principal = Principal(
                role=role_claim,
                scopes=frozenset(),
                is_owner=False,
                authenticated=False,
            )
        return RpcContext(
            conn_id="http",
            principal=principal,
            session_manager=session_manager,
            config=config,
            provider_selector=provider_selector,
            tool_registry=tool_registry,
            subscription_manager=subscription_manager,
            channel_manager=_resolve_channel_manager(),
            usage_tracker=usage_tracker,
            usage_event_sink=usage_event_sink,
            meta_run_writer=meta_run_writer,
            skill_loader=skill_loader,
            cron_scheduler=cron_scheduler,
            turn_runner=turn_runner,
            task_runtime=task_runtime,
            flush_service=flush_service,
            heartbeat_service=heartbeat_service,
            heartbeat_loop=heartbeat_loop,
            agent_registry=agent_registry,
            diagnostics_state=diagnostics_state,
            provider_stats=provider_stats,
            memory_managers=memory_managers or {},
            memory_stores=memory_stores or {},
            memory_retrievers=memory_retrievers or {},
        )

    async def api_channels_status(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "channels.status", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"channels": []})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_channels_logout(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "channels.logout", body, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"ok": True})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result, default=400))

    async def api_channel_pairings(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch(
            "_http",
            "channels.pairings",
            {"channelName": request.query_params.get("channelName", "")},
            ctx,
        )
        if result.ok:
            return JSONResponse(result.payload or {"pairings": []})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result, default=400))

    async def _api_channel_pairing_mutation(
        request: Request,
        method: str,
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", method, body, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"ok": True})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result, default=400))

    async def api_channel_pairing_approve(request: Request) -> JSONResponse:
        return await _api_channel_pairing_mutation(
            request,
            "channels.pairing.approve",
        )

    async def api_channel_pairing_revoke(request: Request) -> JSONResponse:
        return await _api_channel_pairing_mutation(
            request,
            "channels.pairing.revoke",
        )

    async def api_approvals(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "exec.approvals.get", None, ctx)
        if not result.ok:
            return JSONResponse(
                {"error": result.error.message if result.error else "error"},
                status_code=_rpc_status_code(result),
            )
        settings = result.payload or {}
        mode = settings.get("mode", "prompt")
        queue = get_approval_queue()
        pending = _human_actionable_approvals(queue.list_pending())
        items = [build_approval_snapshot_item(item, default_mode=mode) for item in pending]
        return JSONResponse(
            {
                "pending": items,
                "mode": mode,
                "allowPatterns": settings.get("allowPatterns", []),
                "denyPatterns": settings.get("denyPatterns", []),
            }
        )

    async def api_approvals_settings(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        mode = body.get("mode")
        if mode not in {"prompt", "auto-approve", "auto-deny"}:
            return JSONResponse(
                {"error": "mode must be prompt, auto-approve, or auto-deny"},
                status_code=400,
            )
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch(
            "_http",
            "exec.approvals.set",
            {
                "mode": mode,
                "allowPatterns": body.get("allowPatterns"),
                "denyPatterns": body.get("denyPatterns"),
            },
            ctx,
        )
        if not result.ok:
            return JSONResponse(
                {"error": result.error.message if result.error else "error"},
                status_code=_rpc_status_code(result),
            )
        queue = get_approval_queue()
        settings = queue.get_settings()
        return JSONResponse(
            {
                "mode": settings.mode,
                "allowPatterns": settings.allow_patterns,
                "denyPatterns": settings.deny_patterns,
            }
        )

    async def api_approvals_resolve(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        approval_id = body.get("id")
        approved = body.get("approved", False)
        namespace = body.get("namespace", "exec")
        if not approval_id:
            return JSONResponse({"error": "id is required"}, status_code=400)
        ctx = _make_ctx(request)
        method = "plugin.approval.resolve" if namespace == "plugin" else "exec.approval.resolve"
        resolve_params = {
            "id": approval_id,
            "approved": approved,
        }
        choice = body.get("choice") or body.get("decision")
        if isinstance(choice, str) and choice.strip():
            resolve_params["choice"] = choice.strip()
        result = await dispatcher.dispatch(
            "_http",
            method,
            resolve_params,
            ctx,
        )
        if result.ok:
            return JSONResponse(result.payload or {"ok": True})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    async def api_elevated_mode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        if not ctx.principal.is_owner:
            return JSONResponse({"error": "owner privileges required"}, status_code=403)
        session_key = str(body.get("sessionKey") or body.get("session_key") or "").strip()
        if not session_key:
            return JSONResponse({"error": "sessionKey is required"}, status_code=400)
        raw_mode = body.get("mode")
        mode = None if raw_mode in (None, "", "off") else str(raw_mode)
        if mode not in (None, "on", "bypass", "full"):
            return JSONResponse(
                {"error": "mode must be off, on, bypass, or full"},
                status_code=400,
            )
        queue = get_approval_queue()
        try:
            queue.set_elevated_mode(session_key, mode)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        resolved_pending = 0
        if mode in ("bypass", "full"):
            resolved_pending = queue.resolve_pending_for_session(
                session_key,
                approved=True,
                elevated_mode=mode,
            )
        return JSONResponse(
            {
                "sessionKey": session_key,
                "mode": mode or "off",
                "resolvedPending": resolved_pending,
            }
        )

    # ── Agents / Cron HTTP endpoints ────────────────────────────────────────

    async def api_agents(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "agents.list", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"agents": []})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    async def api_cron(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "cron.list", None, ctx)
        if result.ok:
            return JSONResponse(
                result.payload
                if isinstance(result.payload, list)
                else {"jobs": result.payload or []}
            )
        return JSONResponse({"jobs": []})

    # ── WebSocket handler ────────────────────────────────────────────────────

    async def api_chat_history(request: Request) -> JSONResponse:
        """GET /api/chat/history?sessionKey=xxx — return chat transcript."""
        session_key = request.query_params.get("sessionKey", "agent:main:webchat:default")
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch(
            "_http", "chat.history", {"sessionKey": session_key}, ctx
        )
        if result.ok:
            return JSONResponse(result.payload or {"messages": []})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    async def ws_endpoint(ws: WebSocket) -> None:
        await handle_ws_connection(
            ws,
            config,
            dispatcher,
            session_manager,
            provider_selector=provider_selector,
            tool_registry=tool_registry,
            subscription_manager=subscription_manager,
            channel_manager=_resolve_channel_manager,
            usage_tracker=usage_tracker,
            usage_event_sink=usage_event_sink,
            meta_run_writer=meta_run_writer,
            skill_loader=skill_loader,
            cron_scheduler=cron_scheduler,
            turn_runner=turn_runner,
            task_runtime=task_runtime,
            flush_service=flush_service,
            heartbeat_service=heartbeat_service,
            heartbeat_loop=heartbeat_loop,
            agent_registry=agent_registry,
            diagnostics_state=diagnostics_state,
            provider_stats=provider_stats,
            memory_managers=memory_managers,
            memory_stores=memory_stores,
            memory_retrievers=memory_retrievers,
        )

    # ── Routes ───────────────────────────────────────────────────────────────

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/ready", ready, methods=["GET"]),
        Route("/readyz", ready, methods=["GET"]),
        Route("/api/config", api_config, methods=["GET"]),
        Route("/api/sessions", api_sessions, methods=["GET"]),
        Route("/api/chat", _same_origin(api_chat), methods=["POST"]),
        Route("/api/chat/history", api_chat_history, methods=["GET"]),
        Route("/api/agents", api_agents, methods=["GET"]),
        Route("/api/cron", api_cron, methods=["GET"]),
        Route("/api/system/status", api_system_status, methods=["GET"]),
        Route("/api/system/update", api_system_update, methods=["GET"]),
        Route("/api/system/shutdown", _same_origin(api_system_shutdown), methods=["POST"]),
        Route("/api/desktop/identity", _same_origin(api_desktop_identity), methods=["POST"]),
        Route("/api/desktop/shutdown", _same_origin(api_desktop_shutdown), methods=["POST"]),
        Route("/api/usage", api_usage, methods=["GET"]),
        Route("/api/channels/status", api_channels_status, methods=["GET"]),
        Route("/api/channels/logout", _same_origin(api_channels_logout), methods=["POST"]),
        Route("/api/channels/pairings", api_channel_pairings, methods=["GET"]),
        Route(
            "/api/channels/pairings/approve",
            _same_origin(api_channel_pairing_approve),
            methods=["POST"],
        ),
        Route(
            "/api/channels/pairings/revoke",
            _same_origin(api_channel_pairing_revoke),
            methods=["POST"],
        ),
        Route("/api/approvals", api_approvals, methods=["GET"]),
        Route("/api/approvals/settings", _same_origin(api_approvals_settings), methods=["POST"]),
        Route("/api/approvals/resolve", _same_origin(api_approvals_resolve), methods=["POST"]),
        Route("/api/elevated-mode", _same_origin(api_elevated_mode), methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]

    # ── Channel webhook routes (Slack, Feishu) ────────────────────────────
    if extra_routes:
        routes.extend(extra_routes)

    # ── Control UI routes ────────────────────────────────────────────────
    routes.extend(create_control_ui_routes(config))

    # ── Middleware ───────────────────────────────────────────────────────────

    middleware = [Middleware(ErrorHandlingMiddleware)]
    if config.cors.allowed_origins:
        # CORS headers are opt-in: the default empty list installs no CORS
        # middleware at all, so browsers refuse cross-origin reads. The Web UI
        # is same-origin and non-browser clients never need CORS.
        if "*" in config.cors.allowed_origins and config.cors.allow_credentials:
            log.warning(
                "gateway.cors_wildcard_with_credentials",
                detail=(
                    "cors.allowed_origins contains '*' with allow_credentials "
                    "enabled, which lets any website the browser visits read "
                    "authenticated gateway responses — list explicit origins "
                    "instead."
                ),
            )
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=config.cors.allowed_origins,
                allow_credentials=config.cors.allow_credentials,
                allow_methods=config.cors.allowed_methods,
                allow_headers=config.cors.allowed_headers,
            )
        )
    middleware.extend(
        [
            Middleware(RateLimitMiddleware, config=config),
            Middleware(SecurityHeadersMiddleware, path_prefix=config.control_ui.base_path),
            Middleware(AuthMiddleware, config=config),
        ]
    )

    app = Starlette(routes=routes, middleware=middleware, debug=config.debug)
    app.state.diagnostics_state = diagnostics_state

    # Bridge upload endpoint: self-hosted multipart sink that
    # returns an opaque file_uuid the chat.send validator can resolve.
    from opensquilla.gateway.uploads import (  # noqa: PLC0415 — local import keeps app.py boot light
        UploadStore,
        get_upload_store,
        register_upload_routes,
        set_upload_store,
    )

    # Back the store with a persistent marker directory so a staged upload lost
    # across a gateway restart resolves to the specific "lost in restart, please
    # re-upload" error instead of a generic "unknown uuid" (issue #468). Only
    # replace the default in-memory-only singleton; respect a test-injected store.
    _upload_store = get_upload_store()
    if getattr(_upload_store, "marker_dir", None) is None:
        from opensquilla.gateway.uploads import (  # noqa: PLC0415
            _DEFAULT_MAX_TOTAL_BYTES as _UPLOAD_STORE_DEFAULT_TOTAL,
        )
        from opensquilla.paths import media_root_from_config  # noqa: PLC0415

        _store_total_cap = getattr(config.attachments, "upload_store_max_total_bytes", None)
        if not isinstance(_store_total_cap, int) or _store_total_cap <= 0:
            if _store_total_cap is not None:
                log.warning(
                    "attachments.upload_store_max_total_bytes=%r is not a "
                    "positive integer; using the %d byte default (this RAM "
                    "cap can be raised but not disabled)",
                    _store_total_cap,
                    _UPLOAD_STORE_DEFAULT_TOTAL,
                )
            _store_total_cap = _UPLOAD_STORE_DEFAULT_TOTAL
        _upload_store = UploadStore(
            marker_dir=media_root_from_config(config) / "uploads",
            accept_opaque=bool(getattr(config.attachments, "accept_opaque", True)),
            max_total_bytes=_store_total_cap,
        )
        set_upload_store(_upload_store)
    register_upload_routes(app, config=config, store=_upload_store)
    from opensquilla.gateway.artifacts import register_artifact_routes  # noqa: PLC0415
    from opensquilla.gateway.attachments import register_attachment_routes  # noqa: PLC0415
    from opensquilla.gateway.audio_transcription import (  # noqa: PLC0415
        register_audio_transcription_routes,
    )

    register_attachment_routes(
        app,
        config=config,
        session_manager=session_manager,
    )
    register_artifact_routes(
        app,
        config=config,
        session_manager=session_manager,
    )
    register_audio_transcription_routes(app, config=config)
    from opensquilla.gateway.bundle_routes import register_bundle_routes  # noqa: PLC0415

    register_bundle_routes(app, config=config)

    return app
