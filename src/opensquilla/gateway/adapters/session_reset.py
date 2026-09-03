"""Gateway boundary and concrete Ports for session reset."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast

import structlog

from opensquilla.agent_ids import normalize_agent_id
from opensquilla.application.session_reset import (
    GoalLeasePort,
    PromptCacheInvalidationPort,
    ResetSession,
    SessionEpochPort,
    SessionQuiescencePort,
    SessionResetApplication,
    SessionResetFlushExecutionError,
    SessionResetFlushReceipt,
    SessionResetFlushSafetyError,
    SessionResetFlushUnavailableError,
    SessionResetForcePermissionError,
    SessionResetLockPort,
    SessionResetMemoryAssessment,
    SessionResetMemoryPort,
    SessionResetNotFoundError,
    SessionResetResult,
    SessionResetRotation,
    SessionResetSnapshot,
    SessionResetStorePort,
    SessionResetUnavailableError,
    SessionResetUsagePort,
)
from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.session_services import (
    get_session_lock,
    get_session_storage,
    set_session_epoch,
)
from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope
from opensquilla.memory.session_flush import FlushReceipt
from opensquilla.session.compaction_lifecycle import (
    compaction_memory_status,
    flush_receipt_status_for_compaction,
    flush_receipt_to_dict,
    flush_trigger_enabled,
)
from opensquilla.session.models import SessionIntent

log = structlog.get_logger(__name__)

type SessionKeyReader = Callable[[dict[str, Any] | None], str]
type RuntimeCanceller = Callable[..., Awaitable[int]]
type CheckpointCoverage = Callable[
    [Any, str, str | None, list[Any]],
    Awaitable[bool],
]
type FlushCorrelationBuilder = Callable[[RpcContext, object], tuple[str, object | None]]
type SessionEventEmitter = Callable[
    [RpcContext, str, str, dict[str, Any]],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class GatewaySessionResetCallbacks:
    cancel_runtime: RuntimeCanceller
    checkpoint_covers: CheckpointCoverage
    build_flush_correlation: FlushCorrelationBuilder
    emit_session_event: SessionEventEmitter


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
    SessionResetMemoryPort,
    SessionResetUsagePort,
    GoalLeasePort,
    SessionEpochPort,
    PromptCacheInvalidationPort,
):
    """Request-scoped production Ports; the complete RpcContext terminates here."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionResetCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks
        self._manager = context.session_manager
        self._storage = get_session_storage(self._manager)

    async def quiesce(self, session_key: str) -> None:
        task_runtime = getattr(self._context, "task_runtime", None)
        if task_runtime is not None:
            await self._quiesce_task_runtime(task_runtime, session_key)

        active = get_agent_task_registry().get(session_key)
        if active is None or active.done():
            return
        get_agent_task_registry().cancel(session_key)
        try:
            await asyncio.wait_for(active, timeout=2.0)
        except TimeoutError:
            log.warning("sessions.reset.drain_timeout", session_key=session_key)
        except asyncio.CancelledError:
            log.debug("sessions.reset.drain_cancelled", session_key=session_key)
        except Exception as exc:
            log.warning(
                "sessions.reset.drain_failed",
                session_key=session_key,
                error=str(exc),
            )

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

        await self._callbacks.cancel_runtime(
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
        except Exception:
            log.warning(
                "sessions.reset.task_runtime_drain_failed",
                session_key=session_key,
            )

    @asynccontextmanager
    async def hold(self, session_key: str) -> AsyncIterator[None]:
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
        transcript = await self._manager.get_transcript(session_key)
        return SessionResetSnapshot(
            session_key=session_key,
            session_id=str(session.session_id),
            agent_id=normalize_agent_id(getattr(session, "agent_id", None) or "main"),
            epoch=int(getattr(session, "epoch", 0) or 0),
            transcript=tuple(transcript),
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

    @property
    def flush_enabled(self) -> bool:
        return flush_trigger_enabled(self._context.config, "session_reset")

    @property
    def flush_available(self) -> bool:
        return getattr(self._context, "flush_service", None) is not None

    async def checkpoint_covers(self, snapshot: SessionResetSnapshot) -> bool:
        return await self._callbacks.checkpoint_covers(
            self._storage,
            snapshot.session_key,
            snapshot.session_id,
            list(snapshot.transcript),
        )

    def skipped_receipt(self) -> FlushReceipt:
        return FlushReceipt(
            mode="skipped",
            flushed_paths=[],
            slug=None,
            message_count=0,
            duration_ms=0,
            raw_reason=None,
            error=None,
        )

    def failed_receipt(self, *, message_count: int, error: str) -> FlushReceipt:
        return FlushReceipt(
            mode="error",
            flushed_paths=[],
            slug=None,
            message_count=message_count,
            duration_ms=0,
            raw_reason=None,
            error=error,
            result_status="archive_failed",
        )

    async def flush(self, snapshot: SessionResetSnapshot) -> SessionResetFlushReceipt:
        flush_service = self._context.flush_service
        if flush_service is None:
            raise RuntimeError("session flush service is unavailable")
        turn_id, correlation = self._callbacks.build_flush_correlation(
            self._context,
            snapshot.session_id,
        )
        kwargs: dict[str, Any] = {
            "agent_id": snapshot.agent_id,
            "timeout": 30.0,
            "message_window": 0,
            "segment_mode": "auto",
            "raw_capture_policy": "required",
        }
        if _accepts_keyword_arg(flush_service.execute, "turn_id"):
            kwargs["turn_id"] = turn_id
        if correlation is not None and _accepts_keyword_arg(
            flush_service.execute,
            "provider_request_correlation",
        ):
            kwargs["provider_request_correlation"] = correlation
        receipt = await flush_service.execute(
            list(snapshot.transcript),
            snapshot.session_key,
            **kwargs,
        )
        return cast(SessionResetFlushReceipt, receipt)

    async def assess(
        self,
        snapshot: SessionResetSnapshot,
        receipt: SessionResetFlushReceipt,
    ) -> SessionResetMemoryAssessment:
        durable_receipt_safe = await self.checkpoint_covers(snapshot)
        memory_status = compaction_memory_status(
            receipt,
            deterministic_receipt_safe=durable_receipt_safe,
            required=True,
        )
        return SessionResetMemoryAssessment(
            allows_reset=memory_status.allows_destructive_compaction,
            flush_status=flush_receipt_status_for_compaction(
                receipt,
                self._context.config,
            ),
            safety_status=memory_status.safety_status,
            semantic_status=memory_status.semantic_status,
        )

    @asynccontextmanager
    async def account_memory_flush(self, session_key: str) -> AsyncIterator[None]:
        scope = await build_session_usage_scope(
            getattr(self._context, "usage_event_sink", None),
            self._manager,
            session_key,
            run_kind="memory_flush",
        )
        with bind_usage_accounting_scope(scope):
            yield

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
            await self._callbacks.emit_session_event(
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
        *,
        require_key: SessionKeyReader,
    ) -> None:
        self._context = context
        self._application = application
        self._require_key = require_key

    async def reset(self, params: dict[str, Any] | None) -> dict[str, Any]:
        key = self._require_key(params)
        force = bool((params or {}).get("force", False))
        try:
            result = await self._application.reset(
                ResetSession(
                    session_key=key,
                    force=force,
                    force_authorized=self._context.has_scope("operator.admin"),
                )
            )
        except SessionResetFlushUnavailableError as exc:
            raise RpcHandlerError(
                code="flush_unavailable",
                message=(
                    "Reset aborted: flush service is unavailable and the "
                    "transcript is non-empty. Re-run with force=true (admin) "
                    "to discard without backup."
                ),
                details={
                    "key": exc.session_key,
                    "session_id": exc.session_id,
                    "reason": "flush_service_disabled",
                    "message_count": exc.message_count,
                },
            ) from exc
        except SessionResetForcePermissionError as exc:
            raise RpcHandlerError(
                code="permission_denied",
                message="force=true on sessions.reset requires operator.admin scope.",
                details={
                    "key": exc.session_key,
                    "session_id": exc.session_id,
                },
            ) from exc
        except SessionResetFlushExecutionError as exc:
            raise RpcHandlerError(
                code="flush_disk_error",
                message=f"Reset aborted: flush failed ({exc.receipt.error})",
                details={
                    "flush_receipt": flush_receipt_to_dict(exc.receipt),
                    "key": exc.snapshot.session_key,
                    "session_id": exc.snapshot.session_id,
                },
            ) from exc
        except SessionResetFlushSafetyError as exc:
            raise RpcHandlerError(
                code="flush_disk_error",
                message=(
                    f"Reset aborted: flush status {exc.assessment.flush_status!r} "
                    "is not sufficient for destructive reset."
                ),
                details={
                    "flush_receipt": flush_receipt_to_dict(exc.receipt),
                    "key": exc.snapshot.session_key,
                    "session_id": exc.snapshot.session_id,
                    "reason": "destructive_reset_requires_safe_flush",
                    "flush_receipt_status": exc.assessment.flush_status,
                    "memory_safety_status": exc.assessment.safety_status,
                    "semantic_memory_status": exc.assessment.semantic_status,
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
        if result.flush_receipt is not None:
            payload["flush_receipt"] = flush_receipt_to_dict(result.flush_receipt)
        return payload


def build_gateway_session_reset_adapter(
    context: RpcContext,
    callbacks: GatewaySessionResetCallbacks,
    *,
    require_key: SessionKeyReader,
) -> GatewaySessionResetAdapter:
    ports = GatewaySessionResetPorts(context, callbacks)
    application = SessionResetApplication(
        quiescence=ports,
        lock=ports,
        store=ports,
        memory=ports,
        usage=ports,
        goal_leases=ports,
        epochs=ports,
        prompt_cache=ports,
    )
    return GatewaySessionResetAdapter(
        context,
        application,
        require_key=require_key,
    )


__all__ = [
    "GatewaySessionResetAdapter",
    "GatewaySessionResetCallbacks",
    "GatewaySessionResetPorts",
    "SessionResetUseCase",
    "build_gateway_session_reset_adapter",
]
