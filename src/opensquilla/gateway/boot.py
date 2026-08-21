"""Boot sequence orchestration for the gateway."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import socket
import sys
import time
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.memory.manager import MemoryManager
    from opensquilla.memory.store import LongTermMemoryStore
    from opensquilla.memory.sync_manager import (
        MemorySyncManager as MemoryFileWatcher,  # SyncManager replaces watcher
    )
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.selector import ModelSelector
    from opensquilla.scheduler import SchedulerEngine
    from opensquilla.session.manager import SessionManager
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.registry import ToolRegistry

import structlog
import uvicorn
from starlette.applications import Starlette

from opensquilla.agents.scope import resolve_agent_model, resolve_agent_workspace_dir
from opensquilla.artifacts import enrich_artifact_event_dict
from opensquilla.asyncio_utils import create_background_task
from opensquilla.engine.usage import UsageTracker as _UsageTracker
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import (
    GatewayConfig,
    effective_agent_stream_idle_timeout_seconds,
    is_public_bind,
)
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.model_routing import model_routing_snapshot
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_lifecycle import (
    TaskLifecycleEvent,
    apply_task_lifecycle_to_session,
    session_status_for_task_status,
)
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.session_streams import get_session_streams, reset_session_streams
from opensquilla.gateway.task_runtime import TaskRuntimeShutdownResult
from opensquilla.gateway.terminal_activity import (
    append_activity_phase,
    is_usage_accounting_barrier,
    safe_primary_user_message_id,
    safe_retry_after_ms,
    terminal_activity_snapshot,
    usage_barrier_replay_proof,
)
from opensquilla.gateway.websocket import get_registry
from opensquilla.paths import default_opensquilla_home
from opensquilla.permissions import configured_default_elevated
from opensquilla.session.models import SessionStatus
from opensquilla.session.terminal_reply import (
    append_error_ref,
    build_terminal_reply,
    safe_provider_failure_code,
    safe_provider_failure_message,
    sanitize_agent_error,
)

log = structlog.get_logger(__name__)


GATEWAY_GRACEFUL_TIMEOUT_ENV = "OPENSQUILLA_GATEWAY_GRACEFUL_TIMEOUT"
_DEFAULT_GRACEFUL_TIMEOUT_S = 30.0
_MAX_GRACEFUL_TIMEOUT_S = 120.0
_WS_SHUTDOWN_CLOSE_TIMEOUT_S = 2.0
_WS_SHUTDOWN_CANCEL_GRACE_S = 0.05


def _elapsed_monotonic_ms(started_at: float, ended_at: float | None = None) -> int:
    end = time.monotonic() if ended_at is None else ended_at
    return max(0, int((end - started_at) * 1000))


def _log_gateway_startup_phase(
    phase: str,
    *,
    startup_started_at: float,
    phase_started_at: float,
    status: str = "ready",
) -> float:
    completed_at = time.monotonic()
    log.info(
        "gateway.startup_phase",
        phase=phase,
        status=status,
        duration_ms=_elapsed_monotonic_ms(phase_started_at, completed_at),
        startup_elapsed_ms=_elapsed_monotonic_ms(startup_started_at, completed_at),
    )
    # Do not charge structured-log I/O to the phase that follows this one.
    return time.monotonic()


def _start_background_install_telemetry(config: GatewayConfig) -> None:
    def _log_result(result: Any) -> None:
        log.debug(
            "gateway.install_telemetry",
            skipped_reason=result.skipped_reason,
            telemetry_event=result.event,
            sent=result.sent,
            uploaded=result.uploaded,
            endpoint_configured=result.endpoint_configured,
        )

    try:
        from opensquilla.observability.install_telemetry import (
            start_background_install_telemetry,
        )

        start_background_install_telemetry(config=config, on_result=_log_result)
    except Exception:
        log.debug("gateway.install_telemetry_skipped", exc_info=True)


def _prewarm_tokenrhythm_install_id(config: GatewayConfig) -> None:
    """Prime the optional TokenRhythm install header without delaying boot."""

    try:
        from opensquilla.provider.tokenrhythm_correlation import (
            prewarm_tokenrhythm_install_id,
        )

        prewarm_tokenrhythm_install_id(config=config)
    except Exception:
        # Install identity is best-effort request metadata.  A resolver or
        # thread-start failure must never make the gateway/standalone runtime
        # unavailable.
        log.debug("gateway.tokenrhythm_install_id_prewarm_skipped", exc_info=True)


def _auto_propose_usage_execution_context(
    agent_id: str,
    usage_event_sink: Any | None,
) -> Any | None:
    """Create one run identity for a gateway-owned auto-propose workload."""

    if usage_event_sink is None:
        return None
    from opensquilla.engine.usage_accounting import UsageExecutionContext

    execution_id = uuid.uuid4().hex
    return UsageExecutionContext(
        execution_id=execution_id,
        agent_run_id=execution_id,
        turn_id=execution_id,
        session_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"opensquilla:system:auto-propose:{agent_id}",
        ).hex,
        session_epoch=0,
        agent_id=agent_id,
        run_kind="auto_propose",
    )


def gateway_graceful_timeout() -> float:
    """Per-phase graceful drain budget in seconds, env-overridable and bounded.

    Used by :meth:`GatewayServer.close` for both the in-flight turn drain and the
    background-completion drain. Bounded so the worst-case shutdown window stays
    predictable, letting the CLI/desktop kill paths pick a SIGKILL deadline that
    always exceeds it (see :func:`gateway_shutdown_deadline`). Override with
    ``OPENSQUILLA_GATEWAY_GRACEFUL_TIMEOUT`` (seconds).
    """
    raw = os.environ.get(GATEWAY_GRACEFUL_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_GRACEFUL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_GRACEFUL_TIMEOUT_S
    if value <= 0:
        return _DEFAULT_GRACEFUL_TIMEOUT_S
    return min(value, _MAX_GRACEFUL_TIMEOUT_S)


def gateway_shutdown_deadline() -> float:
    """Recommended SIGKILL deadline (seconds) for the CLI/desktop kill paths.

    :meth:`GatewayServer.close` runs two sequential drain phases (in-flight turns,
    then background completions), each bounded by :func:`gateway_graceful_timeout`,
    plus channel/WS/MCP teardown and a 5s server-task join. The sum is padded so a
    clean drain never races the force-kill. Derives from the same env knob, so
    raising the drain budget automatically widens the kill window.
    """
    return gateway_graceful_timeout() * 2 + 15.0


class _FlushReceiptSessionStorage(Protocol):
    async def get_session(self, session_key: str) -> Any | None: ...

    async def list_memory_durable_receipts(self, **kwargs: Any) -> list[Any]: ...

    async def upsert_memory_durable_receipt(
        self,
        receipt: Any,
        *,
        expected_session_id: str | None = None,
    ) -> Any: ...


_AUTO_PROPOSE_TOOL_ALLOWLIST = frozenset(
    {
        "emit_text",
        "meta_skill_fill_slots",
        "meta_skill_assemble",
        "meta_skill_lint_run",
        "meta_skill_smoke_run",
        "meta_skill_runtime_e2e_run",
        "meta_skill_persist_proposal",
    }
)
_DEBUG_FILE_HANDLER_ATTR = "_opensquilla_debug_file_handler"
_CONSOLE_HANDLER_ATTR = "_opensquilla_console_log_handler"
_ENABLED_VALUES = {"1", "true", "yes", "on"}
_DISABLED_VALUES = {"0", "false", "no", "off"}
_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.FATAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def _desktop_fast_start_enabled() -> bool:
    """Return true when desktop startup may defer noncritical warmups."""

    override = os.environ.get("OPENSQUILLA_DESKTOP_FAST_START")
    if override is not None:
        return override.strip().lower() in _ENABLED_VALUES
    return os.environ.get("OPENSQUILLA_DESKTOP", "").strip().lower() in _ENABLED_VALUES


def _desktop_router_preload_enabled() -> bool:
    """Keep desktop first paint fast unless router preload is explicitly requested."""

    override = os.environ.get("OPENSQUILLA_DESKTOP_PRELOAD_ROUTER")
    if override is not None:
        return override.strip().lower() in _ENABLED_VALUES
    return not _desktop_fast_start_enabled()


def _make_auto_propose_tool_invoker(
    registry: ToolRegistry,
    *,
    allowed_tools: frozenset[str] = _AUTO_PROPOSE_TOOL_ALLOWLIST,
) -> Callable[[str, dict[str, Any]], Any]:
    """Build the unattended auto-propose tool invoker through dispatch policy."""

    from opensquilla.skills.meta.orchestrator import make_tool_invoker_from_handler
    from opensquilla.tools.dispatch import build_tool_handler

    ctx = _make_auto_propose_tool_context(allowed_tools=allowed_tools)
    return make_tool_invoker_from_handler(
        tool_handler=build_tool_handler(registry, ctx),
    )


def _make_auto_propose_tool_context(
    *,
    agent_id: str = "auto_propose",
    workspace_dir: str | None = None,
    allowed_tools: frozenset[str] = _AUTO_PROPOSE_TOOL_ALLOWLIST,
) -> Any:
    """Policy context for unattended meta-skill auto-propose work."""

    from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

    return ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CRON,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id=agent_id,
        workspace_dir=workspace_dir,
        workspace_strict=bool(workspace_dir),
        allowed_tools=set(allowed_tools),
        surfaced_tools=set(allowed_tools),
    )


def _resolve_migrations_dir() -> Path:
    """Locate yoyo migrations; kept as a name callers and tests already import.

    The implementation lives in :mod:`opensquilla.persistence.migrator` so that
    offline profile consolidation can resolve migrations without importing the
    gateway, which would close a package import cycle.
    """

    from opensquilla.persistence.migrator import resolve_migrations_dir

    return resolve_migrations_dir()


class TaskRuntimeStreamError(RuntimeError):
    """Terminal error raised after a turn stream emits an error event."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        terminal_reason: str | None = None,
        failure_kind: str | None = None,
        retry_after_ms: int | None = None,
        activity_snapshot: dict[str, Any] | None = None,
        usage_call_index: int | None = None,
        no_prior_provider_dispatch: bool = False,
        replay_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.terminal_reason = terminal_reason
        self.failure_kind = failure_kind
        self.retry_after_ms = retry_after_ms
        self.activity_snapshot = activity_snapshot
        self.usage_call_index = usage_call_index
        self.no_prior_provider_dispatch = no_prior_provider_dispatch
        self.replay_safe = replay_safe


# fmt: off
def _make_channel_rpc_context_factory(svc: ServiceContainer, config: GatewayConfig, *, subscription_manager: Any, channel_manager_ref: Any, turn_runner: Any, heartbeat_service: Any, diagnostics_state: Any | None = None) -> Any:  # noqa: E501
    from opensquilla.channels.command_registry import build_channel_rpc_context

    def _factory(envelope: Any) -> Any:
        names = ("session_manager", "provider_selector", "tool_registry", "usage_tracker", "usage_event_sink", "skill_loader", "cron_scheduler", "task_runtime", "flush_service", "heartbeat_loop", "agent_registry", "memory_managers", "memory_stores", "memory_retrievers")  # noqa: E501
        return build_channel_rpc_context(
            envelope,
            gateway_config=config,
            **{name: getattr(svc, name) for name in names},
            subscription_manager=subscription_manager,
            channel_manager=channel_manager_ref(),
            turn_runner=turn_runner,
            heartbeat_service=heartbeat_service,
            diagnostics_state=diagnostics_state,
        )

    return _factory
# fmt: on


def _interval_h_to_schedule(interval_h: int) -> tuple[Any, str]:
    """Map an hour interval to a structured (kind, value) schedule pair.

    Aligns to a clean cron expression when 24 divides evenly; otherwise falls
    back to a raw interval-in-seconds for the EVERY kind.
    """
    from opensquilla.scheduler.types import ScheduleKind

    if interval_h > 0 and 24 % interval_h == 0:
        return ScheduleKind.CRON, f"0 */{interval_h} * * *"
    return ScheduleKind.EVERY, str(interval_h * 3600)


async def _list_scheduler_jobs(scheduler: Any) -> list[Any]:
    list_jobs = getattr(scheduler, "list_jobs", None)
    if not callable(list_jobs):
        return []
    try:
        result = list_jobs()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        log.warning("boot.dream.list_jobs_failed", error=str(exc))
        return []
    return result if isinstance(result, list) else []


def _warn_if_self_learning_unreachable(config: Any) -> None:
    """Warn when self-learning is on but its training trigger can never fire.

    The retrain rides the post-dream hook, so with dream disabled (or its cron
    unscheduled) capture accumulates samples while training silently never
    runs. Config carries no cross-section validation for this, and the CLI is
    often the only surface an operator watches — one explicit boot line turns
    the silent gap into a diagnosable one. Mirrored by the
    ``router.selflearning.status`` RPC's ``trainingReachable`` field.
    """

    sl_cfg = getattr(getattr(config, "squilla_router", None), "self_learning", None)
    if sl_cfg is None or not bool(getattr(sl_cfg, "enabled", False)):
        return
    if os.getenv("OPENSQUILLA_ROUTER_SELFLEARN_DISABLED") == "1":
        return  # the whole loop is deliberately off; unreachable-trigger noise helps no one
    dream_cfg = getattr(getattr(config, "memory", None), "dream", None)
    dream_on = bool(getattr(dream_cfg, "enabled", False))
    dream_scheduled = dream_on and bool(getattr(dream_cfg, "auto_schedule", False))
    if dream_scheduled and os.getenv("OPENSQUILLA_MEMORY_DREAM_DISABLED") != "1":
        return
    log.warning(
        "router_self_learning.trigger_unreachable",
        dream_enabled=dream_on,
        dream_auto_schedule=bool(getattr(dream_cfg, "auto_schedule", False)),
        hint=(
            "squilla_router.self_learning.enabled is true but the post-dream "
            "training trigger cannot fire; capture will accumulate samples "
            "without ever training. Set memory.dream.enabled=true and "
            "memory.dream.auto_schedule=true (and clear "
            "OPENSQUILLA_MEMORY_DREAM_DISABLED) to activate training."
        ),
    )


async def _register_dream_crons(
    *,
    scheduler: Any,
    memory_config: Any,
    agent_ids: list[str],
) -> None:
    """Register a `memory_dream` cron per agent when enabled.

    Respects the ``OPENSQUILLA_MEMORY_DREAM_DISABLED=1`` kill switch.
    Prefers ``memory_config.dream.cron`` if set, else derives a structured
    ``(kind, value)`` pair from ``interval_h``.
    """
    import os

    from opensquilla.scheduler.types import ScheduleKind, SessionTarget

    dream_cfg = getattr(memory_config, "dream", None)
    existing_jobs = await _list_scheduler_jobs(scheduler)
    existing_by_name = {
        getattr(job, "name", ""): job
        for job in existing_jobs
        if getattr(job, "name", "").startswith("memory_dream:")
    }
    disabled_reason = None
    if os.getenv("OPENSQUILLA_MEMORY_DREAM_DISABLED") == "1":
        disabled_reason = "kill_switch"
    elif dream_cfg is None or not getattr(dream_cfg, "enabled", False):
        disabled_reason = "disabled"
    elif not getattr(dream_cfg, "auto_schedule", False):
        disabled_reason = "auto_schedule_disabled"

    if disabled_reason is not None:
        await _pause_dream_crons(
            scheduler=scheduler,
            jobs=list(existing_by_name.values()),
            reason=disabled_reason,
        )
        return

    assert dream_cfg is not None
    if getattr(dream_cfg, "cron", None):
        schedule_kind, schedule_value = ScheduleKind.CRON, dream_cfg.cron
    else:
        schedule_kind, schedule_value = _interval_h_to_schedule(dream_cfg.interval_h)
    for agent_id in agent_ids:
        name = f"memory_dream:{agent_id}"
        existing = existing_by_name.get(name)
        if existing is not None:
            patch: dict[str, Any] = {}
            existing_kind = getattr(existing, "schedule_kind", None)
            existing_value = getattr(existing, "cron_expr", "") or ""
            if (existing_kind, existing_value) != (schedule_kind, schedule_value):
                patch["schedule_kind"] = schedule_kind
                patch["schedule_value"] = schedule_value
            if getattr(existing, "payload", {}).get("agent_id") != agent_id:
                patch["payload"] = {"agent_id": agent_id}
            if getattr(existing, "session_target", None) != SessionTarget.ISOLATED:
                patch["session_target"] = SessionTarget.ISOLATED
            update_job = getattr(scheduler, "update_job", None)
            if patch and callable(update_job):
                result = update_job(getattr(existing, "id"), **patch)
                if inspect.isawaitable(result):
                    await result
            # A previous disabled-config pass (or boot) may have left the row
            # paused; with dream now enabled the job must actually fire again.
            # Matters for live re-reconciliation after a config RPC edit.
            status = getattr(
                getattr(existing, "status", None), "value", getattr(existing, "status", "")
            )
            resume_job = getattr(scheduler, "resume_job", None)
            if status == "paused" and callable(resume_job):
                result = resume_job(getattr(existing, "id"))
                if inspect.isawaitable(result):
                    await result
                log.info("boot.dream.resumed", agent_id=agent_id)
            log.info(
                "boot.dream.already_registered",
                agent_id=agent_id,
                schedule_kind=schedule_kind.value,
                schedule_value=schedule_value,
            )
            continue

        await scheduler.add_job(
            name=name,
            handler_key="memory_dream",
            payload={"agent_id": agent_id},
            session_target=SessionTarget.ISOLATED,
            schedule_kind=schedule_kind,
            schedule_value=schedule_value,
        )
        log.info(
            "boot.dream.registered",
            agent_id=agent_id,
            schedule_kind=schedule_kind.value,
            schedule_value=schedule_value,
        )


async def _pause_dream_crons(*, scheduler: Any, jobs: list[Any], reason: str) -> None:
    """Pause managed Dream cron jobs so persisted rows cannot bypass config."""
    pause_job = getattr(scheduler, "pause_job", None)
    update_job = getattr(scheduler, "update_job", None)
    for job in jobs:
        status = getattr(getattr(job, "status", None), "value", getattr(job, "status", ""))
        if status in {"paused", "disabled", "deleted"}:
            continue
        job_id = getattr(job, "id", None)
        if not job_id:
            continue
        try:
            if callable(pause_job):
                result = pause_job(job_id)
            elif callable(update_job):
                result = update_job(job_id, enabled=False)
            else:
                continue
            if inspect.isawaitable(result):
                await result
            log.info(
                "boot.dream.paused",
                job_id=job_id,
                name=getattr(job, "name", ""),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "boot.dream.pause_failed",
                job_id=job_id,
                reason=reason,
                error=str(exc),
            )


async def _pause_auto_propose_crons(
    *,
    scheduler: Any,
    agent_ids: list[str],
) -> None:
    """Pause per-agent auto-propose jobs without deleting persisted rows."""

    existing_jobs = await _list_scheduler_jobs(scheduler)
    target_names = {f"auto_propose:{agent_id}" for agent_id in agent_ids}
    pause_job = getattr(scheduler, "pause_job", None)
    update_job = getattr(scheduler, "update_job", None)
    for job in existing_jobs:
        if getattr(job, "name", "") not in target_names:
            continue
        status = getattr(getattr(job, "status", None), "value", getattr(job, "status", ""))
        if status in {"paused", "disabled", "deleted"}:
            continue
        job_id = getattr(job, "id", None)
        if not job_id:
            continue
        try:
            if callable(pause_job):
                result = pause_job(job_id)
            elif callable(update_job):
                result = update_job(job_id, enabled=False)
            else:
                continue
            if inspect.isawaitable(result):
                await result
            log.info("boot.auto_propose.paused", job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("boot.auto_propose.pause_failed", job_id=job_id, error=str(exc))


async def _register_auto_propose_crons(
    *,
    scheduler: Any,
    auto_cfg: Any,
    agent_ids: list[str],
) -> None:
    """Register or resume one isolated auto-propose cron per configured agent."""

    from opensquilla.scheduler.types import ScheduleKind, SessionTarget

    schedule_raw = auto_cfg.cron
    existing_jobs = await _list_scheduler_jobs(scheduler)
    existing_by_name = {
        getattr(job, "name", ""): job
        for job in existing_jobs
        if getattr(job, "name", "").startswith("auto_propose:")
    }
    allowed_agent_ids = set(getattr(auto_cfg, "agent_ids", []) or [])
    if allowed_agent_ids:
        agent_ids = [agent_id for agent_id in agent_ids if agent_id in allowed_agent_ids]

    update_job = getattr(scheduler, "update_job", None)
    resume_job = getattr(scheduler, "resume_job", None)
    for agent_id in agent_ids:
        name = f"auto_propose:{agent_id}"
        existing = existing_by_name.get(name)
        if existing is not None:
            patch: dict[str, Any] = {}
            if getattr(existing, "schedule_raw", "") != schedule_raw:
                patch["schedule_kind"] = ScheduleKind.CRON
                patch["schedule_value"] = schedule_raw
            if getattr(existing, "payload", {}).get("agent_id") != agent_id:
                patch["payload"] = {"agent_id": agent_id}
            if getattr(existing, "session_target", None) != SessionTarget.ISOLATED:
                patch["session_target"] = SessionTarget.ISOLATED
            if patch and callable(update_job):
                result = update_job(getattr(existing, "id"), **patch)
                if inspect.isawaitable(result):
                    await result

            status = getattr(
                getattr(existing, "status", None),
                "value",
                getattr(existing, "status", ""),
            )
            if status == "paused" and callable(resume_job):
                result = resume_job(getattr(existing, "id"))
                if inspect.isawaitable(result):
                    await result
            log.info("boot.auto_propose.already_registered", agent_id=agent_id)
            continue

        await scheduler.add_job(
            name=name,
            schedule_kind=ScheduleKind.CRON,
            schedule_value=schedule_raw,
            handler_key="auto_propose",
            payload={"agent_id": agent_id},
            session_target=SessionTarget.ISOLATED,
        )
        log.info("boot.auto_propose.registered", agent_id=agent_id, schedule=schedule_raw)


@dataclass
class ServiceContainer:
    """Typed container for initialized services. Returned by build_services().

    WARNING: build_services() mutates module-level state:
    - tools.builtin.memory_tools (create_memory_tools)
    - tools.builtin.skill_tools (create_skill_tools)
    - tools.builtin.admin (set_gateway_config, set_scheduler)
    - search.providers (configure_search)
    Do not call build_services() twice in the same process without
    understanding these side effects.
    """

    config: GatewayConfig
    provider_selector: ModelSelector | None = None
    tool_registry: ToolRegistry | None = None
    session_manager: SessionManager | None = None
    skill_loader: SkillLoader | None = None
    skill_watcher: Any = None
    skill_management_service: Any = None
    skill_management_state: dict[str, Any] = field(default_factory=dict)
    usage_tracker: UsageTracker | None = None
    usage_event_sink: Any = None
    usage_backfill_task: asyncio.Task[Any] | None = None
    sandbox_setup_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    profile_import_maintenance_task: asyncio.Task[Any] | None = field(
        default=None,
        repr=False,
    )
    cron_scheduler: SchedulerEngine | None = None
    model_catalog: ModelCatalog | None = None
    model_catalog_refresh_coordinator: Any = None
    agent_registry: Any = None
    memory_managers: dict[str, MemoryManager] = field(default_factory=dict)
    # Legacy per-tier dicts. These are derived views over
    # `memory_managers` populated in build_services(); direct ServiceContainer
    # constructors (e.g. tests) may still set them independently. Once all
    # consumers use `memory_managers`, these legacy fields can be removed.
    memory_stores: dict[str, LongTermMemoryStore] = field(default_factory=dict)
    memory_sync_managers: dict[str, MemoryFileWatcher] = field(default_factory=dict)
    memory_watchers: list[MemoryFileWatcher] = field(default_factory=list)
    memory_retrievers: dict[str, Any] = field(default_factory=dict)
    turn_capture_services: dict[str, Any] = field(default_factory=dict)
    flush_service: Any = None  # SessionFlushService | None (gated by OPENSQUILLA_SESSION_FLUSH)
    memory_repair_service: Any = None
    meta_run_writer: Any = None
    router_decision_writer: Any = None
    turn_error_writer: Any = None
    router_calibration_service: Any = None
    provider_stats: Any = None  # ProviderStatsStore | None (rolling call latency samples)
    task_runtime: Any = None
    goal_service: Any = None
    heartbeat_loop: Any = None
    heartbeat_watcher: Any = None
    prompt_cache_keepalive_service: Any = None
    daily_usage_telemetry_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    deferred_warmups: list[Callable[[], Any]] = field(default_factory=list)
    deferred_warmup_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _compaction_listener_remove: Callable[[], None] | None = None
    _approval_listener_remove: Callable[[], None] | None = None
    _approval_channel_notifier_remove: Callable[[], None] | None = None

    # Backward-compat alias — returns the "main" store (or None).
    @property
    def memory_store(self) -> LongTermMemoryStore | None:
        return self.memory_stores.get("main")

    async def close(self) -> None:
        """Teardown async resources. Idempotent — safe to call twice.

        Ordering rule: scheduled producers (heartbeat watcher/loop and the
        cron scheduler) MUST stop before the memory tier closes; otherwise
        an in-flight cron job or heartbeat tick can drive TurnRunner ->
        TurnCaptureService.capture_turn against an already-closed store.
        """
        # Desktop warmups may enter the catalog refresh boundary after first
        # paint. Stop that producer before closing the coordinator so a task
        # which has not started yet cannot recreate a fresh global coordinator
        # during shutdown.
        deferred_warmup_task = self.deferred_warmup_task
        self.deferred_warmup_task = None
        if deferred_warmup_task is not None:
            deferred_warmup_task.cancel()
            try:
                await deferred_warmup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("gateway.deferred_warmup_close_failed", exc_info=True)

        skill_watcher = self.skill_watcher
        self.skill_watcher = None
        if skill_watcher is not None:
            try:
                await skill_watcher.stop()
            except Exception:
                log.debug("gateway.skill_watcher_close_failed", exc_info=True)

        model_catalog_refresh_coordinator = self.model_catalog_refresh_coordinator
        self.model_catalog_refresh_coordinator = None
        if model_catalog_refresh_coordinator is not None:
            try:
                await model_catalog_refresh_coordinator.close()
            except Exception:
                log.debug("gateway.model_catalog_refresh_close_failed", exc_info=True)
            try:
                from opensquilla.gateway.model_catalog_refresh import (
                    install_tokenrhythm_catalog_coordinator,
                )

                install_tokenrhythm_catalog_coordinator(None)
            except Exception:
                pass

        profile_import_maintenance_task = self.profile_import_maintenance_task
        self.profile_import_maintenance_task = None
        if profile_import_maintenance_task is not None:
            profile_import_maintenance_task.cancel()
            try:
                await profile_import_maintenance_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug(
                    "gateway.profile_import_maintenance_close_failed",
                    exc_info=True,
                )
        try:
            from opensquilla.memory.profile_import.jobs import (
                shutdown_current_profile_import_job_runner,
            )

            await shutdown_current_profile_import_job_runner()
        except Exception:
            log.debug("gateway.profile_import_jobs_close_failed", exc_info=True)
        if self.usage_backfill_task is not None:
            self.usage_backfill_task.cancel()
            try:
                await self.usage_backfill_task
            except (asyncio.CancelledError, Exception):
                pass
            self.usage_backfill_task = None
        sandbox_setup_task = self.sandbox_setup_task
        self.sandbox_setup_task = None
        if sandbox_setup_task is not None:
            cancel = getattr(sandbox_setup_task, "cancel", None)
            if callable(cancel):
                cancel()
            if inspect.isawaitable(sandbox_setup_task):
                try:
                    await sandbox_setup_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.debug("gateway.sandbox_setup_close_failed", exc_info=True)
        remove_compaction_listener = getattr(self, "_compaction_listener_remove", None)
        if callable(remove_compaction_listener):
            try:
                remove_compaction_listener()
            except Exception:
                pass
            self._compaction_listener_remove = None

        remove_approval_listener = getattr(self, "_approval_listener_remove", None)
        if callable(remove_approval_listener):
            try:
                remove_approval_listener()
            except Exception:
                pass
            self._approval_listener_remove = None

        remove_approval_notifier = getattr(self, "_approval_channel_notifier_remove", None)
        if callable(remove_approval_notifier):
            try:
                remove_approval_notifier()
            except Exception:
                pass
            self._approval_channel_notifier_remove = None

        # ── 1. Stop scheduled producers (no further writes after this) ──
        daily_usage_task = self.daily_usage_telemetry_task
        self.daily_usage_telemetry_task = None
        if daily_usage_task is not None:
            daily_usage_task.cancel()
            try:
                await daily_usage_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("gateway.usage_telemetry_close_failed", exc_info=True)
        if self.prompt_cache_keepalive_service is not None:
            try:
                await self.prompt_cache_keepalive_service.close()
            except Exception:
                pass
            self.prompt_cache_keepalive_service = None
        if self.heartbeat_watcher is not None:
            try:
                await self.heartbeat_watcher.stop()
            except Exception:
                pass
        if self.heartbeat_loop is not None:
            try:
                await self.heartbeat_loop.stop()
            except Exception:
                pass
        if self.cron_scheduler is not None:
            try:
                await self.cron_scheduler.stop()
            except Exception:
                pass
            store = getattr(self.cron_scheduler, "_store", None)
            if store is not None and hasattr(store, "close"):
                try:
                    await store.close()
                except Exception:
                    pass
        if self.goal_service is not None:
            try:
                await self.goal_service.close()
            except Exception:
                log.debug("gateway.goal_service_close_failed", exc_info=True)
            self.goal_service = None
        if self.task_runtime is not None:
            try:
                await self.task_runtime.shutdown()
            except Exception:
                pass
            try:
                from opensquilla.tools.builtin.sessions import set_task_runtime

                set_task_runtime(None)
            except Exception:
                pass

        if self.usage_event_sink is not None:
            try:
                await self.usage_event_sink.close()
            except Exception:
                pass

        if self.memory_repair_service is not None:
            try:
                await self.memory_repair_service.stop()
            except Exception:
                pass
        if self.router_calibration_service is not None:
            # Stop the 24h job before its writer is closed below.
            try:
                await self.router_calibration_service.stop()
            except Exception:
                pass
        if self.meta_run_writer is not None:
            try:
                await asyncio.to_thread(self.meta_run_writer.close)
            except Exception:
                pass
        if self.router_decision_writer is not None:
            # Unregister the process-wide hook before closing so a torn-down
            # container cannot leave the router step handing records to a
            # closed connection (same pattern as set_shared_catalog below).
            try:
                from opensquilla.engine.steps.router_decision_record import (
                    drain_pending_flushes,
                    set_decision_writer,
                )

                set_decision_writer(None)
                # Turns finishing near shutdown may still hold in-flight
                # fire-and-forget flush tasks; give them a moment to land
                # before the connection goes away.
                await drain_pending_flushes()
            except Exception:
                pass
            try:
                await asyncio.to_thread(self.router_decision_writer.close)
            except Exception:
                pass
        if self.turn_error_writer is not None:
            try:
                await asyncio.to_thread(self.turn_error_writer.close)
            except Exception:
                pass
        try:
            from opensquilla.gateway.auto_propose_bridge import reset_runtime

            reset_runtime()
        except Exception:
            pass
        # build_services() installs the sandbox runtime process-wide. Clear it
        # with the rest of this container's shared services so a later gateway
        # (or an in-process caller) cannot inherit stale Full Host semantics.
        try:
            from opensquilla.sandbox.integration import reset_runtime as reset_sandbox_runtime
            from opensquilla.sandbox.setup_runtime import (
                reset_sandbox_setup_runtime_state,
            )

            reset_sandbox_runtime()
            reset_sandbox_setup_runtime_state()
        except Exception:
            pass
        # Clear the shared catalog installed by build_services() so a torn-down
        # container does not keep serving its (possibly warmed) catalog to
        # module-level consumers; they revert to the cold-fallback semantics.
        try:
            from opensquilla.provider.model_catalog import set_shared_catalog

            set_shared_catalog(None)
        except Exception:
            pass

        # ── 2. Tear down memory tier through MemoryManager ──
        # In real boot, the legacy `memory_watchers` / `memory_stores` below
        # are the SAME object identities as those reachable via memory_managers,
        # so the subsequent loops are no-op double-stops/closes (both sync_manager
        # and store close are idempotent — see memory/store.py:642 and
        # memory/sync_manager.py:104). Direct ServiceContainer constructors that
        # only populate the legacy fields (e.g. tests) still get torn down by the
        # legacy paths.
        #
        # Retrievers run BEFORE managers so any in-flight search cleanup runs
        # before the underlying DB connection is closed. Per-retriever timeout
        # prevents one wedged retriever from stalling the entire shutdown.
        for retriever in self.memory_retrievers.values():
            try:
                await asyncio.wait_for(retriever.close(), timeout=5.0)
            except (TimeoutError, Exception) as e:  # noqa: BLE001 — fail-open shutdown
                log.warning("retriever_close_failed_or_timed_out", error=str(e))
        for mgr in self.memory_managers.values():
            try:
                await mgr.close()
            except Exception:
                pass
        for watcher in self.memory_watchers:
            try:
                await watcher.stop()
            except Exception:
                pass
        for store in self.memory_stores.values():
            try:
                await store.close()
            except Exception:
                pass
        if self.session_manager is not None:
            storage = get_session_storage(self.session_manager)
            if storage and hasattr(storage, "close"):
                try:
                    await storage.close()
                except Exception:
                    pass


# Server boot timestamp (set once at first start)
_boot_time_ms: int = 0
# Per-boot identity token: lets clients tell "same process, config changed"
# (a pending restart is still pending) from "process restarted" (apply it).
_boot_id: str = ""


def _configured_agent_ids(
    config: GatewayConfig,
    extra: list[str] | None = None,
) -> list[str]:
    """Return agent ids declared by config plus the default main agent.

    ``extra`` lets a caller (e.g. the one-shot CLI runner) opt in additional
    runtime agent ids that are not declared in ``config.channels`` so the
    memory manager / workspace seeding still build per-agent resources for
    them. Legacy ``default`` aliases to the canonical ``main`` agent.
    """
    from opensquilla.session.keys import normalize_agent_id

    declared = {
        normalize_agent_id(getattr(e, "agent_id", "main")) for e in config.channels.channels
    }
    declared.add("main")
    for entry in getattr(config, "agents", []):
        if getattr(entry, "enabled", True):
            declared.add(normalize_agent_id(getattr(entry, "id", "")))
    if extra:
        declared.update(normalize_agent_id(a) for a in extra if a)
    return sorted(declared)


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolved_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None


def _warn_workspace_state_mismatch(config: GatewayConfig) -> None:
    workspace = _resolved_path(getattr(config, "workspace_dir", None))
    if workspace is None:
        return

    expected_roots: dict[str, Path] = {}
    env_state = _resolved_path(os.environ.get("OPENSQUILLA_STATE_DIR"))
    if env_state is not None:
        expected_roots["OPENSQUILLA_STATE_DIR"] = env_state
    env_config = _resolved_path(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    if env_config is not None:
        expected_roots["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = env_config.parent
    config_state = _resolved_path(getattr(config, "state_dir", None))
    if config_state is not None:
        expected_roots["config.state_dir"] = config_state.parent
    config_path = _resolved_path(getattr(config, "config_path", None))
    if config_path is not None:
        expected_roots["config.config_path"] = config_path.parent

    mismatches = {
        source: str(root)
        for source, root in expected_roots.items()
        if not _path_is_relative_to(workspace, root)
    }
    if not mismatches:
        return
    log.warning(
        "build_services.workspace_state_mismatch",
        workspace=str(workspace),
        state_dir=getattr(config, "state_dir", None),
        config_path=getattr(config, "config_path", None),
        expected_roots=mismatches,
    )


def _warn_legacy_home_detected(config: GatewayConfig) -> None:
    """One advisory log line when a fresh home boots beside importable legacy data.

    Fires only when this home holds no ``sessions.db`` yet (the same freshness
    expression the schema-migration block uses), so established installs never
    see it — and the detection import stays behind that check, keeping the
    established-install boot path free of it. Detection is read-only and the
    hint is log-only: migration itself stays behind ``opensquilla migrate
    opensquilla`` and Settings → Advanced → Data maintenance, so headless
    operators still learn their old profile exists without any prompt.
    """
    try:
        from opensquilla.persistence.migrator import _native_sqlite_path

        if os.path.isfile(_native_sqlite_path(_state_path(config, "sessions.db"))):
            return
    except OSError:  # pragma: no cover - unreadable state dir; stay silent.
        return
    import importlib

    legacy_detect = importlib.import_module("opensquilla.migration.legacy_detect")

    candidate = legacy_detect.detect_legacy_home(_gateway_home(config))
    if candidate is None:
        return
    log.warning(
        "build_services.legacy_home_detected",
        legacy_home=str(candidate.path),
        kind=candidate.kind,
        detail=(
            "This profile is empty but a previous OpenSquilla home with "
            "importable data was found. Import it with `opensquilla migrate "
            "opensquilla` or from Settings → Advanced → Data maintenance."
        ),
    )


def _ensure_configured_agent_workspaces(
    config: GatewayConfig,
    *,
    extra_agent_ids: list[str] | None = None,
) -> None:
    """Seed bootstrap templates for explicitly configured agent workspaces."""
    if not config.workspace_dir:
        return

    from opensquilla.identity.bootstrap import ensure_agent_workspace

    for agent_id in _configured_agent_ids(config, extra_agent_ids):
        result = ensure_agent_workspace(resolve_agent_workspace_dir(agent_id, config))
        log.info(
            "build_services.agent_workspace_ready",
            agent_id=agent_id,
            workspace=str(result.workspace_dir),
            created_files=list(result.created_files),
            bootstrap_seeded=result.bootstrap_seeded,
            bootstrap_completed=result.bootstrap_completed,
        )


def _state_path(config: GatewayConfig, filename: str) -> Path:
    state_root = Path(config.state_dir or default_opensquilla_home() / "state")
    return state_root / filename


def _gateway_home(config: GatewayConfig) -> Path:
    state_root = _resolved_path(getattr(config, "state_dir", None))
    if state_root is not None:
        return state_root.parent

    config_path = _resolved_path(getattr(config, "config_path", None))
    if config_path is not None:
        return config_path.parent

    return default_opensquilla_home()


def _desktop_ownership_profile_home(config: GatewayConfig) -> Path:
    """Return the Desktop profile root independently of its runtime state override."""

    config_path = _resolved_path(getattr(config, "config_path", None))
    if config_path is not None:
        return config_path.parent
    default_home = default_opensquilla_home()
    return _resolved_path(str(default_home)) or default_home.absolute()


async def _ensure_sandbox_setup_on_boot(config: GatewayConfig) -> Any | None:
    """Inspect sandbox readiness without elevating during gateway startup."""

    if not config.sandbox.auto_setup:
        log.info("boot.sandbox_setup_auto_disabled")
        return None

    from opensquilla.sandbox.setup_runtime import (
        current_sandbox_capability_report,
        current_sandbox_setup_runtime_status,
    )

    result = await current_sandbox_setup_runtime_status(config)
    log.info(
        "boot.sandbox_setup_status_completed",
        state=result.state.value,
        platform=result.platform,
        requires_admin=result.requires_admin,
        detail=result.detail,
    )
    if result.state.value == "ready":
        try:
            capability = await current_sandbox_capability_report(config)
            log.info(
                "boot.sandbox_capability_prewarm_completed",
                available=getattr(capability, "available", False),
                backend=getattr(capability, "backend", ""),
                code=getattr(capability, "code", ""),
            )
        except Exception as exc:  # noqa: BLE001 - startup prewarm is best-effort.
            log.warning(
                "boot.sandbox_capability_prewarm_failed",
                error=str(exc),
            )
    else:
        log.info(
            "boot.sandbox_setup_deferred",
            state=result.state.value,
            platform=result.platform,
        )
    return result


def _sandbox_settings_for_runtime(config: GatewayConfig) -> Any:
    """Return sandbox settings normalized to the config-level run mode."""

    from opensquilla.run_mode import (
        RunMode,
        config_run_mode,
        run_mode_config_patch,
        sandbox_runtime_capability_mode,
    )

    configured = config_run_mode(config)
    if configured in {RunMode.SAFE, RunMode.SAFE}:
        return config.sandbox

    patch = run_mode_config_patch(sandbox_runtime_capability_mode(config))
    return config.sandbox.model_copy(
        update={
            "run_mode": patch.run_mode.value,
            "sandbox": patch.sandbox,
            "security_grading": patch.security_grading,
            "network_default": patch.network_default,
        }
    )


def _task_runtime_max_concurrency(config: GatewayConfig) -> int:
    return int(config.task_runtime.max_concurrency)


def _task_runtime_max_pending_per_session(config: GatewayConfig) -> int:
    return int(config.task_runtime.max_pending_per_session)


def _task_runtime_turn_hard_deadline_s(config: GatewayConfig) -> float | None:
    configured = getattr(config.task_runtime, "turn_hard_deadline_s", None)
    if configured is None:
        return None
    return float(configured)


def _task_runtime_envelope_owner(envelope: Any) -> bool:
    """Resolve owner privileges from authenticated route metadata."""
    from opensquilla.channels.admission import has_verified_channel_admin_stamp
    from opensquilla.gateway.routing import SourceKind

    if getattr(envelope, "source_kind", None) == SourceKind.CHANNEL:
        return has_verified_channel_admin_stamp(envelope)
    principal_is_owner = getattr(envelope, "metadata", {}).get("principal_is_owner")
    if isinstance(principal_is_owner, bool):
        return principal_is_owner
    return getattr(envelope, "source_kind", None) == SourceKind.CLI


def _task_runtime_envelope_host_execute(envelope: Any) -> bool:
    """Resolve host-execution authority without widening owner privileges."""
    from opensquilla.gateway.routing import (
        PRINCIPAL_HOST_EXECUTE_METADATA_KEY,
        SourceKind,
    )

    if getattr(envelope, "source_kind", None) == SourceKind.CHANNEL:
        return _task_runtime_envelope_owner(envelope)
    principal_host_execute = getattr(envelope, "metadata", {}).get(
        PRINCIPAL_HOST_EXECUTE_METADATA_KEY
    )
    if isinstance(principal_host_execute, bool):
        return principal_host_execute
    return _task_runtime_envelope_owner(envelope)


async def dispatch_task_runtime_turn(
    run: Any,
    *,
    config: Any,
    session_manager: Any,
    turn_runner: Any,
    event_emitter: Any,
) -> None:
    """Drive ``turn_runner.run`` for one ``TaskRun``.

    Pure coroutine extracted from ``build_services``'s
    ``_task_runtime_turn_handler`` closure. Module-level so a
    boot-wiring regression test can drive it with a fake ``turn_runner``
    and capture every kwarg actually flowing into ``turn_runner.run``
    (including the ``semantic_message`` regression surface).
    """
    from opensquilla.gateway.project_workspace_runtime import (
        apply_accepted_run_mode_override,
        apply_run_context_route_metadata,
        authoritative_project_run_context,
        map_project_workspace_error,
    )
    from opensquilla.gateway.routing import tool_context_from_envelope
    from opensquilla.project_workspaces import ProjectWorkspaceStateError

    workspace_dir: Path | str | None = resolve_agent_workspace_dir(run.agent_id, config)
    session = None
    storage = get_session_storage(session_manager)
    if storage is not None:
        session = await storage.get_session(run.session_key)
        if session is None:
            raise KeyError(f"Session not found: {run.session_key}")
        try:
            run_context, _workspace_guard = await authoritative_project_run_context(
                storage=storage,
                session_manager=session_manager,
                session=session,
                config=config,
                default_workspace=(str(workspace_dir) if workspace_dir is not None else None),
            )
        except ProjectWorkspaceStateError as exc:
            mapped = map_project_workspace_error(
                exc,
                owner=_task_runtime_envelope_owner(run.envelope),
            )
            await event_emitter(
                run.session_key,
                "session.event.error",
                {
                    "message": mapped.message,
                    "code": mapped.code,
                    "details": mapped.details,
                    "task_id": getattr(run, "task_id", None),
                },
            )
            raise
        run_context = apply_accepted_run_mode_override(
            run_context,
            getattr(run, "accepted_run_mode_override", None),
        )
        apply_run_context_route_metadata(
            run.envelope,
            run_context,
            principal_is_owner=_task_runtime_envelope_owner(run.envelope),
        )
        if run_context.workspace is not None:
            workspace_dir = run_context.workspace
    workspace_strict = getattr(config, "workspace_strict", None)
    if not isinstance(workspace_strict, bool):
        workspace_strict = bool(workspace_dir)
    is_owner = _task_runtime_envelope_owner(run.envelope)
    tool_context = tool_context_from_envelope(
        run.envelope,
        is_owner=is_owner,
        host_execute_allowed=_task_runtime_envelope_host_execute(run.envelope),
        workspace_dir=(str(workspace_dir) if workspace_dir is not None else None),
        workspace_strict=workspace_strict,
        default_elevated=configured_default_elevated(config),
    )
    from opensquilla.sandbox.policy_store import pin_sandbox_policy

    pin_sandbox_policy(tool_context, config)
    tool_context.task_id = run.task_id
    run_metadata = getattr(run.envelope, "metadata", {})
    parent_session_key = run_metadata.get("parent_session_key")
    parent_task_id = run_metadata.get("parent_task_id")
    tool_context.parent_session_key = (
        parent_session_key if isinstance(parent_session_key, str) else None
    )
    tool_context.parent_task_id = parent_task_id if isinstance(parent_task_id, str) else None
    if (
        session is None
        and session_manager is not None
        and hasattr(
            session_manager,
            "get_session",
        )
    ):
        session = await session_manager.get_session(run.session_key)
    run_kwargs = build_task_runtime_run_kwargs(
        run,
        tool_context=tool_context,
        model=resolve_agent_model(
            run.agent_id,
            config,
            session_model=getattr(session, "model", None),
        ),
    )
    from opensquilla.engine.runtime import accepted_turn_config_scope

    raw_stream_idle_timeout = effective_agent_stream_idle_timeout_seconds(config)
    stream_idle_timeout: float | None = (
        raw_stream_idle_timeout if raw_stream_idle_timeout > 0 else None
    )
    heartbeat_interval = _optional_positive_timeout(
        config, "agent_stream_heartbeat_interval_seconds", 15.0
    )
    from opensquilla.engine.stream_wrappers import is_context_bound_owner

    try:
        with accepted_turn_config_scope(getattr(run, "accepted_config", None)):
            raw_stream = turn_runner.run(run.message, run.session_key, **run_kwargs)
            await _emit_task_runtime_stream_events(
                raw_stream,
                run.session_key,
                event_emitter,
                idle_timeout=stream_idle_timeout,
                heartbeat_interval=heartbeat_interval,
                context_bound=is_context_bound_owner(turn_runner),
                stream_event_sink=getattr(run, "stream_event_sink", None),
                task_id=getattr(run, "task_id", None),
                session_id=getattr(run.envelope, "session_id", None),
                client_message_id=getattr(run.envelope, "metadata", {}).get("client_message_id"),
                user_message_id=getattr(run, "persisted_user_message_id", None),
                surface_id=getattr(run.envelope, "metadata", {}).get("surface_id"),
                input_mode=getattr(run, "input_mode", "user"),
                run_kind=getattr(run, "run_kind", None),
            )
            finalizer_receipt_sink = getattr(run, "finalizer_receipt_sink", None)
            if finalizer_receipt_sink is not None:
                try:
                    finalizer_receipt_sink()
                except Exception:
                    log.warning(
                        "task_runtime.finalizer_receipt_failed",
                        session_key=run.session_key,
                        task_id=getattr(run, "task_id", None),
                        exc_info=True,
                    )
    except TaskRuntimeStreamError as exc:
        if exc.code in {
            "provider_request_budget_exhausted",
            "provider_request_too_large",
            "current_turn_context_exhausted",
        } and (
            str(getattr(run.envelope, "metadata", {}).get("turn_context_intent") or "")
            != "goal_set"
        ):
            rollback_reason = exc.code
            remove_message = getattr(session_manager, "remove_message", None)
            raw_message_ids = getattr(run, "persisted_user_message_ids", ())
            message_ids: list[str] = []
            for message_id in (
                getattr(run, "persisted_user_message_id", None),
                *(raw_message_ids if isinstance(raw_message_ids, list | tuple) else ()),
            ):
                if isinstance(message_id, str) and message_id and message_id not in message_ids:
                    message_ids.append(message_id)

            async def _remove_persisted_messages() -> None:
                assert callable(remove_message)
                for persisted_message_id in message_ids:
                    try:
                        removed = remove_message(
                            run.session_key,
                            persisted_message_id,
                        )
                        if inspect.isawaitable(removed):
                            removed = await removed
                        if removed:
                            log.info(
                                "task_runtime.user_message_rolled_back",
                                session_key=run.session_key,
                                message_id=persisted_message_id,
                                reason=rollback_reason,
                            )
                    except Exception as rb_exc:  # noqa: BLE001 - preserve terminal error
                        # Try every collected id even if one best-effort cleanup
                        # fails; the provider error remains the terminal cause.
                        log.warning(
                            "task_runtime.user_message_rollback_failed",
                            session_key=run.session_key,
                            message_id=persisted_message_id,
                            reason=rollback_reason,
                            error=str(rb_exc),
                        )

            if callable(remove_message) and message_ids:
                rollback_lock = get_session_lock(turn_runner, run.session_key)
                if rollback_lock is None:
                    await _remove_persisted_messages()
                else:
                    async with rollback_lock:
                        await _remove_persisted_messages()
        raise


def build_session_material_cleanup(config: Any) -> Any:
    """Build the session-material cleanup that runs on ``delete_session``.

    Removes all on-disk material stores for a deleted session: the canonical
    transcript-material store (``<media_root>/transcripts/<sid>/``) and the
    tool-visible workspace materialization
    (``<workspace>/.opensquilla/attachments/<segment>/``), plus every ArtifactStore
    bucket owned by that session below ``<media_root>/artifacts``. Session deletion
    is a privacy and disk-reclamation boundary, so both listed and internal
    artifacts are removed. Lives in the gateway
    layer because it resolves the agent workspace via ``agents.scope``; the
    low-level ``session`` package only owns the hook registry + guarded remover.
    """
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.artifacts import ArtifactStore
    from opensquilla.attachment_refs import transcript_material_dir
    from opensquilla.attachment_workspace import _safe_path_segment
    from opensquilla.paths import media_root_from_config
    from opensquilla.session.keys import parse_agent_id
    from opensquilla.session.material_cleanup import rmtree_scoped

    async def _cleanup(session_id: str, session_key: str) -> None:
        # 1. Canonical transcript-material store (keyed by session_id, outside
        #    the workspace).
        media_root = media_root_from_config(config)
        rmtree_scoped(
            transcript_material_dir(media_root, session_id),
            expected_name=session_id,
        )
        # 2. Tool-visible workspace materialization (per-session segment under the
        #    per-agent workspace). Resolve the agent from the session key so the
        #    workspace matches where the material was written.
        agent_id = parse_agent_id(session_key)
        workspace = Path(resolve_agent_workspace_dir(agent_id, config))
        segment = _safe_path_segment(session_id, fallback="session")
        attachments_dir = workspace / ".opensquilla" / "attachments" / segment
        rmtree_scoped(attachments_dir, expected_name=segment)
        # 3. Every artifact owned by the deleted session, including listed
        #    chat artifacts, internal revisions/candidates, bundle blobs, and
        #    legacy layouts. ArtifactStore owns the scoped layout and link guards.
        ArtifactStore(media_root).delete_session_artifacts(session_id)

    return _cleanup


def build_session_artifact_cleanup(config: Any) -> Any:
    """Build post-reset cleanup for internal ArtifactSession material only."""

    from opensquilla.artifacts import ArtifactStore
    from opensquilla.paths import media_root_from_config

    async def _cleanup(session_id: str, _session_key: str) -> None:
        ArtifactStore(media_root_from_config(config)).delete_session_internal_artifacts(
            session_id
        )

    return _cleanup


def build_task_runtime_run_kwargs(
    run: Any,
    *,
    tool_context: Any,
    model: str | None,
) -> dict[str, Any]:
    """Build kwargs for ``turn_runner.run`` from a ``TaskRun``.

    Pure helper extracted from ``_task_runtime_turn_handler`` so the
    boot-level link of semantic message forwarding is directly
    testable: a regression that drops ``semantic_message`` forwarding
    here is caught by ``test_boot_task_runtime_kwargs.py`` without
    requiring a live gateway.
    """
    ingress_steps = list(run.ingress_pipeline_steps) or None
    kwargs: dict[str, Any] = {
        "tool_context": tool_context,
        "agent_id": run.agent_id,
        "model": model,
        "attachments": run.attachments,
        "input_provenance": run.input_provenance,
        "run_kind": run.run_kind,
        "no_memory_capture": run.no_memory_capture,
        "input_mode": getattr(run, "input_mode", "user"),
        "persist_input": bool(getattr(run, "persist_input", False)),
        "history_has_persisted_user": bool(
            getattr(run, "history_has_persisted_user", True)
        ),
        "fresh_user_session": bool(getattr(run, "fresh_user_session", False)),
        "ingress_pipeline_steps": ingress_steps,
        "pending_input_provider": getattr(run, "pending_input_provider", None),
        "root_turn_id": getattr(run, "task_id", None),
    }
    if run.semantic_message is not None:
        # Prefetch query shape: channels carry the raw user text
        # separately from the (potentially stamped) persisted message.
        # Only forward when set so web/CLI legacy paths keep
        # ``TurnRunner.run`` falling back to ``message`` as semantic input.
        kwargs["semantic_message"] = run.semantic_message
    provider_request_correlation = getattr(
        run,
        "provider_request_correlation",
        None,
    )
    if provider_request_correlation is not None:
        kwargs["provider_request_correlation"] = provider_request_correlation
    bound_user_message_id = getattr(run, "persisted_user_message_id", None)
    if bound_user_message_id:
        # Bind history to the exact persisted user message this turn answers so
        # queued/follow-up sends cannot duplicate the current prompt or leak
        # unanswered future prompts into context. Forwarded only when present so
        # legacy callers/mocks without the field keep the positional trim.
        kwargs["bound_user_message_id"] = bound_user_message_id
    assistant_message_sink = getattr(run, "assistant_message_sink", None)
    if assistant_message_sink is not None:
        # Internal-only callback: the finalizer supplies the exact assistant
        # row/content to TaskRuntime for durable channel delivery.
        kwargs["assistant_message_sink"] = assistant_message_sink
    document_mutation_outcome_sink = getattr(
        run,
        "document_mutation_outcome_sink",
        None,
    )
    if document_mutation_outcome_sink is not None:
        kwargs["document_mutation_outcome_sink"] = document_mutation_outcome_sink
    return kwargs


def build_cron_result_payload(
    origin_session_key: str,
    text: str,
    entry: Any,
) -> dict[str, Any]:
    """Build the WS payload for a ``session.event.cron_result`` broadcast.

    Pure helper extracted from the cron-forwarder closure so the wire
    contract is testable by gate 4 without spinning up a live gateway.
    The web frontend at ``chat.js:727`` and any other ``cron_result``
    subscriber relies on these exact keys.
    """
    return {
        "sessionKey": origin_session_key,
        "message": {
            "role": "assistant",
            "text": text,
            "timestamp": getattr(entry, "created_at", None),
            "messageId": getattr(entry, "message_id", None),
            "provenanceKind": getattr(entry, "provenance_kind", None),
            "provenanceSourceTool": getattr(entry, "provenance_source_tool", None),
            "provenanceSourceSessionKey": getattr(entry, "provenance_source_session_key", None),
        },
    }


def _task_run_status_for_session_change(event: TaskLifecycleEvent) -> str | None:
    if event.task_snapshot is not None:
        active_task = event.task_snapshot.active_task
        if active_task is not None:
            return active_task["status"]
        if event.phase in {"queued", "running"}:
            # This is a delayed lifecycle callback for work that has already
            # left the runtime ledger. Do not regress the current run status.
            return None
    status = getattr(event.task_status, "value", str(event.task_status))
    if event.phase == "queued":
        return "queued"
    if event.phase == "running":
        return "running"
    if event.continuation_task_id:
        return "queued"
    if status == "succeeded":
        return "idle"
    if status == "abandoned":
        return "interrupted"
    if status in {"failed", "timeout", "cancelled"}:
        return status
    return "idle"


def _task_state_for_session_change(event: TaskLifecycleEvent) -> dict[str, Any]:
    status = getattr(event.task_status, "value", str(event.task_status))
    task: dict[str, Any] = {
        "task_id": event.task_id,
        "status": "running" if event.phase == "running" else status,
    }
    if event.terminal_reason:
        task["terminal_reason"] = event.terminal_reason
    if event.phase == "terminal" and status != "succeeded":
        task["terminal_message"] = build_terminal_reply(
            {
                "status": status,
                "terminal_reason": event.terminal_reason,
                "error_class": event.error_class,
                "error_message": event.error_message,
            }
        )
    return task


def _make_task_session_lifecycle_listener(
    *,
    session_manager: Any,
    event_emitter: Any,
) -> Any:
    async def _listener(event: TaskLifecycleEvent) -> None:
        if event.run_kind == "subagent":
            return
        task_state = _task_state_for_session_change(event)
        changed = await apply_task_lifecycle_to_session(
            event,
            session_manager=session_manager,
        )
        if not changed:
            return
        if event.task_snapshot is None:
            # A callback identifies only the task that changed, not the
            # session's foreground owner. When the authoritative runtime
            # snapshot is unavailable, publish that limited fact and leave the
            # active/run projection untouched.
            await event_emitter(
                event.session_key,
                "sessions.changed",
                build_sessions_changed_payload(
                    event.session_key,
                    (
                        "task_queued"
                        if event.phase == "queued"
                        else "task_running"
                        if event.phase == "running"
                        else "task_terminal"
                    ),
                    changed_task=task_state,
                ),
            )
            return
        reason = (
            "task_queued"
            if event.phase == "queued"
            else "task_running"
            if event.phase == "running"
            else "task_terminal"
        )
        session_status = session_status_for_task_status(event.task_status)
        active_task = event.task_snapshot.active_task
        if active_task is None and event.continuation_task_id:
            # Shutdown recovery can durably promote a successor without
            # activating it into this process. The continuation id is then the
            # only authoritative queued owner and preserves the established
            # handoff contract.
            active_task = {
                "task_id": event.continuation_task_id,
                "status": "queued",
            }
        if event.phase == "terminal" and active_task is not None:
            session_status = SessionStatus.RUNNING
            task_projection = {
                "last_task": task_state,
                "active_task": active_task,
            }
        elif event.phase == "terminal":
            task_projection = {"last_task": task_state}
        elif active_task is not None:
            task_projection = {"active_task": active_task}
        else:
            task_projection = {}
        if (
            event.phase == "queued"
            and event.task_id in event.task_snapshot.queued_task_ids
            and (
                active_task is None
                or active_task.get("task_id") != event.task_id
                or active_task.get("status") != "queued"
            )
        ):
            task_projection["changed_task"] = task_state
        await event_emitter(
            event.session_key,
            "sessions.changed",
            build_sessions_changed_payload(
                event.session_key,
                reason,
                status=getattr(session_status, "value", session_status),
                run_status=(
                    active_task["status"]
                    if active_task is not None
                    else _task_run_status_for_session_change(event)
                ),
                **task_projection,
            ),
        )

    return _listener


def _optional_positive_timeout(config: Any, attr: str, default: float) -> float | None:
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


async def _emit_task_runtime_stream_events(
    raw_stream: Any,
    session_key: str,
    event_emitter: Any,
    *,
    idle_timeout: float | None = 180.0,
    heartbeat_interval: float | None = None,
    context_bound: bool | None = None,
    stream_event_sink: Any = None,
    task_id: str | None = None,
    session_id: str | None = None,
    client_message_id: str | None = None,
    user_message_id: str | None = None,
    surface_id: str | None = None,
    input_mode: str | None = None,
    run_kind: str | None = None,
) -> None:
    """Emit turn events and fail the task if the stream reports an error.

    ``task_id`` is stamped onto every emitted ``session.event.*`` payload so
    the WebUI can bind the live stream to a single turn. Without it, a stale
    task's late ``tool_use_start`` / ``error`` / ``done`` events are
    indistinguishable from the current turn's and leak into it (issue #344).
    """
    from dataclasses import asdict, is_dataclass

    from opensquilla.engine.stream_wrappers import wrap_stream

    error_message: str | None = None
    error_code: str | None = None
    failure_kind: str | None = None
    terminal_reason: str | None = None
    retry_after_ms: int | None = None
    activity_phases: list[dict[str, Any]] = []
    activity_snapshot: dict[str, Any] | None = None
    replay_proof: dict[str, Any] = {
        "no_prior_provider_dispatch": False,
        "replay_safe": False,
    }
    primary_user_message_id = safe_primary_user_message_id(user_message_id)
    async for event in wrap_stream(
        raw_stream,
        idle_timeout=idle_timeout,
        heartbeat_interval=heartbeat_interval,
        heartbeat_message="Agent run is still active",
        context_bound=context_bound,
    ):
        if is_dataclass(event):
            event_dict = asdict(event)
        else:
            event_dict = {
                key: value
                for key, value in getattr(event, "__dict__", {}).items()
                if not key.startswith("_")
            }
        event_kind = event_dict.pop("kind", getattr(event, "kind", event.__class__.__name__))
        if event_kind == "answer_generation_reset" and event_dict.get("terminal") is True:
            # A terminal generation reset is the canonical visible outcome
            # when the fixed provider also fails.  It intentionally replaces
            # the speculative answer without emitting a second public Error,
            # but TaskRuntime must still classify the turn as failed instead
            # of treating a clean stream EOF as success.
            raw_terminal_text = event_dict.get("terminal_text_snapshot")
            raw_authoritative_text = event_dict.get("authoritative_text_snapshot")
            error_message = (
                raw_terminal_text
                if isinstance(raw_terminal_text, str) and raw_terminal_text
                else raw_authoritative_text
                if isinstance(raw_authoritative_text, str) and raw_authoritative_text
                else "The model could not complete this answer."
            )
            raw_terminal_code = event_dict.get("terminal_error_code")
            error_code = (
                str(raw_terminal_code)
                if isinstance(raw_terminal_code, str) and raw_terminal_code
                else "ensemble_fixed_error"
            )
            raw_terminal_failure_kind = event_dict.get("terminal_failure_kind")
            failure_kind = (
                str(raw_terminal_failure_kind)
                if isinstance(raw_terminal_failure_kind, str)
                and raw_terminal_failure_kind
                else None
            )
            terminal_reason = "error"
            event_dict.setdefault("terminal_reason", terminal_reason)
        if event_kind == "answer_generation_reset":
            # These fields exist only to carry a typed failed outcome through
            # the in-process finalizer contract.  The reset snapshot is the
            # complete public payload; never expose internal failure metadata
            # through either the session-event emitter or a relay sink. Scrub
            # normal and terminal resets alike.
            event_dict.pop("terminal_error_message", None)
            event_dict.pop("terminal_error_code", None)
            event_dict.pop("terminal_failure_kind", None)
        if event_kind == "thinking" and not event_dict.get("block_id"):
            # Preserve the exact legacy payload for producers that still
            # construct an unscoped ThinkingEvent.
            event_dict.pop("block_id", None)
            event_dict.pop("block_index", None)
        append_activity_phase(
            activity_phases,
            event_kind=event_kind,
            payload=event_dict,
            observed_at_ms=int(time.time() * 1_000),
        )
        if event_kind == "artifact":
            event_dict = enrich_artifact_event_dict(event_dict)
        if event_kind == "error":
            raw_message = event_dict.get("message")
            error_message = (
                raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
            )
            code = event_dict.get("code")
            error_code = str(code) if code else None
            raw_retry_after_ms = event_dict.pop("retry_after_ms", None)
            raw_usage_call_index = event_dict.pop("usage_call_index", None)
            raw_no_prior_provider_dispatch = event_dict.pop(
                "no_prior_provider_dispatch", None
            )
            raw_replay_safe = event_dict.pop("replay_safe", None)
            # Keep the normalized provider classification internal to the
            # durable task outcome; it is not part of the public stream event.
            raw_failure_kind = event_dict.pop("failure_kind", None)
            failure_kind = (
                str(raw_failure_kind) if isinstance(raw_failure_kind, str) else None
            )
            if failure_kind:
                error_message = safe_provider_failure_message(failure_kind)
                error_code = safe_provider_failure_code(error_code, failure_kind)
                event_dict["code"] = error_code
                code = error_code
            code_text = str(code or "").lower()
            is_timeout = "timeout" in code_text or "stream idle" in error_message.lower()
            is_output_truncated = code_text == "provider_output_truncated"
            if is_timeout:
                terminal_reason = "timeout"
            elif is_output_truncated:
                terminal_reason = "output_truncated"
            elif code_text == "model_repetition_loop_detected":
                terminal_reason = "model_repetition_loop_detected"
            else:
                terminal_reason = "error"
            terminal_payload = {
                "status": "timeout" if is_timeout else "failed",
                "terminal_reason": terminal_reason,
                "error_class": code,
                "error_message": error_message,
            }
            safe_error_code, safe_error_message = sanitize_agent_error(
                terminal_payload,
                fallback_error_class=error_code,
                fallback_error_message=error_message,
            )
            if safe_error_code == "provider_request_too_large":
                error_code = safe_error_code
                event_dict["code"] = safe_error_code
                terminal_payload["error_class"] = safe_error_code
                terminal_payload["error_message"] = safe_error_message
            if is_usage_accounting_barrier(error_code):
                replay_proof = usage_barrier_replay_proof(
                    usage_call_index=raw_usage_call_index,
                    no_prior_provider_dispatch=raw_no_prior_provider_dispatch,
                    replay_safe=raw_replay_safe,
                )
                terminal_payload.update(replay_proof)
            terminal_message = build_terminal_reply(terminal_payload)
            # Additive ref suffix joining the reply to its durable turn_errors
            # row; absent when no record was written (error_id empty).
            event_error_id = event_dict.get("error_id")
            if isinstance(event_error_id, str) and event_error_id:
                terminal_message = append_error_ref(terminal_message, event_error_id)
            event_dict["message"] = terminal_message
            event_dict["terminal_message"] = terminal_message
            event_dict["terminal_reason"] = terminal_payload["terminal_reason"]
            event_dict["error_message"] = safe_error_message
            # Preserve the stable provider taxonomy inside the typed outcome,
            # without exposing a second raw top-level field. Clients can now
            # offer an explicit retry for transient terminal failures even
            # when a Retry-After hint exceeded the remaining turn deadline.
            if failure_kind or is_usage_accounting_barrier(error_code):
                from opensquilla.engine.outcome import outcome_from_error

                outcome = outcome_from_error(
                    code=error_code,
                    message=safe_error_message,
                    error_class=error_code,
                    failure_kind=failure_kind,
                ).to_dict()
                if is_usage_accounting_barrier(error_code):
                    retry_after_ms = safe_retry_after_ms(raw_retry_after_ms)
                    if retry_after_ms is not None:
                        outcome["retry_after_ms"] = retry_after_ms
                        event_dict["retry_after_ms"] = retry_after_ms
                    event_dict["error_class"] = error_code
                    event_dict["retryable"] = True
                    event_dict.update(replay_proof)
                    outcome.update(replay_proof)
                    if primary_user_message_id is not None:
                        outcome["user_message_id"] = primary_user_message_id
                    if task_id:
                        activity_snapshot = terminal_activity_snapshot(
                            activity_phases,
                            task_id=task_id,
                            turn_id=task_id,
                        )
                        if activity_snapshot is not None:
                            event_dict["activity_snapshot"] = activity_snapshot
                event_dict["turn_outcome"] = outcome
        if stream_event_sink is not None:
            # Internal stream relays normally consume only text/done/artifact
            # events. Still, project provider failures through the same safe
            # Gateway boundary before invoking an arbitrary sink: a sink that
            # logs or persists its input must never receive a raw upstream
            # body from an ErrorEvent.
            sink_event: Any = event
            if event_kind in {"error", "answer_generation_reset"}:
                sink_event = {"kind": event_kind, **event_dict}
            try:
                result = stream_event_sink(sink_event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.debug(
                    "task_runtime.stream_event_sink_failed",
                    session_key=session_key,
                    event_kind=event_kind,
                    exc_info=True,
                )
        if task_id:
            event_dict["task_id"] = task_id
            # Typed generation-reset events carry the engine-owned turn id.
            # Preserve it; ordinary legacy events still receive the runtime
            # task id as their turn identity.
            if not event_dict.get("turn_id"):
                event_dict["turn_id"] = task_id
        if session_id:
            event_dict["session_id"] = session_id
        if client_message_id:
            event_dict["client_message_id"] = client_message_id
        if primary_user_message_id is not None:
            event_dict["user_message_id"] = primary_user_message_id
        if surface_id:
            event_dict["surface_id"] = surface_id
        if input_mode:
            event_dict["input_mode"] = input_mode
        if run_kind:
            event_dict["run_kind"] = run_kind
        await event_emitter(
            session_key,
            f"session.event.{event_kind}",
            event_dict,
        )
        if event_kind == "error":
            message = event_dict.get("error_message")
            error_message = message if isinstance(message, str) and message else "Agent error"
    if error_message is not None:
        raise TaskRuntimeStreamError(
            error_message,
            code=error_code,
            terminal_reason=terminal_reason,
            failure_kind=failure_kind,
            retry_after_ms=retry_after_ms,
            activity_snapshot=activity_snapshot,
            usage_call_index=replay_proof.get("usage_call_index"),
            no_prior_provider_dispatch=(
                replay_proof.get("no_prior_provider_dispatch") is True
            ),
            replay_safe=replay_proof.get("replay_safe") is True,
        )


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _ENABLED_VALUES:
        return True
    if value in _DISABLED_VALUES:
        return False
    return None


def _resolve_log_level(config: GatewayConfig) -> int:
    raw = os.environ.get("OPENSQUILLA_LOG_LEVEL") or config.log_level
    return _LOG_LEVELS.get(str(raw).strip().upper(), logging.DEBUG)


def _remove_debug_file_handlers(root: logging.Logger) -> None:
    opensquilla_logger = logging.getLogger("opensquilla")
    for handler in list(root.handlers):
        if getattr(handler, _DEBUG_FILE_HANDLER_ATTR, False):
            previous_level = getattr(handler, "_opensquilla_previous_logger_level", None)
            root.removeHandler(handler)
            handler.close()
            if isinstance(previous_level, int):
                opensquilla_logger.setLevel(previous_level)


def _render_structlog_event_for_stdlib(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> tuple[tuple[str], dict[str, Any]]:
    """Final structlog processor: render ``event key=value ...`` for stdlib.

    Returns ``(args, kwargs)`` so ``exc_info``/``stack_info`` pass through to
    ``logging`` natively and the Formatter renders tracebacks into every
    attached handler (file and console).
    """
    kwargs: dict[str, Any] = {}
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        kwargs["exc_info"] = exc_info
    stack_info = event_dict.pop("stack_info", None)
    if stack_info:
        kwargs["stack_info"] = stack_info
    event = str(event_dict.pop("event", ""))
    parts = [event] + [f"{key}={event_dict[key]!r}" for key in event_dict]
    return (" ".join(part for part in parts if part),), kwargs


def _structlog_explicitly_configured() -> bool:
    """True when structlog carries an explicit configuration the bridge must respect.

    The CLI entry callback installs a stderr/WARNING structlog default for
    every command (``observability/cli_logging.py``) — including ``gateway
    run``, which reaches this bridge afterwards. That default is overridable:
    treating it as explicit would permanently disable the debug.log bridge for
    foreground gateway runs. Any *other* configuration (e.g. the interactive
    TUI's) is left untouched, matching the previous ``is_configured`` guard.
    """
    if not structlog.is_configured():
        return False
    try:
        from opensquilla.observability.cli_logging import is_cli_default_active
    except Exception:  # noqa: BLE001 - fall back to the historical guard
        return True
    return not is_cli_default_active()


def _bridge_structlog_to_stdlib() -> None:
    """Route structlog events through stdlib logging so they reach debug.log.

    Level filtering is delegated to stdlib (the ``opensquilla`` logger level is
    managed by ``_setup_file_logging``); the explicit-configuration guard
    leaves any prior non-CLI-default configuration (e.g. the interactive
    TUI's) untouched while overriding the CLI's stderr/WARNING default.
    """
    if _structlog_explicitly_configured():
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            _render_structlog_event_for_stdlib,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _remove_console_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _CONSOLE_HANDLER_ATTR, False):
            root.removeHandler(handler)
            handler.close()


def _setup_file_logging(config: GatewayConfig | None = None) -> None:
    """Configure structlog + stdlib logging to write to a debug.log file."""
    config = config or GatewayConfig()
    root = logging.getLogger()
    _remove_debug_file_handlers(root)
    _remove_console_handlers(root)

    # Bridge structlog through stdlib and keep console output for foreground
    # runs. Wrapped so a logging misconfiguration can never block gateway boot.
    bridge_error: Exception | None = None
    log_level = _resolve_log_level(config)
    try:
        _bridge_structlog_to_stdlib()
        console_handler = logging.StreamHandler(sys.stdout)
        setattr(console_handler, _CONSOLE_HANDLER_ATTR, True)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(console_handler)
    except Exception as exc:  # noqa: BLE001 - logging must never block boot
        bridge_error = exc
        logging.getLogger(__name__).warning("structlog bridge disabled: %s", exc)

    enabled = _env_bool("OPENSQUILLA_LOG_FILE_ENABLED")
    if enabled is None:
        enabled = config.log_file_enabled
    opensquilla_logger = logging.getLogger("opensquilla")
    if not enabled:
        # No file handler will manage the level, but bridged console output
        # still depends on it: left NOTSET, the "opensquilla" logger is
        # effectively WARNING and INFO/DEBUG never reach the console handler.
        opensquilla_logger.setLevel(log_level)
        return

    log_dir = Path(os.environ.get("OPENSQUILLA_LOG_DIR", str(default_opensquilla_home() / "logs")))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "debug.log"
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=config.log_file_max_bytes,
            backupCount=config.log_file_backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("file logging disabled: %s", exc)
        opensquilla_logger.setLevel(log_level)
        return
    setattr(file_handler, _DEBUG_FILE_HANDLER_ATTR, True)
    setattr(file_handler, "_opensquilla_previous_logger_level", opensquilla_logger.level)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    root.addHandler(file_handler)
    opensquilla_logger.setLevel(log_level)
    if bridge_error is not None:
        # The first warning fired before the file handler existed, so it only
        # reached the console; re-emit it so debug.log records it too.
        logging.getLogger(__name__).warning("structlog bridge disabled: %s", bridge_error)


@dataclass
class _GatewayShutdownRelay:
    """Accept shutdown requests before the CLI runner installs its handler."""

    _handler: Callable[[str], None] | None = field(default=None, repr=False)
    _pending_reason: str | None = field(default=None, repr=False)

    def __call__(self, reason: str) -> None:
        handler = self._handler
        if handler is not None:
            handler(reason)
        elif self._pending_reason is None:
            self._pending_reason = reason

    def install(self, handler: Callable[[str], None]) -> None:
        self._handler = handler
        pending_reason = self._pending_reason
        self._pending_reason = None
        if pending_reason is not None:
            handler(pending_reason)


def _consume_websocket_close_task(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def _cancel_and_detach_websocket_close_tasks(
    tasks: set[asyncio.Task[None]],
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_websocket_close_task)
        else:
            _consume_websocket_close_task(task)


async def _close_gateway_websocket_connections(connections: list[Any]) -> None:
    tasks: set[asyncio.Task[None]] = set()
    try:
        for index, connection in enumerate(connections):
            tasks.add(
                asyncio.create_task(
                    connection.close(),
                    name=f"gateway-ws-shutdown-close-{index}",
                )
            )
        if not tasks:
            return

        done, pending = await asyncio.wait(
            tasks,
            timeout=_WS_SHUTDOWN_CLOSE_TIMEOUT_S,
        )
        for task in done:
            task.result()
        if not pending:
            return

        log.warning(
            "gateway.ws_shutdown_close_timeout",
            pending_count=len(pending),
            timeout_seconds=_WS_SHUTDOWN_CLOSE_TIMEOUT_S,
        )
        for task in pending:
            task.cancel()
        cancelled, lingering = await asyncio.wait(
            pending,
            timeout=_WS_SHUTDOWN_CANCEL_GRACE_S,
        )
        for task in cancelled:
            _consume_websocket_close_task(task)
        for task in lingering:
            task.add_done_callback(_consume_websocket_close_task)
    except BaseException:
        _cancel_and_detach_websocket_close_tasks(tasks)
        raise


@dataclass
class GatewayServer:
    """Handle returned after gateway startup. Provides close() method."""

    app: Starlette
    config: GatewayConfig
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _preview_server: uvicorn.Server | None = field(default=None, repr=False)
    _preview_task: asyncio.Task | None = field(default=None, repr=False)
    _preview_socket: socket.socket | None = field(default=None, repr=False)
    _preview_service: Any = field(default=None, repr=False)
    _channel_manager: Any = field(default=None, repr=False)
    # Zero-arg resolver for the LIVE manager: live reconcile can create the
    # manager after boot (zero-channel start + first live add), so shutdown
    # must resolve through the holder, not the boot-time snapshot. Kept as a
    # separate field because tests inject (callable) mocks into
    # _channel_manager directly.
    _channel_manager_ref: Any = field(default=None, repr=False)
    _services: ServiceContainer | None = field(default=None, repr=False)
    _background_completion_manager: Any = field(default=None, repr=False)
    _pid_lock: Any = field(default=None, repr=False)

    def _release_pid_lock(self) -> None:
        pid_lock = self._pid_lock
        if pid_lock is None:
            return
        try:
            pid_lock.release()
        finally:
            self._pid_lock = None

    async def close(
        self,
        reason: str = "shutdown",
    ) -> TaskRuntimeShutdownResult | None:
        """Gracefully shut down: stop channels, broadcast shutdown, close WS, stop server."""
        runtime_shutdown_result: TaskRuntimeShutdownResult | None = None
        runtime_shutdown_clean = bool(
            self._services is None
            or getattr(self._services, "task_runtime", None) is None
        )
        try:
            # Drain in-flight turns FIRST so replies are not lost. A bounded
            # shutdown can report residual drivers; in that case transports are
            # stopped below but stateful dependencies stay open until the process
            # watchdog terminates the Gateway.
            drain_budget = gateway_graceful_timeout()
            goal_service = (
                getattr(self._services, "goal_service", None)
                if self._services is not None
                else None
            )
            if goal_service is not None:
                try:
                    await goal_service.prepare_shutdown()
                except Exception:
                    log.debug("gateway.goal_service_shutdown_failed", exc_info=True)
            if self._services is not None and self._services.task_runtime is not None:
                try:
                    runtime_shutdown_result = await self._services.task_runtime.shutdown(
                        graceful=True, graceful_timeout=drain_budget
                    )
                    runtime_shutdown_clean = (
                        runtime_shutdown_result is None
                        or runtime_shutdown_result.clean
                    )
                except Exception:
                    runtime_shutdown_clean = False
                    runtime_shutdown_result = TaskRuntimeShutdownResult(
                        clean=False,
                        elapsed_ms=0,
                        abandoned_task_count=0,
                        remaining_driver_count=0,
                        remaining_reservation_count=0,
                        remaining_auxiliary_count=0,
                    )
                    log.exception("gateway.task_runtime_shutdown_failed")

            if runtime_shutdown_result is not None and not runtime_shutdown_result.clean:
                log.error(
                    "gateway.task_runtime_shutdown_incomplete",
                    elapsed_ms=runtime_shutdown_result.elapsed_ms,
                    abandoned_tasks=runtime_shutdown_result.abandoned_task_count,
                    remaining_drivers=runtime_shutdown_result.remaining_driver_count,
                    remaining_reservations=(
                        runtime_shutdown_result.remaining_reservation_count
                    ),
                    remaining_auxiliary=(
                        runtime_shutdown_result.remaining_auxiliary_count
                    ),
                )

            if self._background_completion_manager is not None and runtime_shutdown_clean:
                try:
                    await self._background_completion_manager.close(timeout=drain_budget)
                except Exception:
                    log.debug("gateway.background_completion_close_failed", exc_info=True)
                try:
                    from opensquilla.gateway.subagent_announce import (
                        set_background_completion_manager,
                    )

                    set_background_completion_manager(None)
                except Exception:
                    pass
                self._background_completion_manager = None
            elif self._background_completion_manager is not None:
                log.warning(
                    "gateway.background_completion_close_skipped_for_live_runtime"
                )

            # Stop channels after task_runtime is drained (no in-flight turns remain)
            live_channel_manager = self._channel_manager
            if live_channel_manager is None and self._channel_manager_ref is not None:
                live_channel_manager = self._channel_manager_ref()
            if live_channel_manager is not None:
                await live_channel_manager.stop_all()
                log.info("gateway.channels_stopped")

            registry = get_registry()
            await registry.broadcast("shutdown", {"reason": reason})

            # Close all active WS connections
            await _close_gateway_websocket_connections(registry.all())

            # Close MCP clients
            if runtime_shutdown_clean:
                try:
                    from opensquilla.mcp.discovery import close_active_clients

                    await close_active_clients()
                    log.info("gateway.mcp_clients_closed")
                except ImportError:
                    log.debug(
                        "gateway.mcp_clients_close_skipped_import_unavailable",
                        exc_info=True,
                    )
            else:
                log.warning("gateway.mcp_clients_close_skipped_for_live_runtime")

            if runtime_shutdown_clean:
                log.info("gateway.stopped", reason=reason)
            else:
                log.warning(
                    "gateway.transports_stopped_with_runtime_residuals",
                    reason=reason,
                )
        finally:
            # Always stop the serve task so it is never left pending, even when a
            # teardown step above raised (close() is now invoked on every shutdown,
            # not only on Ctrl+C, so the serve task is typically still running). A
            # teardown exception still propagates after this runs. The pid lock
            # is released only after a clean runtime shutdown; otherwise process
            # exit remains the ownership fence.
            try:
                if self._server is not None:
                    self._server.should_exit = True
                if self._task is not None:
                    try:
                        await asyncio.wait_for(self._task, timeout=5.0)
                    except TimeoutError:
                        self._task.cancel()
                preview_server = getattr(self, "_preview_server", None)
                preview_task = getattr(self, "_preview_task", None)
                preview_socket = getattr(self, "_preview_socket", None)
                preview_service = getattr(self, "_preview_service", None)
                if preview_service is not None:
                    preview_service.revoke_all()
                    preview_service.clear_listener_port()
                if preview_server is not None:
                    preview_server.should_exit = True
                if preview_task is not None:
                    try:
                        await asyncio.wait_for(preview_task, timeout=5.0)
                    except TimeoutError:
                        preview_task.cancel()
                if preview_socket is not None:
                    preview_socket.close()
                if self._services is not None and runtime_shutdown_clean:
                    try:
                        await self._services.close()
                    except Exception:
                        log.debug("gateway.services_close_failed", exc_info=True)
                elif self._services is not None:
                    log.warning("gateway.services_close_skipped_for_live_runtime")
            finally:
                if runtime_shutdown_clean:
                    self._release_pid_lock()
        return runtime_shutdown_result


def build_flush_service(
    *,
    tool_registry: Any,
    provider_selector: Any,
    config: GatewayConfig | None = None,
    session_manager: Any | None = None,
    memory_managers: Mapping[str, Any] | None = None,
) -> Any:
    """Construct a :class:`SessionFlushService` gated by flush config.

    Returns ``None`` when the kill-switch env var is disabled or gateway memory
    config does not explicitly enable flush. Otherwise returns a service wired to the gateway's tool
    registry and provider selector. ``agent_id`` is threaded through the
    callable signature for future multi-agent support, but today OpenSquilla
    uses a single ModelSelector so we just call its ``resolve()`` and ignore
    the agent id.
    """
    from opensquilla.memory.flush_config import is_session_flush_enabled

    if not is_session_flush_enabled():
        return None
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is None or not getattr(memory_cfg, "flush_enabled", False):
        return None

    from opensquilla.memory.session_flush import SessionFlushService
    from opensquilla.tools.dispatch import build_tool_handler

    tool_handler = build_tool_handler(tool_registry)
    raw_session_storage = get_session_storage(session_manager)
    session_storage: _FlushReceiptSessionStorage | None = None
    if (
        raw_session_storage is not None
        and callable(getattr(raw_session_storage, "get_session", None))
        and callable(getattr(raw_session_storage, "list_memory_durable_receipts", None))
        and callable(getattr(raw_session_storage, "upsert_memory_durable_receipt", None))
    ):
        session_storage = cast(_FlushReceiptSessionStorage, raw_session_storage)

    def _resolve_provider(_agent_id: str) -> Any:
        if provider_selector is None:
            return None
        resolver = getattr(provider_selector, "resolve", None)
        if resolver is None:
            return None
        try:
            return resolver()
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_flush_session_id(session_key: str) -> str | None:
        if session_storage is None:
            return None
        session = await session_storage.get_session(session_key)
        if session is None:
            return None
        return str(getattr(session, "session_id", "") or "") or None

    async def _resolve_flush_checkpoint_exists(
        session_key: str,
        session_id: str | None,
    ) -> bool:
        if session_storage is None or not session_id:
            return False
        rows = await session_storage.list_memory_durable_receipts(
            session_key=session_key,
            session_id=session_id,
            scope="checkpoint",
            status="checkpoint_saved",
            limit=1,
        )
        return bool(rows)

    async def _write_durable_flush_receipt(receipt: Any, **row: Any) -> None:
        if session_storage is None:
            return

        from opensquilla.session.models import MemoryDurableReceipt

        session_key = str(row.get("session_key") or "")
        if not session_key:
            return
        captured_session_id = str(row.get("session_id") or "")
        if not captured_session_id:
            log.warning(
                "session_flush.receipt_write_skipped",
                reason="session_id_missing",
                session_key=session_key,
                result_status=getattr(receipt, "result_status", None),
            )
            return
        current_session = await session_storage.get_session(session_key)
        current_session_id = (
            str(getattr(current_session, "session_id", "") or "")
            if current_session is not None
            else ""
        )
        if not current_session_id:
            log.warning(
                "session_flush.receipt_write_skipped",
                reason="session_missing",
                session_key=session_key,
                captured_session_id=captured_session_id,
                result_status=getattr(receipt, "result_status", None),
            )
            return
        if current_session_id != captured_session_id:
            log.warning(
                "session_flush.receipt_session_mismatch",
                session_key=session_key,
                captured_session_id=captured_session_id,
                current_session_id=current_session_id,
                result_status=getattr(receipt, "result_status", None),
            )
            return

        scope = str(row.get("scope") or "")
        status = str(row.get("status") or "")
        reason = row.get("reason")
        target_path = row.get("target_path")
        target_path = str(target_path) if target_path else None
        source_path = row.get("source_path")
        source_path = str(source_path) if source_path else None
        turn_id = row.get("turn_id")
        turn_id = str(turn_id) if turn_id else None
        content_hash = row.get("content_hash")
        content_hash = str(content_hash) if content_hash else None
        idempotency_key = ":".join(
            [
                "flush-receipt",
                scope,
                session_key,
                captured_session_id,
                turn_id or "",
                status,
                str(reason or ""),
                source_path or "",
                target_path or "",
                content_hash or "",
                str(getattr(receipt, "input_message_count", 0) or 0),
                str(getattr(receipt, "first_included_message", "") or ""),
                str(getattr(receipt, "last_included_message", "") or ""),
            ]
        )
        await session_storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key=session_key,
                session_id=captured_session_id,
                turn_id=turn_id,
                scope=scope,
                source_path=source_path,
                target_path=target_path,
                content_hash=content_hash,
                idempotency_key=idempotency_key,
                status=status,
                reason=str(reason) if reason else None,
                attempt_count=1,
            ),
            expected_session_id=captured_session_id,
        )

    def _resolve_archive_workspace(agent_id: str) -> Path | None:
        if not memory_managers:
            return None
        managers = [memory_managers.get(agent_id), memory_managers.get("main")]
        for attr_name in ("workspace_dir", "memory_dir"):
            for manager in managers:
                if manager is None:
                    continue
                path_value = getattr(manager, attr_name, None)
                if path_value is not None:
                    return Path(path_value).expanduser()
        return None

    service_kwargs: dict[str, Any] = {}
    if memory_cfg is not None:
        service_kwargs["default_timeout"] = getattr(
            memory_cfg,
            "flush_background_timeout_seconds",
            30.0,
        )
        service_kwargs["raw_archive_max_chars"] = getattr(
            memory_cfg,
            "flush_archive_max_bytes",
            800_000,
        )
    if session_storage is not None:
        service_kwargs["receipt_writer"] = _write_durable_flush_receipt
        service_kwargs["session_identity_resolver"] = _resolve_flush_session_id
        service_kwargs["checkpoint_exists_resolver"] = _resolve_flush_checkpoint_exists

    return SessionFlushService(
        provider_selector=_resolve_provider,
        tool_registry=tool_registry,
        tool_handler=tool_handler,
        archive_workspace_resolver=_resolve_archive_workspace,
        **service_kwargs,
    )


def emit_skill_filter_banner(skills_cfg: Any) -> None:
    """One-line startup warning when the ONNX embedding backend is
    unreachable but a non-lexical filter strategy is configured.

    Required runtime: ``onnxruntime`` + ``tokenizers`` +
    the bundled v4 BGE ONNX dir (or a configured override). All three
    ship via ``uv sync --extra recommended``. The previous non-ONNX
    fallback was removed — there is now exactly one backend.

    The banner fires only when filter_enabled=true, strategy ≠ lexical,
    AND the ONNX path is incomplete. Uses stdlib :mod:`logging` so
    operators see it on the standard ``WARNING`` logger and so tests
    can assert on it via ``caplog``.
    """
    import importlib.util
    import logging

    log_std = logging.getLogger("opensquilla.gateway.boot")

    if not getattr(skills_cfg, "filter_enabled", False):
        return
    if getattr(skills_cfg, "filter_strategy", "lexical") == "lexical":
        return

    onnx_ok = False
    try:
        if (
            importlib.util.find_spec("onnxruntime") is not None
            and importlib.util.find_spec("tokenizers") is not None
        ):
            from opensquilla.memory.embedding import LocalEmbeddingProvider

            model_name = getattr(
                skills_cfg, "filter_embedding_model", LocalEmbeddingProvider.DEFAULT_MODEL
            )
            onnx_ok = LocalEmbeddingProvider._bundled_onnx_dir(model_name) is not None
    except ImportError:
        onnx_ok = False

    if onnx_ok:
        return

    log_std.warning(
        "ONNX embedding backend not available; filter_strategy=%r will run "
        "lexical-only. Install via `uv sync --extra recommended` to get "
        "onnxruntime + tokenizers, and verify the bundled BGE ONNX dir "
        "is present.",
        getattr(skills_cfg, "filter_strategy", "lexical"),
    )


def _squilla_router_bundle_dir(router_cfg: Any) -> Path:
    configured = getattr(router_cfg, "v4_bundle_dir", None)
    if configured:
        return Path(configured).expanduser()
    return (
        Path(__file__).resolve().parents[1] / "squilla_router" / "models" / "v4.2_phase3_inference"
    )


def validate_squilla_router_runtime(config: GatewayConfig) -> None:
    """Validate router assets without loading the heavy ML runtime."""
    router_cfg = getattr(config, "squilla_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return

    strategy = getattr(router_cfg, "strategy", "v4_phase3")
    if strategy != "v4_phase3":
        log.warning("build_services.squilla_router_removed_strategy", strategy=strategy)

    bundle_dir = _squilla_router_bundle_dir(router_cfg)
    required = ("runtime_src", "router.runtime.yaml")
    missing = [name for name in required if not (bundle_dir / name).exists()]
    if missing:
        message = f"missing V4 bundle files in {bundle_dir}: {missing}"
        if getattr(router_cfg, "require_router_runtime", False):
            raise RuntimeError(message)
        log.warning(
            "build_services.squilla_router_bundle_missing",
            bundle_dir=str(bundle_dir),
            missing=missing,
        )
        return
    log.info("build_services.squilla_router_bundle_ready", bundle_dir=str(bundle_dir))


def validate_squilla_router_runtime_deep(config: GatewayConfig) -> None:
    """Validate router assets and load the ML runtime once."""
    validate_squilla_router_runtime(config)
    router_cfg = getattr(config, "squilla_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return

    strategy = _preload_squilla_router_strategy(router_cfg)
    if getattr(strategy, "_available", False):
        return

    error = getattr(strategy, "error", None)
    if isinstance(error, BaseException):
        raise RuntimeError(f"V4 Phase 3 router did not become available: {error}") from error
    if error is not None:
        raise RuntimeError(f"V4 Phase 3 router did not become available: {error}")
    raise RuntimeError("V4 Phase 3 router did not become available")


def _preload_squilla_router_strategy(router_cfg: Any) -> object:
    from opensquilla.engine.steps.squilla_router import preload_strategy

    return preload_strategy(router_cfg)


async def preload_squilla_router_runtime(config: GatewayConfig) -> None:
    router_cfg = getattr(config, "squilla_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return

    bundle_dir = _squilla_router_bundle_dir(router_cfg)
    try:
        log.info("gateway.squilla_router_preload_started", bundle_dir=str(bundle_dir))
        strategy = await asyncio.to_thread(_preload_squilla_router_strategy, router_cfg)
        if getattr(strategy, "_available", False):
            log.info("gateway.squilla_router_preloaded", bundle_dir=str(bundle_dir))
            return
        if getattr(router_cfg, "require_router_runtime", False):
            raise RuntimeError("V4 Phase 3 router did not become available")
        log.warning("gateway.squilla_router_preload_unavailable", bundle_dir=str(bundle_dir))
    except Exception as exc:  # noqa: BLE001
        from opensquilla.router_runtime_diagnostics import classify_router_runtime_error

        log.warning(
            "gateway.squilla_router_preload_failed",
            bundle_dir=str(bundle_dir),
            error=str(exc),
            runtime_error_kind=classify_router_runtime_error(exc),
        )


def model_override_entries(config: GatewayConfig) -> dict[str, dict[str, Any]]:
    """Flatten ``[models.<provider>."<model>"]`` overrides for the catalog.

    Produces the ``ModelCatalog.set_user_overrides`` key shape: lowercased
    ``"provider/model"`` keys mapping to only the fields the operator
    actually set (``None`` fields are "no override" and must not flow
    through). ``thinking_level_map`` is config-only metadata, not a catalog
    entry field, so it is dropped here rather than rejected downstream.
    """
    entries: dict[str, dict[str, Any]] = {}
    for provider_id, models in (config.models or {}).items():
        for model_id, override in models.items():
            fields = override.model_dump(exclude_none=True)
            fields.pop("thinking_level_map", None)  # not a catalog entry field
            if fields:
                entries[f"{provider_id}/{model_id}".lower()] = fields
    return entries


def apply_model_catalog_overrides(catalog: ModelCatalog, config: GatewayConfig) -> None:
    """Apply ``[models.*]`` cost/metadata overrides onto ``catalog``.

    Overrides are operator-authored TOML; a malformed value must not crash
    boot or a live config hot-apply (``config.set``/``patch``/``apply``/
    ``reload`` — see ``rpc_config._sync_model_catalog_overrides``). On
    rejection ``ModelCatalog.set_user_overrides`` leaves the previously
    installed overrides in place, and this logs a warning naming the bad
    value rather than dropping it silently.
    """
    try:
        catalog.set_user_overrides(model_override_entries(config))
    except ValueError as exc:
        log.warning("model_catalog.user_override_rejected", error=str(exc))


def _expire_restart_orphaned_approvals(
    session_storage: Any,
    approval_queue: Any,
) -> int:
    """Expire every approval whose in-memory continuation died on restart."""

    take_session_keys = getattr(
        session_storage,
        "take_restart_abandoned_session_keys",
        None,
    )
    if callable(take_session_keys):
        take_session_keys()
    try:
        expired = int(approval_queue.expire_all_pending() or 0)
    except Exception:
        log.exception("approval.restart_recovery_failed")
        return 0
    if expired:
        log.info(
            "approval.restart_recovery_completed",
            expired_count=expired,
        )
    return expired


async def build_services(
    config: GatewayConfig | None = None,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    usage_tracker: Any = None,
    session_db_path: str = ":memory:",
    extra_agent_ids: list[str] | None = None,
    seed_agent_workspaces: bool = True,
) -> ServiceContainer:
    """Initialize reusable services without any gateway-specific side effects.

    This is the standalone entry point for service construction. It builds
    all the pieces that both the ASGI gateway and the CLI ``--standalone``
    path need: session storage, provider selector, tool registry, memory,
    skills, scheduler, search, and MCP discovery.

    Managed-Skill crash recovery runs only when the current thread already
    owns :class:`ProfileOperationLock`. Callers without that capability still
    receive the other services, but the managed Skill layer is quarantined and
    its recovery state is left byte-for-byte untouched.

    Parameters that are *None* are auto-constructed from *config* defaults.
    Pass explicit instances to override (useful for tests and embedding).

    Returns a populated :class:`ServiceContainer`.
    """
    services_started_at = time.monotonic()

    # Electron gives its owned Gateway a one-process bridge credential. Consume
    # and scrub it before loading .env or constructing services/tool
    # subprocesses. This prevents a profile file from manufacturing bridge
    # authority and leaves only the fixed-method runtime client in memory.
    try:
        from opensquilla.gateway.desktop_artifact_bridge import (
            initialize_desktop_artifact_bridge_client,
        )

        initialize_desktop_artifact_bridge_client()
    except ValueError:
        log.warning("artifact.desktop_bridge_environment_rejected")

    # ── Load .env files (cwd/.env > ~/.opensquilla/.env, never override existing) ──
    from opensquilla.env import load_env

    load_env()

    # ── Config ──────────────────────────────────────────────────────
    if config is None:
        config = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
        if config.config_path:
            log.info("build_services.config_loaded", path=config.config_path)

    _prewarm_tokenrhythm_install_id(config)
    deferred_warmups: list[Callable[[], Any]] = []
    sandbox_setup_task: asyncio.Task[Any] | None = None
    _warn_workspace_state_mismatch(config)
    _warn_legacy_home_detected(config)

    # Register session-material filesystem cleanup so deleting a session also
    # removes its transcript media + workspace attachment copies (they are
    # DB-only deletions otherwise and leak on disk). The concrete cleanup lives
    # here (gateway layer) because it resolves the agent workspace via
    # ``agents.scope`` — the low-level ``session`` package must not depend on it.
    from opensquilla.session.material_cleanup import (
        set_session_artifact_cleanup,
        set_session_material_cleanup,
    )

    set_session_material_cleanup(build_session_material_cleanup(config))
    set_session_artifact_cleanup(build_session_artifact_cleanup(config))

    validate_squilla_router_runtime(config)
    from opensquilla.memory.embedding_resolver import resolve_memory_embedding

    resolve_memory_embedding(config.memory, local_available=lambda *_: False)
    if seed_agent_workspaces:
        _ensure_configured_agent_workspaces(config, extra_agent_ids=extra_agent_ids)

    # Inject config into admin tool (needed by both gateway and standalone)
    from opensquilla.tools.builtin.admin import set_gateway_config

    set_gateway_config(config)

    from opensquilla.tools.ssrf import configure_trusted_fake_ip_cidrs

    configure_trusted_fake_ip_cidrs(config.tools.trusted_fake_ip_cidrs)

    # Canonicalize released sandbox state before opening any persistent store.
    # A failed prepared journal is intentionally left for explicit recovery;
    # startup never guesses or rolls the profile back automatically.
    config_path = Path(str(getattr(config, "config_path", "") or ""))
    if config_path.is_file():
        from opensquilla.sandbox.upgrade_migration import (
            ensure_sandbox_upgrade_migrated,
        )

        sandbox_upgrade_started_at = time.monotonic()
        log.info("build_services.sandbox_upgrade_started")
        try:
            upgrade_report = ensure_sandbox_upgrade_migrated(config_path.parent)
        except Exception as exc:
            log.exception(
                "build_services.sandbox_upgrade_failed",
                duration_ms=_elapsed_monotonic_ms(sandbox_upgrade_started_at),
                error_type=type(exc).__name__,
            )
            raise
        log.info(
            "build_services.sandbox_upgrade_finished",
            ok=upgrade_report.ok,
            status=upgrade_report.status,
            duration_ms=_elapsed_monotonic_ms(sandbox_upgrade_started_at),
        )
        if not upgrade_report.ok:
            raise RuntimeError(
                "migration_failed_manual_recovery_required: "
                f"{upgrade_report.error or upgrade_report.status}"
            )

    # ── Sandbox runtime ─────────────────────────────────────────────
    # validate_combination emits structured warnings; configure_runtime
    # assembles the backend + gate + ledger so tool handlers can call
    # through the ``@sandboxed`` decorator.
    try:
        from opensquilla.run_mode import config_run_mode
        from opensquilla.sandbox.integration import configure_runtime

        sandbox_settings = _sandbox_settings_for_runtime(config)
        effective = configure_runtime(
            sandbox_settings,
            workspace=Path(config.workspace_dir) if config.workspace_dir else None,
            default_run_mode=config_run_mode(config),
        )
        log.info(
            "build_services.sandbox_ready",
            **effective.effective.as_dict(),
        )
        if getattr(effective.effective, "sandbox_enabled", True) and sandbox_settings.auto_setup:
            sandbox_setup_task = create_background_task(_ensure_sandbox_setup_on_boot(config))
    except Exception as e:  # pragma: no cover - boot diagnostics only
        log.exception("build_services.sandbox_configure_failed", error=str(e))
        raise

    # ── Schema migrations (before any DB connects) ──────────────────
    # Runs pending migrations on the session DB before SessionStorage opens it,
    # so SQLModel-backed tables (SessionNode, TranscriptEntry, SessionSummary)
    # see the expected columns. Skipped for in-memory DBs (CLI standalone) —
    # yoyo would operate on a separate in-memory connection from storage.
    # Migration failures propagate: code ships behind the migration, never
    # ahead of it — silently booting on an out-of-date schema is worse than
    # failing loud.
    storage_db_path = session_db_path
    if session_db_path != ":memory:":
        from opensquilla.persistence.migrator import _native_sqlite_path, apply_pending

        migrations_started_at = time.monotonic()
        log.info("build_services.migrations_started")
        if "://" not in session_db_path:
            # 0o700: session transcripts are sensitive — keep a freshly created
            # state directory owner-only (umask-masked; no-op on Windows and on
            # pre-existing directories).
            storage_db_path = _native_sqlite_path(session_db_path)
            os.makedirs(os.path.dirname(storage_db_path) or os.curdir, mode=0o700, exist_ok=True)
        migrations_dir = _resolve_migrations_dir()
        applied = apply_pending(storage_db_path, migrations_dir)
        if applied:
            log.info("build_services.migrations_applied", count=len(applied), ids=applied)
        log.info(
            "build_services.migrations_ready",
            count=len(applied),
            duration_ms=_elapsed_monotonic_ms(migrations_started_at),
        )

    # ── Agent registry (built early so SessionManager can resolve agent configs) ─
    from opensquilla.agents.registry import AgentRegistry

    agent_registry = AgentRegistry(config)

    # ── Session manager ─────────────────────────────────────────────
    if session_manager is None:
        from opensquilla.paths import media_root_from_config
        from opensquilla.session.manager import SessionManager
        from opensquilla.session.storage import SessionStorage

        session_storage_started_at = time.monotonic()
        log.info("build_services.session_storage_started")
        if storage_db_path != ":memory:" and "://" not in storage_db_path:
            os.makedirs(os.path.dirname(storage_db_path) or os.curdir, mode=0o700, exist_ok=True)
        storage = SessionStorage(storage_db_path)
        await storage.connect(
            goal_pause_reason=(
                "process_restart"
                if config.goal.execution_enabled
                else "feature_disabled"
            )
        )
        log.info(
            "build_services.session_storage_ready",
            duration_ms=_elapsed_monotonic_ms(session_storage_started_at),
        )
        session_manager = SessionManager(
            storage,
            agent_registry=agent_registry,
            checkpoint_workspace_dir=config.workspace_dir,
            media_root=media_root_from_config(config),
            model_routing_mode_provider=lambda: model_routing_snapshot(config)["mode"],
        )

    # Wire session manager into tool layer (like set_scheduler, set_gateway_config)
    from opensquilla.tools.builtin.sessions import (
        set_gateway_config as _set_sessions_gateway_config,
    )
    from opensquilla.tools.builtin.sessions import set_session_manager

    set_session_manager(session_manager)
    _set_sessions_gateway_config(config)
    session_storage = get_session_storage(session_manager)
    if session_storage is not None and callable(
        getattr(session_storage, "_write_transaction", None)
    ):
        from opensquilla.artifact_session import ArtifactSessionService
        from opensquilla.artifacts import ArtifactStore
        from opensquilla.gateway.artifact_mutation_recovery import (
            reconcile_pending_artifact_mutations,
        )
        from opensquilla.gateway.document_resource_recovery import (
            reconcile_pending_document_resources,
        )
        from opensquilla.gateway.rpc import RpcContext
        from opensquilla.gateway.rpc_workbench_resources import (
            resolve_recovery_import_source,
        )
        from opensquilla.paths import media_root_from_config

        artifact_recovery_service = await ArtifactSessionService.from_session_storage(
            session_storage
        )
        resource_recovery_context = RpcContext(
            conn_id="document-resource-recovery",
            session_manager=session_manager,
            config=config,
        )

        try:
            recovery_summary = await reconcile_pending_artifact_mutations(
                artifact_recovery_service,
                ArtifactStore(media_root_from_config(config)),
            )
            resource_recovery_summary = await reconcile_pending_document_resources(
                artifact_recovery_service,
                ArtifactStore(media_root_from_config(config)),
                import_source_resolver=lambda attempt: resolve_recovery_import_source(
                    resource_recovery_context,
                    attempt,
                ),
            )
        finally:
            await artifact_recovery_service.close()
        if recovery_summary.examined:
            log.info(
                "build_services.artifact_mutations_reconciled",
                examined=recovery_summary.examined,
                applied=recovery_summary.applied,
                failed=recovery_summary.failed,
                ambiguous=recovery_summary.ambiguous,
                deleted_candidates=recovery_summary.deleted_candidates,
            )
        if resource_recovery_summary.examined:
            log.info(
                "build_services.document_resources_reconciled",
                imports_examined=resource_recovery_summary.imports_examined,
                imports_applied=resource_recovery_summary.imports_applied,
                imports_failed=resource_recovery_summary.imports_failed,
                imports_ambiguous=resource_recovery_summary.imports_ambiguous,
                publishes_examined=resource_recovery_summary.publishes_examined,
                publishes_applied=resource_recovery_summary.publishes_applied,
                publishes_failed=resource_recovery_summary.publishes_failed,
                publishes_ambiguous=resource_recovery_summary.publishes_ambiguous,
                deleted_candidates=resource_recovery_summary.deleted_candidates,
                promoted_deliverables=resource_recovery_summary.promoted_deliverables,
            )
    from opensquilla.application.approval_queue import get_approval_queue

    _expire_restart_orphaned_approvals(
        session_storage,
        get_approval_queue(),
    )

    # Establish the ledger cutover before any TurnRunner can send a provider
    # request.  This is intentionally a short sessions-only transaction; the
    # transcript backfill is scheduled after the readiness flag is published.
    usage_event_sink = None
    if session_storage is not None and all(
        hasattr(session_storage, method)
        for method in (
            "initialize_usage_ledger",
            "start_usage_event",
            "finalize_usage_event",
            "mark_usage_event_unknown",
        )
    ):
        from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink

        await session_storage.initialize_usage_ledger()
        recover_started = getattr(session_storage, "recover_started_usage_events", None)
        if callable(recover_started):
            await recover_started(reason="process_restarted")
        usage_event_sink = SessionUsageEventSink(session_storage)
        log.info("build_services.usage_ledger_ready")

    # Wire agent registry into the agents_list tool surface.
    from opensquilla.tools.builtin.agents import set_agent_registry as _set_agent_registry_tool

    _set_agent_registry_tool(agent_registry)

    # ── Provider selector ───────────────────────────────────────────
    llm_runtime = resolve_llm_runtime_config(config)
    api_key = llm_runtime.api_key
    resolved_base = llm_runtime.base_url
    proxy = llm_runtime.proxy
    if provider_selector is None:
        # Always build the selector, even before an API key exists: every
        # service (TurnRunner, RPC contexts, flush, auto-propose) captures
        # this one object at boot, and config hot-apply mutates it in place
        # via sync_primary. Booting without a selector would strand the
        # gateway on "No provider available" until a restart even after the
        # operator configures a key in the Web UI. An unconfigured selector
        # refuses resolve() cleanly (ProviderNotConfiguredError) instead.
        from opensquilla.provider.selector import (
            ModelSelector,
            ProviderConfig,
            SelectorConfig,
        )

        if resolved_base.endswith("/v1"):
            resolved_base = resolved_base[:-3]
        provider_selector = ModelSelector(
            SelectorConfig(
                primary=ProviderConfig(
                    provider=llm_runtime.provider,
                    model=llm_runtime.model,
                    api_key=api_key,
                    base_url=resolved_base,
                    proxy=proxy,
                    provider_routing=llm_runtime.provider_routing,
                )
            )
        )
        if provider_selector.is_configured:
            log.info(
                "build_services.provider_ready",
                provider=llm_runtime.provider,
                model=llm_runtime.model,
            )
        else:
            log.info(
                "build_services.provider_pending_configuration",
                provider=llm_runtime.provider or "",
                hint="configure an API key via the Web UI or config.toml [llm]; applies live",
            )

    # ── Model catalog (boot order: after provider selector) ──────────
    # Keep a catalog for every provider so direct-provider runtime paths still
    # get static fallback capabilities (for example DeepSeek v4 thinking
    # replay) even when only OpenRouter performs a remote model-list fetch.
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

    model_catalog = ModelCatalog()
    # Publish the (soon-to-be-warmed) gateway catalog as the process-wide
    # shared instance so module-level consumers (router decision events,
    # usage RPC context windows, ensemble member wiring) resolve against
    # live data instead of cold snapshot/static-only copies.
    set_shared_catalog(model_catalog)
    # [models.*] cost/metadata overrides (schema: ModelOverrideConfig) become
    # the catalog's highest-authority resolution layer as soon as the shared
    # instance exists, so resolve_model_price/get_capabilities honor them
    # from the first turn onward.
    apply_model_catalog_overrides(model_catalog, config)

    from opensquilla.gateway.model_catalog_refresh import (
        TokenRhythmCatalogCoordinator,
        install_tokenrhythm_catalog_coordinator,
    )

    model_catalog_refresh_coordinator = TokenRhythmCatalogCoordinator(model_catalog)
    install_tokenrhythm_catalog_coordinator(model_catalog_refresh_coordinator)
    # Hydration is disk-only. It exposes a matching last-good entitlement
    # before warmup while preserving the keyless/custom-endpoint zero-network
    # boot contract.
    await model_catalog_refresh_coordinator.hydrate(config)

    async def _warm_model_catalog_and_pricing() -> None:
        # Registry-driven live listing first. The shared refresh boundary
        # re-resolves the CURRENT config at execution time: desktop deferred
        # warmups therefore see a provider/key saved after first paint instead
        # of the stale credential captured when build_services started. It also
        # preserves the keyless-boot zero-network contract and the hard timeout.
        from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

        await refresh_live_model_catalog(config, catalog=model_catalog)

        if not (api_key and config.llm.provider == "openrouter"):
            return
        try:
            await asyncio.wait_for(
                model_catalog.fetch_openrouter(api_key, resolved_base, proxy),
                timeout=5.0,
            )
            log.info("build_services.model_catalog_ready", count=len(model_catalog))
        except Exception as e:
            log.warning("build_services.model_catalog_failed", error=str(e))

        try:
            from opensquilla.engine.pricing import refresh_live_prices

            pricing_models = {str(config.llm.model)} if config.llm.model else set()
            router_cfg = getattr(config, "squilla_router", None)
            if router_cfg is not None:
                for tier_cfg in getattr(router_cfg, "tiers", {}).values():
                    model_id = tier_cfg.get("model") if isinstance(tier_cfg, dict) else None
                    if model_id:
                        pricing_models.add(str(model_id))
            await asyncio.to_thread(
                refresh_live_prices,
                pricing_models,
                f"{resolved_base.rstrip('/')}/v1",
            )
            log.info("build_services.pricing_cache_ready", count=len(pricing_models))
        except Exception as e:
            log.warning("build_services.pricing_cache_failed", error=str(e))

    if _desktop_fast_start_enabled():
        deferred_warmups.append(_warm_model_catalog_and_pricing)
        log.info("build_services.model_catalog_pricing_deferred")
    else:
        await _warm_model_catalog_and_pricing()

    # ── Tool registry ───────────────────────────────────────────────
    if tool_registry is None:
        from opensquilla.tools.registry import get_default_registry

        tool_registry = get_default_registry()

    try:
        from opensquilla.tools.builtin.session_search import create_session_search_tool

        if session_storage is not None:
            create_session_search_tool(session_storage, registry=tool_registry)
            log.info("build_services.session_search_tool_registered")
        else:
            log.warning("build_services.session_search_tool_skipped", reason="storage_unavailable")
    except Exception as e:
        log.warning("build_services.session_search_tool_failed", error=str(e))

    try:
        from opensquilla.tools.builtin.media import configure_audio, configure_image_generation

        configure_image_generation(
            config.image_generation,
            gateway_config=config,
            llm_config=config.llm,
            squilla_router_config=config.squilla_router,
        )
        configure_audio(config.audio)
    except Exception as e:
        log.warning("build_services.image_generation_config_failed", error=str(e))

    # ── Memory tools (boot order 18) — per-agent stores ──────────────
    # Pre-bind to empty defaults so the ServiceContainer init below and
    # the deferred TurnRunner-ref callback both work even if the try
    # block aborts.
    memory_managers: dict[str, MemoryManager] = {}
    memory_stores: dict[str, Any] = {}
    memory_retrievers: dict[str, Any] = {}
    memory_sync_managers: dict[str, Any] = {}
    turn_capture_services: dict[str, Any] = {}
    memory_watchers: list[Any] = []
    _turn_runner_ref: list = []
    memory_started_at = time.monotonic()
    memory_degraded = False
    try:
        from opensquilla.memory.manager import build_memory_managers
        from opensquilla.tools.builtin.memory_tools import create_memory_tools

        agent_ids = _configured_agent_ids(config, extra_agent_ids)
        log.info("build_services.memory_started", agents=len(agent_ids))
        memory_managers = await build_memory_managers(
            config,
            agent_ids,
            session_storage=session_storage,
        )

        # Derive legacy per-tier views from the managers. These remain in
        # `ServiceContainer` until downstream consumers
        # (TurnRunner, CLI, memory_tools) onto `memory_managers` directly.
        memory_stores = {aid: m.store for aid, m in memory_managers.items()}
        memory_retrievers = {aid: m.retriever for aid, m in memory_managers.items()}
        memory_sync_managers = {aid: m.sync_manager for aid, m in memory_managers.items()}
        turn_capture_services = {aid: m.turn_capture for aid, m in memory_managers.items()}
        memory_watchers = [m.sync_manager for m in memory_managers.values()]

        # Deferred callback: TurnRunner doesn't exist yet, so we capture a
        # mutable list ref that start_gateway_server() will populate later.
        def _on_memory_write(agent_id: str) -> None:
            if _turn_runner_ref:
                _turn_runner_ref[0].refresh_memory_snapshot(agent_id)

        if memory_stores and memory_retrievers:
            create_memory_tools(
                stores=memory_stores,
                retrievers=memory_retrievers,
                memory_base=config.state_dir,
                registry=tool_registry,
                memory_source=getattr(config.memory, "source", "state"),
                on_memory_write=_on_memory_write,
                memory_config=config.memory,
                workspace_base=config.workspace_dir
                if getattr(config.memory, "source", "state") == "workspace"
                else None,
            )
            log.info("build_services.memory_tools_registered", agents=list(memory_stores))
    except Exception as e:
        memory_degraded = True
        log.warning("build_services.memory_tools_failed", error=str(e))
    log.info(
        "build_services.memory_ready",
        agents=len(memory_managers),
        degraded=memory_degraded,
        duration_ms=_elapsed_monotonic_ms(memory_started_at),
    )

    # ── Skill loader (boot order 19) ────────────────────────────────
    skill_loader = None
    skill_management_service = None
    skill_management_state: dict[str, Any] = {}
    try:
        from opensquilla.skills.loader import SkillLoader
        from opensquilla.skills.paths import resolve_skill_layer_dirs

        workspace_root_raw = getattr(config, "workspace_dir", None)
        workspace_root = Path(workspace_root_raw) if workspace_root_raw else None
        workspace_override = (
            Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
        )
        layer_dirs = resolve_skill_layer_dirs(
            allow_bundled=config.skills.allow_bundled,
            workspace_root=workspace_root,
            workspace_override=workspace_override,
            managed_override=config.skills.managed_dir,
            extra_dirs=[Path(d) for d in config.skills.extra_dirs],
        )
        managed_skill_dir = layer_dirs.managed_dir
        if managed_skill_dir is None:
            raise RuntimeError("No managed Skill directory is configured")
        # Recover any interrupted managed-Skill transaction before the
        # production loader is allowed to scan that layer. Gateway and supported
        # standalone CLI lifecycles already hold the profile lease on this
        # thread. Public embedders that call build_services() without that
        # capability must remain read-only: quarantine the managed layer instead
        # of racing an active writer or sweeping its transaction reservation.
        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import (
            profile_operation_lock_held_by_current_thread,
        )
        from opensquilla.skills.hub.contracts import (
            DiagnosticPhase,
            DiagnosticSeverity,
            SkillDiagnostic,
        )
        from opensquilla.skills.hub.transaction import (
            journal_path_for_state,
            recover_pending_skill_transaction,
        )

        configured_state = str(getattr(config, "state_dir", "") or "").strip()
        skill_journal_path = journal_path_for_state(
            managed_skill_dir,
            Path(configured_state) if configured_state else None,
        )
        skill_management_state.update(
            {
                "managed_dir": managed_skill_dir,
                "journal_path": skill_journal_path,
            }
        )
        profile_home = default_opensquilla_home()
        if profile_operation_lock_held_by_current_thread(profile_home):
            recovery_diagnostics = recover_pending_skill_transaction(
                managed_dir=managed_skill_dir,
                lockfile_path=profile_home / "skills-lock.json",
                journal_path=skill_journal_path,
                sweep_orphan_staging=True,
            )
        else:
            recovery_diagnostics = [
                SkillDiagnostic(
                    code="PROFILE_LEASE_REQUIRED",
                    severity=DiagnosticSeverity.ERROR,
                    phase=DiagnosticPhase.STORE,
                    message=(
                        "Managed Skill recovery was skipped because this service "
                        "builder does not hold the profile writer lease"
                    ),
                    blocking=True,
                    hint=(
                        "Call build_services() only while ProfileOperationLock is held "
                        "for the active OpenSquilla profile."
                    ),
                )
            ]
        skill_management_state["recovery_diagnostics"] = tuple(recovery_diagnostics)
        for diagnostic in recovery_diagnostics:
            log.warning(
                "build_services.skill_transaction_recovery",
                **diagnostic.to_dict(),
            )
        managed_recovery_required = any(
            item.blocking for item in recovery_diagnostics
        )
        if managed_recovery_required:
            log.warning(
                "build_services.skill_managed_layer_quarantined",
                managed_dir=str(managed_skill_dir),
            )
        candidate_skill_loader = SkillLoader(
            bundled_dir=layer_dirs.bundled_dir,
            workspace_dir=layer_dirs.workspace_dir,
            managed_dir=managed_skill_dir,
            personal_agents_dir=layer_dirs.personal_agents_dir,
            project_agents_dir=layer_dirs.project_agents_dir,
            extra_dirs=layer_dirs.extra_dirs,
        )
        if managed_recovery_required:
            # Quarantine is a loader safety invariant, not a side effect of the
            # optional management-service composition below.  Assign the loader
            # only after the freeze succeeds so a construction failure cannot
            # leave uncommitted managed bytes available to later boot consumers.
            candidate_skill_loader.freeze_catalog_for_recovery(
                reason="skill.management.startup-recovery-required"
            )
        skill_loader = candidate_skill_loader
        log.info(
            "build_services.skill_loader_initialized",
            bundled_dir=str(layer_dirs.bundled_dir),
        )

        # Register skill_list and skill_view tools. Pass a live getter for the
        # skills config so coding-mode / disabled gating is honored at call
        # time (config is updated in place by config.patch).
        from opensquilla.skills.hub.defaults import (
            build_default_skill_management_service,
        )
        from opensquilla.tools.builtin.skill_tools import create_skill_tools

        skill_management_service = build_default_skill_management_service(
            managed_dir=managed_skill_dir,
            loader=skill_loader,
            journal_path=skill_journal_path,
            offline=False,
            startup_recovery_diagnostics=recovery_diagnostics,
        )

        create_skill_tools(
            skill_loader,
            skills_cfg_getter=lambda: getattr(config, "skills", None),
            management_service=skill_management_service,
        )
        log.info("build_services.skill_tools_registered")
    except Exception as e:
        log.warning("build_services.skill_loader_failed", error=str(e))

    # ── Cron scheduler (boot order 20) ──────────────────────────────
    cron_scheduler = None
    try:
        from opensquilla.persistence.migrator import _native_sqlite_path
        from opensquilla.scheduler import JobStore, SchedulerEngine

        scheduler_db = Path(
            os.environ.get("OPENSQUILLA_SCHEDULER_DB", str(_state_path(config, "scheduler.db")))
        )
        if str(scheduler_db) == ":memory:":
            scheduler_storage = ":memory:"
        else:
            scheduler_storage = _native_sqlite_path(scheduler_db)
            os.makedirs(
                os.path.dirname(scheduler_storage) or os.curdir,
                exist_ok=True,
            )
        job_store = JobStore(db_path=scheduler_storage)
        await job_store.open()
        cron_scheduler = SchedulerEngine(
            store=job_store,
            session_store=storage,  # SessionStorage instance from session manager boot
            config={
                "max_concurrent_runs": int(os.environ.get("OPENSQUILLA_CRON_MAX_CONCURRENT", "3")),
                "max_catchup_jobs": int(os.environ.get("OPENSQUILLA_CRON_MAX_CATCHUP", "5")),
                "session_retention": int(
                    os.environ.get("OPENSQUILLA_CRON_SESSION_RETENTION", "86400")
                ),
            },
        )
        # Inject into admin tool so `cron` tool can dispatch to the scheduler
        from opensquilla.tools.builtin.admin import set_scheduler

        set_scheduler(cron_scheduler)
        log.info("build_services.cron_scheduler_initialized")
    except Exception as e:
        log.warning("build_services.cron_scheduler_failed", error=str(e))

    # ── Usage tracker ───────────────────────────────────────────────
    if usage_tracker is None:
        usage_tracker = _UsageTracker()

    # ── Search provider runtime ────────────────────────────────────
    async def _configure_search_provider() -> None:
        try:
            import opensquilla.search.providers.bocha  # noqa: F401 — registers provider
            import opensquilla.search.providers.brave  # noqa: F401 — registers provider
            import opensquilla.search.providers.duckduckgo  # noqa: F401 — registers provider
            import opensquilla.search.providers.exa  # noqa: F401 — registers provider
            import opensquilla.search.providers.iqs  # noqa: F401 — registers provider
            import opensquilla.search.providers.tavily  # noqa: F401 — registers provider
            from opensquilla.tools.builtin.web import configure_search

            provider = config.search_provider
            configure_search(
                provider_name=provider,
                max_results=config.search_max_results,
                api_key=config.search_api_key,
                api_key_env=config.search_api_key_env,
                proxy=config.search_proxy,
                use_env_proxy=config.search_use_env_proxy,
                fallback_policy=config.search_fallback_policy,
                diagnostics=config.search_diagnostics,
            )
            log.info("build_services.search_provider_initialized", provider=provider)
        except Exception as e:
            log.warning("build_services.search_provider_failed", error=str(e))

    if _desktop_fast_start_enabled():
        deferred_warmups.append(_configure_search_provider)
        log.info("build_services.search_provider_deferred", provider=config.search_provider)
    else:
        await _configure_search_provider()

    # ── MCP discovery (boot order 22) ───────────────────────────────
    if config.mcp.enabled and config.mcp.servers:
        from opensquilla.mcp.discovery import discover_and_register
        from opensquilla.mcp.types import MCPServerConfig

        timeout = config.mcp.connect_timeout_seconds
        mcp_started_at = time.monotonic()
        mcp_registered = 0
        mcp_failures = 0
        log.info("build_services.mcp_discovery_started", servers=len(config.mcp.servers))
        for server_index, entry in enumerate(config.mcp.servers):
            server_started_at = time.monotonic()
            try:
                mcp_cfg = MCPServerConfig(
                    name=entry.name,
                    transport=entry.transport,
                    command=entry.command,
                    args=entry.args,
                    url=entry.url,
                    env=entry.env,
                    tool_timeout_seconds=entry.tool_timeout_seconds,
                )
                names = await asyncio.wait_for(
                    discover_and_register(mcp_cfg, tool_registry),
                    timeout=timeout,
                )
                log.info(
                    "build_services.mcp_server_registered",
                    server=entry.name,
                    tools=len(names),
                )
                mcp_registered += 1
                log.info(
                    "build_services.mcp_server_timing",
                    server_index=server_index,
                    status="ready",
                    duration_ms=_elapsed_monotonic_ms(server_started_at),
                    tool_count=len(names),
                )
            except TimeoutError:
                mcp_failures += 1
                log.warning(
                    "build_services.mcp_server_timeout",
                    server=entry.name,
                    timeout=timeout,
                )
                log.info(
                    "build_services.mcp_server_timing",
                    server_index=server_index,
                    status="timeout",
                    duration_ms=_elapsed_monotonic_ms(server_started_at),
                    tool_count=0,
                )
            except Exception as e:
                mcp_failures += 1
                log.warning(
                    "build_services.mcp_server_failed",
                    server=entry.name,
                    error=str(e),
                )
                log.info(
                    "build_services.mcp_server_timing",
                    server_index=server_index,
                    status="failed",
                    duration_ms=_elapsed_monotonic_ms(server_started_at),
                    tool_count=0,
                )
        log.info(
            "build_services.mcp_discovery_ready",
            servers=len(config.mcp.servers),
            registered=mcp_registered,
            failures=mcp_failures,
            duration_ms=_elapsed_monotonic_ms(mcp_started_at),
        )
    elif config.mcp.enabled:
        log.info("build_services.mcp_enabled_no_servers")

    flush_service = build_flush_service(
        tool_registry=tool_registry,
        provider_selector=provider_selector,
        config=config,
        session_manager=session_manager,
        memory_managers=memory_managers,
    )
    if flush_service is not None:
        log.info("build_services.session_flush_service_ready")
    else:
        log.info("build_services.session_flush_service_disabled")

    memory_repair_service = None
    if (
        bool(getattr(config.memory, "repair_enabled", True))
        and flush_service is not None
        and session_manager is not None
    ):
        try:
            from opensquilla.gateway.memory_repair_service import MemoryRepairService

            memory_roots = {
                agent_id: Path(root)
                for agent_id, manager in memory_managers.items()
                for root in [
                    getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
                ]
                if root is not None
            }
            memory_repair_service = MemoryRepairService(
                session_manager=session_manager,
                flush_service=flush_service,
                memory_roots=memory_roots,
                agent_ids=tuple(_configured_agent_ids(config, extra_agent_ids)),
                interval_seconds=float(getattr(config.memory, "repair_interval_seconds", 60.0)),
                max_items_per_tick=int(getattr(config.memory, "repair_max_items_per_tick", 5)),
                usage_event_sink=usage_event_sink,
                config=config,
            )
            log.info("build_services.memory_repair_service_ready")
        except Exception as e:
            log.warning("build_services.memory_repair_service_failed", error=str(e))

    meta_run_writer = None
    try:
        from opensquilla.skills.meta.enabled import is_meta_skill_enabled

        persistence_cfg = getattr(getattr(config, "meta_skill", None), "persistence", None)
        if (
            is_meta_skill_enabled(config)
            and persistence_cfg is not None
            and getattr(persistence_cfg, "enabled", False)
        ):
            meta_storage = get_session_storage(session_manager)
            db_path = getattr(meta_storage, "_db_path", None) if meta_storage is not None else None
            if db_path and db_path != ":memory:":
                from opensquilla.persistence.meta_run_writer import open_meta_run_writer

                meta_run_writer = open_meta_run_writer(db_path)
                if meta_storage is not None and hasattr(meta_storage, "_meta_run_writer"):
                    meta_storage._meta_run_writer = meta_run_writer
                # Synchronous SQLite commit — run off the loop even at boot so a
                # contended WAL write cannot stall service startup wiring.
                await asyncio.to_thread(
                    meta_run_writer.mark_orphans_failed,
                    age_ms=int(getattr(persistence_cfg, "orphan_cleanup_age_seconds", 3600)) * 1000,
                )
    except Exception as e:  # noqa: BLE001 - meta traces must not block boot.
        log.warning("build_services.meta_run_writer_failed", error=str(e))
        meta_run_writer = None

    # ── Router decision records (V017 router_decisions) ─────────────
    # Same yoyo-only-table pattern as meta_run_writer: the writer exists when
    # the session DB is real (not :memory:), even if routing is disabled at
    # boot. The control UI can enable routing live without restarting the
    # gateway, so gating writer construction on the initial enabled flag would
    # silently drop every decision recorded after that transition. Boot only
    # rehydrates sticky/anti-downgrade history when routing starts enabled;
    # disabled gateways avoid that startup read, while newly routed turns
    # still begin accumulating immediately after a live enable.
    router_decision_writer = None
    try:
        router_cfg_for_decisions = getattr(config, "squilla_router", None)
        decisions_storage = get_session_storage(session_manager)
        decisions_db_path = (
            getattr(decisions_storage, "_db_path", None) if decisions_storage is not None else None
        )
        if decisions_db_path and decisions_db_path != ":memory:":
            from opensquilla.engine.steps.router_decision_record import (
                rehydrate_history_from_writer,
                set_decision_writer,
            )
            from opensquilla.persistence.router_decision_writer import (
                open_router_decision_writer,
            )

            router_decision_writer = open_router_decision_writer(
                decisions_db_path,
                retention_days=int(
                    getattr(router_cfg_for_decisions, "decision_retention_days", 30) or 30
                ),
            )
            set_decision_writer(router_decision_writer)
            if getattr(router_cfg_for_decisions, "enabled", False):
                rehydrated = rehydrate_history_from_writer(router_decision_writer)
                if rehydrated:
                    log.info(
                        "build_services.router_history_rehydrated",
                        sessions=rehydrated,
                    )
    except Exception as e:  # noqa: BLE001 - decision records must not block boot.
        log.warning("build_services.router_decision_writer_failed", error=str(e))
        try:
            from opensquilla.engine.steps.router_decision_record import set_decision_writer

            set_decision_writer(None)
            if router_decision_writer is not None:
                router_decision_writer.close()
        except Exception:
            pass
        router_decision_writer = None

    # ── Turn error records (V019 turn_errors) ──────────────────────────
    # Same yoyo-only-table pattern as router_decision_writer: the writer
    # exists only when the session DB is real (not :memory:).
    turn_error_writer = None
    try:
        errors_storage = get_session_storage(session_manager)
        errors_db_path = (
            getattr(errors_storage, "_db_path", None) if errors_storage is not None else None
        )
        if errors_db_path and errors_db_path != ":memory:":
            from opensquilla.persistence.turn_error_writer import (
                open_turn_error_writer,
            )

            turn_error_writer = open_turn_error_writer(errors_db_path)
    except Exception as e:  # noqa: BLE001 - error records must not block boot.
        log.warning("build_services.turn_error_writer_failed", error=str(e))
        try:
            if turn_error_writer is not None:
                turn_error_writer.close()
        except Exception:
            pass
        turn_error_writer = None

    # ── Router calibration (opt-in 24h in-process job) ──────────────
    # Only when squilla_router.calibration_enabled is true AND a real decision
    # writer exists. Default-off: no service is constructed, so gateway boot is
    # unchanged and the confidence gate stays byte-identical to today.
    router_calibration_service = None
    try:
        _router_cfg_calib = getattr(config, "squilla_router", None)
        if (
            router_decision_writer is not None
            and getattr(_router_cfg_calib, "enabled", False)
            and getattr(_router_cfg_calib, "calibration_enabled", False)
        ):
            from opensquilla.engine.routing.calibration_service import (
                RouterCalibrationService,
            )

            router_calibration_service = RouterCalibrationService(
                writer=router_decision_writer,
            )
    except Exception as e:  # noqa: BLE001 - calibration must not block boot.
        log.warning("build_services.router_calibration_service_failed", error=str(e))
        router_calibration_service = None

    # ── Provider call stats (rolling latency/health samples) ────────
    from opensquilla.gateway.provider_stats import ProviderStatsStore

    provider_stats = ProviderStatsStore()

    svc = ServiceContainer(
        config=config,
        provider_selector=provider_selector,
        tool_registry=tool_registry,
        session_manager=session_manager,
        skill_loader=skill_loader,
        skill_management_service=skill_management_service,
        skill_management_state=skill_management_state,
        usage_tracker=usage_tracker,
        usage_event_sink=usage_event_sink,
        cron_scheduler=cron_scheduler,
        model_catalog=model_catalog,
        model_catalog_refresh_coordinator=model_catalog_refresh_coordinator,
        agent_registry=agent_registry,
        memory_managers=memory_managers,
        memory_stores=memory_stores,
        memory_sync_managers=memory_sync_managers,
        memory_watchers=memory_watchers,
        memory_retrievers=memory_retrievers,
        turn_capture_services=turn_capture_services,
        flush_service=flush_service,
        memory_repair_service=memory_repair_service,
        meta_run_writer=meta_run_writer,
        router_decision_writer=router_decision_writer,
        turn_error_writer=turn_error_writer,
        router_calibration_service=router_calibration_service,
        provider_stats=provider_stats,
        deferred_warmups=deferred_warmups,
        sandbox_setup_task=sandbox_setup_task,
    )
    if skill_loader is not None:
        try:
            from opensquilla.skills.watcher import SkillCatalogWatcher

            skill_watcher = SkillCatalogWatcher(skill_loader)
            await skill_watcher.start()
            svc.skill_watcher = skill_watcher
        except Exception:
            log.warning("build_services.skill_watcher_failed", exc_info=True)
    # Attach deferred callback ref so start_gateway_server can wire TurnRunner
    svc._turn_runner_ref = _turn_runner_ref  # type: ignore[attr-defined]
    log.info(
        "build_services.ready",
        duration_ms=_elapsed_monotonic_ms(services_started_at),
    )
    return svc


def build_provider_call_observer(provider_stats: Any) -> Callable[..., None] | None:
    """Adapt a ``ProviderStatsStore`` to the engine's provider-call observer seam.

    Returns ``None`` when no store is available so the TurnRunner/Agent path
    stays observer-free. Module-level so boot-wiring tests can drive the
    adapter with a fake store without booting a gateway.
    """
    if provider_stats is None:
        return None

    def _observe_provider_call(
        *,
        provider_id: str,
        model: str,
        ttft_ms: int | None,
        duration_ms: int,
        ok: bool,
        failure_kind: str = "",
    ) -> None:
        provider_stats.record(
            provider_id=provider_id,
            model=model,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            ok=ok,
            failure_kind=failure_kind,
        )

    return _observe_provider_call


def build_turn_runner_from_services(
    svc: Any,
    *,
    config: GatewayConfig | None = None,
    diagnostics_state: Any | None = None,
) -> Any:
    """Build a TurnRunner with every service-backed memory integration wired.

    Provides a standalone per-session lock dict for CLI/standalone paths (no
    TaskRuntime).  When the caller is the gateway boot path, the boot wiring
    overrides ``task_runtime._get_session_lock_for_turn`` so both classes
    share a single lock per session.
    """
    import asyncio as _asyncio

    from opensquilla.engine.runtime import TurnRunner

    resolved_config = config if config is not None else svc.config
    # Standalone lock dict for CLI / test paths (no TaskRuntime involved).
    # Gateway path replaces this with task_runtime._get_session_lock_for_turn
    # immediately after task_runtime is constructed.
    _standalone_locks: dict[str, _asyncio.Lock] = {}

    def _standalone_lock_provider(session_key: str) -> _asyncio.Lock:
        return _standalone_locks.setdefault(session_key, _asyncio.Lock())

    return TurnRunner(
        provider_selector=svc.provider_selector,
        tool_registry=svc.tool_registry,
        session_manager=svc.session_manager,
        skill_loader=svc.skill_loader,
        usage_tracker=svc.usage_tracker,
        usage_event_sink=getattr(svc, "usage_event_sink", None),
        config=resolved_config,
        memory_sync_managers=getattr(svc, "memory_sync_managers", None) or None,
        model_catalog=getattr(svc, "model_catalog", None),
        memory_retrievers=getattr(svc, "memory_retrievers", None) or None,
        turn_capture_services=getattr(svc, "turn_capture_services", None) or None,
        session_flush_service=getattr(svc, "flush_service", None),
        session_lock_provider=_standalone_lock_provider,
        diagnostics_state=diagnostics_state,
        # Hook registries forwarded from services when present so any future
        # user-registered TurnHook / CompactionHook instance flows through to
        # TurnRunner without another boot edit.
        # None today (no production services expose either registry); the
        # plumbing stays here so the path is wired end-to-end.
        turn_hooks=getattr(svc, "turn_hooks", None),
        compaction_hooks=getattr(svc, "compaction_hooks", None),
        meta_run_writer=getattr(svc, "meta_run_writer", None),
        turn_error_writer=getattr(svc, "turn_error_writer", None),
        provider_call_observer=build_provider_call_observer(getattr(svc, "provider_stats", None)),
    )


async def _run_deferred_warmups(svc: ServiceContainer) -> None:
    warmups = list(getattr(svc, "deferred_warmups", []) or [])
    if not warmups:
        return
    log.info("gateway.deferred_warmups_started", count=len(warmups))
    for warmup in warmups:
        try:
            result = warmup()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - warmups must not kill the gateway.
            log.warning(
                "gateway.deferred_warmup_failed",
                warmup=getattr(warmup, "__name__", type(warmup).__name__),
                error=str(exc),
            )
    log.info("gateway.deferred_warmups_ready", count=len(warmups))


async def start_gateway_server(
    port: int | None = None,
    config: GatewayConfig | None = None,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    run: bool = True,
    _startup_started_at: float | None = None,
) -> GatewayServer:
    """
    Boot sequence:
    1. Load/validate config
    2. Ensure auth token exists
    3. Build ASGI app
    4. Start uvicorn server
    """
    startup_started_at = time.monotonic() if _startup_started_at is None else _startup_started_at
    startup_phase_started_at = startup_started_at

    # ── Gateway-specific config handling ─────────────────────────────
    if config is None:
        config = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))

    # Apply runtime port override
    if port is not None:
        config = config.model_copy(update={"port": port})

    _setup_file_logging(config)
    if config.config_path:
        log.info("gateway.config_loaded", path=config.config_path)

    if subscription_manager is None:
        from opensquilla.gateway.websocket import SubscriptionManager

        subscription_manager = SubscriptionManager()

    # Gateway-specific: set env var for other components to discover
    os.environ["OPENSQUILLA_GATEWAY_PORT"] = str(config.port)

    # Gateway-specific: ensure auth token exists
    if config.auth.mode == "token" and not config.auth.token:
        token = secrets.token_urlsafe(32)
        config.auth = config.auth.model_copy(update={"token": token})
        config.mark_runtime_secret("auth.token")
        log.info("gateway.auth_token_generated")

    # Gateway-specific: resolve Control UI root directory (boot order 17)
    if config.control_ui.enabled:
        from opensquilla.gateway.control_ui import _STATIC_DIR, _TEMPLATE_DIR

        if not _TEMPLATE_DIR.is_dir():
            log.warning("gateway.control_ui.templates_missing", path=str(_TEMPLATE_DIR))
        if not _STATIC_DIR.is_dir():
            log.warning("gateway.control_ui.static_missing", path=str(_STATIC_DIR))
        log.info(
            "gateway.control_ui.resolved",
            base_path=config.control_ui.base_path,
            templates=str(_TEMPLATE_DIR),
            static=str(_STATIC_DIR),
        )
    else:
        log.info("gateway.control_ui.disabled")

    # Surface lexical degradation when the operator enabled filter_enabled=true
    # with a strategy that needs the local ONNX embedding backend.
    emit_skill_filter_banner(config.skills)
    startup_phase_started_at = _log_gateway_startup_phase(
        "config",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )

    # ── PID file lock ───────────────────────────────────────────────
    # Prevents two gateway instances from sharing the same STATE_DIR.
    # Must run before build_services so the lock is held before any DB work.
    from opensquilla.gateway.pidlock import GatewayPidLock

    _pid_lock = GatewayPidLock(_state_path(config, ""))
    _pid_lock.acquire()

    # The profile PID lock proves there is no live Gateway writer for this
    # runtime state. Reconcile exact persisted owners before any new turn can
    # launch work; malformed or unsupported rows remain fail-safe and are never
    # widened into PID/name scans.
    try:
        from opensquilla.process_tree import reconcile_persisted_processes

        await reconcile_persisted_processes(getattr(config, "state_dir", None))
    except Exception:
        log.warning("gateway.process_owner_reconcile_failed")

    # A Desktop child opts into a stronger, nonce-verifiable ownership record.
    # Keep it separate from gateway.pid so legacy CLI/readers retain their
    # exact schema. Electron supplies a separate userData control directory so
    # an external or intentionally missing runtime state_dir remains untouched.
    # Cleanup is deliberately owned by cli.main.gateway_run:
    # that outer boundary removes the record only *after* the profile writer
    # lock exits, so record disappearance is a safe restart signal.
    _desktop_gateway_ownership = None
    if run:
        try:
            from opensquilla.gateway.desktop_ownership import (
                activate_desktop_gateway_ownership,
            )

            desktop_profile_home = _desktop_ownership_profile_home(config)
            _desktop_gateway_ownership = activate_desktop_gateway_ownership(
                profile_home=desktop_profile_home,
                port=config.port,
            )
        except BaseException:
            _pid_lock.release()
            raise

    # Stream cursors belong to the Gateway lifecycle that owns every startup
    # claim, not merely to an attempted call to ``start_gateway_server``. In
    # Desktop and embedded processes a second start may be attempted while the
    # active Gateway still owns its generation; resetting before PID/Desktop
    # ownership made that failed contender erase the live generation and seq.
    # Only publish the new process-local generation after all applicable
    # ownership claims succeed and before any service can emit session events.
    try:
        reset_session_streams()
    except BaseException:
        _pid_lock.release()
        raise
    startup_phase_started_at = _log_gateway_startup_phase(
        "ownership",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )

    # Passive update-availability check is best-effort and runs in a background
    # daemon thread: it must never block startup. It powers the "a newer version
    # is available" notice in the Control UI (and `opensquilla version --check`).
    try:
        from opensquilla.observability.update_check import start_background_update_check

        start_background_update_check(config=config)
    except Exception:
        log.debug("gateway.update_check_skipped", exc_info=True)

    # ── Reusable service initialization via build_services ───────────
    try:
        svc = await build_services(
            config=config,
            session_manager=session_manager,
            provider_selector=provider_selector,
            tool_registry=tool_registry,
            usage_tracker=usage_tracker,
            session_db_path=str(_state_path(config, "sessions.db")),
        )
    except BaseException:
        _pid_lock.release()
        raise
    startup_phase_started_at = _log_gateway_startup_phase(
        "services",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )

    # An interrupted profile import may span USER.md and a separate memory
    # root. Recover those canonical files before TurnRunner, channels, or the
    # HTTP server can observe a half-published batch. Recovery is deliberately
    # serial because every agent shares the same profile operation lock.
    try:
        from opensquilla.gateway.rpc_memory_import import (
            run_profile_import_startup_recovery,
        )

        recovered_profile_import_batches = await run_profile_import_startup_recovery(
            config=config,
            memory_managers=svc.memory_managers,
        )
    except BaseException:
        log.error("gateway.profile_import_recovery_failed", exc_info=True)
        try:
            await svc.close()
        except Exception:
            log.debug("gateway.services_close_after_recovery_failed", exc_info=True)
        finally:
            _pid_lock.release()
        raise
    startup_phase_started_at = _log_gateway_startup_phase(
        "profile_recovery",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )

    # Some embedding/tests provide a service-shaped object from an older
    # bootstrap contract.  Treat the ledger sink as an optional additive
    # capability so those callers keep booting unchanged.
    usage_event_sink = getattr(svc, "usage_event_sink", None)

    # Record boot time for uptime calculation (gateway-specific)
    global _boot_time_ms, _boot_id
    _boot_time_ms = int(time.time() * 1000)
    _boot_id = secrets.token_hex(16)

    log.info(
        "gateway.starting",
        host=config.host,
        port=config.port,
        auth_mode=config.auth.mode,
    )

    # ── Diagnostics runtime state ───────────────────────────────────
    from opensquilla.gateway.diagnostics import DiagnosticsState

    diagnostics_state = DiagnosticsState.from_config(config)

    # ── TurnRunner (shared agent orchestration layer) ────────────────
    turn_runner = build_turn_runner_from_services(
        svc,
        config=config,
        diagnostics_state=diagnostics_state,
    )
    # Patch deferred callback so memory writes refresh TurnRunner snapshots
    if hasattr(svc, "_turn_runner_ref"):
        svc._turn_runner_ref.append(turn_runner)  # type: ignore[attr-defined]

    # Canonical journal recovery already completed before TurnRunner was built.
    # Permission hardening, expired private-input cleanup, and derived index
    # refresh are best-effort and may continue after readiness.
    async def maintain_profile_imports() -> None:
        try:
            from opensquilla.gateway.rpc_memory_import import (
                run_profile_import_startup_maintenance,
            )

            failures = await run_profile_import_startup_maintenance(
                config=config,
                memory_managers=svc.memory_managers,
                turn_runner=turn_runner,
                recovered_batches=recovered_profile_import_batches,
            )
            for agent_id, error in failures.items():
                log.warning(
                    "gateway.profile_import_maintenance_failed",
                    agent_id=agent_id,
                    error=error,
                )
        except Exception:
            log.warning("gateway.profile_import_maintenance_failed", exc_info=True)

    svc.profile_import_maintenance_task = create_background_task(
        maintain_profile_imports(),
    )

    memory_repair_service = getattr(svc, "memory_repair_service", None)
    if memory_repair_service is not None:
        memory_repair_service.start()
        log.info("gateway.memory_repair_service_started")

    router_calibration_service = getattr(svc, "router_calibration_service", None)
    if router_calibration_service is not None:
        router_calibration_service.start()
        log.info("gateway.router_calibration_service_started")

    # Lazy ref for channel_manager — cron handler captures it via closure,
    # populated after channel_manager is constructed below.
    _cm_holder: list = [None]
    from opensquilla.scheduler.heartbeat import (
        HeartbeatConfigWatcher,
        HeartbeatRunner,
    )
    from opensquilla.scheduler.heartbeat_loop import HeartbeatLoop
    from opensquilla.scheduler.heartbeat_service import HeartbeatService

    heartbeat_service = HeartbeatService(
        turn_runner=turn_runner,
        session_storage=get_session_storage(svc.session_manager) or svc.session_manager,
        channel_manager_ref=lambda: _cm_holder[0],
    )
    heartbeat_loop = HeartbeatLoop(
        config=config,
        heartbeat_service=heartbeat_service,
    )

    from opensquilla.gateway.background_completion import BackgroundCompletionManager
    from opensquilla.gateway.event_bridge import EventBridge
    from opensquilla.gateway.session_model_routing import (
        capture_accepted_model_routing_config,
    )
    from opensquilla.gateway.subagent_announce import set_background_completion_manager
    from opensquilla.gateway.task_runtime import TaskRun, TaskRuntime

    runtime_event_bridge = EventBridge(
        subscription_manager=subscription_manager,
        connection_registry=get_registry(),
    )

    from opensquilla.engine.cache_break_monitor import add_compaction_listener

    prompt_cache_keepalive_service: Any = None

    def _emit_runtime_compaction_event(
        session_key: str,
        payload: dict[str, Any],
    ) -> None:
        from opensquilla.gateway.session_streams import get_session_streams

        event_payload = dict(payload or {})
        event_payload.setdefault("status", "completed")
        event_payload.setdefault("source", "automatic")
        if (
            event_payload.get("status") == "completed"
            and prompt_cache_keepalive_service is not None
        ):
            prompt_cache_keepalive_service.refresh_required(session_key)
        # Lifecycle claiming and replay append now happen in one synchronous
        # call stack. Network delivery remains asynchronous and at-least-once.
        event_payload = get_session_streams().record(
            session_key,
            "session.event.compaction",
            event_payload,
        )
        emit_coro = runtime_event_bridge.emit(
            session_key,
            "session.event.compaction",
            event_payload,
            replay_recorded=True,
        )
        try:
            create_background_task(emit_coro)
        except RuntimeError:
            emit_coro.close()

    svc._compaction_listener_remove = add_compaction_listener(_emit_runtime_compaction_event)

    from opensquilla.application.approval_queue import get_approval_queue
    from opensquilla.gateway.approval_events import register_approval_event_bridge
    from opensquilla.gateway.approval_notify import register_approval_channel_notifier

    svc._approval_listener_remove = register_approval_event_bridge(
        get_approval_queue(),
        runtime_event_bridge,
        schedule=create_background_task,
    )
    svc._approval_channel_notifier_remove = register_approval_channel_notifier(
        get_approval_queue(),
        session_manager=svc.session_manager,
        channel_manager_ref=lambda: _cm_holder[0],
        schedule=create_background_task,
        config=config,
    )

    background_completion_manager = BackgroundCompletionManager(
        session_manager=svc.session_manager,
        event_emitter=runtime_event_bridge.emit,
        channel_manager_ref=lambda: _cm_holder[0],
    )
    set_background_completion_manager(background_completion_manager)

    async def _subagent_completion_listener(event: Any) -> None:
        from opensquilla.gateway.subagent_announce import announce_subagent_completion

        await announce_subagent_completion(
            event,
            session_manager=svc.session_manager,
            event_emitter=runtime_event_bridge.emit,
            channel_manager=_cm_holder[0],
            task_runtime=task_runtime,
        )

    async def _task_runtime_turn_handler(run: TaskRun) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=svc.session_manager,
            turn_runner=turn_runner,
            event_emitter=runtime_event_bridge.emit,
        )

    async def _capture_task_accepted_config(
        *,
        session_key: str,
        run_kind: str,
    ) -> Any:
        return await capture_accepted_model_routing_config(
            config,
            svc.session_manager,
            session_key=session_key,
            run_kind=run_kind,
        )

    session_lifecycle_listener = _make_task_session_lifecycle_listener(
        session_manager=svc.session_manager,
        event_emitter=runtime_event_bridge.emit,
    )
    task_runtime = TaskRuntime(
        storage=get_session_storage(svc.session_manager) or svc.session_manager,
        turn_handler=_task_runtime_turn_handler,
        event_emitter=runtime_event_bridge.emit,
        terminal_listener=_subagent_completion_listener,
        lifecycle_listener=session_lifecycle_listener,
        max_concurrency=_task_runtime_max_concurrency(config),
        max_pending_per_session=_task_runtime_max_pending_per_session(config),
        subagent_reserved_slots=int(
            getattr(getattr(config, "subagents", None), "subagent_reserved_slots", 0)
        ),
        turn_hard_deadline_s=_task_runtime_turn_hard_deadline_s(config),
        accepted_config_provider=_capture_task_accepted_config,
        pending_overflow_policy=getattr(
            config.task_runtime, "pending_overflow_policy", "reject_newest"
        ),
    )
    from opensquilla.gateway.goal_service import GoalService

    goal_service = GoalService(
        storage=get_session_storage(svc.session_manager) or svc.session_manager,
        session_manager=svc.session_manager,
        task_runtime=task_runtime,
        event_emitter=runtime_event_bridge.emit,
        subscription_manager=subscription_manager,
        # Keep the live root object: config.set/reload replace ``config.goal``
        # in place, and the Goal kill switch must observe that replacement.
        config=config,
    )

    async def _ordered_task_lifecycle(event: TaskLifecycleEvent) -> None:
        # Session projection remains first. Goal settlement is independently
        # isolated so one observer cannot suppress the other.
        try:
            await session_lifecycle_listener(event)
        except Exception:
            log.warning(
                "gateway.session_lifecycle_projection_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                exc_info=True,
            )
        try:
            await goal_service.on_task_lifecycle(event)
        except Exception:
            log.warning(
                "gateway.goal_lifecycle_settlement_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                exc_info=True,
            )

    task_runtime.set_lifecycle_listener(_ordered_task_lifecycle)
    task_runtime.set_activation_listener(goal_service.on_task_activation)
    task_runtime.set_idle_listener(goal_service.on_runtime_idle)
    task_runtime.set_goal_service(goal_service)
    subscription_manager.set_message_unsubscribe_listener(
        goal_service.on_subscription_lost
    )
    # Wire task_runtime's short write-lock provider into turn_runner.
    turn_runner.set_session_lock_provider(task_runtime._get_session_lock_for_turn)
    svc.task_runtime = task_runtime
    from opensquilla.gateway.prompt_cache_keepalive import PromptCacheKeepaliveService

    prompt_cache_keepalive_service = PromptCacheKeepaliveService(
        task_runtime=task_runtime,
        session_manager=svc.session_manager,
        usage_event_sink=usage_event_sink,
    )
    set_keepalive_recorder = getattr(
        turn_runner,
        "set_prompt_cache_keepalive_recorder",
        None,
    )
    if callable(set_keepalive_recorder):
        set_keepalive_recorder(
            prompt_cache_keepalive_service.record_candidate,
            armed=prompt_cache_keepalive_service.is_enabled,
        )
    else:
        log.warning("gateway.prompt_cache_keepalive_recorder_unavailable")
    svc.prompt_cache_keepalive_service = prompt_cache_keepalive_service
    svc.goal_service = goal_service
    # Wire the runtime into SessionManager so kill_session can cascade-cancel.
    attach_runtime = getattr(svc.session_manager, "attach_task_runtime", None)
    if callable(attach_runtime):
        attach_runtime(task_runtime)
    from opensquilla.tools.builtin.sessions import set_task_runtime

    set_task_runtime(task_runtime)
    recovered_meta_controls = await task_runtime.recover_durable_meta_controls()
    if recovered_meta_controls:
        log.info(
            "task_runtime.meta_controls_recovered",
            count=recovered_meta_controls,
        )
    recover_stranded_steers = getattr(task_runtime, "recover_stranded_steers", None)
    if callable(recover_stranded_steers):
        steer_recovery = await recover_stranded_steers()
        if any(
            int(steer_recovery.get(field, 0) or 0)
            for field in ("applied", "promoted", "cancelled", "rejected", "resumed")
        ):
            log.info(
                "gateway.steer_restart_recovery_completed",
                **steer_recovery,
            )

    # Resolve HEARTBEAT.md path; instantiate Runner + Watcher;
    # start Watcher BEFORE the Loop so the first tick already sees any
    # frontmatter overrides. ``reload_now()`` runs synchronously at start.
    heartbeat_runner = HeartbeatRunner()
    workspace_dir = config.workspace_dir or ""
    md_path_setting = getattr(config.heartbeat, "config_path", None)
    if md_path_setting:
        heartbeat_md_path = Path(md_path_setting).expanduser()
    elif workspace_dir:
        heartbeat_md_path = Path(workspace_dir).expanduser() / "HEARTBEAT.md"
    else:
        heartbeat_md_path = Path.home() / ".opensquilla" / "workspace" / "HEARTBEAT.md"
    heartbeat_watcher = HeartbeatConfigWatcher(
        heartbeat_runner,
        heartbeat_md_path,
        loop_listener=heartbeat_loop.apply_overrides,
    )
    await heartbeat_watcher.start()
    svc.heartbeat_watcher = heartbeat_watcher

    await heartbeat_loop.start()
    svc.heartbeat_loop = heartbeat_loop

    # Register cron agent_run handler (DI-based, no monkey-patch)
    if svc.cron_scheduler is not None:
        from opensquilla.gateway.auto_propose_bridge import (
            AutoProposeRuntime,
            register_runtime,
        )
        from opensquilla.memory.dream_factory import build_dream_factory
        from opensquilla.scheduler.auto_propose_handler import make_auto_propose_handler
        from opensquilla.scheduler.delivery import DeliveryChain
        from opensquilla.scheduler.dream_handler import make_memory_dream_handler
        from opensquilla.scheduler.handlers import (
            make_agent_run_handler,
            make_static_message_handler,
            make_system_event_handler,
        )
        from opensquilla.scheduler.heartbeat_service import HeartbeatService
        from opensquilla.skills.creator.auto_propose import auto_propose
        from opensquilla.skills.meta.orchestrator import (
            MetaOrchestrator,
            make_agent_runner_from_parent,
            make_llm_chat_from_provider,
            make_tool_invoker_from_handler,
        )
        from opensquilla.tools.dispatch import build_tool_handler

        async def _cron_ws_emitter(topic: str, event: str, payload: dict) -> int:
            """Targeted WS push with per-connection error isolation."""
            _registry = get_registry()
            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return 0
            conn_ids = _sub_mgr.get_topic_subscribers(topic)
            conn_ids |= _sub_mgr.get_topic_subscribers("cron:*")
            sent = 0
            for conn_id in conn_ids:
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event(event, payload)
                        sent += 1
                    except Exception:
                        pass
            return sent

        async def _session_forwarder(
            origin_session_key: str,
            text: str,
            provenance: dict,
        ) -> None:
            if svc.session_manager is None:
                return

            entry = await svc.session_manager.append_message(
                origin_session_key,
                role="assistant",
                content=text,
                provenance=provenance,
            )

            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return

            payload = build_cron_result_payload(origin_session_key, text, entry)

            _registry = get_registry()
            stream_payload = get_session_streams().record(
                origin_session_key,
                "session.event.cron_result",
                payload,
            )
            for conn_id in _sub_mgr.get_message_subscribers(origin_session_key):
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event("session.event.cron_result", stream_payload)
                    except Exception:
                        pass

            sessions_changed_payload = build_sessions_changed_payload(
                origin_session_key, "cron_result"
            )
            for conn_id in (
                _sub_mgr.get_message_subscribers(origin_session_key)
                | _sub_mgr.get_session_subscribers()
            ):
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event("sessions.changed", sessions_changed_payload)
                    except Exception:
                        pass

        async def _emit_session_event(
            session_key: str,
            event_name: str,
            payload: dict[str, Any],
        ) -> None:
            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return

            _registry = get_registry()
            stream_payload = (
                get_session_streams().record(session_key, event_name, payload)
                if event_name.startswith("session.event.")
                else payload
            )
            conn_ids = _sub_mgr.get_message_subscribers(session_key)
            if event_name.startswith("sessions."):
                conn_ids |= _sub_mgr.get_session_subscribers()

            for conn_id in conn_ids:
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event(event_name, stream_payload)
                    except Exception:
                        pass

        delivery_chain = DeliveryChain(
            channel_manager_ref=lambda: _cm_holder[0],
            ws_emitter=_cron_ws_emitter,
            session_forwarder=_session_forwarder,
        )

        # Plug DeliveryChain.dispatch_failure_alert into execute_with_timeout
        # so every failed cron run (agent_run raise, system_event raise,
        # TimeoutError, generic exception) reaches the job's configured
        # FailureDestination at runtime. Without this wire the dispatch
        # plumbing is dead in production even though unit tests cover the
        # hook directly.
        from opensquilla.scheduler.jobs import set_failure_dispatcher, set_terminal_notifier

        set_failure_dispatcher(delivery_chain.dispatch_failure_alert)
        set_terminal_notifier(
            lambda job, execution: delivery_chain.notify_finished(
                job,
                success=execution.success,
                summary=execution.summary,
                session_key=execution.session_key,
                run_id=execution.id,
                error=execution.error,
            )
        )

        def _cron_workspace_resolver(agent_id: str) -> tuple[str | None, bool]:
            workspace_dir = resolve_agent_workspace_dir(agent_id, config)
            workspace_strict = getattr(config, "workspace_strict", None)
            if not isinstance(workspace_strict, bool):
                workspace_strict = bool(workspace_dir)
            return str(workspace_dir), workspace_strict

        auto_cfg = config.meta_skill.auto_propose
        auto_home = _gateway_home(config)
        auto_proposals_dir = auto_home / "proposals"
        auto_log_dir = Path(os.environ.get("OPENSQUILLA_LOG_DIR", str(auto_home / "logs")))
        auto_agent_ids = _configured_agent_ids(config)

        def _build_auto_propose_orchestrator(
            agent_id: str,
            *,
            triggered_by: str,
        ) -> MetaOrchestrator:
            if svc.provider_selector is None or not getattr(
                svc.provider_selector, "is_configured", True
            ):
                raise RuntimeError("auto_propose provider not configured")
            provider_selector = svc.provider_selector
            router_cfg = getattr(config, "squilla_router", None)
            tiers = getattr(router_cfg, "tiers", {}) if router_cfg is not None else {}
            from opensquilla.router_tiers import HIGHEST_TEXT_TIER

            t3_tier = tiers.get(HIGHEST_TEXT_TIER) if isinstance(tiers, dict) else None
            t3_model = ""
            t3_thinking_level = ""
            if isinstance(t3_tier, dict):
                t3_model = str(t3_tier.get("model") or "").strip()
                t3_thinking_level = str(
                    t3_tier.get("thinking_level") or t3_tier.get("thinking") or ""
                ).strip()

            clone_selector = getattr(provider_selector, "clone", None)
            if t3_model and callable(clone_selector):
                provider_selector = clone_selector()
                override_model = getattr(provider_selector, "override_model", None)
                if callable(override_model):
                    override_model(t3_model)

            resolver = getattr(provider_selector, "resolve", None)
            if not callable(resolver):
                raise RuntimeError("auto_propose provider selector has no resolve()")
            provider = resolver()
            workspace_dir = resolve_agent_workspace_dir(agent_id, config)
            workspace_str = str(workspace_dir) if workspace_dir else None
            ctx = _make_auto_propose_tool_context(
                agent_id=agent_id,
                workspace_dir=workspace_str,
            )
            if svc.tool_registry is None:
                raise RuntimeError("auto_propose tool registry not configured")
            if svc.skill_loader is None:
                raise RuntimeError("auto_propose skill loader not configured")
            tool_handler = build_tool_handler(svc.tool_registry, ctx)
            from opensquilla.engine.agent import Agent
            from opensquilla.engine.types import AgentConfig
            from opensquilla.skills.creator.proposer import (
                reset_runtime_e2e_context,
                reset_smoke_fixture_context,
                set_runtime_e2e_context,
                set_smoke_fixture_context,
            )
            from opensquilla.skills.creator.runtime_e2e import make_runtime_e2e_context

            auto_model_id = t3_model or resolve_agent_model(agent_id, config)
            auto_metadata: dict[str, Any] = {
                "routing_source": "meta_skill_auto_propose",
                "routing_applied": bool(t3_model),
            }
            if t3_model:
                auto_metadata.update(
                    {
                        "routed_tier": HIGHEST_TEXT_TIER,
                        "routed_model": t3_model,
                        "applied_model": t3_model,
                    }
                )
            if t3_thinking_level:
                auto_metadata.update(
                    {
                        "thinking_requested": True,
                        "thinking_level": t3_thinking_level,
                    }
                )

            base_config = AgentConfig(
                model_id=auto_model_id,
                provider_id=getattr(provider_selector, "active_provider_id", ""),
                workspace_dir=workspace_str,
                metadata=auto_metadata,
            )
            tool_definitions = svc.tool_registry.to_tool_definitions(ctx)
            auto_usage_context = _auto_propose_usage_execution_context(
                agent_id,
                usage_event_sink,
            )
            llm_chat = make_llm_chat_from_provider(
                provider=provider,
                base_config=base_config,
                usage_tracker=svc.usage_tracker,
                session_key=f"auto_propose:{agent_id}",
                usage_event_sink=usage_event_sink,
                usage_execution_context=auto_usage_context,
            )
            base_tool_invoker = make_tool_invoker_from_handler(tool_handler=tool_handler)
            runtime_e2e_ctx = make_runtime_e2e_context(
                provider=provider,
                base_config=base_config,
                skill_loader=svc.skill_loader,
                tool_definitions=tool_definitions,
                tool_handler=tool_handler,
                agent_factory=Agent,
                llm_chat=llm_chat,
                tool_invoker=base_tool_invoker,
                workspace_dir=workspace_str,
                usage_tracker=svc.usage_tracker,
                session_key=f"auto_propose:{agent_id}",
                tool_registry=svc.tool_registry,
                tool_context=ctx,
                system_prompt=base_config.system_prompt or "",
                baseline_model=base_config.model_id or "",
            )

            async def _tool_invoker(tool_name: str, args: dict[str, Any]) -> Any:
                if tool_name == "meta_skill_persist_proposal":
                    args = dict(args)
                    args.setdefault("home", str(auto_home))
                    args.setdefault("auto_enable_manual", False)
                token = set_runtime_e2e_context(runtime_e2e_ctx)
                smoke_token = set_smoke_fixture_context({"llm_chat": llm_chat})
                try:
                    return await base_tool_invoker(tool_name, args)
                finally:
                    reset_smoke_fixture_context(smoke_token)
                    reset_runtime_e2e_context(token)

            return MetaOrchestrator(
                agent_runner=make_agent_runner_from_parent(
                    provider=provider,
                    base_config=base_config,
                    tool_definitions=tool_definitions,
                    tool_handler=tool_handler,
                    agent_factory=Agent,
                    workspace_dir=workspace_str,
                    usage_tracker=svc.usage_tracker,
                    session_key=f"auto_propose:{agent_id}",
                    usage_event_sink=usage_event_sink,
                    usage_execution_context=auto_usage_context,
                ),
                skill_loader=svc.skill_loader,
                llm_chat=llm_chat,
                tool_invoker=_tool_invoker,
                workspace_dir=workspace_str,
                run_writer=getattr(svc, "meta_run_writer", None),
                triggered_by=triggered_by,
                session_key=f"auto_propose:{agent_id}",
                turn_id=None,
                usage_tracker=svc.usage_tracker,
            )

        async def _register_auto_propose_runtime_crons() -> None:
            await _register_auto_propose_crons(
                scheduler=svc.cron_scheduler,
                auto_cfg=auto_cfg,
                agent_ids=auto_agent_ids,
            )

        async def _pause_auto_propose_runtime_crons() -> None:
            await _pause_auto_propose_crons(
                scheduler=svc.cron_scheduler,
                agent_ids=auto_agent_ids,
            )

        async def _maybe_run_router_self_learning(agent_id: str) -> None:
            """Opportunistic router retrain, piggybacking on the dream cadence.

            Gated (off by default) and run in a worker thread so the
            subprocess-bounded LightGBM fit never blocks the event loop. Never
            raises onto the dream hook.
            """
            router_cfg = getattr(config, "squilla_router", None)
            sl_cfg = getattr(router_cfg, "self_learning", None)
            if sl_cfg is None or not bool(getattr(sl_cfg, "enabled", False)):
                return
            try:
                import anyio

                from opensquilla.squilla_router.self_learning.orchestrator import (
                    maybe_run_update_router,
                )

                result = await anyio.to_thread.run_sync(
                    lambda: maybe_run_update_router(agent_id, router_cfg=router_cfg)
                )
                log.info(
                    "router_self_learning.post_dream",
                    agent_id=agent_id,
                    ran=result.ran,
                    reason=result.reason,
                    version=result.version,
                )
            except Exception as exc:  # never poison the dream hook
                log.warning(
                    "router_self_learning.post_dream_error",
                    agent_id=agent_id,
                    error=str(exc),
                )

        async def _post_dream_auto_propose(
            agent_id: str,
            dream_summary: str = "",
        ) -> None:
            await _maybe_run_router_self_learning(agent_id)
            if not bool(getattr(auto_cfg, "on_dream_complete", False)):
                return
            result = await auto_propose(
                orchestrator=_build_auto_propose_orchestrator(
                    agent_id,
                    triggered_by="auto_dream",
                ),
                skill_loader=cast("SkillLoader", svc.skill_loader),
                log_dir=auto_log_dir,
                window_days=auto_cfg.window_days,
                min_freq=auto_cfg.min_freq,
                top_k=auto_cfg.top_k,
                triggered_by="dream",
                proposals_dir=auto_proposals_dir,
                auto_enable=bool(getattr(auto_cfg, "auto_enable", False)),
                auto_enable_max_risk=str(
                    getattr(auto_cfg, "auto_enable_max_risk", "low"),
                ),
                source_context=dream_summary,
            )
            log.info(
                "auto_propose.dream_hook.complete",
                agent_id=agent_id,
                summary=result.summary(),
                proposal_ids=result.proposals_created,
                enabled_proposal_ids=result.proposals_enabled,
                skipped=result.skipped,
                errors=result.errors,
            )

        agent_handler = make_agent_run_handler(
            delivery_chain=delivery_chain,
            turn_runner_ref=lambda: turn_runner,
            session_manager_ref=lambda: svc.session_manager,
            task_runtime_ref=lambda: task_runtime,
            workspace_resolver=_cron_workspace_resolver,
            default_elevated=lambda: configured_default_elevated(config),
        )
        system_handler = make_system_event_handler(
            delivery_chain=delivery_chain,
            turn_runner_ref=lambda: turn_runner,
            session_manager_ref=lambda: svc.session_manager,
            session_event_emitter=_emit_session_event,
            heartbeat_service_ref=lambda: heartbeat_service,
            heartbeat_loop_ref=lambda: heartbeat_loop,
            workspace_resolver=_cron_workspace_resolver,
            default_elevated=lambda: configured_default_elevated(config),
        )
        static_handler = make_static_message_handler(
            delivery_chain=delivery_chain,
            session_manager_ref=lambda: svc.session_manager,
            session_event_emitter=_emit_session_event,
        )
        auto_propose_handler = make_auto_propose_handler(
            build_orchestrator=lambda agent_id: _build_auto_propose_orchestrator(
                agent_id,
                triggered_by="auto_cron",
            ),
            skill_loader=cast("SkillLoader", svc.skill_loader),
            log_dir=auto_log_dir,
            proposals_dir=auto_proposals_dir,
            config=auto_cfg,
            enabled_predicate=lambda: bool(getattr(auto_cfg, "enabled", False)),
        )
        dream_handler = make_memory_dream_handler(
            build_dream=build_dream_factory(
                config=config,
                turn_runner=turn_runner,
            ),
            should_skip=lambda: (
                "disabled" if not getattr(config.memory.dream, "enabled", False) else None
            ),
            post_dream_hook=_post_dream_auto_propose,
            usage_event_sink=usage_event_sink,
        )
        svc.cron_scheduler.register_handler("agent_run", agent_handler)
        svc.cron_scheduler.register_handler("static_message", static_handler)
        svc.cron_scheduler.register_handler("system_event", system_handler)
        svc.cron_scheduler.register_handler("memory_dream", dream_handler)
        svc.cron_scheduler.register_handler("auto_propose", auto_propose_handler)
        log.info("gateway.cron_handler_registered", handler_key="agent_run")
        log.info("gateway.cron_handler_registered", handler_key="static_message")
        log.info("gateway.cron_handler_registered", handler_key="system_event")
        log.info("gateway.cron_handler_registered", handler_key="memory_dream")
        log.info("gateway.cron_handler_registered", handler_key="auto_propose")
        register_runtime(
            AutoProposeRuntime(
                config=auto_cfg,
                home=auto_home,
                register_crons=_register_auto_propose_runtime_crons,
                pause_crons=_pause_auto_propose_runtime_crons,
            )
        )
        await _register_dream_crons(
            scheduler=svc.cron_scheduler,
            memory_config=config.memory,
            agent_ids=_configured_agent_ids(config),
        )
        _warn_if_self_learning_unreachable(config)

        async def _reconcile_dream_runtime_crons() -> None:
            # Re-run the idempotent registrar against the LIVE config object:
            # a config RPC edit (e.g. the self-learning -> dream linkage) has
            # already mutated it in place by the time this fires, so jobs are
            # created/resumed/paused to match without a gateway restart.
            await _register_dream_crons(
                scheduler=svc.cron_scheduler,
                memory_config=config.memory,
                agent_ids=_configured_agent_ids(config),
            )

        from opensquilla.gateway.dream_bridge import register_dream_reconciler

        register_dream_reconciler(_reconcile_dream_runtime_crons)
        if bool(getattr(auto_cfg, "enabled", False)):
            await _register_auto_propose_runtime_crons()
        else:
            await _pause_auto_propose_runtime_crons()

        # Startup catch-up can execute overdue jobs immediately. Start only
        # after delivery, terminal notifications, and every handler are ready.
        await svc.cron_scheduler.start()
        log.info("build_services.cron_scheduler_started")

    # Build channel adapters (don't start yet -- app doesn't exist)
    webhook_routes: list = []
    if channel_manager is None and config.channels.channels:
        from opensquilla.channels.manager import ChannelManager
        from opensquilla.gateway.event_bridge import EventBridge

        event_bridge = EventBridge(
            subscription_manager=subscription_manager,
            connection_registry=get_registry(),
        )
        channel_rpc_context_factory = _make_channel_rpc_context_factory(
            svc,
            config,
            subscription_manager=subscription_manager,
            channel_manager_ref=lambda: _cm_holder[0],
            turn_runner=turn_runner,
            heartbeat_service=heartbeat_service,
            diagnostics_state=diagnostics_state,
        )
        channel_manager = ChannelManager.from_config(
            config.channels.channels,
            turn_runner=turn_runner,
            session_manager=svc.session_manager,
            event_bridge=event_bridge,
            config=config,
            task_runtime=task_runtime,
            rpc_dispatcher=get_dispatcher(),
            channel_rpc_context_factory=channel_rpc_context_factory,
        )
        webhook_routes = channel_manager.collect_webhook_routes()
        # Populate lazy ref so cron handler can deliver to channels
        _cm_holder[0] = channel_manager
        log.info(
            "gateway.channels_built",
            count=len(config.channels.channels),
            webhooks=len(webhook_routes),
        )

    # Ensure lazy ref covers pre-injected channel_manager too
    if channel_manager is not None:
        _cm_holder[0] = channel_manager

    async def _reconcile_runtime_channels() -> dict[str, str]:
        # Make running adapters match the LIVE config object: a channel CRUD
        # RPC has already mutated it in place by the time this fires. When no
        # manager exists yet (gateway booted with zero channels), build an
        # empty one so the first channel ever added still starts live —
        # webhook-mode entries are declined by reconcile() and stay
        # restart-gated because their HTTP routes are bound at app creation.
        manager = _cm_holder[0]
        if manager is None:
            from opensquilla.channels.manager import ChannelManager
            from opensquilla.gateway.event_bridge import EventBridge

            manager = ChannelManager.from_config(
                [],
                turn_runner=turn_runner,
                session_manager=svc.session_manager,
                event_bridge=EventBridge(
                    subscription_manager=subscription_manager,
                    connection_registry=get_registry(),
                ),
                config=config,
                task_runtime=task_runtime,
                rpc_dispatcher=get_dispatcher(),
                channel_rpc_context_factory=_make_channel_rpc_context_factory(
                    svc,
                    config,
                    subscription_manager=subscription_manager,
                    channel_manager_ref=lambda: _cm_holder[0],
                    turn_runner=turn_runner,
                    heartbeat_service=heartbeat_service,
                    diagnostics_state=diagnostics_state,
                ),
            )
            _cm_holder[0] = manager
        results: dict[str, str] = await manager.reconcile(config.channels.channels)
        return results

    from opensquilla.gateway.channels_bridge import register_channels_reconciler

    register_channels_reconciler(_reconcile_runtime_channels)

    # ── ASGI app ─────────────────────────────────────────────────────
    skill_management_state = getattr(svc, "skill_management_state", None)
    if skill_management_state is None:
        skill_management_state = {}
    app = create_gateway_app(
        config,
        session_manager=svc.session_manager,
        provider_selector=svc.provider_selector,
        tool_registry=svc.tool_registry,
        subscription_manager=subscription_manager,
        # Resolved per request: live reconcile may create the manager after
        # boot when the gateway started with zero channels configured.
        channel_manager=lambda: _cm_holder[0],
        usage_tracker=svc.usage_tracker,
        usage_event_sink=usage_event_sink,
        meta_run_writer=getattr(svc, "meta_run_writer", None),
        skill_loader=svc.skill_loader,
        skill_management_service=getattr(svc, "skill_management_service", None),
        skill_management_state=skill_management_state,
        cron_scheduler=svc.cron_scheduler,
        turn_runner=turn_runner,
        task_runtime=task_runtime,
        flush_service=svc.flush_service,
        heartbeat_service=heartbeat_service,
        heartbeat_loop=heartbeat_loop,
        prompt_cache_keepalive_service=prompt_cache_keepalive_service,
        agent_registry=svc.agent_registry,
        diagnostics_state=diagnostics_state,
        provider_stats=getattr(svc, "provider_stats", None),
        memory_managers=svc.memory_managers,
        memory_stores=svc.memory_stores,
        memory_retrievers=svc.memory_retrievers,
        extra_routes=webhook_routes or None,
    )
    app.state.gateway_ready = False
    app.state.desktop_gateway_ownership = _desktop_gateway_ownership
    if run:
        # Publish a shutdown trigger before uvicorn can expose the Desktop
        # identity endpoint. cli.gateway_cmd installs the final event handler
        # after this function returns; an early authenticated request is queued
        # by the relay instead of transiently returning 503 and wedging recovery.
        shutdown_relay = _GatewayShutdownRelay()
        app.state.request_shutdown = shutdown_relay
        app.state.install_shutdown_handler = shutdown_relay.install
    startup_phase_started_at = _log_gateway_startup_phase(
        "app",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )
    listener_ready = False
    runtime_state_ready = False
    gateway_ready_phase_emitted = False
    post_ready_observability_started = False
    gateway_ready_wait_started_at = startup_phase_started_at

    def _start_post_ready_observability() -> None:
        nonlocal post_ready_observability_started
        if post_ready_observability_started:
            return
        post_ready_observability_started = True

        # Anonymous install telemetry is best-effort. Its daemon worker is
        # never joined during shutdown, so a slow proxy cannot delay bind or
        # exit. Daily usage keeps its existing owned/cancelled asyncio task.
        _start_background_install_telemetry(config)
        try:
            from opensquilla.observability.usage_telemetry import (
                run_daily_usage_upload_loop,
            )

            daily_usage_storage = get_session_storage(svc.session_manager)
            if daily_usage_storage is not None:
                svc.daily_usage_telemetry_task = create_background_task(
                    run_daily_usage_upload_loop(
                        daily_usage_storage,
                        config=config,
                    )
                )
        except Exception:
            log.debug("gateway.usage_telemetry_upload_skipped", exc_info=True)

    def _publish_gateway_ready_if_complete() -> None:
        nonlocal gateway_ready_phase_emitted
        if gateway_ready_phase_emitted or not listener_ready or not runtime_state_ready:
            return
        gateway_ready_phase_emitted = True
        ready_at = time.monotonic()
        log.info(
            "gateway.startup_phase",
            phase="gateway_ready",
            status="ready",
            duration_ms=_elapsed_monotonic_ms(gateway_ready_wait_started_at, ready_at),
            startup_elapsed_ms=_elapsed_monotonic_ms(startup_started_at, ready_at),
        )
        _start_post_ready_observability()

    server_handle = GatewayServer(app=app, config=config)
    server_handle._pid_lock = _pid_lock
    server_handle._channel_manager = channel_manager
    server_handle._channel_manager_ref = lambda: _cm_holder[0]
    server_handle._services = svc
    server_handle._background_completion_manager = background_completion_manager
    server_handle._preview_service = getattr(app.state, "artifact_preview_service", None)

    if run:
        preview_service = server_handle._preview_service
        if preview_service is not None and config.control_ui.enabled:
            preview_socket: socket.socket | None = None
            try:
                from opensquilla.gateway.artifact_preview import (
                    create_artifact_preview_resource_app,
                )

                preview_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                preview_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                preview_socket.bind(("127.0.0.1", 0))
                preview_socket.listen(128)
                preview_socket.setblocking(False)
                preview_port = int(preview_socket.getsockname()[1])
                preview_service.set_listener_port(preview_port)
                preview_config = uvicorn.Config(
                    app=create_artifact_preview_resource_app(preview_service),
                    host="127.0.0.1",
                    port=preview_port,
                    log_level="warning",
                    access_log=False,
                    lifespan="off",
                )
                preview_server = uvicorn.Server(preview_config)
                setattr(preview_server, "install_signal_handlers", lambda: None)  # noqa: B010
                server_handle._preview_server = preview_server
                server_handle._preview_socket = preview_socket
                server_handle._preview_task = create_background_task(
                    preview_server.serve(sockets=[preview_socket])
                )
                log.info(
                    "gateway.artifact_preview_listener_started",
                    host="127.0.0.1",
                    port=preview_port,
                )
            except Exception:
                preview_service.clear_listener_port()
                if preview_socket is not None:
                    preview_socket.close()
                log.warning(
                    "gateway.artifact_preview_listener_unavailable",
                    category="listener_start_failed",
                )

        listener_scheduled_at = time.monotonic()
        listener_phase_emitted = False

        async def _notify_listener_ready() -> None:
            nonlocal listener_phase_emitted, listener_ready
            if listener_phase_emitted:
                return
            listener_phase_emitted = True
            listener_ready = True
            ready_at = time.monotonic()
            log.info(
                "gateway.startup_phase",
                phase="listener",
                status="ready",
                duration_ms=_elapsed_monotonic_ms(listener_scheduled_at, ready_at),
                startup_elapsed_ms=_elapsed_monotonic_ms(startup_started_at, ready_at),
            )
            _publish_gateway_ready_if_complete()

        uvicorn_kwargs: dict[str, Any] = {
            "app": app,
            "host": config.host,
            "port": config.port,
            "log_level": "info" if not config.debug else "debug",
            # Uvicorn invokes this only after its socket server has been
            # created. Keep the callback one-shot because callback_notify is
            # also used for periodic worker health notifications.
            "callback_notify": _notify_listener_ready,
            # Capability URLs and historical sessionKey query parameters are
            # bearer material. Keep request targets out of uvicorn's access
            # logger; structured gateway events remain available.
            "access_log": False,
        }
        if config.tls.keyfile and config.tls.certfile:
            uvicorn_kwargs["ssl_keyfile"] = config.tls.keyfile
            uvicorn_kwargs["ssl_certfile"] = config.tls.certfile
        uv_config = uvicorn.Config(
            **uvicorn_kwargs,
        )
        server = uvicorn.Server(uv_config)
        # The run command (cli.gateway_cmd._run) installs its own SIGINT/SIGTERM
        # handlers that trigger GatewayServer.close() — the only path that drains
        # in-flight agent turns and background completions. uvicorn's default
        # handlers would race ours and exit without that drain, so suppress them
        # and let the embedding process own shutdown signalling.
        # setattr (not direct assignment) so this is robust to uvicorn type stubs
        # that don't expose install_signal_handlers — it exists at runtime.
        setattr(server, "install_signal_handlers", lambda: None)  # noqa: B010
        server_handle._server = server

        listener_scheduled_at = time.monotonic()
        task = create_background_task(server.serve())
        server_handle._task = task

        # Warn loudly before the normal started line so operators
        # see the network-exposure notice even on info-level log streams.
        if is_public_bind(config.host):
            log.warning(
                "gateway.bind.public",
                host=config.host,
                port=config.port,
                message=(
                    "gateway bound to a wildcard address; reachable from "
                    "every interface. Opt-in required — only expose behind "
                    "a trusted reverse proxy or VPN."
                ),
            )
        log.info("gateway.started", host=config.host, port=config.port)
        if _desktop_fast_start_enabled():
            svc.deferred_warmup_task = create_background_task(_run_deferred_warmups(svc))

    # Start channels (after app is ready to receive webhooks)
    if channel_manager is not None:
        results = await channel_manager.start_all()
        start_errors_fn = getattr(channel_manager, "start_errors", None)
        start_errors = start_errors_fn() if start_errors_fn is not None else {}
        for name, ok in results.items():
            if ok:
                log.info("gateway.channel_started", channel=name)
            else:
                details = start_errors.get(name, {})
                log.warning(
                    "gateway.channel_failed",
                    channel=name,
                    error_type=details.get("error_type"),
                    error=details.get("error"),
                    exception=details.get("exception"),
                )

    if run and _desktop_router_preload_enabled():
        create_background_task(preload_squilla_router_runtime(config))
    elif run:
        log.info("gateway.squilla_router_preload_skipped", reason="desktop_fast_start")

    app.state.gateway_ready = True
    runtime_state_ready = True
    _log_gateway_startup_phase(
        "runtime_state",
        startup_started_at=startup_started_at,
        phase_started_at=startup_phase_started_at,
    )
    _publish_gateway_ready_if_complete()
    if not run:
        # Embedders/tests without a network listener still retain the existing
        # telemetry lifecycle, but only after in-process readiness is visible.
        _start_post_ready_observability()
    usage_storage = get_session_storage(svc.session_manager)
    if usage_storage is not None and hasattr(usage_storage, "get_usage_backfill_batch"):
        from opensquilla.gateway.usage_backfill import run_usage_backfill

        svc.usage_backfill_task = create_background_task(run_usage_backfill(usage_storage))
    return server_handle
