"""Gateway boundary and concrete Ports for manual session compaction."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast

import structlog

from opensquilla.agent_ids import normalize_agent_id
from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionDeadlineError,
    SessionCompactionEvent,
    SessionCompactionExecutionResult,
    SessionCompactionExecutorPort,
    SessionCompactionFlushSafetyError,
    SessionCompactionLifecyclePort,
    SessionCompactionLock,
    SessionCompactionLockPort,
    SessionCompactionMemoryAssessment,
    SessionCompactionMemoryPort,
    SessionCompactionMemoryResult,
    SessionCompactionMilestone,
    SessionCompactionNotFoundError,
    SessionCompactionOwnershipPort,
    SessionCompactionPhaseTimeoutError,
    SessionCompactionPlan,
    SessionCompactionPlanningPort,
    SessionCompactionResult,
    SessionCompactionSession,
    SessionCompactionTiming,
    SessionCompactionUnavailableError,
    SessionCompactionUsagePort,
    SessionMaintenance,
)
from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
from opensquilla.gateway.compaction_target import (
    GatewayConsumerBudget,
    build_gateway_consumer_admission,
    effective_session_model,
    limit_gateway_consumer_budget,
    resolve_gateway_compaction_target,
    resolve_gateway_consumer_budget,
)
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
)
from opensquilla.session.compaction import (
    CompactionConfig,
    arm_compaction_deadline,
    await_compaction_phase,
    build_compaction_config_from_provider,
    call_compact_with_optional_config,
)
from opensquilla.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_PERSISTED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    CompactionTimeoutError,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    compaction_memory_status,
    flush_receipt_is_successful_flush,
    flush_receipt_status_for_compaction,
    flush_receipt_to_dict,
    flush_trigger_enabled,
    new_compaction_id,
    pre_compaction_flush_requires_safe_receipt,
)

log = structlog.get_logger(__name__)

_MILESTONE_TO_RUNTIME_EVENT = {
    SessionCompactionMilestone.TRIGGERED: COMPACTION_TRIGGERED_EVENT,
    SessionCompactionMilestone.CHUNK_SUMMARIZED: COMPACTION_CHUNK_SUMMARIZED_EVENT,
    SessionCompactionMilestone.SUMMARY_VERIFIED: COMPACTION_SUMMARY_VERIFIED_EVENT,
    SessionCompactionMilestone.PERSISTED: COMPACTION_PERSISTED_EVENT,
}

type SessionKeyReader = Callable[[dict[str, Any] | None], str]
type CheckpointCoverage = Callable[
    [Any, str, str | None, list[Any]],
    Awaitable[bool],
]
type EventPreparer = Callable[
    [RpcContext, str, str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]
type EventBuffer = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
type EventSender = Callable[
    [RpcContext, str, str, dict[str, Any]],
    Awaitable[None],
]
type TerminalStatusReader = Callable[[str], str | None]
type BackgroundRegistrar = Callable[[str, str, asyncio.Task[Any]], None]


class CompactionNotifier(Protocol):
    def __call__(
        self,
        session_key: str,
        *,
        notify_listeners: object = True,
        track_current_task: object = True,
        **payload: Any,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class GatewaySessionMaintenanceCallbacks:
    checkpoint_covers: CheckpointCoverage
    prepare_event: EventPreparer
    buffer_event: EventBuffer
    send_event: EventSender
    notify_compaction: CompactionNotifier
    terminal_status: TerminalStatusReader
    register_background: BackgroundRegistrar


@dataclass(frozen=True, slots=True)
class _GatewayCompactionPlan:
    budget: GatewayConsumerBudget
    config: CompactionConfig
    compaction_correlation: ProviderRequestCorrelation | None
    flush_correlation: ProviderRequestCorrelation | None


@dataclass(frozen=True, slots=True)
class _GatewayBufferedCompactionEvent:
    session_key: str
    payload: dict[str, Any]


_manual_compaction_tasks: set[asyncio.Task[Any]] = set()


def _accepts_keyword_arg(func: Callable[..., object], name: str) -> bool:
    side_effect = getattr(func, "side_effect", None)
    if callable(side_effect):
        func = side_effect
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


class GatewaySessionMaintenancePorts(
    SessionCompactionPlanningPort,
    SessionCompactionLockPort,
    SessionCompactionMemoryPort,
    SessionCompactionExecutorPort,
    SessionCompactionLifecyclePort,
    SessionCompactionOwnershipPort,
    SessionCompactionUsagePort,
):
    """Request-scoped runtime Ports; the complete ``RpcContext`` terminates here."""

    _EVENT_NAME = "session.event.compaction"

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionMaintenanceCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks
        self._manager = context.session_manager
        self._storage = get_session_storage(self._manager)

    def timing(self) -> SessionCompactionTiming:
        settings = getattr(getattr(self._context, "config", None), "compaction", None)
        try:
            total = float(getattr(settings, "total_timeout_seconds", 120.0))
        except (TypeError, ValueError):
            total = 120.0
        if total <= 0:
            total = 120.0
        try:
            heartbeat = float(getattr(settings, "heartbeat_interval_seconds", 15.0))
        except (TypeError, ValueError):
            heartbeat = 15.0
        return SessionCompactionTiming(
            total_timeout_seconds=total,
            heartbeat_interval_seconds=max(0.1, heartbeat),
        )

    def default_context_window_tokens(self) -> int:
        raw = getattr(self._context.config, "context_budget_tokens", 100_000)
        if isinstance(raw, bool):
            raise ValueError("contextWindowTokens must be a positive integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "contextWindowTokens must be a positive integer"
            ) from exc
        if value <= 0:
            raise ValueError("contextWindowTokens must be a positive integer")
        return value

    async def load_session(self, session_key: str) -> SessionCompactionSession | None:
        if self._manager is None:
            raise SessionCompactionUnavailableError
        session: object | None = None
        if self._storage is not None:
            session = await self._storage.get_session(session_key)
        else:
            getter = getattr(self._manager, "get_session", None)
            if callable(getter):
                session = await getter(session_key)
        if session is None:
            return None
        session_id = getattr(session, "session_id", None)
        return SessionCompactionSession(
            session_id=(session_id if isinstance(session_id, str) and session_id else None),
            agent_id=normalize_agent_id(getattr(session, "agent_id", None) or "main"),
            runtime_value=session,
        )

    def is_ephemeral_session_key(self, session_key: str) -> bool:
        parts = session_key.split(":")
        return (
            self._storage is not None
            and len(parts) == 4
            and parts[0] == "agent"
            and parts[2] == "webchat"
            and bool(parts[3])
        )

    def _consumer_budget(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
    ) -> GatewayConsumerBudget:
        raw_session = session.runtime_value if session is not None else None
        return limit_gateway_consumer_budget(
            resolve_gateway_consumer_budget(self._context, raw_session),
            requested_tokens,
        )

    def resolve_context_window_tokens(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
    ) -> int:
        return self._consumer_budget(session, requested_tokens).context_window_tokens

    def build_plan(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
        compaction_id: str,
        operation_deadline: float,
    ) -> SessionCompactionPlan:
        raw_session = session.runtime_value if session is not None else None
        budget = self._consumer_budget(session, requested_tokens)
        target = resolve_gateway_compaction_target(self._context, raw_session)
        config = build_compaction_config_from_provider(
            target.provider,
            model_override=target.model or effective_session_model(raw_session),
            compaction_config=getattr(
                getattr(self._context, "config", None),
                "compaction",
                None,
            ),
            compaction_plan=target.plan,
        )
        config.deadline_at_monotonic = operation_deadline
        arm_compaction_deadline(config, operation_id=compaction_id)
        session_id = session.session_id if session is not None else None
        compaction_correlation = (
            ProviderRequestCorrelation(
                session_id=session_id,
                turn_id=compaction_id,
                execution_id=uuid.uuid4().hex,
                call_kind="auxiliary.compaction",
            )
            if session_id
            and not provider_request_correlation_disabled(config=self._context.config)
            else None
        )
        flush_correlation = derive_provider_request_correlation(
            compaction_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.session_flush",
        )
        runtime = _GatewayCompactionPlan(
            budget=budget,
            config=config,
            compaction_correlation=compaction_correlation,
            flush_correlation=flush_correlation,
        )
        return SessionCompactionPlan(
            context_window_tokens=budget.context_window_tokens,
            runtime_value=runtime,
        )

    def for_session(self, session_key: str) -> SessionCompactionLock | None:
        return cast(
            SessionCompactionLock | None,
            get_session_lock(self._context.turn_runner, session_key),
        )

    @property
    def flush_enabled(self) -> bool:
        return flush_trigger_enabled(self._context.config, "manual")

    @property
    def flush_available(self) -> bool:
        return self._context.flush_service is not None

    async def transcript(self, session_key: str) -> tuple[object, ...] | None:
        getter = getattr(self._manager, "get_transcript", None)
        if not callable(getter):
            return None
        return tuple(await getter(session_key))

    @staticmethod
    def _runtime_plan(plan: SessionCompactionPlan) -> _GatewayCompactionPlan:
        if not isinstance(plan.runtime_value, _GatewayCompactionPlan):
            raise TypeError("invalid gateway compaction plan")
        return plan.runtime_value

    async def flush(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
        plan: SessionCompactionPlan,
        compaction_id: str,
    ) -> object:
        service = self._context.flush_service
        if service is None:
            raise RuntimeError("flush service unavailable")
        runtime = self._runtime_plan(plan)
        memory_cfg = getattr(getattr(self._context, "config", None), "memory", None)
        raw_timeout = getattr(memory_cfg, "flush_background_timeout_seconds", 120.0)
        try:
            timeout = max(float(raw_timeout), 0.0)
        except (TypeError, ValueError):
            timeout = 120.0
        kwargs: dict[str, Any] = {
            "agent_id": session.agent_id,
            "timeout": timeout,
            "message_window": 0,
            "segment_mode": "auto",
            "raw_capture_policy": "required",
            "turn_id": compaction_id,
        }
        if runtime.flush_correlation is not None and _accepts_keyword_arg(
            service.execute,
            "provider_request_correlation",
        ):
            kwargs["provider_request_correlation"] = runtime.flush_correlation
        try:
            return await await_compaction_phase(
                service.execute(
                    list(transcript),
                    self._session_key(session),
                    **kwargs,
                ),
                runtime.config,
                phase="flushing",
            )
        except CompactionTimeoutError as exc:
            raise SessionCompactionPhaseTimeoutError(exc.phase) from exc

    @staticmethod
    def _session_key(session: SessionCompactionSession) -> str:
        return str(getattr(session.runtime_value, "session_key", "") or "")

    def receipt_status(self, receipt: object | None) -> str:
        return flush_receipt_status_for_compaction(receipt, self._context.config)

    def receipt_is_successful(self, receipt: object) -> bool:
        return flush_receipt_is_successful_flush(receipt)

    @property
    def requires_safe_receipt(self) -> bool:
        return pre_compaction_flush_requires_safe_receipt(self._context.config)

    async def checkpoint_covers(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
    ) -> bool:
        if self._storage is None:
            return False
        return await self._callbacks.checkpoint_covers(
            self._storage,
            self._session_key(session),
            session.session_id,
            list(transcript),
        )

    def assess(
        self,
        receipt: object | None,
        *,
        checkpoint_safe: bool,
        required: bool,
    ) -> SessionCompactionMemoryAssessment:
        status = compaction_memory_status(
            receipt,
            deterministic_receipt_safe=checkpoint_safe,
            required=required,
        )
        return SessionCompactionMemoryAssessment(
            allows_destructive_compaction=status.allows_destructive_compaction,
            safety_status=status.safety_status,
            semantic_status=status.semantic_status,
        )

    def record(self, outcome: str, **details: object) -> None:
        safe_details = dict(details)
        receipt = safe_details.get("receipt")
        if receipt is not None:
            safe_details["receipt"] = flush_receipt_to_dict(receipt)
        if outcome in {"flush_failed", "flush_degraded", "flush_skipped"}:
            log.warning(f"sessions.context_compact.{outcome}", **safe_details)
        else:
            log.info(f"sessions.context_compact.{outcome}", **safe_details)

    async def compact(
        self,
        command: CompactSession,
        plan: SessionCompactionPlan,
        memory: SessionCompactionMemoryResult,
    ) -> SessionCompactionExecutionResult:
        manager = self._manager
        if manager is None:
            raise SessionCompactionUnavailableError
        runtime = self._runtime_plan(plan)
        compact_with_result = getattr(manager, "compact_with_result", None)
        if callable(compact_with_result):
            kwargs: dict[str, Any] = {"custom_instructions": command.instructions}
            optional = {
                "compaction_id": runtime.config.operation_id,
                "trigger_reason": "manual",
                "flush_receipt_status": memory.receipt_status,
                "provider_request_correlation": runtime.compaction_correlation,
                "context_window_chars": runtime.budget.provider_request_max_chars,
            }
            for name, value in optional.items():
                if value is not None and _accepts_keyword_arg(compact_with_result, name):
                    kwargs[name] = value
            consumer_admission, consumer_admission_fingerprint = (
                build_gateway_consumer_admission(runtime.budget)
            )
            if _accepts_keyword_arg(compact_with_result, "consumer_admission"):
                kwargs["consumer_admission"] = consumer_admission
            if _accepts_keyword_arg(
                compact_with_result,
                "consumer_admission_fingerprint",
            ):
                kwargs["consumer_admission_fingerprint"] = (
                    consumer_admission_fingerprint
                )
            try:
                result = await await_compaction_phase(
                    compact_with_result(
                        command.session_key,
                        plan.context_window_tokens,
                        runtime.config,
                        **kwargs,
                    ),
                    runtime.config,
                    phase="summarizing",
                )
            except CompactionTimeoutError as exc:
                raise SessionCompactionPhaseTimeoutError(exc.phase) from exc
            summary = str(getattr(result, "summary", "") or "")
            removed_count = int(getattr(result, "removed_count", 0) or 0)
            return SessionCompactionExecutionResult(
                applied=bool(
                    summary
                    and (
                        removed_count > 0
                        or bool(getattr(result, "replaced_previous_summary", False))
                    )
                ),
                summary_len=len(summary),
                summary_source=str(
                    getattr(result, "summary_source", "unknown") or "unknown"
                ),
                tokens_before=int(getattr(result, "tokens_before", 0) or 0),
                tokens_after=int(getattr(result, "tokens_after", 0) or 0),
                remaining_budget_tokens=int(
                    getattr(result, "remaining_budget_tokens", 0) or 0
                ),
                removed_count=removed_count,
                kept_count=len(getattr(result, "kept_entries", None) or []),
                chunk_count=int(getattr(result, "chunks_processed", 0) or 0),
                coverage_status=str(
                    getattr(result, "coverage_status", "unknown") or "unknown"
                ),
                missing_obligation_count=len(
                    getattr(result, "missing_obligations", None) or []
                ),
                critical_carry_forward_count=len(
                    getattr(result, "critical_carry_forward", None) or []
                ),
                state_kind=str(getattr(result, "summary_format", "text") or "text"),
                skip_reason=str(getattr(result, "skip_reason", "") or ""),
                quality_report=dict(getattr(result, "quality_report", None) or {}),
            )

        try:
            summary = await await_compaction_phase(
                call_compact_with_optional_config(
                    manager.compact,
                    command.session_key,
                    plan.context_window_tokens,
                    runtime.config,
                    provider_request_correlation=runtime.compaction_correlation,
                ),
                runtime.config,
                phase="summarizing",
            )
        except CompactionTimeoutError as exc:
            raise SessionCompactionPhaseTimeoutError(exc.phase) from exc
        return SessionCompactionExecutionResult(
            applied=bool(summary),
            summary_len=len(summary),
            removed_count=1 if summary else 0,
            skip_reason="" if summary else "empty_summary",
        )

    @staticmethod
    def _result_payload(result: SessionCompactionExecutionResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "remaining_budget_tokens": result.remaining_budget_tokens,
            "removed_count": result.removed_count,
            "kept_count": result.kept_count,
            "chunk_count": result.chunk_count,
            "coverage_status": result.coverage_status,
            "missing_obligation_count": result.missing_obligation_count,
            "critical_carry_forward_count": result.critical_carry_forward_count,
            "state_kind": result.state_kind,
            "quality_report": dict(result.quality_report),
            "summary_len": result.summary_len,
            "summary_source": result.summary_source,
        }
        if result.skip_reason:
            payload["skip_reason"] = result.skip_reason
        return payload

    async def prepare(self, event: SessionCompactionEvent) -> object:
        payload: dict[str, Any] = {
            "key": event.session_key,
            "source": "manual",
            "phase": "manual",
            "context_window_tokens": event.context_window_tokens,
            **compaction_effect_payload(
                status=event.status,
                source="manual",
                reason=event.reason,
                user_visible=True,
            ),
            "status": event.status,
            **compaction_lifecycle_payload(
                event.compaction_id,
                _MILESTONE_TO_RUNTIME_EVENT[event.milestone],
            ),
        }
        if event.result is not None:
            payload.update(self._result_payload(event.result))
        for name, value in {
            "reason": event.reason,
            "message": event.message,
            "stage": event.stage,
            "heartbeat_at": event.heartbeat_at,
            "elapsed_ms": event.elapsed_ms,
            "heartbeat_interval_seconds": event.heartbeat_interval_seconds,
            "flush_receipt_status": event.flush_receipt_status,
            "observation_error": event.observation_error,
        }.items():
            if value is not None:
                payload[name] = value
        if event.heartbeat:
            payload["heartbeat"] = True
        if event.cancellation_reconciled:
            payload["cancellation_reconciled"] = True
        if event.deadline_reconciled:
            payload["deadline_reconciled"] = True
        return await self._callbacks.prepare_event(
            self._context,
            event.session_key,
            self._EVENT_NAME,
            payload,
        )

    def claim_and_buffer(
        self,
        prepared: object,
        event: SessionCompactionEvent,
        *,
        track_current_task: bool,
    ) -> object | None:
        prepared_payload = cast(dict[str, Any], prepared)
        normalized = self._callbacks.notify_compaction(
            event.session_key,
            notify_listeners=False,
            track_current_task=track_current_task,
            **prepared_payload,
        )
        if normalized is None:
            if self._callbacks.terminal_status(event.compaction_id) is not None:
                return None
            normalized = prepared_payload
        buffered = self._callbacks.buffer_event(
            event.session_key,
            self._EVENT_NAME,
            normalized,
        )
        return _GatewayBufferedCompactionEvent(event.session_key, buffered)

    async def broadcast(self, buffered: object) -> None:
        envelope = cast(_GatewayBufferedCompactionEvent, buffered)
        await self._callbacks.send_event(
            self._context,
            envelope.session_key,
            self._EVENT_NAME,
            envelope.payload,
        )

    def register(
        self,
        session_key: str,
        compaction_id: str,
        task: asyncio.Task[object],
    ) -> None:
        self._callbacks.register_background(session_key, compaction_id, task)
        _manual_compaction_tasks.add(task)
        task.add_done_callback(_manual_compaction_tasks.discard)

    def background_failed(
        self,
        session_key: str,
        compaction_id: str,
        error: Exception,
    ) -> None:
        log.warning(
            "sessions.context_compact.background_failed",
            key=session_key,
            compaction_id=compaction_id,
            error=str(error),
        )

    @asynccontextmanager
    async def account(self, session_key: str) -> AsyncIterator[None]:
        scope = await build_session_usage_scope(
            getattr(self._context, "usage_event_sink", None),
            self._manager,
            session_key,
            run_kind="session_compaction",
        )
        with bind_usage_accounting_scope(scope):
            yield


class SessionMaintenanceUseCase(Protocol):
    async def compact(self, command: CompactSession) -> SessionCompactionResult: ...


class GatewaySessionMaintenanceAdapter:
    """Translate v4 fields and project the transport-neutral compaction result."""

    def __init__(
        self,
        application: SessionMaintenanceUseCase,
        *,
        require_key: SessionKeyReader,
    ) -> None:
        self._application = application
        self._require_key = require_key

    async def compact(self, params: dict[str, Any] | None) -> dict[str, Any]:
        key = self._require_key(params)
        raw = params or {}
        instructions = raw.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message="instructions must be a string when provided.",
                details={"field": "instructions"},
            )
        context_window_tokens = raw.get(
            "contextWindowTokens",
            raw.get("context_window_tokens"),
        )
        if context_window_tokens is not None:
            if isinstance(context_window_tokens, bool):
                raise RpcHandlerError(
                    code="INVALID_PARAMS",
                    message="contextWindowTokens must be a positive integer.",
                    details={"field": "contextWindowTokens"},
                )
            try:
                context_window_tokens = int(context_window_tokens)
            except (TypeError, ValueError) as exc:
                raise RpcHandlerError(
                    code="INVALID_PARAMS",
                    message="contextWindowTokens must be a positive integer.",
                    details={"field": "contextWindowTokens"},
                ) from exc
            if context_window_tokens <= 0:
                raise RpcHandlerError(
                    code="INVALID_PARAMS",
                    message="contextWindowTokens must be a positive integer.",
                    details={"field": "contextWindowTokens"},
                )
        try:
            result = await self._application.compact(
                CompactSession(
                    session_key=key,
                    wait=bool(raw.get("wait", True)),
                    context_window_tokens=context_window_tokens,
                    instructions=instructions,
                )
            )
        except ValueError as exc:
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message=str(exc),
                details={"field": "contextWindowTokens"},
            ) from exc
        except SessionCompactionDeadlineError as exc:
            raise RpcHandlerError(
                code="COMPACTION_TIMEOUT",
                message="Compaction exceeded its absolute deadline.",
                details={
                    "key": exc.session_key,
                    "compaction_id": exc.compaction_id,
                    "phase": exc.phase,
                },
            ) from exc
        except SessionCompactionFlushSafetyError as exc:
            raise RpcHandlerError(
                code="CONTEXT_FLUSH_FAILED",
                message=(
                    "Manual compaction aborted: flush receipt is not sufficient "
                    "for destructive compaction."
                ),
                details={
                    "flush_receipt": flush_receipt_to_dict(exc.receipt),
                    "key": exc.session_key,
                    "session_id": exc.session_id,
                    "reason": "destructive_manual_compact_requires_safe_flush",
                    "flush_receipt_status": exc.receipt_status,
                    "memory_safety_status": exc.assessment.safety_status,
                    "semantic_memory_status": exc.assessment.semantic_status,
                },
            ) from exc
        except SessionCompactionUnavailableError as exc:
            raise KeyError("No session manager available") from exc
        except SessionCompactionNotFoundError as exc:
            raise KeyError(f"Session not found: {exc.session_key}") from exc
        return self._to_wire(result)

    @staticmethod
    def _to_wire(result: SessionCompactionResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": result.session_key,
            "compaction_id": result.compaction_id,
            "status": result.status,
            "compacted": result.applied,
            "applied": result.applied,
            "durability": "durable" if result.applied else "none",
            "user_visible": True,
        }
        if result.status == "started":
            return payload
        payload.update(
            {
                "mode": "summary",
                "summary_len": result.summary_len,
                "summary_source": result.summary_source,
                "context_window_tokens": result.context_window_tokens,
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "remaining_budget_tokens": result.remaining_budget_tokens,
                "removed_count": result.removed_count,
                "kept_count": result.kept_count,
                "chunk_count": result.chunk_count,
                "coverage_status": result.coverage_status,
                "missing_obligation_count": result.missing_obligation_count,
                "critical_carry_forward_count": result.critical_carry_forward_count,
                "state_kind": result.state_kind,
            }
        )
        if result.quality_report:
            payload["quality_report"] = dict(result.quality_report)
        if result.reason is not None:
            payload["reason"] = result.reason
            payload["skip_reason"] = result.reason
        if result.flush_receipt is not None:
            payload["flush_receipt"] = flush_receipt_to_dict(result.flush_receipt)
        if result.flush_receipt_status is not None:
            payload["flush_receipt_status"] = result.flush_receipt_status
        return payload


def build_gateway_session_maintenance_adapter(
    context: RpcContext,
    callbacks: GatewaySessionMaintenanceCallbacks,
    *,
    require_key: SessionKeyReader,
) -> GatewaySessionMaintenanceAdapter:
    ports = GatewaySessionMaintenancePorts(context, callbacks)
    application = SessionMaintenance(
        planning=ports,
        locking=ports,
        memory=ports,
        executor=ports,
        lifecycle=ports,
        ownership=ports,
        usage=ports,
        new_compaction_id=new_compaction_id,
    )
    return GatewaySessionMaintenanceAdapter(
        application,
        require_key=require_key,
    )


__all__ = [
    "GatewaySessionMaintenanceAdapter",
    "GatewaySessionMaintenanceCallbacks",
    "GatewaySessionMaintenancePorts",
    "SessionMaintenanceUseCase",
    "build_gateway_session_maintenance_adapter",
]
