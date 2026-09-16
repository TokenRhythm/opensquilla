"""Gateway boundary and concrete Ports for session reset."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

import structlog

from opensquilla.application.session_reset import (
    GoalLeasePort,
    PromptCacheInvalidationPort,
    ResetSession,
    SessionEpochPort,
    SessionQuiescencePort,
    SessionResetApplication,
    SessionResetForcePermissionError,
    SessionResetLockPort,
    SessionResetNotFoundError,
    SessionResetResult,
    SessionResetRotation,
    SessionResetSnapshot,
    SessionResetStorePort,
    SessionResetUnavailableError,
)
from opensquilla.engine.steps.router_decision_record import (
    drain_pending_flushes_for_sessions,
)
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.session_event_publisher import emit_session_event
from opensquilla.gateway.session_maintenance_runtime import (
    cancel_task_runtime,
)
from opensquilla.gateway.session_services import (
    get_session_lock,
    get_session_storage,
    set_session_epoch,
)
from opensquilla.gateway.subagent_announce import quiesce_background_completion_sessions
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.session.models import SessionIntent

log = structlog.get_logger(__name__)


def _require_session_key(params: dict[str, Any] | None) -> str:
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    key = params["key"]
    if not isinstance(key, str):
        raise ValueError("params.key must be a string")
    return canonicalize_session_key(key)


def _accepts_keyword_arg(func: Callable[..., object], name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


_RESET_RUNTIME_SETTLE_SECONDS = 0.25
_RESET_RUNTIME_CANCEL_DRAIN_SECONDS = 2.0
_RESET_ACTIVE_TASK_STATUSES = frozenset({"queued", "running"})


def _task_status_value(status: object) -> str:
    return str(getattr(status, "value", status) or "")


class GatewaySessionResetPorts(
    SessionQuiescencePort,
    SessionResetLockPort,
    SessionResetStorePort,
    GoalLeasePort,
    SessionEpochPort,
    PromptCacheInvalidationPort,
):
    """Request-scoped production Ports; the complete RpcContext terminates here."""

    def __init__(
        self,
        context: RpcContext,
    ) -> None:
        self._context = context
        self._manager = context.session_manager
        self._storage = get_session_storage(self._manager)
        self._locks_held_by_quiesce: set[str] = set()

    def _session_reset_busy(
        self, session_key: str, phase: str, exc: BaseException
    ) -> RpcHandlerError:
        log.warning(
            "sessions.reset.quiesce_failed",
            session_key=session_key,
            phase=phase,
            error_type=type(exc).__name__,
        )
        return RpcHandlerError(
            code="STORAGE_BUSY",
            message=(
                "Reset aborted because session work could not be fully drained. "
                "Wait for the active turn to settle and retry."
            ),
            details={"key": session_key, "phase": phase},
            retryable=True,
            retry_after_ms=250,
        )

    @asynccontextmanager
    async def quiesce(self, session_key: str) -> AsyncIterator[None]:
        task_runtime = getattr(self._context, "task_runtime", None)
        if task_runtime is not None:
            try:
                async with asyncio.timeout(
                    _RESET_RUNTIME_SETTLE_SECONDS + _RESET_RUNTIME_CANCEL_DRAIN_SECONDS
                ):
                    await self._quiesce_task_runtime(task_runtime, session_key)
            except Exception as exc:  # noqa: BLE001 - reset must fail closed.
                raise self._session_reset_busy(session_key, "task_runtime_drain", exc) from exc

        session_keys = (session_key,)
        turn_runner = getattr(self._context, "turn_runner", None)
        lock = get_session_lock(turn_runner, session_key)
        async with contextlib.AsyncExitStack() as fences:
            try:
                async with asyncio.timeout(_RESET_RUNTIME_CANCEL_DRAIN_SECONDS):
                    await fences.enter_async_context(
                        quiesce_background_completion_sessions(session_keys)
                    )

                    quiesce_runtime = getattr(task_runtime, "quiesce_sessions", None)
                    if callable(quiesce_runtime):
                        quiesce_kwargs: dict[str, str] = {}
                        if all(
                            _accepts_keyword_arg(quiesce_runtime, name)
                            for name in ("cancel_source", "cancel_reason")
                        ):
                            quiesce_kwargs = {
                                "cancel_source": "sessions_reset",
                                "cancel_reason": "session_reset",
                            }
                        await fences.enter_async_context(
                            quiesce_runtime(session_keys, **quiesce_kwargs)
                        )

                    await fences.enter_async_context(
                        get_agent_task_registry().quiesce_sessions(session_keys)
                    )

                    if lock is not None:
                        await fences.enter_async_context(lock)
                        self._locks_held_by_quiesce.add(session_key)

                    await drain_pending_flushes_for_sessions(session_keys)
            except Exception as exc:  # noqa: BLE001 - rotation must not follow partial drain.
                raise self._session_reset_busy(session_key, "writer_quiesce", exc) from exc
            try:
                yield
            finally:
                self._locks_held_by_quiesce.discard(session_key)

    async def _quiesce_task_runtime(self, task_runtime: Any, session_key: str) -> None:
        """Let just-finished turns settle, then cancel and drain live work."""
        has_runtime_listing = hasattr(task_runtime, "list") and hasattr(
            task_runtime,
            "wait",
        )
        if has_runtime_listing:
            try:
                rows = await task_runtime.list(session_key=session_key)
                for row in rows:
                    if _task_status_value(getattr(row, "status", None)) != "running":
                        continue
                    try:
                        await asyncio.wait_for(
                            task_runtime.wait(row.task_id),
                            timeout=_RESET_RUNTIME_SETTLE_SECONDS,
                        )
                    except TimeoutError:
                        pass
            except Exception:
                log.warning(
                    "sessions.reset.task_runtime_settle_failed",
                    session_key=session_key,
                )

        await cancel_task_runtime(
            task_runtime,
            session_key=session_key,
            source="sessions_reset",
            reason="session_reset",
        )
        if not has_runtime_listing:
            return

        try:
            rows = await task_runtime.list(session_key=session_key)
            for row in rows:
                if (
                    _task_status_value(getattr(row, "status", None))
                    not in _RESET_ACTIVE_TASK_STATUSES
                ):
                    continue
                await asyncio.wait_for(
                    task_runtime.wait(row.task_id),
                    timeout=_RESET_RUNTIME_CANCEL_DRAIN_SECONDS,
                )
        except TimeoutError:
            log.warning(
                "sessions.reset.task_runtime_drain_timeout",
                session_key=session_key,
            )
            raise
        except Exception:
            log.warning(
                "sessions.reset.task_runtime_drain_failed",
                session_key=session_key,
            )
            raise

    @asynccontextmanager
    async def hold(self, session_key: str) -> AsyncIterator[None]:
        if session_key in self._locks_held_by_quiesce:
            yield
            return
        lock = get_session_lock(self._context.turn_runner, session_key)
        if lock is None:
            yield
            return
        async with lock:
            yield

    @property
    def storage_available(self) -> bool:
        return self._manager is not None and self._storage is not None

    async def load(self, session_key: str) -> SessionResetSnapshot | None:
        if self._manager is None or self._storage is None:
            return None
        session = await self._storage.get_session(session_key)
        if session is None:
            return None
        return SessionResetSnapshot(
            session_key=session_key,
            session_id=str(session.session_id),
            epoch=int(getattr(session, "epoch", 0) or 0),
        )

    async def rotate(self, session_key: str) -> SessionResetRotation:
        if self._manager is None:
            raise RuntimeError("session manager became unavailable during reset")
        updated, rotated = await self._manager.apply_intent(
            session_key,
            SessionIntent.RESET_SAME_KEY,
        )
        return SessionResetRotation(
            session_id=str(updated.session_id),
            rotated=bool(rotated),
        )

    async def ensure_durable_epoch(self, session_key: str, previous_epoch: int) -> int:
        increment = getattr(self._storage, "increment_epoch", None)
        if not callable(increment):
            return 0
        new_epoch = previous_epoch
        get_session = getattr(self._storage, "get_session", None)
        if callable(get_session):
            try:
                current = await get_session(session_key)
                new_epoch = int(getattr(current, "epoch", previous_epoch) or 0)
            except Exception:
                new_epoch = previous_epoch
        try:
            if new_epoch <= previous_epoch:
                new_epoch = int(await increment(session_key))
        except Exception:
            log.warning(
                "sessions.reset.epoch_increment_failed",
                session_key=session_key,
            )
            return 0
        return new_epoch

    def revoke(self, session_key: str) -> None:
        goal_service = getattr(
            getattr(self._context, "task_runtime", None),
            "goal_service",
            None,
        )
        revoke = getattr(goal_service, "revoke_session", None)
        if callable(revoke):
            revoke(session_key)

    def update_cache(self, session_key: str, epoch: int) -> None:
        set_session_epoch(self._manager, session_key, epoch)

    async def publish(self, session_key: str, epoch: int) -> None:
        try:
            await emit_session_event(
                self._context,
                session_key,
                "session.epoch_changed",
                {"key": session_key, "epoch": epoch},
            )
        except Exception:
            log.warning(
                "sessions.reset.epoch_emit_failed",
                session_key=session_key,
                new_epoch=epoch,
            )

    async def invalidate(self, session_key: str) -> None:
        keepalive = getattr(self._context, "prompt_cache_keepalive_service", None)
        if keepalive is not None:
            await keepalive.invalidate(session_key)


class SessionResetUseCase(Protocol):
    async def reset(self, command: ResetSession) -> SessionResetResult: ...


class GatewaySessionResetAdapter:
    """Translate the v4 reset request and project its domain result."""

    def __init__(
        self,
        context: RpcContext,
        application: SessionResetUseCase,
    ) -> None:
        self._context = context
        self._application = application

    async def reset(self, params: dict[str, Any] | None) -> dict[str, Any]:
        key = _require_session_key(params)
        force = bool((params or {}).get("force", False))
        try:
            result = await self._application.reset(
                ResetSession(
                    session_key=key,
                    force=force,
                    force_authorized=self._context.has_scope("operator.admin"),
                )
            )
        except SessionResetForcePermissionError as exc:
            raise RpcHandlerError(
                code="permission_denied",
                message="force=true on sessions.reset requires operator.admin scope.",
                details={
                    "key": exc.session_key,
                    "session_id": exc.session_id,
                },
            ) from exc
        except SessionResetUnavailableError as exc:
            raise KeyError("No session storage available") from exc
        except SessionResetNotFoundError as exc:
            raise KeyError(f"Session not found: {exc.session_key}") from exc
        payload: dict[str, Any] = {
            "key": result.session_key,
            "reset": True,
            "rotated": result.rotated,
            "previous_session_id": result.previous_session_id,
            "session_id": result.session_id,
            "epoch": result.epoch,
        }
        return payload


def build_gateway_session_reset_adapter(
    context: RpcContext,
) -> GatewaySessionResetAdapter:
    ports = GatewaySessionResetPorts(context)
    application = SessionResetApplication(
        quiescence=ports,
        lock=ports,
        store=ports,
        goal_leases=ports,
        epochs=ports,
        prompt_cache=ports,
    )
    return GatewaySessionResetAdapter(
        context,
        application,
    )


__all__ = [
    "GatewaySessionResetAdapter",
    "GatewaySessionResetPorts",
    "SessionResetUseCase",
    "build_gateway_session_reset_adapter",
]
