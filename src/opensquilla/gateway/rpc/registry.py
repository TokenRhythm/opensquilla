"""RPC method registry and dispatcher.

Historically every gateway RPC handler registered against a module-level
``_dispatcher`` singleton. That singleton is now a
first-class :class:`RpcRegistry` living in this module so the set of
method names is discoverable, testable, and mockable without import
side-effects.

The legacy surface (``RpcDispatcher`` / ``get_dispatcher``) still resolves
from ``opensquilla.gateway.rpc`` — see this package's ``__init__.py`` — so every
``from opensquilla.gateway.rpc import ...`` caller keeps working.

Scope policy is owned by :mod:`opensquilla.gateway.scopes`. Registrations on
the module-level singleton are audited at import time via
:func:`validate_classification` once every release-surface ``rpc_*.py``
sibling has loaded; the registry is then locked so late imports cannot
silently grow the RPC surface.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from opensquilla.engine.runtime import TurnRunner

from opensquilla import __version__
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import (
    ERROR_METHOD_NOT_FOUND,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
    ResFrame,
    make_error_res,
    make_ok_res,
)
from opensquilla.gateway.scopes import (
    NODE_ROLE_METHODS,
    authorize_call,
    is_classified,
    operator_scope_satisfies,
    resolve_required_scope,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.storage import StorageBusyError

log = structlog.get_logger(__name__)

# Handler type: (params, context) -> payload or raises
RpcHandlerFn = Callable[[Any, "RpcContext"], Coroutine[Any, Any, Any]]


@dataclass
class RpcContext:
    """Per-request execution context passed to RPC handlers."""

    conn_id: str
    # Test-friendly default. Production call sites (``websocket._message_loop``,
    # ``app.create_rpc_context``, channel dispatch) always pass ``principal``
    # explicitly, so this fallback is only reached by unit tests that construct
    # a bare ``RpcContext``.
    principal: Principal = field(
        default_factory=lambda: Principal(
            role="operator",
            scopes=frozenset(["operator.admin"]),
            is_owner=True,
            authenticated=False,
        )
    )
    session_manager: Any = None
    config: Any = None
    start_time_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    provider_selector: Any = None  # ModelSelector instance (injected at boot)
    tool_registry: Any = None  # ToolRegistry instance (injected at boot)
    subscription_manager: Any = None  # SubscriptionManager for session-scoped events
    channel_manager: Any = None  # ChannelManager | None (injected at boot)
    usage_tracker: Any = None  # UsageTracker instance (injected at boot)
    usage_event_sink: Any = None  # Durable per-provider-call accounting sink.
    meta_run_writer: Any = None  # MetaRunWriter instance (injected at boot)
    skill_loader: Any = None  # SkillLoader instance (injected at boot)
    cron_scheduler: Any = None  # SchedulerEngine instance (injected at boot)
    turn_runner: TurnRunner | None = None  # TurnRunner instance (injected at boot)
    task_runtime: Any = None  # TaskRuntime instance (injected at boot)
    flush_service: Any = None  # SessionFlushService | None (injected at boot)
    heartbeat_service: Any = None  # Task-style heartbeat service (injected at boot)
    heartbeat_loop: Any = None  # Background heartbeat loop (injected at boot)
    agent_registry: Any = None  # AgentRegistry instance (injected at boot)
    diagnostics_state: Any = None  # DiagnosticsState instance (injected at boot)
    provider_stats: Any = None  # ProviderStatsStore instance (injected at boot)
    memory_managers: dict[str, Any] = field(default_factory=dict)
    memory_stores: dict[str, Any] = field(default_factory=dict)
    memory_retrievers: dict[str, Any] = field(default_factory=dict)
    originating_envelope: Any = None  # Channel RouteEnvelope for RPC side effects

    @property
    def role(self) -> str:
        return self.principal.role

    @property
    def scopes(self) -> list[str]:
        return list(self.principal.scopes)

    def has_scope(self, required: str) -> bool:
        """Namespace-bounded scope check.

        Delegates to :func:`operator_scope_satisfies` so that the same
        implication rules (``admin ⇒ operator.*``, ``write ⇒ read``,
        ``admin ⇒ node`` as a superuser pragma) are honored whether the
        call arrives via this helper or through the dispatcher's
        authorization path.
        """
        return operator_scope_satisfies(required, self.principal.scopes)


@dataclass
class RpcMethodEntry:
    name: str
    handler: RpcHandlerFn
    required_scope: str


class RpcUnavailableError(RuntimeError):
    """Raised when a method exists but its backing capability is not wired."""


class RpcHandlerError(Exception):
    """Structured error a handler can raise to carry a code + details payload.

    The dispatcher converts this into a ``ResFrame`` with
    :class:`ErrorShape` populated from the exception's ``code``, ``message``,
    and ``details`` attributes. Handlers use it when a raw exception would
    lose context the client needs — e.g. ``sessions.reset`` returning a
    :class:`FlushReceipt` alongside the error so the UI can render the
    failure mode.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        accepted: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.accepted = accepted


class ScopeDriftError(RuntimeError):
    """Raised when a registered scope disagrees with the central table."""


class RpcRegistry:
    """Canonical registry + dispatcher for gateway RPC method names.

    Thin adapter over the historical ``RpcDispatcher`` shape — the two names
    are aliased in the module's public export (:data:`RpcDispatcher`) so that
    every caller written against the old class keeps working verbatim.

    The method surface is intentionally tiny and stable:

    * :meth:`register` / :meth:`method` add a handler. ``scope`` is required;
      there is no silent default.
    * :meth:`unregister` removes one.
    * :meth:`methods` / :meth:`list_methods` enumerate registered names.
    * :meth:`dispatch` routes a request to a registered handler with scope
      enforcement.
    """

    def __init__(self) -> None:
        self._methods: dict[str, RpcMethodEntry] = {}
        self._locked = False

    def register(self, name: str, handler: RpcHandlerFn, scope: str) -> None:
        """Register ``handler`` for ``name``.

        ``scope`` must be supplied explicitly; no default is applied. The
        process-wide registry is locked after boot-time classification so
        retained/deprecated modules cannot register handlers through a late
        import.
        """
        if self._locked:
            raise ScopeDriftError(
                f"RPC registry is locked; refusing late registration for {name!r}"
            )
        self._methods[name] = RpcMethodEntry(name=name, handler=handler, required_scope=scope)

    def lock_registration(self) -> None:
        """Prevent additional methods from being registered after boot."""
        self._locked = True

    def method(self, name: str, scope: str) -> Callable:
        """Decorator form of :meth:`register`. ``scope`` is required."""

        def decorator(fn: RpcHandlerFn) -> RpcHandlerFn:
            self.register(name, fn, scope)
            return fn

        return decorator

    def unregister(self, name: str) -> bool:
        """Remove a method by name. Returns True if it existed."""
        return self._methods.pop(name, None) is not None

    def methods(self) -> list[str]:
        """Return the list of registered method names (sorted, stable)."""
        return sorted(self._methods.keys())

    # Backwards-compatible alias retained for call-sites that still use the
    # older name. New code should prefer :meth:`methods`.
    def list_methods(self) -> list[str]:
        return self.methods()

    def get_entry(self, name: str) -> RpcMethodEntry | None:
        """Return the registered entry for ``name`` or None."""
        return self._methods.get(name)

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: RpcContext) -> ResFrame:
        entry = self._methods.get(method)
        if entry is None:
            return make_error_res(req_id, ERROR_METHOD_NOT_FOUND, f"Method not found: {method}")

        allowed, missing = authorize_call(
            method,
            entry.required_scope,
            ctx.principal.role,
            ctx.principal.scopes,
        )
        if not allowed:
            detail = f": missing {missing}" if missing else ""
            return make_error_res(
                req_id, ERROR_UNAUTHORIZED, f"Insufficient scope for method: {method}{detail}"
            )

        try:
            result = await entry.handler(params, ctx)
            return make_ok_res(req_id, result)
        except RpcHandlerError as exc:
            return make_error_res(
                req_id,
                exc.code,
                exc.message,
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
                accepted=exc.accepted,
                details=exc.details,
            )
        except RpcUnavailableError as exc:
            return make_error_res(req_id, ERROR_UNAVAILABLE, str(exc), retryable=True)
        except StorageBusyError as exc:
            return make_error_res(
                req_id,
                "STORAGE_BUSY",
                "Session storage is temporarily busy. Retry this operation.",
                retryable=True,
                retry_after_ms=exc.retry_after_ms,
                details={
                    "operation": exc.operation,
                    "waited_ms": exc.waited_ms,
                },
            )
        except ValueError as exc:
            return make_error_res(req_id, "INVALID_REQUEST", str(exc))
        except KeyError as exc:
            return make_error_res(req_id, "NOT_FOUND", str(exc))
        except Exception as exc:
            log.error(
                "rpc.dispatch_failed",
                method=method,
                error=str(exc),
                exc_info=True,
            )
            return make_error_res(req_id, "INTERNAL_ERROR", str(exc))


# Backwards-compatible alias: the historical class name remains importable.
RpcDispatcher = RpcRegistry


# ---------------------------------------------------------------------------
# Module-level singleton + built-in handlers
# ---------------------------------------------------------------------------

_registry = RpcRegistry()


async def _health(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return {"status": "ok", "uptime_ms": int(time.time() * 1000) - ctx.start_time_ms}


async def _status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.gateway.boot import _boot_time_ms

    now = int(time.time() * 1000)
    uptime = now - _boot_time_ms if _boot_time_ms > 0 else 0

    provider_name = None
    if ctx.provider_selector is not None and getattr(
        ctx.provider_selector, "is_configured", True
    ):
        # Configured provider id (e.g. "openrouter"), not the OpenAI-compatible
        # backend class physically serving it. See app.api_system_status.
        provider_name = getattr(ctx.provider_selector, "active_provider_id", None)
        if not provider_name:
            try:
                p = ctx.provider_selector.resolve()
                provider_name = getattr(p, "provider_name", None)
            except Exception:
                pass

    active_sessions = 0
    if ctx.session_manager is not None:
        storage = get_session_storage(ctx.session_manager)
        if storage is not None:
            try:
                sessions = await storage.list_sessions(limit=1000)
                active_sessions = len(sessions)
            except Exception:
                pass

    return {
        "status": "running",
        "version": __version__,
        "uptime_ms": uptime,
        "provider": provider_name,
        "active_sessions": active_sessions,
    }


async def _config_get(params: Any, ctx: RpcContext) -> Any:
    if ctx.config is None:
        return {}
    cfg_dict = (
        ctx.config.to_public_dict()
        if hasattr(ctx.config, "to_public_dict")
        else ctx.config.model_dump()
        if hasattr(ctx.config, "model_dump")
        else {}
    )
    if isinstance(params, dict):
        path = params.get("path")
        if path:
            parts = path.split(".")
            val: Any = cfg_dict
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    val = None
                    break
            return val
    return cfg_dict


async def _sessions_get(params: Any, ctx: RpcContext) -> dict[str, Any]:
    if ctx.session_manager is None:
        raise KeyError("No session manager available")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    session = await storage.get_session(params["key"])
    if session is None:
        raise KeyError(f"Session not found: {params['key']}")
    return {
        "session_key": session.session_key,
        "session_id": session.session_id,
        "status": session.status,
        "agent_id": session.agent_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


async def _gateway_identity_get(params: Any, ctx: RpcContext) -> dict[str, Any]:
    import socket

    return {"machine_name": socket.gethostname(), "version": __version__, "conn_id": ctx.conn_id}


async def _last_heartbeat(params: Any, ctx: RpcContext) -> dict[str, Any]:
    heartbeat_service = getattr(ctx, "heartbeat_service", None)
    if heartbeat_service is not None:
        status = getattr(heartbeat_service, "last_run_status", None)
        if status:
            return dict(status)
    return {"ts": int(time.time() * 1000)}


# Register all built-in methods against the singleton.
_registry.register("health", _health, "operator.read")
_registry.register("status", _status, "operator.read")
_registry.register("config.get", _config_get, "operator.read")
_registry.register("sessions.get", _sessions_get, "operator.read")
_registry.register("gateway.identity.get", _gateway_identity_get, "operator.read")
_registry.register("last-heartbeat", _last_heartbeat, "operator.read")


def get_registry() -> RpcRegistry:
    """Return the process-wide :class:`RpcRegistry` singleton."""

    return _registry


def get_dispatcher() -> RpcRegistry:
    """Backwards-compatible alias for :func:`get_registry`.

    The historical public name remains supported so every existing
    ``from opensquilla.gateway.rpc import get_dispatcher`` caller keeps working
    without touching sibling submodules. Sub-module imports that trigger
    handler registration live in ``opensquilla.gateway.rpc.__init__`` so that
    this registry module stays free of circular-import hazards.
    """

    return _registry


def validate_classification(registry: RpcRegistry | None = None) -> None:
    """Audit the registry against the central scope table.

    Called at the end of ``opensquilla.gateway.rpc.__init__`` once every
    sibling ``rpc_*.py`` module has registered its handlers. Raises
    :class:`ScopeDriftError` on the first violation so startup fails
    loudly rather than serving a method under the wrong policy.

    Three violations are checked per registered method:

    1. The name has no classification (neither an explicit entry in
       ``METHOD_SCOPES``, a match in ``ADMIN_METHOD_PREFIXES``, nor
       membership in ``NODE_ROLE_METHODS``).
    2. The method is a node-role method but its recorded scope is not
       the ``node`` scope.
    3. The method is classified as operator-scope but the recorded
       scope disagrees with :func:`resolve_required_scope`.
    """
    target = registry if registry is not None else _registry
    for name in target.methods():
        entry = target.get_entry(name)
        if entry is None:  # defensive; methods() returns live names
            continue
        declared = entry.required_scope

        if name in NODE_ROLE_METHODS:
            if declared != "node":
                raise ScopeDriftError(
                    f"{name!r} is a node-role method but was registered with "
                    f"scope={declared!r}; expected 'node'"
                )
            continue

        if not is_classified(name):
            raise ScopeDriftError(
                f"{name!r} is registered but has no entry in METHOD_SCOPES "
                f"and does not match any admin prefix — classify it in "
                f"opensquilla/gateway/scopes.py"
            )

        expected = resolve_required_scope(name)
        if expected is None:  # should be unreachable given is_classified
            continue
        if declared != expected:
            raise ScopeDriftError(
                f"{name!r} registered with scope={declared!r} disagrees with "
                f"central table expecting {expected!r}"
            )
    if registry is None:
        target.lock_registration()
