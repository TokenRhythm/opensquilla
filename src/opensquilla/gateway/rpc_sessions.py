"""RPC handlers for the sessions domain."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import re
import threading
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import structlog

from opensquilla.agents.scope import default_workspace_dir, resolve_agent_workspace_dir
from opensquilla.application.admission_views import AdmissionAcceptance
from opensquilla.application.admission_views import SessionIdentity as AdmissionSessionIdentity
from opensquilla.application.pending_input_queue import (
    PendingCancellationConflictError,
    PendingDispatchReplay,
    PendingInputConflictError,
    PendingInputMissingError,
    PendingInputProjection,
    PendingInputQueuePort,
    PendingInputRevision,
)
from opensquilla.application.session_directory import (
    SessionDirectory,
    SessionSearchProjection,
    _resolve_session_record_for_bootstrap,
)
from opensquilla.application.session_lifecycle import (
    ForkSessionSpec,
    NewSession,
    SessionCreationKind,
    SessionCreationPolicyPort,
    SessionDeletionPort,
    SessionForked,
    SessionForkMode,
    SessionIdentity,
    SessionLifecycle,
    SessionLifecycleEventsPort,
    SessionLifecycleStorePort,
    SessionWorkspaceBinding,
)
from opensquilla.application.session_read import (
    SessionMetadataQuery,
    SessionPlanningState,
    SessionReadApplication,
    SessionRunModeLock,
    SessionTaskState,
    SessionWorkspaceState,
    deferred_session_read_metadata,
)
from opensquilla.application.turn_acceptance import DurableTurnAdmission
from opensquilla.application.turn_acceptance_ports import (
    AdmissionPolicy,
    AdmissionPrimitives,
    AdmissionStorage,
)
from opensquilla.application.turn_admission import (
    AdmitTurn,
    AdmitTurnResult,
    TurnAdmission,
)
from opensquilla.application.turn_cancellation import (
    CancellationPrimitives,
    CancellationTiming,
    ExactCancellationUnavailableError,
    TurnCancellation,
)
from opensquilla.application.turn_input import IncomingTurnSource, PlanAdmissionContext
from opensquilla.application.turn_steering import TurnSteering
from opensquilla.attachment_refs import (
    PENDING_CHAT_INPUT_MATERIAL_STORE,
    cleanup_pending_chat_input_material,
    read_pending_chat_input_promotions,
    transcript_material_path,
)
from opensquilla.engine.cache_break_monitor import (
    cancel_active_compactions,
)
from opensquilla.engine.steps.router_decision_record import (
    drain_pending_flushes_for_sessions,
)
from opensquilla.gateway.adapters.pending_input_queue import (
    GatewayPendingInputQueueAdapter,
)
from opensquilla.gateway.adapters.pending_input_queue_contract import (
    register_pending_input_queue_contract,
)
from opensquilla.gateway.adapters.plans_contract import (
    register_plans_cancel_run_contract,
    register_plans_capabilities_contract,
    register_plans_implement_contract,
    register_plans_revise_contract,
    register_plans_set_mode_contract,
)
from opensquilla.gateway.adapters.session_control_contract import (
    register_session_control_contract,
)
from opensquilla.gateway.adapters.session_history_projection import read_chat_history_v4
from opensquilla.gateway.adapters.session_lifecycle import (
    GatewaySessionLifecycleAdapter,
)
from opensquilla.gateway.adapters.session_lifecycle_contract import (
    register_session_lifecycle_contract,
)
from opensquilla.gateway.adapters.session_maintenance import (
    GatewaySessionMaintenanceAdapter,
    build_gateway_session_maintenance_adapter,
)
from opensquilla.gateway.adapters.session_maintenance_contract import (
    register_session_maintenance_contract,
)
from opensquilla.gateway.adapters.session_preview import (
    SystemClock,
    preview_params_from_v4,
    preview_query_from_v4_values,
    preview_result_to_v4,
)
from opensquilla.gateway.adapters.session_read import (
    GatewaySessionReadPorts,
    build_v4_session_read_application,
    session_read_metadata_to_v4,
    session_read_snapshot_to_v4,
)
from opensquilla.gateway.adapters.session_read_contract import (
    register_sessions_messages_hydrate_contract,
    register_sessions_messages_snapshot_contract,
    register_sessions_messages_subscribe_contract,
    register_sessions_messages_unsubscribe_contract,
    register_sessions_preview_contract,
)
from opensquilla.gateway.adapters.session_reset import (
    GatewaySessionResetAdapter,
    build_gateway_session_reset_adapter,
)
from opensquilla.gateway.adapters.sessions_list_contract import (
    register_sessions_list_contract,
)
from opensquilla.gateway.adapters.sessions_resolve_contract import (
    register_sessions_resolve_contract,
)
from opensquilla.gateway.adapters.sessions_search_contract import (
    register_sessions_search_contract,
)
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    map_admission_error,
)
from opensquilla.gateway.adapters.turn_admission_contract import (
    register_turn_admission_contract,
)
from opensquilla.gateway.admission_failures import translate_admission_failure
from opensquilla.gateway.admission_input import decode_admit_turn, source_hint_from_turn
from opensquilla.gateway.admission_preparation import (
    ArtifactBinding,
    PreparedRuntimeRoute,
)
from opensquilla.gateway.admission_preparation import (
    bind_artifact as bind_admission_artifact,
)
from opensquilla.gateway.admission_preparation import (
    prepare_route as prepare_admission_route,
)
from opensquilla.gateway.admission_runtime import GatewayAdmissionRuntime
from opensquilla.gateway.admission_storage import GatewayAdmissionSessions, GatewayAdmissionStorage
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    artifact_product_error,
    logged_artifact_product_error,
)
from opensquilla.gateway.compaction_target import (
    validate_gateway_session_deployment_override,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.model_routing import model_routing_patches
from opensquilla.gateway.pending_input_primitives import (
    GatewayPendingInputPrimitives,
    pending_input_projection,
)
from opensquilla.gateway.project_workspace_runtime import (
    authoritative_project_run_context,
    map_project_workspace_error,
    persisted_project_workspace_snapshot,
    project_workspace_snapshot,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_event_publisher import (
    buffer_session_event,
    prepare_session_event_payload,
    send_prepared_to_subscribers,
)
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_maintenance_runtime import (
    TaskScopedCancelUnsupportedError as _TaskScopedCancelUnsupportedError,
)
from opensquilla.gateway.session_maintenance_runtime import (
    build_session_flush_correlation as _build_session_flush_correlation,
)
from opensquilla.gateway.session_maintenance_runtime import (
    cancel_task_runtime as _cancel_task_runtime,
)
from opensquilla.gateway.session_maintenance_runtime import (
    durable_checkpoint_covers_transcript as _durable_receipt_allows_covered_destructive_compaction,
)
from opensquilla.gateway.session_services import (
    get_session_epoch,
    get_session_lock,
    get_session_storage,
    set_session_epoch,
)
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.session_view import build_session_view_item, derive_transcript_title
from opensquilla.gateway.subagent_announce import (
    quiesce_background_completion_sessions,
)
from opensquilla.gateway.turn_ingress import (
    accepted_turn_payload,
)
from opensquilla.gateway.turn_steering import GatewaySteeringPrimitives
from opensquilla.gateway.uploads import get_upload_store
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.paths import media_root_from_config, native_io_path
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
)
from opensquilla.run_mode import (
    RunMode,
    config_run_mode,
    normalize_run_mode,
    project_default_run_mode,
)
from opensquilla.sandbox.guest_profile import (
    GuestProfileFactory,
)
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    RunContext,
)
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    principal_has_host_execute,
    run_mode_allowed_for_principal,
)
from opensquilla.session.compaction_lifecycle import (
    compaction_memory_status,
    flush_receipt_status_for_compaction,
    flush_receipt_to_dict,
    flush_trigger_enabled,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id, parse_agent_id
from opensquilla.session.models import (
    AgentTaskStatus,
    SessionStatus,
)
from opensquilla.session.naming import (
    generate_session_title,
    is_naming_eligible,
    title_slot_is_empty,
)
from opensquilla.session.plans import PlanConflictError, PlanRunConflictError
from opensquilla.session.storage import (
    PendingChatInput,
    PendingChatInputConflictError,
    PendingChatInputNotFoundError,
    SessionListCursor,
    SessionRoutingConflictError,
    SessionStorage,
    TurnAcceptanceResult,
    bounded_interactive_storage_reads,
)
from opensquilla.session.terminal_reply import (
    append_error_ref,
    build_terminal_reply,
    safe_provider_failure_code,
    safe_provider_failure_message,
    sanitize_agent_error,
)

_d = get_dispatcher()

_SESSION_ROUTING_MODES = frozenset({"direct", "router", "ensemble"})

_PENDING_INPUT_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _pending_input_lock_for(pending_input_id: str) -> asyncio.Lock:
    """Serialize filesystem ownership with the SQLite pending-row lifecycle."""

    lock = _PENDING_INPUT_LOCKS.get(pending_input_id)
    if lock is None:
        lock = asyncio.Lock()
        _PENDING_INPUT_LOCKS[pending_input_id] = lock
    return lock


async def _pending_input_enqueue_lock(
    ctx: RpcContext,
    session_key: str,
    pending_input_id: str,
):
    """Fence enqueue against session reset/delete after serializing its id."""

    async with _pending_input_lock_for(pending_input_id):
        session_lock = get_session_lock(ctx.turn_runner, session_key)
        if session_lock is None:
            yield
        else:
            async with session_lock:
                yield


log = structlog.get_logger(__name__)
_ELEVATED_MODES = frozenset({"full"})
_TRUSTED_ELEVATED_ALIASES = frozenset({"on", "bypass"})


def _prompt_annotation_source_only_context(context: Any) -> Any:
    """Downgrade a PromptAnnotation turn when candidate preview is unavailable.

    The autonomous ten-tool surface requires a live protocol-v4 Desktop
    bridge: without it a writer could stage a DRAFT but could never obtain a
    verification receipt or finish it.  Use the established source-only
    compatibility surface instead of exposing a dead-end candidate loop.  The
    source writer remains durable and the prompt explicitly tells the model
    not to claim a preview verification it could not perform.
    """

    from opensquilla.gateway.artifact_contexts import (
        PROMPT_ANNOTATION_SOURCE_TOOL_NAMES,
    )
    from opensquilla.prompt_annotations import render_active_prompt_annotation_context

    snapshots = getattr(context, "snapshots", ())
    return replace(
        context,
        tool_names=PROMPT_ANNOTATION_SOURCE_TOOL_NAMES,
        request_context_prompt=(
            render_active_prompt_annotation_context(
                snapshots,
                autonomous_loop=False,
            )
            or context.request_context_prompt
        ),
    )


def _desktop_artifact_bridge_supports_candidate_loop(capabilities: Any) -> bool:
    """Return whether the active Desktop surface can complete a candidate loop.

    Protocol version alone is not enough: the v4 contract is also used for
    non-HTML/office surfaces and for a shell whose active preview is still
    loading.  Exposing the ten-tool contract in those states would create a
    DRAFT that can be staged but can never obtain a verification receipt or
    restore the canonical preview.  Require the capabilities that are stable
    before the first candidate is bound; ``browserAct`` is intentionally not
    required because the native surface enables it only after binding the
    opaque candidate handle.
    """

    if capabilities is None:
        return False
    if isinstance(capabilities, Mapping):
        version = capabilities.get("version")

        def _flag(*names: str) -> bool:
            return any(capabilities.get(name) is True for name in names)

    else:
        version = getattr(capabilities, "version", None)

        def _flag(*names: str) -> bool:
            return any(getattr(capabilities, name, None) is True for name in names)

    values = (
        _flag("available"),
        _flag("browserInspect", "browser_inspect"),
        _flag("bindCandidatePreview", "bind_candidate_preview"),
        _flag("restoreCanonicalPreview", "restore_canonical_preview"),
    )
    return (
        isinstance(version, int) and not isinstance(version, bool) and version >= 4 and all(values)
    )


def _emit_steer_metric(disposition: str, **labels: Any) -> None:
    log.info(
        "steer_inputs_total",
        metric="steer_inputs_total",
        value=1,
        disposition=disposition,
        **labels,
    )


if TYPE_CHECKING:
    pass

_SESSION_SUBSCRIBE_REPLAY_BUDGET_SECONDS = 2.0
_ARTIFACT_STATE_EVENT_FIELDS = (
    "artifactEventSeq",
    "documentId",
    "revisionId",
    "changeSetId",
    "action",
)


def _coerce_positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _accepts_keyword_arg(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _artifact_state_event_emitter(
    ctx: RpcContext,
    session_key: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Bind a metadata-only ArtifactSession event sink to this RPC connection."""

    from opensquilla.gateway.event_bridge import EventBridge
    from opensquilla.gateway.websocket import get_registry

    bridge = EventBridge(ctx.subscription_manager, get_registry())

    async def emit(payload: dict[str, Any]) -> None:
        sequence = payload.get("artifactEventSeq")
        document_id = payload.get("documentId")
        action = payload.get("action")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(document_id, str)
            or not document_id
            or not isinstance(action, str)
            or not action
        ):
            raise ValueError("invalid artifact state event metadata")
        for field_name in ("revisionId", "changeSetId"):
            value = payload.get(field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("invalid artifact state event identifier")
        safe_payload = {
            field_name: payload.get(field_name) for field_name in _ARTIFACT_STATE_EVENT_FIELDS
        }
        # Dual-publish while existing clients still subscribe to the artifact
        # event name. Both notifications carry the same metadata-only payload.
        await bridge.emit(session_key, "session.event.artifact_state", safe_payload)
        await bridge.emit(session_key, "document.state_changed", safe_payload)

    return emit


_FORK_TITLE_SUFFIX_RE = re.compile(r"^(?P<base>.+) \((?P<number>[2-9][0-9]*)\)$")
_FORK_TITLE_SCAN_PAGE_SIZE = 500
_FORK_TITLE_ALLOCATOR_GUARD = threading.Lock()
_FORK_TITLE_ALLOCATOR_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int, str, str],
    asyncio.Lock,
] = weakref.WeakValueDictionary()


def _fork_title_family(title: str) -> tuple[str, int]:
    """Parse a possible copy suffix without deciding whether it is system-owned."""

    match = _FORK_TITLE_SUFFIX_RE.fullmatch(title)
    if match is None:
        return title, 1
    try:
        number = int(match.group("number"))
    except ValueError:
        return title, 1
    return match.group("base"), number


def _session_fork_title_family(
    session: Any,
    *,
    titles_by_key: dict[str, str],
    sessions_by_key: dict[str, Any],
    memo: dict[str, tuple[str, int]],
    visiting: set[str],
) -> tuple[str, int]:
    """Resolve a title family only when fork lineage proves the suffix is generated."""

    session_key = str(getattr(session, "session_key", "") or "")
    title = titles_by_key.get(session_key, "")
    cached = memo.get(session_key)
    if cached is not None:
        return cached
    literal = (title, 1)
    if not session_key or session_key in visiting:
        return literal
    if not getattr(session, "forked_from_parent", False):
        memo[session_key] = literal
        return literal
    parent_key = str(getattr(session, "parent_session_key", "") or "")
    parent = sessions_by_key.get(parent_key)
    parsed_base, parsed_number = _fork_title_family(title)
    if parent is None or parsed_number == 1:
        memo[session_key] = literal
        return literal

    visiting.add(session_key)
    try:
        parent_base, parent_number = _session_fork_title_family(
            parent,
            titles_by_key=titles_by_key,
            sessions_by_key=sessions_by_key,
            memo=memo,
            visiting=visiting,
        )
    finally:
        visiting.discard(session_key)
    resolved = (
        (parsed_base, parsed_number)
        if parsed_base == parent_base and parsed_number > parent_number
        else literal
    )
    memo[session_key] = resolved
    return resolved


def _fork_title_allocator_lock(
    storage: Any,
    *,
    agent_id: str,
    base_title: str,
) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), id(storage), agent_id, base_title)
    with _FORK_TITLE_ALLOCATOR_GUARD:
        lock = _FORK_TITLE_ALLOCATOR_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _FORK_TITLE_ALLOCATOR_LOCKS[key] = lock
        return lock


def _session_sidebar_title(
    session: Any,
    *,
    transcript_title: str,
    channel_types: dict[str, str],
) -> str:
    view = build_session_view_item(
        session,
        entry_count=0,
        task_rows=[],
        now_ms=int(time.time() * 1000),
        transcript_title=transcript_title,
        channel_types=channel_types,
    )
    return str(view.get("title") or "")


async def _fork_title_state(
    ctx: RpcContext,
    storage: Any,
    parent: Any,
) -> tuple[str, int]:
    """Return the lineage-aware title family and current highest copy number."""

    parent_key = str(getattr(parent, "session_key", "") or "")
    agent_id = _effective_agent_id_for_session(parent, parent_key)
    sessions: list[Any] = []
    offset = 0
    while True:
        page = await storage.list_sessions(
            agent_id=agent_id,
            limit=_FORK_TITLE_SCAN_PAGE_SIZE,
            offset=offset,
        )
        sessions.extend(page)
        if len(page) < _FORK_TITLE_SCAN_PAGE_SIZE:
            break
        offset += len(page)

    if all(getattr(session, "session_key", None) != parent.session_key for session in sessions):
        sessions.append(parent)

    transcript_titles = await _list_transcript_titles(storage, sessions)
    channel_types = _channel_types_from_config(getattr(ctx, "config", None))
    sessions_by_key = {
        str(getattr(session, "session_key", "") or ""): session for session in sessions
    }
    titles_by_key = {
        str(getattr(session, "session_key", "") or ""): _session_sidebar_title(
            session,
            transcript_title=transcript_titles.get(getattr(session, "session_id", ""), ""),
            channel_types=channel_types,
        )
        for session in sessions
    }
    memo: dict[str, tuple[str, int]] = {}
    base_title, parent_number = _session_fork_title_family(
        parent,
        titles_by_key=titles_by_key,
        sessions_by_key=sessions_by_key,
        memo=memo,
        visiting=set(),
    )
    highest_number = parent_number
    for candidate in sessions:
        candidate_base, candidate_number = _session_fork_title_family(
            candidate,
            titles_by_key=titles_by_key,
            sessions_by_key=sessions_by_key,
            memo=memo,
            visiting=set(),
        )
        if candidate_base == base_title:
            highest_number = max(highest_number, candidate_number)
    return base_title, highest_number


async def _next_fork_display_name(ctx: RpcContext, storage: Any, parent: Any) -> str:
    """Allocate the next copy-style title using the same title contract as sessions.list."""

    base_title, highest_number = await _fork_title_state(ctx, storage, parent)
    return f"{base_title} ({highest_number + 1})"


@contextlib.asynccontextmanager
async def _fork_title_allocation_context(
    ctx: RpcContext,
    storage: Any,
    parent: Any,
):
    """Serialize one title family while holding the source session mutation lock."""

    parent_key = str(getattr(parent, "session_key", "") or "")
    parent_lock = get_session_lock(ctx.turn_runner, parent_key)

    @contextlib.asynccontextmanager
    async def allocation_locked():
        current_parent = await storage.get_session(parent_key)
        if current_parent is None:
            raise KeyError(f"Session not found: {parent_key}")
        base_title, _highest_number = await _fork_title_state(ctx, storage, current_parent)
        agent_id = _effective_agent_id_for_session(current_parent, parent_key)
        allocator_lock = _fork_title_allocator_lock(
            storage,
            agent_id=agent_id,
            base_title=base_title,
        )
        async with allocator_lock:
            yield

    if parent_lock is None:
        async with allocation_locked():
            yield
        return
    async with parent_lock:
        async with allocation_locked():
            yield


async def _fork_with_numbered_title(
    ctx: RpcContext,
    storage: Any,
    parent_key: str,
    child_key: str,
    *,
    explicit_title: str | None,
    **branch_kwargs: Any,
) -> Any:
    """Create and title a fork while holding the parent's mutation lock when available."""

    async def create_with_display_name(display_name: str) -> Any:
        return await ctx.session_manager.branch(
            parent_key,
            child_key,
            display_name=display_name,
            **branch_kwargs,
        )

    async def create_explicit_locked() -> Any:
        parent = await storage.get_session(parent_key)
        if parent is None:
            raise KeyError(f"Session not found: {parent_key}")
        assert explicit_title is not None
        return await create_with_display_name(explicit_title)

    parent = await storage.get_session(parent_key)
    if parent is None:
        raise KeyError(f"Session not found: {parent_key}")
    if explicit_title:
        lock = get_session_lock(ctx.turn_runner, parent_key)
        if lock is None:
            return await create_explicit_locked()
        async with lock:
            return await create_explicit_locked()
    async with _fork_title_allocation_context(ctx, storage, parent):
        current_parent = await storage.get_session(parent_key)
        if current_parent is None:
            raise KeyError(f"Session not found: {parent_key}")
        display_name = await _next_fork_display_name(ctx, storage, current_parent)
        return await create_with_display_name(display_name)


def _clean_cancel_source(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", ".", ":"} else "_" for ch in text)
    return (safe.strip("_") or default)[:80]


def _truncate_removed_entries(transcript: list[Any], max_messages: int) -> list[Any]:
    if max_messages < 0:
        return list(transcript)
    if len(transcript) <= max_messages:
        return []
    if max_messages == 0:
        return list(transcript)
    return list(transcript[:-max_messages])


def _truncate_checkpoint_scope_entries(
    transcript: list[Any],
    max_messages: int,
) -> list[Any]:
    removed_entries = _truncate_removed_entries(transcript, max_messages)
    return removed_entries or list(transcript)


def _trusted_elevated_hint(ctx: RpcContext, source_hint: dict[str, Any]) -> str | None:
    """Return an operator-owned elevated hint, or None."""

    value = source_hint.get("elevated")
    if isinstance(value, str) and value in _ELEVATED_MODES and ctx.principal.is_owner:
        return value
    return None


def _trusted_run_mode_hint(ctx: RpcContext, source_hint: dict[str, Any]) -> Any | None:
    value = source_hint.get("runMode") or source_hint.get("run_mode")
    if isinstance(value, str):
        try:
            run_mode = normalize_run_mode(value)
        except ValueError:
            return None
        if run_mode == RunMode.FULL and not principal_has_host_execute(ctx.principal):
            raise RpcHandlerError(
                "HOST_CAPABILITY_REQUIRED",
                "Full access requires a valid token with host execution permission.",
            )
        if run_mode_allowed_for_principal(run_mode, ctx.principal):
            return run_mode
        return None

    elevated = source_hint.get("elevated")
    if not isinstance(elevated, str):
        return None
    if not ctx.principal.is_owner:
        return None
    if elevated in _TRUSTED_ELEVATED_ALIASES:
        return RunMode.SAFE
    if elevated == "full":
        return RunMode.FULL
    return None


def _guest_profile_for_principal(
    principal: Any,
    task_id: str,
    *,
    state_dir: str | Path,
):
    has_capability = getattr(principal, "has", lambda _capability: False)
    if has_capability("guest.safe") and not principal_has_host_execute(principal):
        runtime_roots: tuple[Path, ...] = ()
        runtime_path: tuple[Path, ...] = ()
        try:
            from opensquilla.runtime_packs import (
                RuntimePackResolver,
                get_runtime_pack_service,
            )
            from opensquilla.sandbox.policy_store import SandboxPolicyStore

            runtime_policy = SandboxPolicyStore(Path(state_dir) / "sessions.db").read().runtimes
            service = get_runtime_pack_service(state_dir)
            if service.management_supported:
                resolver = RuntimePackResolver(service)
                runtime_roots = resolver.runtime_roots(runtime_policy)
                runtime_path = resolver.managed_path(runtime_policy)
            else:
                from opensquilla.sandbox.runtime_launcher import bundled_runtime_resolver

                legacy = bundled_runtime_resolver()
                runtime_roots = legacy.runtime_roots(runtime_policy) if legacy is not None else ()
                runtime_path = legacy.bundled_path(runtime_policy) if legacy is not None else ()
        except (OSError, RuntimeError, ValueError):
            # Guest remains strictly managed with an empty PATH. Runtime state
            # corruption must not make session creation or Gateway boot fail.
            runtime_roots = ()
            runtime_path = ()
        return GuestProfileFactory.create(
            task_id,
            state_dir=state_dir,
            runtime_roots=runtime_roots,
            runtime_path=runtime_path,
        )
    return None


def _is_remote_web_guest(principal: Any, source_hint: dict[str, Any]) -> bool:
    # Source hints are client-controlled presentation metadata.  They must not
    # weaken the server-computed authority of an unauthenticated guest.
    del source_hint
    has_capability = getattr(principal, "has", lambda _capability: False)
    return bool(has_capability("guest.safe") and not principal_has_host_execute(principal))


def _channel_types_from_config(config: Any) -> dict[str, str]:
    """Lowercased configured-channel-name -> platform-type map for the view."""
    channels_cfg = getattr(getattr(config, "channels", None), "channels", None) or []
    out: dict[str, str] = {}
    for entry in channels_cfg:
        name = str(getattr(entry, "name", "") or "").strip().lower()
        ctype = str(getattr(entry, "type", "") or "").strip().lower()
        if name and ctype:
            out[name] = ctype
    return out


_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS = 2.0
_ABORT_OWNED_CLEANUP_SECONDS = 30.0
_ABORT_SESSION_LOOKUP_SECONDS = 0.05
_ABORT_TREE_STABILIZATION_PASSES = 8
_ACTIVE_TASK_STATUSES = frozenset({"queued", "running"})


def _consume_abort_background_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


async def _await_abort_operation(
    awaitable: Any,
    *,
    deadline_at_monotonic: float,
    operation: str,
    default: Any,
) -> Any:
    """Run one Stop operation without letting it extend the shared deadline.

    ``asyncio.wait_for`` may wait past its timeout while a callee handles task
    cancellation.  Stop must return promptly, so a timed-out operation is
    cancelled and consumed in the background instead of being synchronously
    drained.  Cancellation requests already issued by that operation remain
    best-effort and may still settle after the RPC returns.
    """

    if isinstance(awaitable, asyncio.Future) and awaitable.done():
        return awaitable.result()
    remaining = max(0.0, deadline_at_monotonic - time.monotonic())
    if remaining <= 0:
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            awaitable.add_done_callback(_consume_abort_background_result)
            log.warning("sessions.abort.operation_budget_exhausted", operation=operation)
            return default
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        log.warning("sessions.abort.operation_budget_exhausted", operation=operation)
        return default

    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_abort_background_result)
        raise
    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_abort_background_result)
    log.warning("sessions.abort.operation_timed_out", operation=operation)
    return default


async def _await_abort_background_task(
    task: asyncio.Task[Any],
    *,
    deadline_at_monotonic: float,
    operation: str,
    default: Any,
) -> Any:
    """Observe safety cleanup within the RPC budget without cancelling it.

    Process-tree ownership has its own bounded cleanup deadline.  The Stop RPC
    may return first, but it must never cancel a cleanup task that has already
    discovered or started terminating exact task-owned descendants.
    """

    if task.done():
        return task.result()
    remaining = max(0.0, deadline_at_monotonic - time.monotonic())
    if remaining > 0:
        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_abort_background_result)
            raise
        if task in done:
            return task.result()
    task.add_done_callback(_consume_abort_background_result)
    log.warning("sessions.abort.cleanup_continuing", operation=operation)
    return default


def _task_status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


async def _active_task_runtime_ids(task_runtime: Any, session_key: str) -> tuple[str, ...]:
    if not hasattr(task_runtime, "list"):
        return ()
    try:
        rows = await task_runtime.list(session_key=session_key)
    except Exception:
        log.warning("sessions.abort.task_runtime_list_failed", session_key=session_key)
        return ()
    task_ids: list[str] = []
    for row in rows:
        if _task_status_value(getattr(row, "status", None)) not in _ACTIVE_TASK_STATUSES:
            continue
        task_id = getattr(row, "task_id", "")
        if isinstance(task_id, str) and task_id and task_id not in task_ids:
            task_ids.append(task_id)
    return tuple(task_ids)


def _task_record_session_key(row: Any) -> str | None:
    session_key = getattr(row, "session_key", None)
    if isinstance(session_key, str) and session_key:
        return session_key
    return None


def _task_record_parent_identity(row: Any) -> tuple[str, str] | None:
    """Return the exact parent task/session for one durable subagent task row."""

    if str(getattr(row, "run_kind", "") or "") != "subagent":
        return None
    details = getattr(row, "details", None)
    metadata = details.get("metadata") if isinstance(details, dict) else None
    if not isinstance(metadata, dict):
        return None
    parent_task_id = metadata.get("parent_task_id")
    parent_session_key = metadata.get("parent_session_key")
    if not isinstance(parent_task_id, str) or not parent_task_id:
        return None
    if not isinstance(parent_session_key, str) or not parent_session_key:
        return None
    return parent_task_id, parent_session_key


async def _task_runtime_rows(task_runtime: Any) -> tuple[Any, ...]:
    """Read active/background-owner rows plus their bounded durable ancestry."""

    list_tasks = getattr(task_runtime, "list", None)
    if not callable(list_tasks):
        return ()
    try:
        rows: list[Any] = []
        for status_value in _ACTIVE_TASK_STATUSES:
            rows.extend(await list_tasks(status=status_value))
    except TypeError:
        try:
            rows = list(await list_tasks())
        except (TypeError, NotImplementedError):
            return ()
        except Exception:
            log.warning("sessions.abort.task_runtime_tree_list_failed")
            return ()
    except NotImplementedError:
        return ()
    except Exception:
        log.warning("sessions.abort.task_runtime_tree_list_failed")
        return ()

    # A child may have already yielded after spawning a still-running
    # grandchild. Hydrate only the parent chain of active rows instead of
    # scanning the unbounded task ledger.
    status = getattr(task_runtime, "status", None)
    rows_by_id = {
        str(task_id): row
        for row in rows
        if isinstance((task_id := getattr(row, "task_id", None)), str) and task_id
    }
    from opensquilla.tools.builtin.shell import active_background_process_task_owners

    if callable(status):
        for owner_session_key, owner_task_id in active_background_process_task_owners():
            if owner_task_id in rows_by_id:
                continue
            try:
                owner = await status(owner_task_id)
            except (KeyError, NotImplementedError):
                continue
            except Exception:
                log.warning(
                    "sessions.abort.background_owner_status_failed",
                    task_id=owner_task_id,
                )
                continue
            if _task_record_session_key(owner) == owner_session_key:
                rows_by_id[owner_task_id] = owner
    pending_parent_ids = [
        parent_task_id
        for row in tuple(rows_by_id.values())
        if (identity := _task_record_parent_identity(row)) is not None
        for parent_task_id in (identity[0],)
        if parent_task_id not in rows_by_id
    ]
    while callable(status) and pending_parent_ids:
        parent_task_id = pending_parent_ids.pop()
        if parent_task_id in rows_by_id:
            continue
        try:
            parent = await status(parent_task_id)
        except (KeyError, NotImplementedError):
            continue
        except Exception:
            log.warning(
                "sessions.abort.task_runtime_ancestor_status_failed",
                task_id=parent_task_id,
            )
            continue
        rows_by_id[parent_task_id] = parent
        identity = _task_record_parent_identity(parent)
        if identity is not None and identity[0] not in rows_by_id:
            pending_parent_ids.append(identity[0])
    return tuple(rows_by_id.values())


async def _task_runtime_owns_session(
    task_runtime: Any,
    *,
    task_id: str,
    session_key: str,
) -> bool:
    """Verify a durable root identity before following child-supplied lineage."""

    status = getattr(task_runtime, "status", None)
    if not callable(status):
        return False
    try:
        record = await status(task_id)
    except (KeyError, NotImplementedError):
        return False
    except Exception:
        log.warning(
            "sessions.abort.task_runtime_status_failed",
            session_key=session_key,
            task_id=task_id,
        )
        return False
    return _task_record_session_key(record) == session_key


def _task_owned_descendant_rows(
    rows: tuple[Any, ...],
    *,
    owned_tasks: dict[str, str],
) -> tuple[Any, ...]:
    """Expand exact task ancestry without widening to sibling session work."""

    discovered: list[Any] = []
    discovered_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for row in rows:
            task_id = getattr(row, "task_id", None)
            session_key = _task_record_session_key(row)
            parent_identity = _task_record_parent_identity(row)
            if (
                not isinstance(task_id, str)
                or not task_id
                or session_key is None
                or parent_identity is None
                or task_id in owned_tasks
            ):
                continue
            parent_task_id, parent_session_key = parent_identity
            if owned_tasks.get(parent_task_id) != parent_session_key:
                continue
            owned_tasks[task_id] = session_key
            discovered_ids.add(task_id)
            discovered.append(row)
            changed = True
    return tuple(row for row in discovered if getattr(row, "task_id", None) in discovered_ids)


def _session_row_value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


async def _session_tree_keys(session_manager: Any, root_key: str) -> tuple[str, ...]:
    """Return root plus every recursively spawned child session in BFS order."""
    list_sessions = getattr(session_manager, "list_sessions", None)
    if not callable(list_sessions):
        return (root_key,)

    seen = {root_key}
    ordered = [root_key]
    parents = [root_key]
    page_size = 100
    while parents:
        parent_key = parents.pop(0)
        offset = 0
        while True:
            try:
                rows = await list_sessions(
                    spawned_by=parent_key,
                    limit=page_size,
                    offset=offset,
                )
            except TypeError:
                try:
                    rows = await list_sessions(limit=10000)
                except Exception:
                    rows = []
                rows = [
                    row
                    for row in rows
                    if _session_row_value(row, "spawned_by") == parent_key
                    or _session_row_value(row, "parent_session_key") == parent_key
                ]
                offset = -1
            except Exception:
                log.warning(
                    "sessions.abort.descendant_list_failed",
                    parent_session_key=parent_key,
                )
                rows = []

            for row in rows:
                child_key = _session_row_value(row, "session_key")
                if not isinstance(child_key, str) or not child_key or child_key in seen:
                    continue
                seen.add(child_key)
                ordered.append(child_key)
                parents.append(child_key)
            if offset < 0 or len(rows) < page_size:
                break
            offset += page_size
    return tuple(ordered)


async def _drain_cancelled_task_runtime(
    task_runtime: Any,
    *,
    session_key: str,
    task_ids: tuple[str, ...],
    deadline_at_monotonic: float | None = None,
) -> None:
    if not task_ids or not hasattr(task_runtime, "wait"):
        return

    timeout = _ABORT_RUNTIME_CANCEL_DRAIN_SECONDS
    if deadline_at_monotonic is not None:
        timeout = max(0.0, deadline_at_monotonic - time.monotonic())
    if timeout <= 0:
        for task_id in task_ids:
            log.warning(
                "sessions.abort.task_runtime_drain_timeout",
                session_key=session_key,
                task_id=task_id,
            )
        return

    waiters = {asyncio.create_task(task_runtime.wait(task_id)): task_id for task_id in task_ids}
    done, pending = await asyncio.wait(waiters, timeout=timeout)
    for waiter in done:
        try:
            waiter.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning(
                "sessions.abort.task_runtime_drain_failed",
                session_key=session_key,
                task_id=waiters[waiter],
            )
    for waiter in pending:
        waiter.cancel()
        waiter.add_done_callback(_consume_abort_background_result)
        log.warning(
            "sessions.abort.task_runtime_drain_timeout",
            session_key=session_key,
            task_id=waiters[waiter],
        )
    if pending:
        # Give cooperative waiters one loop turn to observe cancellation, but
        # never synchronously join a waiter that delays or suppresses it.
        await asyncio.sleep(0)


async def _cancel_task_owned_auxiliary_work(
    *,
    session_key: str,
    task_id: str,
    deadline_at_monotonic: float,
    process_state_dir: str | Path | None = None,
) -> int:
    """Stop task-owned completion delivery and registered background processes."""

    from opensquilla.gateway.subagent_announce import (
        cancel_background_completion_for_task,
    )
    from opensquilla.process_tree import cancel_persisted_processes_for_task
    from opensquilla.tools.builtin.shell import cancel_background_processes_for_task

    completion_task = asyncio.create_task(
        cancel_background_completion_for_task(session_key, task_id)
    )
    process_task = asyncio.create_task(cancel_background_processes_for_task(session_key, task_id))
    persisted_process_task = asyncio.create_task(
        cancel_persisted_processes_for_task(process_state_dir, session_key, task_id)
    )
    cancelled_completions = await _await_abort_operation(
        completion_task,
        deadline_at_monotonic=deadline_at_monotonic,
        operation="cancel_task_background_completion",
        default=0,
    )
    cancelled_processes = await _await_abort_background_task(
        process_task,
        deadline_at_monotonic=deadline_at_monotonic,
        operation="cancel_task_background_processes",
        default=0,
    )
    cancelled_persisted_processes = await _await_abort_background_task(
        persisted_process_task,
        deadline_at_monotonic=deadline_at_monotonic,
        operation="cancel_task_persisted_processes",
        default=0,
    )
    return (
        int(cancelled_completions) + int(cancelled_processes) + int(cancelled_persisted_processes)
    )


async def _cancel_task_owned_descendants(
    task_runtime: Any,
    *,
    root_session_key: str,
    root_task_id: str,
    source: str,
    reason: str,
    deadline_at_monotonic: float,
    process_state_dir: str | Path | None = None,
) -> int:
    """Cancel active subagent descendants proven to belong to one exact task."""

    initial_rows_task = asyncio.create_task(_task_runtime_rows(task_runtime))
    root_status_task = asyncio.create_task(
        _task_runtime_owns_session(
            task_runtime,
            task_id=root_task_id,
            session_key=root_session_key,
        )
    )
    initial_rows = await _await_abort_operation(
        initial_rows_task,
        deadline_at_monotonic=deadline_at_monotonic,
        operation="initial_list_task_owned_descendants",
        default=(),
    )
    root_verified = any(
        getattr(row, "task_id", None) == root_task_id
        and _task_record_session_key(row) == root_session_key
        for row in initial_rows
    )
    root_verified = root_verified or bool(
        await _await_abort_operation(
            root_status_task,
            deadline_at_monotonic=deadline_at_monotonic,
            operation="verify_task_owned_descendant_root",
            default=False,
        )
    )
    if not root_verified:
        return 0

    owned_tasks = {root_task_id: root_session_key}
    processed_task_ids = {root_task_id}
    cancelled_task_ids: list[str] = []
    stable_passes = 0

    for pass_index in range(_ABORT_TREE_STABILIZATION_PASSES):
        if time.monotonic() >= deadline_at_monotonic:
            break
        if pass_index == 0:
            rows = initial_rows
        else:
            rows = await _await_abort_operation(
                _task_runtime_rows(task_runtime),
                deadline_at_monotonic=deadline_at_monotonic,
                operation="list_task_owned_descendants",
                default=(),
            )
        descendants = _task_owned_descendant_rows(rows, owned_tasks=owned_tasks)
        new_rows = [
            row
            for row in descendants
            if isinstance(getattr(row, "task_id", None), str)
            and getattr(row, "task_id") not in processed_task_ids
        ]
        if not new_rows:
            stable_passes += 1
            if stable_passes >= 2:
                break
            await asyncio.sleep(0)
            continue

        stable_passes = 0
        auxiliary_tasks: list[asyncio.Task[int]] = []
        runtime_cancel_tasks: list[tuple[str, asyncio.Task[int]]] = []
        for row in new_rows:
            task_id = str(getattr(row, "task_id"))
            session_key = _task_record_session_key(row)
            if session_key is None:
                continue
            processed_task_ids.add(task_id)
            auxiliary_tasks.append(
                asyncio.create_task(
                    _cancel_task_owned_auxiliary_work(
                        session_key=session_key,
                        task_id=task_id,
                        deadline_at_monotonic=deadline_at_monotonic,
                        process_state_dir=process_state_dir,
                    )
                )
            )
            if _task_status_value(getattr(row, "status", None)) not in _ACTIVE_TASK_STATUSES:
                continue
            runtime_cancel_tasks.append(
                (
                    task_id,
                    asyncio.create_task(
                        _cancel_task_runtime(
                            task_runtime,
                            session_key=session_key,
                            task_id=task_id,
                            source=source,
                            reason=reason,
                        )
                    ),
                )
            )

        for auxiliary_task in auxiliary_tasks:
            await _await_abort_background_task(
                auxiliary_task,
                deadline_at_monotonic=deadline_at_monotonic,
                operation="cancel_task_owned_descendant_auxiliary_work",
                default=0,
            )
        for task_id, runtime_cancel_task in runtime_cancel_tasks:
            cancelled = await _await_abort_operation(
                runtime_cancel_task,
                deadline_at_monotonic=deadline_at_monotonic,
                operation="cancel_task_owned_descendant",
                default=0,
            )
            if int(cancelled) > 0:
                cancelled_task_ids.append(task_id)

        if cancelled_task_ids:
            await _drain_cancelled_task_runtime(
                task_runtime,
                session_key=root_session_key,
                task_ids=tuple(cancelled_task_ids),
                deadline_at_monotonic=deadline_at_monotonic,
            )

    return len(set(cancelled_task_ids))


def _optional_stream_seq(params: dict | None) -> int | None:
    if not isinstance(params, dict):
        return None
    raw = params.get("since_stream_seq", params.get("sinceStreamSeq"))
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, value)


def _optional_stream_generation(params: dict | None) -> str | None:
    if not isinstance(params, dict):
        return None
    raw = params.get(
        "since_stream_generation",
        params.get("sinceStreamGeneration"),
    )
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if value else None


def _buffer_session_event(
    session_key: str,
    event_name: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return buffer_session_event(
        session_key,
        event_name,
        payload,
        streams=get_session_streams(),
    )


def _require_key(params: dict | None) -> str:
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    key = params["key"]
    if not isinstance(key, str):
        raise ValueError("params.key must be a string")
    return canonicalize_session_key(key)


def _optional_string_param(params: Mapping[str, Any] | None, *names: str) -> str | None:
    if params is None:
        return None
    for name in names:
        if name not in params:
            continue
        value = params.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        value = value.strip()
        return value or None
    return None


def _effective_agent_id_for_session(session: Any | None, session_key: str) -> str:
    """Prefer the explicit agent encoded in modern session keys.

    Older WebChat paths could accidentally persist ``agent_id='main'`` for a
    key such as ``agent:ops:webchat:...``.  Routing, workspace selection, and
    memory lookup must follow the canonical session key in that case.
    """

    parsed = parse_agent_id(session_key)
    stored = normalize_agent_id(getattr(session, "agent_id", None) or "main")
    if parsed != "main":
        return parsed
    return stored


def _bootstrap_identity_text(value: Any, *, limit: int) -> str | None:
    """Return one terminal-safe display field for bootstrap consumers.

    ``sessions.bootstrap`` is consumed by several surfaces, so the identity
    snapshot must stay presentation-only: no source documents, avatar paths,
    or control sequences cross this contract.
    """

    if not isinstance(value, str):
        return None
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    normalized = without_ansi.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    clean = "".join(char for char in normalized if ord(char) >= 32 and ord(char) != 127)
    clean = " ".join(clean.split()).strip()
    return clean[:limit] or None


async def _bootstrap_agent_identity(ctx: RpcContext, agent_id: str) -> dict[str, str | None]:
    """Resolve a small, additive identity snapshot without making bootstrap fragile."""

    payload: dict[str, str | None] = {
        "agent_id": agent_id,
        "name": agent_id,
        "emoji": None,
        "theme": None,
    }
    registry = getattr(ctx, "agent_registry", None)
    getter = getattr(registry, "get_identity", None)
    if not callable(getter):
        return payload
    try:
        raw = getter(agent_id)
        if inspect.isawaitable(raw):
            raw = await raw
    except Exception:  # noqa: BLE001 - identity decoration must not block session access
        log.warning("sessions.bootstrap.identity_lookup_failed", agent_id=agent_id)
        return payload
    if not isinstance(raw, dict):
        return payload
    nested = raw.get("identity")
    identity = nested if isinstance(nested, dict) else raw
    payload["name"] = _bootstrap_identity_text(identity.get("name"), limit=80) or agent_id
    payload["emoji"] = _bootstrap_identity_text(identity.get("emoji"), limit=16)
    payload["theme"] = _bootstrap_identity_text(identity.get("theme"), limit=48)
    return payload


def _normalize_workspace_display_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return text


def _same_workspace_path(left: str, right: str | Path) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(
            strict=False
        )
    except (OSError, RuntimeError, ValueError):
        return str(left).rstrip("/\\") == str(right).rstrip("/\\")


def _is_default_opensquilla_workspace(workspace: str) -> bool:
    return _same_workspace_path(workspace, default_workspace_dir())


def _workspace_metadata_for_session(session: Any, config: Any) -> dict[str, str]:
    origin = getattr(session, "origin", None)
    origin_map = origin if isinstance(origin, dict) else {}
    context_payload = origin_map.get(RUN_CONTEXT_ORIGIN_KEY)
    workspace = context_payload.get("workspace") if isinstance(context_payload, dict) else None
    workspace_path = _normalize_workspace_display_path(workspace)

    if workspace_path is None:
        session_key = str(getattr(session, "session_key", "") or "")
        agent_id = _effective_agent_id_for_session(session, session_key)
        workspace_path = _normalize_workspace_display_path(
            str(resolve_agent_workspace_dir(agent_id, config))
        )

    if workspace_path is None or _is_default_opensquilla_workspace(workspace_path):
        return {}

    label = Path(workspace_path).name or workspace_path
    return {
        "workspace": workspace_path,
        "workspaceLabel": label,
        "workspaceDisplayPath": workspace_path,
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _model_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _aliased_optional_string_param(
    params: dict[str, Any],
    *names: str,
) -> tuple[bool, str | None]:
    """Read one nullable string field while rejecting conflicting aliases."""

    values: list[str | None] = []
    for name in names:
        if name not in params:
            continue
        value = params[name]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string or null")
        values.append(value.strip() or None if isinstance(value, str) else None)
    if not values:
        return False, None
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"params aliases for {names[0]} must agree")
    return True, values[0]


def _rpc_session_deployment_fields(
    params: dict[str, Any],
) -> tuple[bool, str | None, bool, str | None]:
    provider_present, provider = _aliased_optional_string_param(
        params,
        "provider",
        "providerOverride",
        "provider_override",
    )
    auth_profile_present, auth_profile = _aliased_optional_string_param(
        params,
        "authProfile",
        "authProfileOverride",
        "auth_profile",
        "auth_profile_override",
    )
    return (
        provider_present,
        provider.lower() if provider else None,
        auth_profile_present,
        auth_profile,
    )


def _validate_rpc_session_deployment(
    ctx: RpcContext,
    *,
    session_key: str,
    provider: str | None,
    model: str | None,
    auth_profile: str | None,
) -> None:
    reason = validate_gateway_session_deployment_override(
        getattr(ctx, "config", None),
        provider_id=provider or "",
        model=model or "",
        auth_profile_id=auth_profile or "",
        session_key=session_key,
    )
    if reason:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="Invalid session deployment override.",
            details={"reason": reason},
        )


def _raise_explicit_session_deployment_model_required() -> NoReturn:
    raise RpcHandlerError(
        code="INVALID_PARAMS",
        message="A session provider binding requires an explicit model.",
        details={"reason": "session_deployment_requires_explicit_model"},
    )


def _agent_registry_model(ctx: RpcContext, agent_id: str) -> str | None:
    registry = getattr(ctx, "agent_registry", None)
    getter = getattr(registry, "get_agent_model", None)
    if not callable(getter):
        return None
    try:
        return _model_value(getter(agent_id))
    except Exception:  # noqa: BLE001 - registry lookup must not break legacy sessions
        log.warning("sessions.agent_model_lookup_failed", agent_id=agent_id)
        return None


async def _agent_registry_has(ctx: RpcContext, agent_id: str) -> bool:
    """Return True iff *agent_id* exists in the registry (built-in main always True).

    Returns ``True`` when no registry is wired so legacy code paths that ran
    without an agent registry continue to work — the validation only kicks in
    when a registry is available to consult.
    """
    if normalize_agent_id(agent_id) == "main":
        return True
    registry = getattr(ctx, "agent_registry", None)
    lister = getattr(registry, "list_agents", None)
    if not callable(lister):
        return True
    try:
        agents = await lister(include_builtin=True)
    except Exception:  # noqa: BLE001 - never block session create on registry hiccups
        log.warning("sessions.agent_registry_list_failed", agent_id=agent_id)
        return True
    target = normalize_agent_id(agent_id)
    for entry in agents:
        if normalize_agent_id(str(entry.get("id", ""))) == target:
            return True
    return False


def _session_turn_model(ctx: RpcContext, session: Any | None, agent_id: str) -> str | None:
    return _model_value(getattr(session, "model", None)) or _agent_registry_model(ctx, agent_id)


def _task_summary(row: Any) -> dict[str, Any]:
    task_id = getattr(row, "task_id", None)
    summary = {
        "task_id": task_id,
        "turn_id": task_id,
        "status": _enum_value(getattr(row, "status", None)),
        "queue_mode": _enum_value(getattr(row, "queue_mode", None)),
        "run_kind": getattr(row, "run_kind", None),
        "source_kind": getattr(row, "source_kind", None),
        "created_at": getattr(row, "created_at", None),
        "started_at": getattr(row, "started_at", None),
    }
    details = getattr(row, "details", None)
    if isinstance(details, dict):
        for field in (
            "turn_id",
            "client_message_id",
            "user_message_id",
            "surface_id",
            "session_id",
        ):
            value = details.get(field)
            if isinstance(value, str) and value:
                summary[field] = value
        turn_outcome = details.get("turn_outcome")
        if isinstance(turn_outcome, dict):
            summary["turn_outcome"] = dict(turn_outcome)
        steer_capability = details.get("steer_capability")
        if isinstance(steer_capability, dict):
            summary["steer_capability"] = dict(steer_capability)
        if isinstance(details.get("cancellation_requested"), dict):
            summary["cancel_requested"] = True
    finished_at = getattr(row, "finished_at", None)
    if finished_at is not None:
        summary["finished_at"] = finished_at
    terminal_reason = getattr(row, "terminal_reason", None)
    if terminal_reason is not None:
        summary["terminal_reason"] = terminal_reason
    if summary.get("status") in {"failed", "timeout", "abandoned", "cancelled"}:
        summary["terminal_message"] = build_terminal_reply(
            {
                "status": summary.get("status"),
                "terminal_reason": terminal_reason,
                "error_class": getattr(row, "error_class", None),
                "error_message": getattr(row, "error_message", None),
            }
        )
    return summary


def _normalize_terminal_event_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_name != "session.event.error":
        return payload

    prior_outcome = payload.get("turn_outcome")
    prior_failure_kind = (
        prior_outcome.get("failure_kind")
        if isinstance(prior_outcome, dict)
        else payload.get("failure_kind")
    )
    message = payload.get("message")
    error_message = payload.get("error_message")
    raw_message = error_message if isinstance(error_message, str) and error_message else message
    raw_text = raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
    if isinstance(prior_failure_kind, str) and prior_failure_kind:
        raw_text = safe_provider_failure_message(prior_failure_kind)
    code = payload.get("code")
    if isinstance(prior_failure_kind, str) and prior_failure_kind:
        code = safe_provider_failure_code(
            str(code) if code is not None else None,
            prior_failure_kind,
        )
    code_text = str(code or "").lower()
    is_timeout = "timeout" in code_text or "stream idle" in raw_text.lower()
    terminal_payload = {
        "status": "timeout" if is_timeout else "failed",
        "terminal_reason": payload.get("terminal_reason") or ("timeout" if is_timeout else "error"),
        "error_class": code,
        "error_message": raw_text,
        **payload,
    }
    _, safe_error_message = sanitize_agent_error(
        terminal_payload,
        fallback_error_class=str(code) if code else None,
        fallback_error_message=raw_text,
    )
    # Join the user-visible reply to its durable turn_errors row: hex ids keep
    # substring-based timeout classification stable, and append_error_ref is
    # idempotent so the CLI client's re-normalization cannot double-suffix.
    error_id = payload.get("error_id")
    error_ref = error_id if isinstance(error_id, str) else None
    terminal_message = append_error_ref(build_terminal_reply(terminal_payload), error_ref)
    # Serialize the typed turn outcome onto the wire so every surface (Web UI,
    # CLI, channels) can render a specific cause + retryability + recovery
    # affordance instead of parsing the human string. The taxonomy already
    # classifies these codes (engine/outcome.py); this is the missing link that
    # carries it to clients.
    from opensquilla.engine.outcome import outcome_from_error

    outcome = outcome_from_error(
        code=str(code) if code else None,
        message=safe_error_message,
        error_class=str(code) if code else None,
        failure_kind=(str(prior_failure_kind) if isinstance(prior_failure_kind, str) else None),
    )
    sensitive_provider_fields = {
        "provider_error_message",
        "provider_response_body",
        "raw_error_body",
        "request_payload",
        "request_payload_head",
        "response_body",
    }
    safe_payload = {
        key: value for key, value in payload.items() if key not in sensitive_provider_fields
    }
    return {
        **safe_payload,
        "code": code,
        "message": terminal_message,
        "terminal_message": terminal_message,
        "terminal_reason": terminal_payload["terminal_reason"],
        "error_message": safe_error_message,
        "turn_outcome": outcome.to_dict(),
    }


def _sorted_task_rows(rows: list[Any]) -> list[Any]:
    return sorted(rows, key=lambda row: getattr(row, "created_at", 0) or 0, reverse=True)


def _active_task_summary(rows: list[Any]) -> dict[str, Any] | None:
    active = [
        row for row in rows if _enum_value(getattr(row, "status", None)) in {"queued", "running"}
    ]
    if not active:
        return None
    running = [row for row in active if _enum_value(getattr(row, "status", None)) == "running"]
    if running:
        return _task_summary(_sorted_task_rows(running)[0])
    # TaskRuntime executes a session's pending lane FIFO. Hydration must expose
    # that same oldest queued owner; choosing the newest accepted row would make
    # reconnecting clients target Stop/steer at a later task that is not next.
    queued = sorted(
        active,
        key=lambda row: (
            getattr(row, "created_at", 0) or 0,
            str(getattr(row, "task_id", "")),
        ),
    )
    return _task_summary(queued[0])


def _last_task_summary(rows: list[Any]) -> dict[str, Any] | None:
    if not rows:
        return None
    return _task_summary(_sorted_task_rows(rows)[0])


def _task_run_status(active_task: dict[str, Any] | None, last_task: dict[str, Any] | None) -> str:
    if active_task is not None:
        status = active_task.get("status")
        return str(status or "running")
    if last_task is None:
        return "idle"
    status = str(last_task.get("status") or "")
    if status == "abandoned":
        return "interrupted"
    if status in {"failed", "timeout", "cancelled"}:
        return status
    return "idle"


def _task_state_summary(rows: list[Any]) -> dict[str, Any]:
    active_task = _active_task_summary(rows)
    last_task = _last_task_summary(rows)
    return {
        "tasks": [_task_summary(row) for row in _sorted_task_rows(rows)],
        "active_task": active_task,
        "last_task": last_task,
        "run_status": _task_run_status(active_task, last_task),
    }


async def _overlay_runtime_task_snapshot(
    ctx: RpcContext,
    session_key: str,
    task_state: dict[str, Any],
) -> None:
    """Overlay live FIFO ownership onto a durable task-ledger snapshot.

    SQLite timestamps cannot encode the exact ordering of two same-millisecond
    admissions. While this process owns the runtime, its state-locked pending
    lane is authoritative for both foreground selection and ordered queued ids.
    A non-empty durable projection is retained when the live snapshot is empty
    during the short acceptance-commit-to-runtime-activation window; startup
    recovery abandons stale unfinished rows before requests can reach here.
    """

    getter = getattr(getattr(ctx, "task_runtime", None), "session_task_snapshot", None)
    if not callable(getter):
        return
    try:
        candidate = getter(session_key)
        snapshot = await candidate if inspect.isawaitable(candidate) else candidate
    except Exception:  # noqa: BLE001 - durable hydration remains a safe fallback.
        log.warning(
            "sessions.runtime_task_snapshot_failed",
            session_key=session_key,
            exc_info=True,
        )
        return

    running_value = getattr(snapshot, "running_task_id", None)
    running_task_id = (
        running_value.strip() if isinstance(running_value, str) and running_value.strip() else None
    )
    raw_queued_ids = getattr(snapshot, "queued_task_ids", ())
    queued_task_ids: list[str] = []
    if isinstance(raw_queued_ids, (list, tuple)):
        for value in raw_queued_ids:
            task_id = value.strip() if isinstance(value, str) else ""
            if task_id and task_id != running_task_id and task_id not in queued_task_ids:
                queued_task_ids.append(task_id)
    raw_cancel_requested_ids = getattr(snapshot, "cancel_requested_task_ids", ())
    cancel_requested_task_ids = {
        value.strip()
        for value in raw_cancel_requested_ids
        if isinstance(value, str) and value.strip()
    }

    active_task_id = running_task_id or (queued_task_ids[0] if queued_task_ids else None)
    durable_active = task_state.get("active_task")
    if active_task_id is None and isinstance(durable_active, dict):
        durable_status = str(durable_active.get("status") or "").strip().lower()
        durable_task_id = str(durable_active.get("task_id") or "").strip()
        if durable_task_id and durable_status in {"queued", "running"}:
            # accept_turn persists the QUEUED ledger row before activating it
            # into TaskRuntime. A hydrate in that commit-to-activation window
            # therefore sees durable work and an empty runtime snapshot. Keep
            # the durable fail-closed projection; process-start recovery has
            # already abandoned stale rows before requests can reach here.
            if durable_status == "queued":
                queued_task_ids = [
                    str(task.get("task_id") or "").strip()
                    for task in sorted(
                        (
                            task
                            for task in task_state.get("tasks", [])
                            if isinstance(task, dict)
                            and str(task.get("status") or "").strip().lower() == "queued"
                        ),
                        key=lambda task: (
                            int(task.get("created_at") or 0),
                            str(task.get("task_id") or ""),
                        ),
                    )
                    if isinstance(task, dict) and str(task.get("task_id") or "").strip()
                ]
                if durable_task_id not in queued_task_ids:
                    queued_task_ids.insert(0, durable_task_id)
            task_state["queued_task_ids"] = queued_task_ids
            return
    active_status = "running" if running_task_id is not None else "queued"
    active_task: dict[str, Any] | None = None
    if active_task_id is not None:
        active_task = next(
            (
                dict(task)
                for task in task_state.get("tasks", [])
                if isinstance(task, dict) and task.get("task_id") == active_task_id
            ),
            None,
        )
        if active_task is None:
            active_task = {"task_id": active_task_id}
        active_task["status"] = active_status
        if active_task_id in cancel_requested_task_ids:
            active_task["cancel_requested"] = True

    task_state["active_task"] = active_task
    task_state["queued_task_ids"] = queued_task_ids
    task_state["run_status"] = _task_run_status(
        active_task,
        task_state.get("last_task"),
    )


async def _attach_active_steer_capability(
    ctx: RpcContext,
    session_key: str,
    task_state: dict[str, Any],
) -> None:
    """Enrich active-task hydration from the live accepted routing snapshot."""

    active_task = task_state.get("active_task")
    if not isinstance(active_task, dict):
        return
    getter = getattr(getattr(ctx, "task_runtime", None), "steer_capability", None)
    if not callable(getter):
        return
    try:
        capability = getter(session_key)
        if inspect.isawaitable(capability):
            capability = await capability
    except Exception:  # noqa: BLE001 - task hydration remains usable without it.
        log.warning(
            "sessions.steer_capability_hydration_failed",
            session_key=session_key,
            exc_info=True,
        )
        return
    if not isinstance(capability, dict):
        return
    active_task["steer_capability"] = dict(capability)
    active_task_id = active_task.get("task_id")
    for task in task_state.get("tasks", []):
        if isinstance(task, dict) and task.get("task_id") == active_task_id:
            task["steer_capability"] = dict(capability)
            break


def _active_task_run_mode(rows: list[Any]) -> str | None:
    active = [
        row for row in rows if _enum_value(getattr(row, "status", None)) in _ACTIVE_TASK_STATUSES
    ]
    running = [row for row in active if _enum_value(getattr(row, "status", None)) == "running"]
    candidates = _sorted_task_rows(running or active)
    for row in candidates:
        details = getattr(row, "details", None)
        accepted = details.get("accepted_run_mode") if isinstance(details, dict) else None
        mode = accepted.get("run_mode") if isinstance(accepted, dict) else None
        if isinstance(mode, str) and mode:
            return mode
    return None


def _session_origin_run_mode(session: Any | None) -> str | None:
    origin = getattr(session, "origin", None)
    run_context = origin.get(RUN_CONTEXT_ORIGIN_KEY) if isinstance(origin, dict) else None
    mode = run_context.get("run_mode") if isinstance(run_context, dict) else None
    return mode if isinstance(mode, str) and mode else None


def _run_mode_lock_payload(
    *,
    task_rows: list[Any],
    active_task_group_ids: list[str],
    background_override: Any | None,
    session: Any | None,
    principal: Any,
) -> dict[str, Any]:
    has_active_task = any(
        _enum_value(getattr(row, "status", None)) in _ACTIVE_TASK_STATUSES for row in task_rows
    )
    has_background_group = bool(active_task_group_ids)
    if not has_active_task and not has_background_group:
        return {"locked": False}

    mode = _active_task_run_mode(task_rows)
    source = "task"
    if mode is None and has_background_group:
        accepted_mode = getattr(background_override, "run_mode", None)
        mode = getattr(accepted_mode, "value", accepted_mode)
        source = "background"
    if not isinstance(mode, str) or not mode:
        mode = _session_origin_run_mode(session)
        source = "session"
    if not isinstance(mode, str) or not mode:
        return {"locked": True}

    coerced = coerce_run_mode_for_principal(mode, principal)
    return {
        "locked": True,
        "runMode": coerced.value,
        "source": source,
    }


async def _list_task_rows(ctx: RpcContext, storage: Any | None, session_key: str) -> list[Any]:
    if storage is not None:
        recent_storage_list = getattr(storage, "list_recent_agent_tasks", None)
        if callable(recent_storage_list):
            try:
                return list(await recent_storage_list(session_key))
            except Exception:
                log.warning(
                    "sessions.recent_agent_task_storage_state_failed",
                    session_key=session_key,
                )

    task_runtime = getattr(ctx, "task_runtime", None)
    if task_runtime is not None:
        runtime_list = getattr(task_runtime, "list", None)
        if callable(runtime_list):
            try:
                return list(await runtime_list(session_key=session_key))
            except Exception:
                log.warning("sessions.task_runtime_state_failed", session_key=session_key)

    if storage is None:
        return []
    storage_list = getattr(storage, "list_agent_tasks", None)
    if not callable(storage_list):
        return []
    try:
        return list(await storage_list(session_key=session_key))
    except Exception:
        log.warning("sessions.agent_task_storage_state_failed", session_key=session_key)
        return []


async def _list_task_rows_by_session(
    ctx: RpcContext,
    storage: Any | None,
    session_keys: list[str],
) -> dict[str, list[Any]]:
    keys = [canonicalize_session_key(key) for key in session_keys]
    if not keys:
        return {}

    if storage is not None:
        storage_batch = getattr(storage, "list_agent_tasks_for_sessions", None)
        if callable(storage_batch):
            try:
                grouped = await storage_batch(keys)
                return {key: list(grouped.get(key, [])) for key in keys}
            except Exception:
                log.warning("sessions.agent_task_storage_batch_state_failed")

    return {key: await _list_task_rows(ctx, storage, key) for key in keys}


async def _list_transcript_titles(storage: Any, sessions: list[Any]) -> dict[str, str]:
    session_ids = [str(getattr(session, "session_id", "") or "") for session in sessions]
    session_ids = [session_id for session_id in session_ids if session_id]
    if not session_ids:
        return {}

    title_inputs: dict[str, list[str]] = {session_id: [] for session_id in session_ids}
    storage_batch = getattr(storage, "list_user_transcript_content_batch", None)
    if callable(storage_batch):
        try:
            grouped = await storage_batch(session_ids, limit_per_session=3)
            title_inputs.update(
                {
                    str(session_id): [str(value) for value in values if value]
                    for session_id, values in grouped.items()
                }
            )
        except Exception:
            log.warning("sessions.transcript_title_batch_failed", exc_info=True)

    if not any(title_inputs.values()):
        storage_get_transcript = getattr(storage, "get_transcript", None)
        if callable(storage_get_transcript):
            for session_id in session_ids:
                try:
                    entries = await storage_get_transcript(session_id, limit=8)
                except Exception:
                    log.warning(
                        "sessions.transcript_title_read_failed",
                        session_id=session_id,
                    )
                    continue
                title_inputs[session_id] = [
                    str(getattr(entry, "content", "") or "")
                    for entry in entries
                    if str(getattr(entry, "role", "") or "").lower() == "user"
                ][:3]

    titles: dict[str, str] = {}
    for session_id, values in title_inputs.items():
        for value in values:
            title = derive_transcript_title(value)
            if title:
                titles[session_id] = title
                break
    return titles


def _create_session_key(agent_id: str, kind: object = None) -> str:
    short_id = uuid.uuid4().hex[:8]
    normalized_kind = str(kind or "").strip().lower().replace("_", "-")
    if normalized_kind == "web":
        normalized_kind = "webchat"
    if normalized_kind in {"cli", "webchat"}:
        return f"agent:{agent_id}:{normalized_kind}:{short_id}"
    return f"agent:{agent_id}:{short_id}"


def _derive_source_metadata(session: Any) -> dict[str, Any]:
    key = str(getattr(session, "session_key", "") or "")
    origin = getattr(session, "origin", None)
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    last_channel = getattr(session, "last_channel", None)
    channel = getattr(session, "channel", None)
    source_kind = origin_kind
    channel_kind = last_channel or channel
    if ":webchat:" in key:
        source_kind = source_kind or "webui"
        channel_kind = channel_kind or "webchat"
    elif ":cli:" in key or ":standalone:" in key:
        source_kind = source_kind or "cli"
        channel_kind = channel_kind or "cli"
    elif ":subagent:" in key:
        source_kind = source_kind or "subagent"
        channel_kind = channel_kind or "subagent"
    elif key.startswith("cron:") or ":cron:" in key:
        source_kind = source_kind or "cron"
        channel_kind = channel_kind or "cron"
    elif last_channel:
        source_kind = source_kind or "channel"
    return {
        "source_kind": source_kind,
        "sourceKind": source_kind,
        "channel_kind": channel_kind,
        "channelKind": channel_kind,
        "channel_id": getattr(session, "last_to", None),
        "channelId": getattr(session, "last_to", None),
    }


_SESSION_COUNT_VIEW = "session-count-v1"
_SESSION_LIST_VIEW = "session-list-v1"
_SESSION_LIST_CURSOR_VERSION = 1
_SESSION_LIST_CURSOR_MAX_CHARS = 8192
_MAX_SQLITE_INTEGER = (1 << 63) - 1


def _encode_session_list_cursor(cursor: SessionListCursor | None) -> str | None:
    if cursor is None:
        return None
    payload = json.dumps(
        {
            "v": _SESSION_LIST_CURSOR_VERSION,
            "a": cursor.activity_at,
            "u": cursor.updated_at,
            "k": cursor.session_key,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_session_list_cursor(value: Any) -> SessionListCursor | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _SESSION_LIST_CURSOR_MAX_CHARS:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor must be a valid sessions.list cursor",
        )
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(value + padding, altchars=b"-_", validate=True))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor must be a valid sessions.list cursor",
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != _SESSION_LIST_CURSOR_VERSION:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor must be a valid sessions.list cursor",
        )
    activity_at = payload.get("a")
    updated_at = payload.get("u")
    session_key = payload.get("k")
    if (
        isinstance(activity_at, bool)
        or not isinstance(activity_at, int)
        or not 0 <= activity_at <= _MAX_SQLITE_INTEGER
        or isinstance(updated_at, bool)
        or not isinstance(updated_at, int)
        or not 0 <= updated_at <= _MAX_SQLITE_INTEGER
        or not isinstance(session_key, str)
        or not session_key
        or len(session_key) > 512
    ):
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor must be a valid sessions.list cursor",
        )
    try:
        session_key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor must be a valid sessions.list cursor",
        ) from exc
    return SessionListCursor(
        activity_at=activity_at,
        updated_at=updated_at,
        session_key=session_key,
    )


async def _handle_sessions_list(params: dict | None, ctx: RpcContext) -> dict:
    """List all sessions."""
    now_ms = int(time.time() * 1000)
    request = params or {}
    count_only = request.get("view") == _SESSION_COUNT_VIEW
    paginated = request.get("view") == _SESSION_LIST_VIEW

    def empty_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {"sessions": [], "count": 0, "ts": now_ms}
        if count_only:
            payload.update({"totalCount": 0, "total_count": 0})
        if paginated:
            payload.update(
                {
                    "has_more": False,
                    "hasMore": False,
                    "next_cursor": None,
                    "nextCursor": None,
                }
            )
        return payload

    if ctx.session_manager is None:
        return empty_payload()

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return empty_payload()

    limit = request.get("limit", 50)
    cursor = _decode_session_list_cursor(request.get("cursor")) if paginated else None
    if request.get("cursor") is not None and not paginated:
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="params.cursor requires view=session-list-v1",
        )
    from opensquilla.gateway.guest_rpc_policy import GuestRpcPolicy, guest_owns_session_key

    is_guest = GuestRpcPolicy.is_guest(ctx)
    owner_id = getattr(ctx.principal, "guest_owner_id", None) if is_guest else None
    if not is_guest:
        try:
            numeric_limit = int(limit)
        except (TypeError, ValueError):
            pass
        else:
            if numeric_limit < 1:
                raise ValueError("params.limit must be >= 1")
    if count_only:
        count_sessions = getattr(storage, "count_sessions", None)
        if callable(count_sessions):
            try:
                total_count = (
                    await count_sessions(guest_owner_id=owner_id)
                    if is_guest
                    else await count_sessions()
                )
            except TypeError:
                # Older test doubles and alternative storage adapters may not
                # implement the additive count contract. Fall through to the
                # legacy list response so mixed-version clients still render.
                pass
            else:
                total_count = max(0, int(total_count))
                return {
                    "sessions": [],
                    "count": 0,
                    "totalCount": total_count,
                    "total_count": total_count,
                    "ts": now_ms,
                }

    if paginated:
        try:
            page_limit = int(limit)
        except (TypeError, ValueError):
            page_limit = 50
        page_limit = max(1, page_limit)
        if is_guest:
            page_limit = min(page_limit, 100)
        list_page = getattr(storage, "list_sessions_page", None)
        if callable(list_page):
            page = await list_page(
                limit=page_limit,
                cursor=cursor,
                guest_owner_id=owner_id if is_guest else None,
            )
            sessions = page.sessions
            has_more = bool(page.has_more)
            next_cursor = _encode_session_list_cursor(page.next_cursor)
        else:
            # Additive compatibility for older storage adapters and test
            # doubles: return one legacy page and mark it terminal so a newer
            # client never loops over the same first page.
            legacy_kwargs: dict[str, Any] = {"limit": page_limit}
            if is_guest:
                legacy_kwargs["guest_owner_id"] = owner_id
            sessions = await storage.list_sessions(**legacy_kwargs)
            has_more = False
            next_cursor = None
    elif is_guest:
        try:
            guest_limit = int(limit)
        except (TypeError, ValueError):
            guest_limit = 50
        limit = max(1, min(guest_limit, 100))
        sessions = await storage.list_sessions(limit=limit, guest_owner_id=owner_id)
    else:
        sessions = await storage.list_sessions(limit=limit)

    if is_guest:
        sessions = [
            session
            for session in sessions
            if guest_owns_session_key(owner_id, getattr(session, "session_key", None))
        ]
    task_rows_by_session = await _list_task_rows_by_session(
        ctx,
        storage,
        [s.session_key for s in sessions],
    )
    transcript_titles = await _list_transcript_titles(storage, sessions)

    # Batch transcript counts in one round-trip to avoid N+1 against
    # count_transcript_entries. Storage layers that don't implement the batch
    # method fall back gracefully to the legacy per-row path so old FakeStorage
    # / channel-only test doubles keep working.
    entry_counts: dict[str, int] = {}
    batch_count = getattr(storage, "count_transcript_entries_batch", None)
    if callable(batch_count):
        try:
            entry_counts = await batch_count([s.session_id for s in sessions])
        except Exception:
            log.warning("sessions.list.count_batch_failed", exc_info=True)
            entry_counts = {}

    result = []
    channel_types = _channel_types_from_config(ctx.config)
    for s in sessions:
        # Fetch entry count for metadata
        entry_count = entry_counts.get(s.session_id, 0)
        if not entry_count and not entry_counts:
            try:
                entry_count = await storage.count_transcript_entries(s.session_id)
            except Exception:
                pass

        row = {
            "key": s.session_key,
            "agent_id": getattr(s, "agent_id", None),
            "agentId": getattr(s, "agent_id", None),
            "status": getattr(s, "status", "unknown"),
            "model": getattr(s, "model", None),
            "updated_at": getattr(s, "updated_at", now_ms),
            "updatedAt": getattr(s, "updated_at", now_ms),
            "display_name": getattr(s, "display_name", None),
            "displayName": getattr(s, "display_name", None),
            "channel": getattr(s, "channel", None),
            "chat_type": getattr(s, "chat_type", None),
            "chatType": getattr(s, "chat_type", None),
            "group_id": getattr(s, "group_id", None),
            "groupId": getattr(s, "group_id", None),
            "subject": getattr(s, "subject", None),
            "last_channel": getattr(s, "last_channel", None),
            "lastChannel": getattr(s, "last_channel", None),
            "last_to": getattr(s, "last_to", None),
            "lastTo": getattr(s, "last_to", None),
            "last_account_id": getattr(s, "last_account_id", None),
            "lastAccountId": getattr(s, "last_account_id", None),
            "last_thread_id": getattr(s, "last_thread_id", None),
            "lastThreadId": getattr(s, "last_thread_id", None),
            "delivery_context": getattr(s, "delivery_context", None),
            "deliveryContext": getattr(s, "delivery_context", None),
            "parent_session_key": getattr(s, "parent_session_key", None),
            "parentSessionKey": getattr(s, "parent_session_key", None),
            "spawned_by": getattr(s, "spawned_by", None),
            "spawnedBy": getattr(s, "spawned_by", None),
            "spawn_depth": getattr(s, "spawn_depth", 0),
            "spawnDepth": getattr(s, "spawn_depth", 0),
            "forked_from_parent": bool(getattr(s, "forked_from_parent", False)),
            "forkedFromParent": bool(getattr(s, "forked_from_parent", False)),
            "origin": getattr(s, "origin", None),
            "workspace_id": getattr(s, "workspace_id", None),
            "workspaceId": getattr(s, "workspace_id", None),
            "message_count": entry_count,
            "entry_count": entry_count,
            "size_bytes": None,
        }
        row.update(_derive_source_metadata(s))
        task_rows = task_rows_by_session.get(canonicalize_session_key(s.session_key), [])
        task_summary = _task_state_summary(task_rows)
        view_fields = build_session_view_item(
            s,
            entry_count=entry_count,
            task_rows=task_rows,
            now_ms=now_ms,
            transcript_title=transcript_titles.get(s.session_id, ""),
            channel_types=channel_types,
        )
        row.update(task_summary)
        row.update(view_fields)
        row.update(_workspace_metadata_for_session(s, ctx.config))
        result.append(row)

    payload = {"sessions": result, "count": len(result), "ts": now_ms}
    if paginated:
        payload.update(
            {
                "has_more": has_more,
                "hasMore": has_more,
                "next_cursor": next_cursor,
                "nextCursor": next_cursor,
            }
        )
    return payload


_handle_sessions_list_contract = register_sessions_list_contract(
    _d,
    _handle_sessions_list,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _handle_sessions_search(params: dict | None, ctx: RpcContext) -> dict:
    """Search sessions by title and by transcript content.

    ``sessions`` holds title matches across ALL sessions (not just a recent
    page); ``messages`` holds content matches. Content search uses the ranked
    FTS index for ASCII queries and a substring (LIKE) scan for queries the FTS
    tokenizer can't segment (CJK and other non-ASCII scripts), so Chinese
    conversations are searchable too. Covers every surface (webchat, channels,
    cron) because the transcript store is shared. Titles are derived the same
    way ``sessions.list`` derives them so results read like the sidebar.
    """
    now_ms = int(time.time() * 1000)
    raw_query: object = ""
    raw_limit: object = 20
    if isinstance(params, dict):
        raw_query = params.get("query")
        raw_limit = params.get("limit", 20)
    query, _ = SessionDirectory.normalize_search_input(raw_query, raw_limit)
    empty = {"sessions": [], "messages": [], "query": query, "ts": now_ms}
    if not query or ctx.session_manager is None:
        return empty
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return empty

    channel_types = _channel_types_from_config(getattr(ctx, "config", None))

    def project(session: Any, transcript_title: str) -> SessionSearchProjection:
        view = build_session_view_item(
            session,
            entry_count=0,
            task_rows=[],
            now_ms=now_ms,
            transcript_title=transcript_title,
            channel_types=channel_types,
        )
        return SessionSearchProjection(
            title=str(view.get("title") or ""),
            effective_agent_id=view.get("effectiveAgentId"),
            surface=view.get("surface"),
            updated_at=view.get("updatedAt"),
        )

    result = await SessionDirectory(storage).search(
        raw_query,
        raw_limit,
        now_ms=now_ms,
        project=project,
        derive_transcript_title=derive_transcript_title,
    )
    return {
        "sessions": [
            {
                "key": hit.key,
                "title": hit.projection.title,
                "effectiveAgentId": hit.projection.effective_agent_id,
                "surface": hit.projection.surface,
                "updatedAt": hit.projection.updated_at,
            }
            for hit in result.sessions
        ],
        "messages": [
            {
                "key": hit.key,
                "title": hit.title,
                "role": hit.role,
                "snippet": hit.snippet,
                "createdAt": hit.created_at,
            }
            for hit in result.messages
        ],
        "query": result.query,
        "ts": result.ts,
    }


_handle_sessions_search_contract = register_sessions_search_contract(
    _d,
    _handle_sessions_search,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


class _GatewaySessionLifecyclePorts(
    SessionCreationPolicyPort,
    SessionLifecycleStorePort,
    SessionDeletionPort,
    SessionLifecycleEventsPort,
):
    """Concrete Application Ports over the one SessionManager runtime."""

    def __init__(self, context: RpcContext) -> None:
        self._context = context
        self._manager = context.session_manager
        self._storage = get_session_storage(self._manager)

    @property
    def available(self) -> bool:
        return self._manager is not None

    @property
    def deletion_available(self) -> bool:
        return self._manager is not None and self._storage is not None

    def new_session_key(self, agent_id: str, kind: SessionCreationKind) -> str:
        wire_kind: str | None = None if kind is SessionCreationKind.DEFAULT else kind.value
        return _create_session_key(agent_id, wire_kind)

    async def default_model(self, agent_id: str) -> str | None:
        return _agent_registry_model(self._context, agent_id)

    async def agent_exists(self, agent_id: str) -> bool:
        return await _agent_registry_has(self._context, agent_id)

    def validate_deployment(
        self,
        *,
        session_key: str,
        provider: str | None,
        model: str | None,
        auth_profile: str | None,
    ) -> None:
        _validate_rpc_session_deployment(
            self._context,
            session_key=session_key,
            provider=provider,
            model=model,
            auth_profile=auth_profile,
        )

    async def resolve_workspace(self, workspace_id: str) -> SessionWorkspaceBinding:
        if self._storage is None:
            raise RpcUnavailableError("sessions.create(workspaceId=...) requires session storage")
        try:
            validated = await resolve_validated_project_workspace(
                self._storage,
                workspace_id,
            )
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(
                exc,
                owner=self._context.principal.is_owner,
            ) from exc
        mode = project_default_run_mode(self._context.config)
        source = (
            "project_default"
            if mode is RunMode.SAFE and config_run_mode(self._context.config) is RunMode.FULL
            else "operator_default"
        )
        return SessionWorkspaceBinding(
            workspace_id=validated.workspace.workspace_id,
            path=str(validated.workspace.path),
            run_mode=mode.value,
            run_mode_source=source,
        )

    async def create(self, session: NewSession) -> SessionIdentity:
        if self._manager is None:
            raise RpcUnavailableError("sessions.create requires a session manager")
        create_kwargs: dict[str, Any] = {
            "session_key": session.session_key,
            "agent_id": session.agent_id,
            "display_name": session.display_name,
            "model": session.model,
        }
        if session.provider.present:
            create_kwargs["provider_override"] = session.provider.value
        if session.auth_profile.present:
            create_kwargs["auth_profile_override"] = session.auth_profile.value
            create_kwargs["auth_profile_override_source"] = (
                "rpc" if session.auth_profile.value else None
            )
        if session.workspace is not None:
            workspace = session.workspace
            create_kwargs["workspace_id"] = workspace.workspace_id
            create_kwargs["origin"] = {
                RUN_CONTEXT_ORIGIN_KEY: RunContext(
                    run_mode=RunMode(workspace.run_mode),
                    workspace=workspace.path,
                    run_mode_source=workspace.run_mode_source,
                    source=workspace.source,
                ).to_origin_payload()
            }
        created = await self._manager.create(**create_kwargs)
        return SessionIdentity(
            session_key=str(created.session_key),
            session_id=str(created.session_id),
        )

    async def append_initial_user_message(self, session_key: str, message: str) -> None:
        if self._manager is None:
            raise RpcUnavailableError("sessions.create(message=...) requires a session manager")
        await self._manager.append_message(session_key, role="user", content=message)

    async def rename(self, session_key: str, display_name: str) -> None:
        if self._manager is None:
            raise KeyError("No session manager available")
        if self._storage is None:
            raise KeyError("No session storage available")
        session = await self._storage.get_session(session_key)
        if session is None:
            raise KeyError(f"Session not found: {session_key}")
        update = getattr(self._manager, "update", None)
        if callable(update):
            await update(session_key, display_name=display_name)
            return
        setattr(session, "display_name", display_name)
        upsert = getattr(self._storage, "upsert_session", None)
        if callable(upsert):
            await upsert(session)

    async def fork_agent_id(self, parent_key: str) -> str:
        if self._storage is None:
            raise KeyError("No session storage available")
        parent = await self._storage.get_session(parent_key)
        if parent is None:
            raise KeyError(f"Session not found: {parent_key}")
        return _effective_agent_id_for_session(parent, parent_key)

    async def fork(self, spec: ForkSessionSpec) -> SessionIdentity:
        if self._manager is None:
            raise KeyError("No session manager available")
        if self._storage is None:
            raise KeyError("No session storage available")
        fork_kwargs: dict[str, Any] = {
            "fork_transcript": True,
            "status": SessionStatus.DONE,
        }
        if spec.point.mode is SessionForkMode.BEFORE_MESSAGE:
            fork_kwargs["fork_before_message_id"] = spec.point.anchor_id
        elif spec.point.mode is SessionForkMode.THROUGH_TURN:
            fork_kwargs["fork_through_turn_id"] = spec.point.anchor_id
        child = await _fork_with_numbered_title(
            self._context,
            self._storage,
            spec.parent_key,
            spec.child_key,
            explicit_title=spec.title,
            **fork_kwargs,
        )
        return SessionIdentity(
            session_key=str(getattr(child, "session_key")),
            session_id=str(getattr(child, "session_id")),
        )

    async def delete_one(self, canonical_key: str) -> None:
        if self._storage is None:
            raise KeyError("No session storage available")
        await _delete_session_with_lifecycle(
            canonical_key=canonical_key,
            ctx=self._context,
            storage=self._storage,
        )

    async def publish_forked(self, event: SessionForked) -> None:
        await _emit_to_subscribers(
            self._context,
            event.child_key,
            "sessions.changed",
            build_sessions_changed_payload(
                event.child_key,
                "forked",
                run_status="idle",
            ),
        )


class _SessionLifecycleDeletionPort(SessionDeletionPort):
    def __init__(self, ports: _GatewaySessionLifecyclePorts) -> None:
        self._ports = ports

    @property
    def available(self) -> bool:
        return self._ports.deletion_available

    async def delete_one(self, canonical_key: str) -> None:
        await self._ports.delete_one(canonical_key)


def _session_lifecycle_adapter(ctx: RpcContext) -> GatewaySessionLifecycleAdapter:
    ports = _GatewaySessionLifecyclePorts(ctx)
    application = SessionLifecycle(
        creation_policy=ports,
        store=ports,
        deletion=_SessionLifecycleDeletionPort(ports),
        events=ports,
    )
    return GatewaySessionLifecycleAdapter(ctx, application)


async def _handle_sessions_create(params: dict | None, ctx: RpcContext) -> dict:
    return await _session_lifecycle_adapter(ctx).create(params)


_handle_sessions_create_contract = register_session_lifecycle_contract(
    _d,
    "sessions.create",
    _handle_sessions_create,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _handle_sessions_fork(params: dict | None, ctx: RpcContext) -> dict:
    """Fork a session using the backwards-compatible full/prefix contract."""

    return await _session_lifecycle_adapter(ctx).fork(
        params,
        require_through_turn=False,
    )


async def _handle_sessions_fork_through_turn(params: dict | None, ctx: RpcContext) -> dict:
    """Fork through one terminal turn without a silent full-fork fallback."""

    return await _session_lifecycle_adapter(ctx).fork(
        params,
        require_through_turn=True,
    )


_handle_sessions_fork_contract = register_session_lifecycle_contract(
    _d,
    "sessions.fork",
    _handle_sessions_fork,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_fork_through_turn_contract = register_session_lifecycle_contract(
    _d,
    "sessions.forkThroughTurn",
    _handle_sessions_fork_through_turn,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _should_auto_title(
    ctx: RpcContext,
    storage: Any,
    session: Any,
    key: str,
    session_id: str,
) -> bool:
    try:
        naming_cfg = getattr(getattr(ctx, "config", None), "naming", None)
        if naming_cfg is None or not getattr(naming_cfg, "enabled", False):
            return False
        if not title_slot_is_empty(session):
            return False
        from opensquilla.gateway.session_view import _session_kind, _surface

        origin = getattr(session, "origin", None)
        origin_map = origin if isinstance(origin, dict) else {}
        surface = _surface(
            session, key, origin_map, _channel_types_from_config(getattr(ctx, "config", None))
        )
        session_kind = _session_kind(session, key, surface, origin_map)
        if not is_naming_eligible(naming_cfg, surface, session_kind):
            return False
        return bool(await storage.count_transcript_entries(session_id) == 0)
    except Exception:  # noqa: BLE001 - naming is best-effort
        return False


def _schedule_auto_title(
    ctx: RpcContext,
    key: str,
    first_message: str,
    *,
    enabled: bool,
    session_id: str | None = None,
    root_turn_id: str | None = None,
) -> None:
    if not enabled:
        return
    provider_request_correlation = (
        ProviderRequestCorrelation(
            session_id=session_id,
            turn_id=root_turn_id,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.naming",
        )
        if isinstance(session_id, str)
        and session_id
        and isinstance(root_turn_id, str)
        and root_turn_id
        and not provider_request_correlation_disabled(config=ctx.config)
        else None
    )
    asyncio.create_task(
        generate_session_title(
            ctx,
            key,
            first_message,
            provider_request_correlation=provider_request_correlation,
        ),
        name=f"session-title:{key}",
    )


def _turn_source_scope(source_hint: dict[str, Any], ctx: RpcContext) -> str:
    caller_kind = str(source_hint.get("caller_kind") or "rpc").strip().lower()
    channel_kind = str(source_hint.get("channel_kind") or caller_kind).strip().lower()
    principal_role = str(getattr(ctx.principal, "role", "operator") or "operator")
    return f"{caller_kind}:{channel_kind}:{principal_role}"[:256]


async def _load_followup_annotation_focus(
    storage: SessionStorage,
    *,
    session_id: str,
    document_id: str,
) -> str | None:
    """Return a short read-only focus for the current document follow-up.

    This is intentionally derived from the accepted transcript envelope rather
    than reusing an annotation authority.  The current document context still
    performs the normal owner, session, head, and CAS checks below.
    """

    try:
        get_transcript = getattr(storage, "get_canonical_transcript", None)
        if not callable(get_transcript):
            get_transcript = storage.get_transcript
        entries = await get_transcript(session_id)
        from opensquilla.prompt_annotations import (
            prompt_annotations_from_transcript_envelope,
            render_followup_prompt_annotation_focus,
        )

        user_entries: list[tuple[int, Any, tuple[dict[str, Any], ...]]] = []
        for index, entry in enumerate(entries):
            if getattr(entry, "role", None) != "user":
                continue
            snapshots = prompt_annotations_from_transcript_envelope(getattr(entry, "content", None))
            if snapshots:
                user_entries.append((index, entry, snapshots))
        if not user_entries:
            return None

        annotation_index, _entry, snapshots = user_entries[-1]
        matching = tuple(
            snapshot
            for snapshot in snapshots
            if isinstance(snapshot.get("document"), Mapping)
            and snapshot["document"].get("id") == document_id
        )
        if not matching:
            return None

        later_user_turns = sum(
            1 for entry in entries[annotation_index + 1 :] if getattr(entry, "role", None) == "user"
        )
        if later_user_turns > 1:
            return None
        return render_followup_prompt_annotation_focus(matching)
    except Exception:  # noqa: BLE001 - context continuity must fail open.
        log.debug(
            "sessions.followup_annotation_focus_unavailable",
            session_id=session_id,
            document_id=document_id,
            exc_info=True,
        )
        return None


async def _accepted_turn_response(
    result: TurnAcceptanceResult,
    *,
    client_request_id: str,
    storage: SessionStorage,
    turn_context: dict[str, Any] | None = None,
    accepted_prompt_annotation_ids: Sequence[str] = (),
) -> AdmitTurnResult:
    payload = accepted_turn_payload(result, client_request_id=client_request_id)
    receipt = result.receipt
    payload["session_key"] = receipt.accepted_session_key
    payload["user_message_id"] = receipt.message_id
    if receipt.task_id is not None:
        payload["turn_id"] = receipt.task_id
    normalized_annotation_ids = [
        item.strip()
        for item in accepted_prompt_annotation_ids
        if isinstance(item, str) and item.strip()
    ]
    # A pending-input dispatch can be replayed after the staged row has been
    # consumed.  That replay only has the ingress receipt, not the original
    # RPC payload, so it cannot pass promptAnnotationIds directly.  Recover
    # the immutable ids from the accepted user transcript envelope; otherwise
    # the renderer keeps the local draft and sends it again on the next
    # annotation turn, where preflight correctly rejects the already-SENT row.
    if not normalized_annotation_ids and result.replayed:
        try:
            get_entry = getattr(storage, "get_canonical_transcript_entry", None)
            if callable(get_entry):
                accepted_entry = await get_entry(receipt.session_id, receipt.message_id)
            else:
                get_transcript = getattr(storage, "get_canonical_transcript", None)
                if not callable(get_transcript):
                    get_transcript = storage.get_transcript
                entries = await get_transcript(receipt.session_id)
                accepted_entry = next(
                    (entry for entry in entries if entry.message_id == receipt.message_id),
                    None,
                )
            content = getattr(accepted_entry, "content", None)
            from opensquilla.prompt_annotations import (
                prompt_annotations_from_transcript_envelope,
            )

            normalized_annotation_ids = [
                str(snapshot["annotationId"])
                for snapshot in prompt_annotations_from_transcript_envelope(content)
                if isinstance(snapshot.get("annotationId"), str)
                and snapshot["annotationId"].strip()
            ]
        except Exception:  # noqa: BLE001 - replay response must remain deliverable.
            log.exception(
                "sessions.send.accepted_annotation_recovery_failed",
                session_id=receipt.session_id,
                message_id=receipt.message_id,
            )
    if normalized_annotation_ids:
        payload["acceptedPromptAnnotationIds"] = normalized_annotation_ids

    def _apply_identity_context(context: dict[str, Any]) -> None:
        stable_turn_id = context.get("turn_id")
        if isinstance(stable_turn_id, str) and stable_turn_id:
            payload["turn_id"] = stable_turn_id
        client_message_id = context.get("client_message_id")
        if isinstance(client_message_id, str) and client_message_id:
            payload["client_message_id"] = client_message_id
            payload["clientMessageId"] = client_message_id
        surface_id = context.get("surface_id")
        if isinstance(surface_id, str) and surface_id:
            payload["surface_id"] = surface_id
            payload["surfaceId"] = surface_id

    async def _apply_persisted_identity_context() -> None:
        try:
            get_transcript = getattr(storage, "get_canonical_transcript", None)
            if not callable(get_transcript):
                get_transcript = storage.get_transcript
            entries = await get_transcript(receipt.session_id)
            accepted_entry = next(
                (entry for entry in entries if entry.message_id == receipt.message_id),
                None,
            )
            if accepted_entry is not None and isinstance(accepted_entry.turn_context, dict):
                _apply_identity_context(accepted_entry.turn_context)
        except Exception:  # noqa: BLE001 - accepted response remains deliverable.
            log.exception(
                "sessions.send.accepted_identity_read_failed",
                session_id=receipt.session_id,
                message_id=receipt.message_id,
            )

    if isinstance(turn_context, dict):
        _apply_identity_context(turn_context)

    if receipt.task_id is None:
        if turn_context is None:
            await _apply_persisted_identity_context()
        return payload
    try:
        task_record = await storage.get_agent_task(receipt.task_id)
    except Exception:  # noqa: BLE001 - accepted responses must remain deliverable.
        log.exception(
            "sessions.send.terminal_status_read_failed",
            task_id=receipt.task_id,
        )
        return payload
    if task_record is None:
        return payload
    details = task_record.details if isinstance(task_record.details, dict) else {}
    metadata = details.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if turn_context is None:
        persisted_ids = details.get("persisted_user_message_ids")
        first_persisted_id = (
            persisted_ids[0]
            if isinstance(persisted_ids, list)
            and persisted_ids
            and isinstance(persisted_ids[0], str)
            else details.get("persisted_user_message_id")
        )
        is_later_collected_input = (
            isinstance(first_persisted_id, str)
            and first_persisted_id
            and first_persisted_id != receipt.message_id
        )
        if not is_later_collected_input:
            _apply_identity_context(
                {
                    "turn_id": receipt.task_id,
                    "client_message_id": metadata.get("client_message_id"),
                    "surface_id": metadata.get("surface_id"),
                }
            )
        if is_later_collected_input or "client_message_id" not in payload:
            # A collected task can own several independently identified
            # prompts. The transcript row, not the task's first metadata
            # snapshot, is canonical for a replay of a later input.
            await _apply_persisted_identity_context()

    if result.task_status is None:
        return payload
    if result.task_status not in {
        AgentTaskStatus.SUCCEEDED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.TIMEOUT,
        AgentTaskStatus.ABANDONED,
    }:
        return payload
    payload["terminal_reason"] = task_record.terminal_reason
    payload["terminal_message"] = build_terminal_reply(task_record)
    return payload


class _IngressTurnAuthorityScope:
    """Own newly acquired turn authorities until runtime admission succeeds."""

    def __init__(self) -> None:
        self.authorities: list[Any] = []

    def register(self, authority: Any) -> None:
        self.authorities.append(authority)

    async def close_untransferred(self) -> None:
        for authority in tuple(self.authorities):
            if getattr(authority, "ingress_owned", False) is not True:
                continue
            try:
                await authority.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - preserve the ingress outcome
                log.warning("sessions.send.turn_authority_cleanup_failed", exc_info=True)


_INGRESS_TURN_AUTHORITY_SCOPE: ContextVar[_IngressTurnAuthorityScope | None] = ContextVar(
    "opensquilla_ingress_turn_authority_scope",
    default=None,
)


def _pending_input_storage(ctx: RpcContext) -> SessionStorage:
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is unavailable")
    candidate = get_session_storage(ctx.session_manager)
    if candidate is None:
        raise RpcUnavailableError("Session storage is unavailable")
    return cast(SessionStorage, candidate)


def _pending_input_attachment_scopes(row: PendingChatInput | None) -> set[str]:
    scopes: set[str] = set()
    if row is None:
        return scopes
    for attachment in row.payload.get("attachments") or []:
        if (
            isinstance(attachment, dict)
            and attachment.get("store") == PENDING_CHAT_INPUT_MATERIAL_STORE
            and attachment.get("pending_input_id") == row.pending_input_id
            and isinstance(attachment.get("scope"), str)
            and attachment["scope"]
        ):
            scopes.add(cast(str, attachment["scope"]))
    return scopes


async def _pending_input_current_session_id(
    storage: SessionStorage,
    session_key: str,
) -> str | None:
    session = await storage.get_session(session_key)
    session_id = getattr(session, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _cleanup_pending_input_scopes(
    *,
    ctx: RpcContext,
    pending_input_id: str,
    session_ids: set[str],
) -> None:
    media_root = media_root_from_config(ctx.config)
    for session_id in session_ids:
        try:
            cleanup_pending_chat_input_material(
                media_root=media_root,
                session_id=session_id,
                pending_input_id=pending_input_id,
            )
        except OSError:
            # The durable row lifecycle is authoritative. A filesystem cleanup
            # failure is retried by session deletion and must not turn a
            # committed cancel/dispatch into a misleading RPC failure.
            log.warning(
                "pending_inputs.material_cleanup_failed",
                pending_input_id=pending_input_id,
                session_id=session_id,
            )


def _material_ids_in_transcript_content(content: Any) -> set[str]:
    if not isinstance(content, str):
        return set()
    try:
        root = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return set()
    found: set[str] = set()
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            material_id = value.get("sha256_ref")
            if isinstance(material_id, str) and len(material_id) == 64:
                found.add(material_id.lower())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return found


async def _cleanup_unreferenced_pending_promotions(
    *,
    ctx: RpcContext,
    storage: SessionStorage,
    session_key: str,
    pending_input_id: str,
    source_session_ids: set[str],
) -> None:
    """Delete failed-dispatch canonical copies only when no durable owner remains."""

    media_root = media_root_from_config(ctx.config)
    promotions: dict[str, set[str]] = {}
    for source_session_id in source_session_ids:
        for target_session_id, material_ids in read_pending_chat_input_promotions(
            media_root=media_root,
            source_session_id=source_session_id,
            pending_input_id=pending_input_id,
        ).items():
            promotions.setdefault(target_session_id, set()).update(material_ids)
    if not promotions:
        return

    current_session = await storage.get_session(session_key)
    current_session_id = getattr(current_session, "session_id", None)
    if not isinstance(current_session_id, str) or not current_session_id:
        return

    # Another staged input with the same content is a live reference even if
    # its canonical promotion has not yet been accepted.
    other_pending_ids: set[str] = set()
    try:
        for pending in await storage.list_pending_chat_inputs(session_key):
            if pending.pending_input_id == pending_input_id:
                continue
            for attachment in pending.payload.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                material_id = attachment.get("sha256") or attachment.get("material_id")
                if isinstance(material_id, str) and len(material_id) == 64:
                    other_pending_ids.add(material_id.lower())
    except Exception:  # noqa: BLE001 - cleanup must fail closed.
        return

    for target_session_id, material_ids in promotions.items():
        if target_session_id != current_session_id:
            # A reset archive or child session can still reference a retired
            # generation outside the active SQLite transcript. Without a
            # complete reference proof, preserve its canonical material.
            continue
        try:
            transcript = await storage.get_canonical_transcript(target_session_id)
        except Exception:  # noqa: BLE001 - never delete without a reference proof.
            continue
        transcript_ids: set[str] = set()
        for entry in transcript:
            transcript_ids.update(_material_ids_in_transcript_content(entry.content))
        for material_id in material_ids - transcript_ids - other_pending_ids:
            path = native_io_path(
                transcript_material_path(media_root, target_session_id, material_id)
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "pending_inputs.promotion_cleanup_failed",
                    pending_input_id=pending_input_id,
                    session_id=target_session_id,
                    material_id=material_id,
                )


async def _prepare_session_event_payload(
    ctx: RpcContext,
    session_key: str,
    event_name: str,
    payload: dict,
) -> dict:
    return await prepare_session_event_payload(ctx, session_key, event_name, payload)


async def _send_prepared_to_subscribers(
    ctx: RpcContext,
    session_key: str,
    event_name: str,
    send_payload: dict,
) -> None:
    await send_prepared_to_subscribers(ctx, session_key, event_name, send_payload)


async def _emit_to_subscribers(
    ctx: RpcContext,
    session_key: str,
    event_name: str,
    payload: dict,
) -> None:
    """Prepare, durably replay-buffer, then broadcast one session event."""
    prepared = await _prepare_session_event_payload(
        ctx,
        session_key,
        event_name,
        payload,
    )
    send_payload = _buffer_session_event(session_key, event_name, prepared)
    await _send_prepared_to_subscribers(
        ctx,
        session_key,
        event_name,
        send_payload,
    )


class _GatewayCancellationPorts(CancellationPrimitives):
    """Adapt individual cancellation primitives, never an entire RPC command."""

    def __init__(self, context: RpcContext) -> None:
        self._context = context

    @property
    def session_available(self) -> bool:
        return self._context.session_manager is not None

    @property
    def runtime_available(self) -> bool:
        return self._context.task_runtime is not None

    async def session_exists(self, key: str) -> bool:
        storage = get_session_storage(self._context.session_manager)
        return not storage or await storage.get_session(key) is not None

    def cancel_compactions(self, key: str) -> tuple[asyncio.Task[object], ...]:
        return cancel_active_compactions(key)

    async def cancel_runtime(self, key: str, task_id: str | None, source: str) -> int:
        try:
            return await _cancel_task_runtime(
                self._context.task_runtime,
                session_key=key,
                task_id=task_id,
                source=source,
                reason="user_abort",
            )
        except _TaskScopedCancelUnsupportedError as exc:
            raise ExactCancellationUnavailableError from exc

    async def cancel_auxiliary(self, key: str, task_id: str, deadline: float) -> int:
        return await _cancel_task_owned_auxiliary_work(
            session_key=key,
            task_id=task_id,
            deadline_at_monotonic=deadline,
            process_state_dir=getattr(self._context.config, "state_dir", None),
        )

    async def cancel_descendants(
        self,
        key: str,
        task_id: str,
        source: str,
        deadline: float,
    ) -> int:
        return await _cancel_task_owned_descendants(
            self._context.task_runtime,
            root_session_key=key,
            root_task_id=task_id,
            source=source,
            reason="user_abort",
            deadline_at_monotonic=deadline,
            process_state_dir=getattr(self._context.config, "state_dir", None),
        )

    async def active_task_ids(self, key: str) -> tuple[str, ...]:
        return await _active_task_runtime_ids(self._context.task_runtime, key)

    async def session_tree(self, key: str) -> tuple[str, ...]:
        return await _session_tree_keys(self._context.session_manager, key)

    async def cancel_completion(self, key: str) -> int:
        from opensquilla.gateway.subagent_announce import cancel_background_completion_for_session

        return await cancel_background_completion_for_session(key)

    async def cancel_processes(self, key: str) -> int:
        from opensquilla.process_tree import cancel_persisted_processes_for_session

        return await cancel_persisted_processes_for_session(
            getattr(self._context.config, "state_dir", None),
            key,
        )

    def reject_approvals(self, key: str) -> int:
        from opensquilla.gateway.approval_queue import get_approval_queue

        return get_approval_queue().resolve_pending_for_session(key, approved=False)

    async def drain(self, key: str, task_ids: tuple[str, ...], deadline: float) -> None:
        await _drain_cancelled_task_runtime(
            self._context.task_runtime,
            session_key=key,
            task_ids=task_ids,
            deadline_at_monotonic=deadline,
        )

    def cancel_legacy(self, key: str) -> tuple[bool, bool]:
        registry = get_agent_task_registry()
        task = registry.get(key)
        cancelled = registry.cancel(key)
        needs_terminal = bool(
            cancelled
            and task is not None
            and not getattr(task, "_opensquilla_started", True)
            and not getattr(task, "_opensquilla_terminal_emitted", False)
        )
        if needs_terminal:
            setattr(task, "_opensquilla_terminal_emitted", True)
        return cancelled, needs_terminal

    async def publish_terminal(self, key: str, *, legacy: bool) -> None:
        if legacy:
            await _emit_to_subscribers(
                self._context,
                key,
                "session.event.done",
                {"reason": "aborted"},
            )
        else:
            await _emit_to_subscribers(
                self._context,
                key,
                "sessions.changed",
                build_sessions_changed_payload(
                    key,
                    "task_terminal",
                    run_status="cancelled",
                    last_task={"status": "cancelled", "terminal_reason": "user_abort"},
                ),
            )

    async def bounded[T](
        self,
        operation: Awaitable[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T:
        return cast(
            T,
            await _await_abort_operation(
                operation,
                deadline_at_monotonic=deadline,
                operation=label,
                default=default,
            ),
        )

    async def observe[T](
        self,
        task: asyncio.Task[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T:
        return cast(
            T,
            await _await_abort_background_task(
                task,
                deadline_at_monotonic=deadline,
                operation=label,
                default=default,
            ),
        )


async def _apply_sessions_patch(
    params: dict[str, Any],
    ctx: RpcContext,
    *,
    key: str,
    storage: Any,
) -> dict[str, Any]:
    """Validate and persist one patch while the caller holds its turn fence."""

    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")

    update_values: dict[str, Any] = {}
    (
        provider_present,
        provider_override,
        auth_profile_present,
        auth_profile_override,
    ) = _rpc_session_deployment_fields(params)
    model_present = "model" in params
    existing_provider_value = _model_value(getattr(session, "provider_override", None))
    existing_provider = existing_provider_value.lower() if existing_provider_value else None
    existing_model = _model_value(getattr(session, "model", None))
    existing_auth_profile = _model_value(getattr(session, "auth_profile_override", None))
    final_provider = provider_override if provider_present else existing_provider
    final_auth_profile = auth_profile_override if auth_profile_present else existing_auth_profile
    raw_model = params.get("model")
    requested_model = _model_value(raw_model) if model_present else existing_model
    final_model = requested_model if model_present else existing_model

    provider_changed = bool(provider_present and provider_override != existing_provider)
    auth_profile_changed = bool(
        auth_profile_present and auth_profile_override != existing_auth_profile
    )
    if (provider_changed and provider_override) or (auth_profile_changed and auth_profile_override):
        if not model_present or not isinstance(raw_model, str) or requested_model is None:
            _raise_explicit_session_deployment_model_required()

    if model_present and (
        provider_present or auth_profile_present or existing_provider or existing_auth_profile
    ):
        if raw_model is not None and not isinstance(raw_model, str):
            raise ValueError("params.model must be a string or null")
    if (
        provider_present
        or auth_profile_present
        or (model_present and (existing_provider or existing_auth_profile))
    ):
        _validate_rpc_session_deployment(
            ctx,
            session_key=key,
            provider=final_provider,
            model=final_model,
            auth_profile=final_auth_profile,
        )

    field_map = {
        "displayName": "display_name",
        "model": "model",
        "thinkingLevel": "thinking_level",
        "metadata": "meta",
    }
    updated_fields: list[str] = []
    for field, attr in field_map.items():
        if field in params and hasattr(session, attr):
            update_values[attr] = params[field]
            updated_fields.append(field)
    if model_present and (
        provider_present or auth_profile_present or existing_provider or existing_auth_profile
    ):
        update_values["model"] = final_model
    if provider_present:
        update_values["provider_override"] = provider_override
        updated_fields.append("provider")
    if auth_profile_present:
        update_values["auth_profile_override"] = auth_profile_override
        update_values["auth_profile_override_source"] = "rpc" if auth_profile_override else None
        updated_fields.append("authProfile")

    model_changed = bool(model_present and final_model != existing_model)
    deployment_binding_changed = bool(provider_changed or auth_profile_changed or model_changed)
    if deployment_binding_changed:
        # Physical provenance describes the deployment that already executed.
        # Once an operator changes the future session binding it is no longer a
        # valid pair for compaction target/consumer resolution, so clear rather
        # than forge it as the newly requested deployment.
        update_values["model_provider"] = None
        update_values["model_override"] = None

    if update_values:
        update = getattr(ctx.session_manager, "update", None)
        if update is not None:
            await update(key, **update_values)
        else:
            for attr, value in update_values.items():
                setattr(session, attr, value)
            upsert = getattr(storage, "upsert_session", None)
            if upsert is not None:
                await upsert(session)

    return {"key": key, "updated": updated_fields}


_SESSION_DEPLOYMENT_PATCH_FIELDS = frozenset(
    {
        "model",
        "provider",
        "providerOverride",
        "provider_override",
        "authProfile",
        "authProfileOverride",
        "auth_profile",
        "auth_profile_override",
    }
)


@_d.method("sessions.patch", scope="operator.admin")
async def _handle_sessions_patch(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    assert isinstance(params, dict)
    deployment_patch = any(field in params for field in _SESSION_DEPLOYMENT_PATCH_FIELDS)
    lock = get_session_lock(ctx.turn_runner, key) if deployment_patch else None
    if lock is not None:
        async with lock:
            result = await _apply_sessions_patch(
                params,
                ctx,
                key=key,
                storage=storage,
            )
    else:
        result = await _apply_sessions_patch(
            params,
            ctx,
            key=key,
            storage=storage,
        )
    if deployment_patch:
        keepalive_service = getattr(ctx, "prompt_cache_keepalive_service", None)
        if keepalive_service is not None:
            keepalive_service.refresh_required(key, "session_deployment_changed")
    return result


async def _handle_sessions_rename(params: dict | None, ctx: RpcContext) -> dict:
    """Rename one session without exposing admin-only deployment fields."""

    return await _session_lifecycle_adapter(ctx).rename(params)


_handle_sessions_rename_contract = register_session_lifecycle_contract(
    _d,
    "sessions.rename",
    _handle_sessions_rename,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _delete_session_with_lifecycle(
    *,
    canonical_key: str,
    ctx: RpcContext,
    storage: Any,
) -> None:
    """Quiesce every writer and fail closed before deleting one session."""

    session_keys = [canonical_key]
    async with contextlib.AsyncExitStack() as fences:
        # Child completion can schedule a parent wake while the runtime task is
        # draining, so fence that path before cancelling the task driver.
        await fences.enter_async_context(quiesce_background_completion_sessions(session_keys))

        task_runtime = getattr(ctx, "task_runtime", None)
        quiesce_runtime = getattr(task_runtime, "quiesce_sessions", None)
        if callable(quiesce_runtime):
            await fences.enter_async_context(quiesce_runtime(session_keys))

        await fences.enter_async_context(get_agent_task_registry().quiesce_sessions(session_keys))

        lock = get_session_lock(ctx.turn_runner, canonical_key)
        if lock is not None:
            await fences.enter_async_context(lock)

        # These durable writers may outlive the task coroutine that scheduled
        # them. Settle both before the row and its generation disappear.
        await drain_pending_flushes_for_sessions(session_keys)
        drain_turn_writes = getattr(
            ctx.turn_runner,
            "drain_session_background_writes",
            None,
        )
        if callable(drain_turn_writes):
            await drain_turn_writes(session_keys)

        get_session = getattr(storage, "get_session", None)
        session = await get_session(canonical_key) if callable(get_session) else None
        session_id = getattr(session, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        # Pending owners can still live under a pre-reset session id while the
        # stable session key points at a newer generation. Capture every owner
        # before the DB cascade removes the rows, then reclaim only those
        # private directories after the delete commits.
        pending_material_owners: dict[str, set[str]] = {}
        list_pending = getattr(storage, "list_pending_chat_inputs", None)
        if callable(list_pending):
            for pending in await list_pending(canonical_key):
                scopes = _pending_input_attachment_scopes(pending)
                if scopes:
                    pending_material_owners[pending.pending_input_id] = scopes

        # Terminal task cleanup normally expires owned approvals. Repeat the
        # operation here so already-orphaned and claimed approvals also fail
        # closed before their session record is removed.
        from opensquilla.gateway.approval_queue import get_approval_queue

        get_approval_queue().expire_pending_for_session(canonical_key)
        await storage.delete_session(canonical_key)
        get_session_streams().evict(canonical_key)
        for pending_input_id, session_ids in pending_material_owners.items():
            _cleanup_pending_input_scopes(
                ctx=ctx,
                pending_input_id=pending_input_id,
                session_ids=session_ids,
            )
        keepalive_service = getattr(ctx, "prompt_cache_keepalive_service", None)
        if keepalive_service is not None:
            await keepalive_service.invalidate(canonical_key)

        goal_service = getattr(getattr(ctx, "task_runtime", None), "goal_service", None)
        revoke_goal_lease = getattr(goal_service, "revoke_session", None)
        if callable(revoke_goal_lease):
            revoke_goal_lease(canonical_key)

        evict_runtime_state = getattr(
            ctx.session_manager,
            "evict_session_runtime_state",
            None,
        )
        if callable(evict_runtime_state):
            evict_runtime_state(canonical_key, session_id=session_id)


async def _handle_sessions_delete(params: dict | None, ctx: RpcContext) -> dict:
    """Delete one or more sessions. Accepts {key} for single or {keys} for bulk."""
    return await _session_lifecycle_adapter(ctx).delete(params)


_handle_sessions_delete_contract = register_session_lifecycle_contract(
    _d,
    "sessions.delete",
    _handle_sessions_delete,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


def _session_maintenance_adapter(ctx: RpcContext) -> GatewaySessionMaintenanceAdapter:
    return build_gateway_session_maintenance_adapter(ctx)


def _session_reset_adapter(ctx: RpcContext) -> GatewaySessionResetAdapter:
    return build_gateway_session_reset_adapter(ctx)


async def _handle_sessions_reset(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return await _session_reset_adapter(ctx).reset(params)


async def _handle_sessions_context_compact(params: dict | None, ctx: RpcContext) -> dict:
    return await _session_maintenance_adapter(ctx).compact(params)


async def _handle_sessions_compact(params: dict | None, ctx: RpcContext) -> dict:
    return await _session_maintenance_adapter(ctx).compact(params)


_handle_sessions_reset_contract = register_session_maintenance_contract(
    _d,
    "sessions.reset",
    _handle_sessions_reset,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)

_handle_sessions_context_compact_contract = register_session_maintenance_contract(
    _d,
    "sessions.contextCompact",
    _handle_sessions_context_compact,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)

_handle_sessions_compact_contract = register_session_maintenance_contract(
    _d,
    "sessions.compact",
    _handle_sessions_compact,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


@_d.method("sessions.truncate", scope="operator.write")
async def _handle_sessions_truncate(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.memory.session_flush import FlushReceipt

    key = _require_key(params)
    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    max_messages = (params or {}).get("maxMessages", 20)
    force = bool((params or {}).get("force", False))

    turn_runner = ctx.turn_runner
    lock = get_session_lock(turn_runner, key)

    async def _run_locked() -> dict[str, Any]:
        receipt: FlushReceipt | None = None
        storage = get_session_storage(ctx.session_manager)
        session = None
        if storage is not None:
            session = await storage.get_session(key)
        previous_session_id = getattr(session, "session_id", None) if session else None

        truncate_flush_enabled = flush_trigger_enabled(ctx.config, "session_reset")
        if truncate_flush_enabled and ctx.flush_service is None:
            # Fail-closed: refuse to truncate a non-empty transcript without
            # an admin force override. Empty transcripts are safe to truncate.
            transcript = await ctx.session_manager.get_transcript(key)
            if transcript and not force:
                checkpoint_safe = (
                    storage is not None
                    and await _durable_receipt_allows_covered_destructive_compaction(
                        storage,
                        key,
                        previous_session_id,
                        _truncate_checkpoint_scope_entries(transcript, max_messages),
                    )
                )
                if not checkpoint_safe:
                    raise RpcHandlerError(
                        code="flush_unavailable",
                        message=(
                            "Truncate aborted: flush service is unavailable and "
                            "the transcript is non-empty. Re-run with force=true "
                            "(admin) to truncate without backup."
                        ),
                        details={
                            "key": key,
                            "session_id": previous_session_id,
                            "reason": "flush_service_disabled",
                            "message_count": len(transcript),
                        },
                    )
            if transcript and force and "operator.admin" not in ctx.principal.scopes:
                raise RpcHandlerError(
                    code="permission_denied",
                    message="force=true on sessions.truncate requires operator.admin scope.",
                    details={"key": key, "session_id": previous_session_id},
                )
        elif truncate_flush_enabled:
            if storage is None:
                raise KeyError("No session storage available")
            if session is None:
                raise KeyError(f"Session not found: {key}")
            agent_id = normalize_agent_id(getattr(session, "agent_id", None) or "main")
            transcript = await ctx.session_manager.get_transcript(key)
            if transcript:
                try:
                    flush_turn_id, flush_correlation = _build_session_flush_correlation(
                        ctx,
                        previous_session_id,
                    )
                    flush_kwargs: dict[str, Any] = {
                        "agent_id": agent_id,
                        "timeout": 30.0,
                        "message_window": 0,
                        "segment_mode": "auto",
                        "raw_capture_policy": "required",
                    }
                    if _accepts_keyword_arg(ctx.flush_service.execute, "turn_id"):
                        flush_kwargs["turn_id"] = flush_turn_id
                    if flush_correlation is not None and _accepts_keyword_arg(
                        ctx.flush_service.execute,
                        "provider_request_correlation",
                    ):
                        flush_kwargs["provider_request_correlation"] = flush_correlation
                    receipt = await ctx.flush_service.execute(
                        transcript,
                        key,
                        **flush_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 — both LLM and raw-dump failed
                    receipt = FlushReceipt(
                        mode="error",
                        flushed_paths=[],
                        slug=None,
                        message_count=len(transcript),
                        duration_ms=0,
                        raw_reason=None,
                        error=str(exc),
                        result_status="archive_failed",
                    )
                    raise RpcHandlerError(
                        code="CONTEXT_FLUSH_FAILED",
                        message=f"Truncate aborted: flush failed ({receipt.error})",
                        details={
                            "flush_receipt": receipt.to_dict(),
                            "key": key,
                            "session_id": previous_session_id,
                        },
                    ) from exc

                durable_receipt_safe = await _durable_receipt_allows_covered_destructive_compaction(
                    storage,
                    key,
                    previous_session_id,
                    _truncate_checkpoint_scope_entries(transcript, max_messages),
                )
                memory_status = compaction_memory_status(
                    receipt,
                    deterministic_receipt_safe=durable_receipt_safe,
                    required=True,
                )
                if not memory_status.allows_destructive_compaction:
                    flush_status = flush_receipt_status_for_compaction(receipt, ctx.config)
                    raise RpcHandlerError(
                        code="CONTEXT_FLUSH_FAILED",
                        message=(
                            f"Truncate aborted: flush status {flush_status!r} is not "
                            "sufficient for destructive truncate."
                        ),
                        details={
                            "flush_receipt": flush_receipt_to_dict(receipt),
                            "key": key,
                            "session_id": previous_session_id,
                            "reason": "destructive_truncate_requires_safe_flush",
                            "flush_receipt_status": flush_status,
                            "memory_safety_status": memory_status.safety_status,
                            "semantic_memory_status": memory_status.semantic_status,
                        },
                    )
            else:
                receipt = FlushReceipt(
                    mode="skipped",
                    flushed_paths=[],
                    slug=None,
                    message_count=0,
                    duration_ms=0,
                    raw_reason=None,
                    error=None,
                )

        result = await ctx.session_manager.truncate(key, max_messages=max_messages)
        payload = {
            "key": key,
            "compacted": result["truncated"],
            "mode": "truncate",
            "before_count": result["before_count"],
            "after_count": result["after_count"],
        }
        if receipt is not None:
            payload["flush_receipt"] = flush_receipt_to_dict(receipt)
        return payload

    async def _run_accounted() -> dict[str, Any]:
        from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
        from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

        usage_scope = await build_session_usage_scope(
            getattr(ctx, "usage_event_sink", None),
            ctx.session_manager,
            key,
            run_kind="memory_flush",
        )
        with bind_usage_accounting_scope(usage_scope):
            return await _run_locked()

    if lock is None:
        return await _run_accounted()
    async with lock:
        return await _run_accounted()


async def _handle_sessions_subscribe(params: dict | None, ctx: RpcContext) -> None:
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.subscribe_sessions(ctx.conn_id)
    return None


async def _handle_sessions_unsubscribe(params: dict | None, ctx: RpcContext) -> None:
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.unsubscribe_sessions(ctx.conn_id)
    return None


async def _build_sessions_messages_subscription_payload(
    params: dict | None,
    ctx: RpcContext,
    *,
    key: str,
    subscribed: bool,
    fast_ack: bool,
) -> dict[str, Any]:
    streams = get_session_streams()
    since_stream_seq = _optional_stream_seq(params)
    since_stream_generation = _optional_stream_generation(params)
    if since_stream_generation is None:
        # Pre-generation clients retain only a numeric cursor.  Lift the new
        # process counter before replay/ACK so the next live event is visible
        # even when this Gateway restarted at sequence zero.
        promote_legacy_cursor = getattr(streams, "promote_legacy_cursor", None)
        if callable(promote_legacy_cursor):
            promote_legacy_cursor(key, since_stream_seq)
        replay = streams.replay(key, since_stream_seq)
    else:
        replay = streams.replay(
            key,
            since_stream_seq,
            since_stream_generation,
        )
    replayed_count = 0
    if subscribed and replay.events:
        from opensquilla.gateway.protocol import project_session_event_for_client
        from opensquilla.gateway.websocket import get_registry

        conn = get_registry().get(ctx.conn_id)
        if conn is not None:
            client_caps: frozenset[str] = getattr(conn, "client_caps", frozenset())
            replay_deadline = (
                asyncio.get_running_loop().time() + _SESSION_SUBSCRIBE_REPLAY_BUDGET_SECONDS
            )
            for event in replay.events:
                projected = project_session_event_for_client(
                    event.event_name,
                    event.payload,
                    client_caps=client_caps,
                )
                if projected is None:
                    continue
                event_name, event_payload = projected
                remaining = replay_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Session replay send budget exhausted")
                async with asyncio.timeout(remaining):
                    await conn.send_event(
                        event_name,
                        event_payload,
                        meta={"replayed": True},
                    )
                replayed_count += 1

    replay_payload = {
        "subscribed": subscribed,
        "key": key,
        "stream_generation": replay.stream_generation,
        "current_stream_seq": replay.current_stream_seq,
        "replay_complete": replay.replay_complete,
        "replay_gap_reason": replay.gap_reason,
        "replayed_count": replayed_count,
    }
    if fast_ack:
        return {
            **replay_payload,
            **_deferred_sessions_messages_metadata(),
        }
    # Mixed-version clients still expect the legacy enriched ACK. Keep that
    # payload shape, but never let its storage reads pin the connection's
    # serialized dispatcher indefinitely.
    with bounded_interactive_storage_reads():
        metadata = await _hydrate_sessions_messages_metadata(
            ctx,
            key,
            include_project_workspace=True,
        )
    return {**replay_payload, **metadata}


def _deferred_sessions_messages_metadata() -> dict[str, Any]:
    return session_read_metadata_to_v4(
        deferred_session_read_metadata("deferred"),
        include_key=False,
    )


def _build_session_read_application(
    ctx: RpcContext,
    *,
    clock: SystemClock | None = None,
) -> SessionReadApplication:
    """Compose request-scoped Ports without exposing ``RpcContext`` upstream."""

    streams = get_session_streams()
    manager = getattr(ctx, "session_manager", None)
    storage = cast(SessionStorage | None, get_session_storage(manager))
    session_missing = object()
    cached_session: object = session_missing

    async def read_session(session_key: str) -> Any | None:
        nonlocal cached_session
        if cached_session is session_missing:
            cached_session = await storage.get_session(session_key) if storage is not None else None
        return cached_session

    async def read_tasks(session_key: str) -> SessionTaskState:
        task_rows = await _list_task_rows(ctx, storage, session_key)
        task_state = _task_state_summary(task_rows)
        await _overlay_runtime_task_snapshot(ctx, session_key, task_state)
        await _attach_active_steer_capability(ctx, session_key, task_state)
        from opensquilla.gateway.subagent_announce import (
            active_background_completion_group_ids,
            active_background_completion_run_mode_override,
        )

        active_task_group_ids = await active_background_completion_group_ids(session_key)
        background_run_mode_override = (
            await active_background_completion_run_mode_override(session_key)
            if active_task_group_ids
            else None
        )
        session = await read_session(session_key)
        lock = _run_mode_lock_payload(
            task_rows=task_rows,
            active_task_group_ids=active_task_group_ids,
            background_override=background_run_mode_override,
            session=session,
            principal=ctx.principal,
        )
        queued_task_ids = task_state.get("queued_task_ids")
        return SessionTaskState(
            tasks=tuple(cast(Sequence[Mapping[str, Any]], task_state.get("tasks", ()))),
            active_task=cast(
                Mapping[str, Any] | None,
                task_state.get("active_task"),
            ),
            last_task=cast(
                Mapping[str, Any] | None,
                task_state.get("last_task"),
            ),
            run_status=str(task_state.get("run_status") or "idle"),
            queued_task_ids=(
                tuple(cast(Sequence[str], queued_task_ids)) if queued_task_ids is not None else None
            ),
            active_task_group_ids=tuple(active_task_group_ids),
            run_mode_lock=SessionRunModeLock(
                locked=bool(lock.get("locked")),
                run_mode=(lock.get("runMode") if isinstance(lock.get("runMode"), str) else None),
                source=(lock.get("source") if isinstance(lock.get("source"), str) else None),
            ),
        )

    async def read_workspace(
        session_key: str,
        include_project_workspace: bool,
    ) -> SessionWorkspaceState:
        session = await read_session(session_key)
        workspace_id = getattr(session, "workspace_id", None)
        project_snapshot = (
            await persisted_project_workspace_snapshot(storage, session)
            if include_project_workspace and storage is not None and session is not None
            else None
        )
        return SessionWorkspaceState(
            workspace_id=cast(str | None, workspace_id),
            project_workspace=cast(Mapping[str, Any] | None, project_snapshot),
            project_workspace_deferred=(bool(workspace_id) and not include_project_workspace),
        )

    async def read_pending_inputs(
        session_key: str,
    ) -> Sequence[Mapping[str, Any]]:
        getter = getattr(
            getattr(ctx, "task_runtime", None),
            "pending_user_inputs",
            None,
        )
        if not callable(getter):
            return ()
        candidate = getter(session_key)
        result = await candidate if inspect.isawaitable(candidate) else candidate
        return cast(Sequence[Mapping[str, Any]], result)

    async def read_routing(session_key: str) -> Mapping[str, Any]:
        return await _resolve_session_routing_snapshot(ctx, session_key)

    async def read_planning(session_key: str) -> SessionPlanningState:
        session = await read_session(session_key)
        collaboration: Mapping[str, Any] | None = None
        current_plan_payload: Mapping[str, Any] | None = None
        active_plan_run_payload: Mapping[str, Any] | None = None
        goal_payload: Mapping[str, Any] | None = None
        session_epoch: int | None = None
        if storage is not None and session is not None:
            session_epoch = await _bootstrap_epoch(
                ctx.session_manager,
                storage,
                session,
                session_key,
            )
            collaboration = _plan_collaboration_snapshot(session)
            get_current_plan = getattr(storage, "get_current_plan_revision", None)
            get_active_run = getattr(storage, "get_active_plan_run", None)
            current_plan = (
                await get_current_plan(session_key) if callable(get_current_plan) else None
            )
            active_plan_run = (
                await get_active_run(session_key) if callable(get_active_run) else None
            )
            from opensquilla.session.plans import (
                plan_revision_snapshot,
                plan_run_snapshot,
            )

            if current_plan is not None:
                current_plan_payload = plan_revision_snapshot(
                    current_plan,
                    current=True,
                )
            if active_plan_run is not None:
                active_plan_run_payload = plan_run_snapshot(active_plan_run)
            get_goal = getattr(storage, "get_goal", None)
            goal = await get_goal(session_key) if callable(get_goal) else None
            if goal is not None:
                goal_service = getattr(
                    getattr(ctx, "task_runtime", None),
                    "goal_service",
                    None,
                )
                snapshot = getattr(goal_service, "snapshot", None)
                if callable(snapshot):
                    goal_payload = cast(Mapping[str, Any], await snapshot(goal))
                else:
                    from opensquilla.session.goals import goal_snapshot

                    goal_payload = goal_snapshot(goal)

        return SessionPlanningState(
            collaboration=collaboration,
            current_plan=current_plan_payload,
            active_plan_run=active_plan_run_payload,
            goal=goal_payload,
            epoch=session_epoch,
        )

    ports = GatewaySessionReadPorts(
        streams=streams,
        read_tasks=read_tasks,
        read_workspace=read_workspace,
        read_pending_inputs=read_pending_inputs,
        read_routing=read_routing,
        read_planning=read_planning,
    )
    return build_v4_session_read_application(
        streams=streams,
        session_manager=manager,
        storage=storage,
        ports=ports,
        clock=clock,
    )


async def _hydrate_sessions_messages_metadata(
    ctx: RpcContext,
    key: str,
    *,
    include_project_workspace: bool = False,
) -> dict[str, Any]:
    """Load authoritative subscription metadata outside the fast ACK path."""

    application = _build_session_read_application(ctx)
    metadata = await application.read_metadata(
        SessionMetadataQuery(
            session_key=key,
            include_project_workspace=include_project_workspace,
        )
    )
    return session_read_metadata_to_v4(metadata)


async def _handle_sessions_messages_subscribe(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)
    if ":subagent:" in key:
        storage = get_session_storage(getattr(ctx, "session_manager", None))
        session = await storage.get_session(key) if storage is not None else None
        if session is None:
            raise RpcHandlerError(
                "SESSION_NOT_FOUND",
                "Session was deleted or does not exist.",
                retryable=False,
                accepted=False,
            )
    fast_ack = (params or {}).get("fast_ack") is True
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    registered_new = False
    if subscription_mgr is not None:
        registered_new = ctx.conn_id not in subscription_mgr.get_message_subscribers(key)
        subscription_mgr.subscribe_messages(ctx.conn_id, key)

    try:
        return await _build_sessions_messages_subscription_payload(
            params,
            ctx,
            key=key,
            subscribed=subscription_mgr is not None,
            fast_ack=fast_ack,
        )
    except BaseException:
        # Registration precedes replay so no event can fall into a subscribe
        # gap.  If replay or payload assembly then fails, remove only the
        # registration created by this request; repeated subscribe stays idempotent.
        if subscription_mgr is not None and registered_new:
            subscription_mgr.unsubscribe_messages(ctx.conn_id, key)
        raise


async def _handle_sessions_messages_hydrate(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)
    # This is an interactive continuation of the fast subscribe ACK. Keep all
    # storage coordination inside the same bounded-read contract as history so
    # metadata cannot pin the connection's serialized dispatcher indefinitely.
    with bounded_interactive_storage_reads():
        return await _hydrate_sessions_messages_metadata(ctx, key)


async def _handle_sessions_messages_snapshot(params: dict | None, ctx: RpcContext) -> dict:
    """Return a compact active-turn base before a client subscribes for deltas."""

    from opensquilla.gateway.websocket import get_registry

    key = _require_key(params)
    connection = get_registry().get(ctx.conn_id)
    client_caps: frozenset[str] = getattr(connection, "client_caps", frozenset())
    application = _build_session_read_application(ctx)
    return session_read_snapshot_to_v4(
        key,
        application.read_snapshot(key, client_caps=client_caps),
    )


async def _handle_sessions_messages_unsubscribe(params: dict | None, ctx: RpcContext) -> None:
    key = _require_key(params)
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.unsubscribe_messages(ctx.conn_id, key)
    return None


async def _handle_sessions_preview(params: dict | None, ctx: RpcContext) -> dict:
    # Preserve the legacy order: a truthy non-mapping params value raises from
    # ``.get`` before an unavailable manager can produce an empty response.
    raw_keys, raw_limit = preview_params_from_v4(params)
    clock = SystemClock()
    now_ms = clock.now_ms()

    if ctx.session_manager is None:
        return {"ts": now_ms, "previews": []}

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return {"ts": now_ms, "previews": []}

    application = _build_session_read_application(ctx, clock=clock)

    # Preview is an interactive read. Keep storage lock acquisition bounded
    # while preserving the existing key/list selection and response shape.
    with bounded_interactive_storage_reads():
        # Key iteration stays inside the same bounded section as the old
        # handler; malformed iterables therefore fail at the same point.
        query = preview_query_from_v4_values(raw_keys, raw_limit)
        result = await application.read_previews(query)

    return preview_result_to_v4(result)


_handle_sessions_messages_subscribe_contract = register_sessions_messages_subscribe_contract(
    _d,
    _handle_sessions_messages_subscribe,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_messages_hydrate_contract = register_sessions_messages_hydrate_contract(
    _d,
    _handle_sessions_messages_hydrate,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_messages_snapshot_contract = register_sessions_messages_snapshot_contract(
    _d,
    _handle_sessions_messages_snapshot,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_messages_unsubscribe_contract = register_sessions_messages_unsubscribe_contract(
    _d,
    _handle_sessions_messages_unsubscribe,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_preview_contract = register_sessions_preview_contract(
    _d,
    _handle_sessions_preview,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _handle_sessions_resolve(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    resolution = await SessionDirectory(storage).resolve(key)

    return {
        "session_key": resolution.key,
        "session_id": resolution.session_id,
        "status": resolution.status,
        "agent_id": resolution.agent_id,
        "model": resolution.model,
        "workspaceId": resolution.workspace_id,
        "projectWorkspaceDeferred": bool(resolution.workspace_id),
        "created_at": resolution.created_at,
        "updated_at": resolution.updated_at,
    }


_handle_sessions_resolve_contract = register_sessions_resolve_contract(
    _d,
    _handle_sessions_resolve,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


async def _bootstrap_epoch(
    session_manager: Any,
    storage: Any,
    session: Any,
    session_key: str,
) -> int:
    cached = get_session_epoch(session_manager, session_key)
    if cached is not None:
        return int(cached)

    epoch: Any = None
    get_epoch = getattr(storage, "get_epoch", None)
    if callable(get_epoch):
        try:
            epoch = await get_epoch(session_key)
        except Exception:
            log.warning("sessions.bootstrap.epoch_read_failed", session_key=session_key)
    if epoch is None:
        epoch = getattr(session, "epoch", 0)
    try:
        resolved = max(0, int(epoch or 0))
    except (TypeError, ValueError):
        resolved = 0
    set_session_epoch(session_manager, session_key, resolved)
    return resolved


def _require_plan_session_key(params: dict | None) -> str:
    key = _optional_string_param(params, "sessionKey", "session_key", "key")
    if key is None:
        raise ValueError("params.sessionKey is required")
    return canonicalize_session_key(key)


def _plan_collaboration_snapshot(
    session: Any,
    *,
    applies_to: str = "next_turn",
) -> dict[str, Any]:
    return {
        "mode": str(getattr(session, "collaboration_mode", "default") or "default"),
        "revision": int(getattr(session, "collaboration_revision", 0) or 0),
        "appliesTo": applies_to,
    }


def _session_routing_snapshot(
    value: Any,
    *,
    applies_to: str = "next_accepted_turn",
) -> dict[str, Any]:
    """Normalize the storage/manager routing result for public RPCs."""

    if isinstance(value, dict):
        raw_mode = value.get("mode")
        revision = value.get("revision", 0)
        source = value.get("source", "session")
        initialized = value.get("initialized", False)
    else:
        raw_mode = getattr(value, "mode", None)
        revision = getattr(value, "revision", 0)
        source = getattr(value, "source", "session")
        initialized = getattr(value, "initialized", False)
    mode = str(raw_mode or "direct").strip().lower()
    if mode not in _SESSION_ROUTING_MODES:
        mode = "direct"
    return {
        "mode": mode,
        "revision": max(0, int(revision or 0)),
        "source": str(source or "session"),
        "initialized": bool(initialized),
        "appliesTo": applies_to,
    }


def _global_session_routing_snapshot(ctx: RpcContext) -> dict[str, Any]:
    """Return the current global default for a not-yet-created session."""

    from opensquilla.gateway.model_routing import model_routing_snapshot

    mode = str(model_routing_snapshot(ctx.config).get("mode") or "direct")
    return _session_routing_snapshot(
        {
            "mode": mode,
            "revision": 0,
            "source": "global",
            "initialized": False,
        }
    )


async def _resolve_session_routing_snapshot(
    ctx: RpcContext,
    key: str,
) -> dict[str, Any]:
    """Resolve one durable row; draft keys expose their global creation default."""

    manager = ctx.session_manager
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    fallback = _global_session_routing_snapshot(ctx)["mode"]
    getter = getattr(manager, "get_session_routing", None)
    try:
        if callable(getter):
            return _session_routing_snapshot(await getter(key, fallback_mode=fallback))
        storage = get_session_storage(manager)
        resolver = getattr(storage, "resolve_model_routing_mode", None)
        if callable(resolver):
            return _session_routing_snapshot(await resolver(key, fallback))
    except KeyError:
        # This is a new-chat draft, not a durable inherited value. The first
        # accepted turn writes its `initialRoutingMode` or this global value.
        return _global_session_routing_snapshot(ctx)
    # Mixed-version/in-memory session services can still provide the global
    # default until their durable resolver is available.
    return _global_session_routing_snapshot(ctx)


async def _goal_owned_plan_run_for_revision(
    storage: Any,
    revision_id: str,
) -> Any | None:
    """Return the Goal-owned execution overlay for an internal revision."""

    getter = getattr(storage, "get_latest_plan_run_for_revision", None)
    if not callable(getter):
        return None
    run = await getter(revision_id)
    return run if run is not None and str(getattr(run, "driver_kind", "") or "") == "goal" else None


async def _handle_plans_capabilities(
    _params: dict | None,
    _ctx: RpcContext,
) -> dict[str, bool]:
    """Advertise mode contracts that must fail closed across mixed versions."""

    return {
        "planMode": True,
        "initialModeOnSend": True,
        "atomicInitialMode": True,
    }


async def _handle_sessions_routing_get(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Return a session's effective routing strategy and CAS generation."""

    key = _require_plan_session_key(params)
    snapshot = await _resolve_session_routing_snapshot(ctx, key)
    return {
        "key": key,
        "sessionKey": key,
        **snapshot,
        "routing": snapshot,
    }


async def _handle_sessions_routing_set(
    params: dict | None,
    ctx: RpcContext,
    *,
    _explicit_ingress_intent_registered: bool = False,
) -> dict[str, Any]:
    """CAS-update a durable session mode before the next admitted turn."""

    key = _require_plan_session_key(params)
    runtime = getattr(ctx, "task_runtime", None)
    register = getattr(runtime, "explicit_ingress_intent", None)
    if not _explicit_ingress_intent_registered and callable(register):
        async with register(key):
            return cast(
                dict[str, Any],
                await _handle_sessions_routing_set(
                    params,
                    ctx,
                    _explicit_ingress_intent_registered=True,
                ),
            )
    mode = _optional_string_param(params, "mode")
    if mode not in _SESSION_ROUTING_MODES:
        raise ValueError("params.mode must be direct, router, or ensemble")
    expected_revision = (params or {}).get(
        "expectedRevision",
        (params or {}).get("expected_revision"),
    )
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("params.expectedRevision must be a non-negative integer")
    # Reuse the global control's activation planner as validation only. It
    # catches an unbuildable Ensemble lineup without changing shared config.
    from opensquilla.gateway.model_routing import model_routing_patches

    model_routing_patches(ctx.config, mode)
    manager = ctx.session_manager
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    setter = getattr(manager, "set_session_routing", None)
    storage = get_session_storage(manager)
    if not callable(setter):
        setter = getattr(storage, "set_model_routing_mode", None)
    if not callable(setter):
        raise RpcUnavailableError("Session routing storage is not configured")

    async def _commit() -> dict[str, Any]:
        try:
            return _session_routing_snapshot(
                await setter(key, mode, expected_revision=expected_revision)
            )
        except KeyError as exc:
            raise RpcHandlerError(
                "SESSION_NOT_FOUND",
                "Set a new chat's initialRoutingMode with its first message instead.",
                retryable=False,
                accepted=False,
            ) from exc

    try:
        collector = getattr(runtime, "collect_admission", None)
        if callable(collector):
            async with collector(key):
                snapshot = await _commit()
        else:
            lock = get_session_lock(ctx.turn_runner, key)
            if lock is None:
                snapshot = await _commit()
            else:
                async with lock:
                    snapshot = await _commit()
    except SessionRoutingConflictError as exc:
        latest = await _resolve_session_routing_snapshot(ctx, key)
        raise RpcHandlerError(
            "SESSION_ROUTING_CHANGED",
            str(exc),
            details={"routing": latest},
            retryable=True,
            accepted=False,
        ) from exc

    event = {
        "key": key,
        "sessionKey": key,
        "routing": snapshot,
        **snapshot,
    }
    await _emit_to_subscribers(ctx, key, "sessions.routing.changed", event)
    return event


async def _handle_plans_set_mode(
    params: dict | None,
    ctx: RpcContext,
    *,
    _explicit_ingress_intent_registered: bool = False,
) -> dict:
    key = _require_plan_session_key(params)
    runtime = getattr(ctx, "task_runtime", None)
    register = getattr(runtime, "explicit_ingress_intent", None)
    if not _explicit_ingress_intent_registered and callable(register):
        async with register(key):
            return cast(
                dict[Any, Any],
                await _handle_plans_set_mode(
                    params,
                    ctx,
                    _explicit_ingress_intent_registered=True,
                ),
            )
    mode = _optional_string_param(params, "mode")
    if mode not in {"default", "plan"}:
        raise ValueError("params.mode must be default or plan")
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    expected_raw = (params or {}).get(
        "expectedRevision",
        (params or {}).get("expected_revision"),
    )
    if expected_raw is not None and (
        isinstance(expected_raw, bool) or not isinstance(expected_raw, int)
    ):
        raise ValueError("params.expectedRevision must be an integer")
    current = await storage.get_session(key)
    if current is None:
        if expected_raw not in {None, 0}:
            raise RpcHandlerError(
                "COLLABORATION_CHANGED",
                "The session does not exist at the expected revision.",
                details={
                    "collaboration": {
                        "mode": "default",
                        "revision": 0,
                        "appliesTo": "next_turn",
                    }
                },
                retryable=True,
                accepted=False,
            )
        lock = get_session_lock(ctx.turn_runner, key)

        async def _materialize_draft() -> Any:
            existing = await storage.get_session(key)
            if existing is not None:
                return existing
            try:
                return await ctx.session_manager.create(
                    key,
                    agent_id=parse_agent_id(key),
                    display_name="WebChat",
                )
            except ValueError:
                raced = await storage.get_session(key)
                if raced is None:
                    raise
                return raced

        if lock is None:
            current = await _materialize_draft()
        else:
            async with lock:
                current = await _materialize_draft()
        await _emit_to_subscribers(
            ctx,
            key,
            "sessions.changed",
            build_sessions_changed_payload(key, "created", run_status="idle"),
        )
    if expected_raw is None:
        expected_revision = int(current.collaboration_revision or 0)
    else:
        expected_revision = expected_raw

    async def _commit_mode() -> Any:
        return await storage.set_collaboration_mode(
            key,
            mode,
            expected_revision=expected_revision,
        )

    try:
        if (
            runtime is not None
            and callable(getattr(runtime, "explicit_ingress_intent", None))
            and callable(getattr(runtime, "collect_admission", None))
        ):
            async with runtime.collect_admission(key):
                updated = await _commit_mode()
        else:
            updated = await _commit_mode()
    except PlanConflictError as exc:
        latest = await storage.get_session(key)
        raise RpcHandlerError(
            "COLLABORATION_CHANGED",
            str(exc),
            details={
                "collaboration": (
                    _plan_collaboration_snapshot(latest) if latest is not None else None
                )
            },
            retryable=True,
            accepted=False,
        ) from exc
    active_task_id = None
    active_task = getattr(ctx.task_runtime, "active_task_id", None)
    if callable(active_task):
        active_task_id = await active_task(key)
    snapshot = _plan_collaboration_snapshot(updated)
    snapshot["activeTaskId"] = active_task_id
    await _emit_to_subscribers(
        ctx,
        key,
        "session.event.collaboration_mode",
        {"session_key": key, "collaboration": snapshot},
    )
    goal_service = getattr(getattr(ctx, "task_runtime", None), "goal_service", None)
    on_mode_committed = getattr(goal_service, "on_mode_committed", None)
    if callable(on_mode_committed):
        try:
            await on_mode_committed(key, mode)
        except Exception:  # noqa: BLE001 - collaboration commit is authoritative.
            log.warning(
                "plans.set_mode.goal_hook_failed",
                session_key=key,
                exc_info=True,
            )
    return {"sessionKey": key, "collaboration": snapshot}


async def _handle_plans_implement(
    params: dict | None,
    ctx: RpcContext,
    *,
    _explicit_ingress_intent_registered: bool = False,
) -> dict:
    key = _require_plan_session_key(params)
    revision_id = _optional_string_param(
        params,
        "planRevisionId",
        "plan_revision_id",
    )
    if revision_id is None:
        raise ValueError("params.planRevisionId is required")
    if not _explicit_ingress_intent_registered:
        runtime = getattr(ctx, "task_runtime", None)
        register = getattr(runtime, "explicit_ingress_intent", None)
        if callable(register):
            async with register(key):
                return cast(
                    dict[Any, Any],
                    await _handle_plans_implement(
                        params,
                        ctx,
                        _explicit_ingress_intent_registered=True,
                    ),
                )
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    goal_run = await _goal_owned_plan_run_for_revision(storage, revision_id)
    if goal_run is not None:
        raise RpcHandlerError(
            "PLAN_RUN_GOAL_OWNED",
            "This revision belongs to a Goal run and is not a Plan proposal.",
            details={"runId": goal_run.run_id},
            retryable=False,
            accepted=False,
        )
    client_request_id = (
        _optional_string_param(
            params,
            "clientRequestId",
            "client_request_id",
        )
        or uuid.uuid4().hex
    )
    intent = _optional_string_param(params, "intent")
    revision = await storage.get_plan_revision(revision_id)
    if revision is None:
        # A new-task implementation owns an independent copied lineage. If the
        # source session is later deleted, an exact retry must still replay the
        # already accepted target task/run instead of failing before ingress
        # idempotency gets a chance to match it.
        previous = await storage.get_turn_ingress_receipt(
            source_scope=_turn_source_scope(
                {
                    "caller_kind": "web",
                    "source_name": "plans.implement",
                },
                ctx,
            ),
            request_session_key=key,
            client_request_id=client_request_id,
        )
        previous_task_id = previous.receipt.task_id if previous is not None else None
        previous_task = await storage.get_agent_task(previous_task_id) if previous_task_id else None
        previous_details = (
            previous_task.details
            if previous_task is not None and isinstance(previous_task.details, dict)
            else {}
        )
        previous_metadata = previous_details.get("metadata")
        previous_metadata = previous_metadata if isinstance(previous_metadata, dict) else {}
        accepted_revision_id = str(previous_metadata.get("plan_revision_id") or "").strip()
        accepted_revision = (
            await storage.get_plan_revision(accepted_revision_id) if accepted_revision_id else None
        )
        if accepted_revision is None:
            raise KeyError(f"Plan revision not found: {revision_id}")
        revision_title = accepted_revision.title
    else:
        revision_title = revision.title
    explicit_message = _optional_string_param(params, "message")
    message = explicit_message or (
        f"Implement the approved plan “{revision_title}”. "
        "Work through its ordered steps and record truthful checkpoints."
    )
    send_params = {
        "key": key,
        "message": message,
        "clientRequestId": client_request_id,
        "intent": intent or "continue",
        "queueMode": "followup",
        "inputProvenanceKind": "plan_implementation",
        "noMemoryCapture": True,
        "source": {
            "caller_kind": "web",
            "source_name": "plans.implement",
        },
    }
    if explicit_message is None:
        # The generated instruction is control-plane input, not user-authored
        # conversation text. Keep it durable and provider-visible while asking
        # display surfaces to omit it from the visible transcript.
        send_params["displayText"] = ""
    target_before_acceptance = await storage.get_session(key)
    current_session_implementation = send_params["intent"] == "continue"
    command = replace(
        decode_admit_turn(
            send_params,
            principal_role=str(ctx.principal.role),
            connection_id=ctx.conn_id,
            fingerprint_params={
                "action": "plans.implement",
                "sessionKey": key,
                "planRevisionId": revision_id,
                "message": message,
                "intent": send_params["intent"],
            },
        ),
        plan=PlanAdmissionContext(
            revision_id=revision_id,
            required_collaboration_mode="default",
            expected_collaboration_revision=(
                int(target_before_acceptance.collaboration_revision or 0)
                if current_session_implementation and target_before_acceptance is not None
                else None
            ),
            expected_active_revision_id=revision_id if current_session_implementation else None,
            require_idle=current_session_implementation,
        ),
        explicit_intent_registered=_explicit_ingress_intent_registered,
    )
    try:
        result = await build_turn_admission_application(ctx).admit(command)
    except Exception as exc:
        mapped = map_admission_error(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    accepted_key = str(result.get("session_key") or key)
    task_id = str(result.get("turn_id") or result.get("task_id") or "").strip()
    task_record = await storage.get_agent_task(task_id) if task_id else None
    task_details = (
        task_record.details
        if task_record is not None and isinstance(task_record.details, dict)
        else {}
    )
    task_metadata = task_details.get("metadata")
    task_metadata = task_metadata if isinstance(task_metadata, dict) else {}
    accepted_run_id = str(task_metadata.get("plan_run_id") or "").strip()
    accepted_revision_id = str(task_metadata.get("plan_revision_id") or "").strip()
    if not accepted_run_id or not accepted_revision_id:
        raise RuntimeError("Accepted plan implementation lost its durable binding")
    accepted_run = await storage.get_plan_run(accepted_run_id)
    accepted_revision = await storage.get_plan_revision(accepted_revision_id)
    if accepted_run is None or accepted_revision is None:
        raise RuntimeError("Accepted plan implementation binding no longer exists")
    session = await storage.get_session(accepted_key)
    from opensquilla.session.plans import plan_revision_snapshot, plan_run_snapshot

    collaboration = (
        _plan_collaboration_snapshot(session)
        if session is not None
        else {"mode": "default", "revision": 0, "appliesTo": "next_turn"}
    )
    run_snapshot = plan_run_snapshot(accepted_run)
    await _emit_to_subscribers(
        ctx,
        accepted_key,
        "session.event.plan_run",
        {"session_key": accepted_key, "plan_run": run_snapshot},
    )
    await _emit_to_subscribers(
        ctx,
        accepted_key,
        "session.event.collaboration_mode",
        {"session_key": accepted_key, "collaboration": collaboration},
    )
    return {
        **result,
        "sessionKey": accepted_key,
        "collaboration": collaboration,
        "planRevision": plan_revision_snapshot(accepted_revision, current=True),
        "planRun": run_snapshot,
    }


async def _handle_plans_revise(
    params: dict | None,
    ctx: RpcContext,
    *,
    _explicit_ingress_intent_registered: bool = False,
) -> dict:
    key = _require_plan_session_key(params)
    revision_id = _optional_string_param(
        params,
        "planRevisionId",
        "plan_revision_id",
    )
    prompt = _optional_string_param(params, "prompt")
    if revision_id is None:
        raise ValueError("params.planRevisionId is required")
    if prompt is None:
        raise ValueError("params.prompt is required")
    if not _explicit_ingress_intent_registered:
        runtime = getattr(ctx, "task_runtime", None)
        register = getattr(runtime, "explicit_ingress_intent", None)
        if callable(register):
            async with register(key):
                return cast(
                    dict[Any, Any],
                    await _handle_plans_revise(
                        params,
                        ctx,
                        _explicit_ingress_intent_registered=True,
                    ),
                )
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    goal_run = await _goal_owned_plan_run_for_revision(storage, revision_id)
    if goal_run is not None:
        raise RpcHandlerError(
            "PLAN_RUN_GOAL_OWNED",
            "This revision belongs to a Goal run and cannot be revised through Plan mode.",
            details={"runId": goal_run.run_id},
            retryable=False,
            accepted=False,
        )
    client_request_id = (
        _optional_string_param(
            params,
            "clientRequestId",
            "client_request_id",
        )
        or uuid.uuid4().hex
    )
    provider_message = (
        "Create a complete replacement for the current plan revision. "
        "Preserve still-valid context, incorporate the user's requested changes, "
        "and submit the full revised plan rather than a patch.\n\n"
        f"Requested changes:\n{prompt}"
    )
    send_params = {
        "key": key,
        "message": provider_message,
        "displayText": prompt,
        "clientRequestId": client_request_id,
        "intent": "continue",
        "queueMode": "followup",
        "source": {
            "caller_kind": "web",
            "source_name": "plans.revise",
        },
    }
    fingerprint_params = {
        "action": "plans.revise",
        "sessionKey": key,
        "planRevisionId": revision_id,
        "prompt": prompt,
    }

    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    command = replace(
        decode_admit_turn(
            send_params,
            principal_role=str(ctx.principal.role),
            connection_id=ctx.conn_id,
            fingerprint_params=fingerprint_params,
        ),
        plan=PlanAdmissionContext(
            context_revision_id=revision_id,
            required_collaboration_mode="plan",
            expected_collaboration_revision=int(session.collaboration_revision or 0),
            expected_active_revision_id=revision_id,
            atomic_mode_update=True,
        ),
        explicit_intent_registered=_explicit_ingress_intent_registered,
    )
    try:
        result = await build_turn_admission_application(ctx).admit(command)
    except Exception as exc:
        mapped = map_admission_error(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    accepted_session = await storage.get_session(key)
    collaboration = (
        _plan_collaboration_snapshot(accepted_session)
        if accepted_session is not None
        else {"mode": "plan", "revision": 0, "appliesTo": "next_turn"}
    )
    if not bool(result.get("replayed")):
        await _emit_to_subscribers(
            ctx,
            key,
            "session.event.collaboration_mode",
            {"session_key": key, "collaboration": collaboration},
        )
    return {**result, "sessionKey": key, "collaboration": collaboration}


async def _handle_plans_cancel_run(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_plan_session_key(params)
    run_id = _optional_string_param(params, "runId", "run_id")
    if run_id is None:
        raise ValueError("params.runId is required")
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    run = await storage.get_plan_run(run_id)
    if run is None or run.session_key != key:
        raise KeyError(f"Plan run not found: {run_id}")
    if str(getattr(run, "driver_kind", "") or "") == "goal":
        raise RpcHandlerError(
            "PLAN_RUN_GOAL_OWNED",
            "This execution belongs to Goal mode; use the Goal controls to pause or clear it.",
            details={"runId": run.run_id},
            retryable=False,
            accepted=False,
        )
    expected_raw = (params or {}).get(
        "expectedStateRevision",
        (params or {}).get("expected_state_revision"),
    )
    if expected_raw is None:
        expected_revision = int(run.state_revision)
    elif isinstance(expected_raw, bool) or not isinstance(expected_raw, int):
        raise ValueError("params.expectedStateRevision must be an integer")
    else:
        expected_revision = expected_raw
    from opensquilla.session.plans import (
        PLAN_RUN_ACTIVE_STATUSES,
        plan_run_snapshot,
    )

    def _changed(exc: Exception, latest: Any) -> RpcHandlerError:
        return RpcHandlerError(
            "PLAN_RUN_CHANGED",
            str(exc),
            details={"planRun": plan_run_snapshot(latest) if latest is not None else None},
            retryable=True,
            accepted=False,
        )

    if int(run.state_revision) != expected_revision:
        raise _changed(
            PlanRunConflictError("plan run state changed before cancellation"),
            run,
        )

    # Cancellation is a safety action, not a cosmetic status change.  Stop the
    # implementation task first, then CAS the durable run to ``cancelled``.
    # The runtime's terminal cleanup may pause the run in between; retry that
    # self-induced revision once using the freshly read state.
    candidate = run
    cancelled_task_ids: set[str] = set()
    updated = None
    for _attempt in range(3):
        active_task_id = str(candidate.active_task_id or "").strip()
        if candidate.status in {"queued", "running"} and not active_task_id:
            raise RpcHandlerError(
                "PLAN_RUN_TASK_UNKNOWN",
                "The implementation task cannot be identified safely.",
                retryable=True,
                accepted=False,
            )
        if active_task_id and active_task_id not in cancelled_task_ids:
            task_runtime = getattr(ctx, "task_runtime", None)
            runtime_cancel = getattr(task_runtime, "cancel", None)
            runtime_wait = getattr(task_runtime, "wait", None)
            if task_runtime is None or not callable(runtime_cancel) or not callable(runtime_wait):
                raise RpcUnavailableError(
                    "Task runtime is unavailable; the implementation was not cancelled"
                )
            cancelled_count = await _cancel_task_runtime(
                task_runtime,
                session_key=key,
                task_id=active_task_id,
                source="plans.cancelRun",
                reason="cancelled_by_user",
            )
            try:
                terminal_task = await runtime_wait(active_task_id, timeout=10.0)
            except TimeoutError as exc:
                raise RpcHandlerError(
                    "PLAN_RUN_CANCEL_PENDING",
                    "The implementation is still stopping; retry cancellation.",
                    retryable=True,
                    accepted=False,
                ) from exc
            terminal_status = str(getattr(terminal_task, "status", ""))
            if terminal_status not in {
                AgentTaskStatus.SUCCEEDED.value,
                AgentTaskStatus.FAILED.value,
                AgentTaskStatus.CANCELLED.value,
                AgentTaskStatus.TIMEOUT.value,
                AgentTaskStatus.ABANDONED.value,
            }:
                raise RpcHandlerError(
                    "PLAN_RUN_CANCEL_PENDING",
                    "The implementation task did not acknowledge cancellation.",
                    details={
                        "taskId": active_task_id,
                        "cancelledCount": cancelled_count,
                    },
                    retryable=True,
                    accepted=False,
                )
            cancelled_task_ids.add(active_task_id)
        try:
            updated = await storage.cancel_plan_run(
                run_id,
                expected_state_revision=int(candidate.state_revision),
                reason="cancelled_by_user",
            )
            break
        except PlanRunConflictError as exc:
            latest = await storage.get_plan_run(run_id)
            if latest is not None and latest.status == "cancelled":
                updated = latest
                break
            if latest is None or latest.status not in PLAN_RUN_ACTIVE_STATUSES:
                raise _changed(exc, latest) from exc
            candidate = latest
    if updated is None:
        latest = await storage.get_plan_run(run_id)
        raise _changed(
            PlanRunConflictError("plan run kept changing during cancellation"),
            latest,
        )
    snapshot = plan_run_snapshot(updated)
    await _emit_to_subscribers(
        ctx,
        key,
        "session.event.plan_run",
        {"session_key": key, "plan_run": snapshot},
    )
    return {"sessionKey": key, "planRun": snapshot}


_handle_plans_set_mode_contract = register_plans_set_mode_contract(
    _d,
    _handle_plans_set_mode,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_plans_implement_contract = register_plans_implement_contract(
    _d,
    _handle_plans_implement,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_plans_revise_contract = register_plans_revise_contract(
    _d,
    _handle_plans_revise,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_plans_cancel_run_contract = register_plans_cancel_run_contract(
    _d,
    _handle_plans_cancel_run,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_plans_capabilities_contract = register_plans_capabilities_contract(
    _d,
    _handle_plans_capabilities,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


@_d.method("sessions.bootstrap", scope="operator.read")
async def _handle_sessions_bootstrap(params: dict | None, ctx: RpcContext) -> dict:
    """Return the canonical startup snapshot for an interactive session client.

    This composes existing session, history, task-ledger, epoch, and stream
    services.  It intentionally does not subscribe the connection: clients use
    the returned ``stream_cursor`` with ``sessions.messages.subscribe`` so no
    events are consumed or routed away from other surfaces during bootstrap.
    """

    key = _require_key(params)
    if ctx.session_manager is None:
        raise KeyError("No session manager available")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    session = await _resolve_session_record_for_bootstrap(storage, key)
    session_key = canonicalize_session_key(session.session_key)
    # Capture the cursor before the slower durable reads below.  A client that
    # subscribes from this cursor may see duplicate state (deduped by stable
    # ids), but cannot miss a live event emitted while bootstrap is reading.
    stream_cursor = get_session_streams().current_seq(session_key)
    history_params: dict[str, Any] = {
        "sessionKey": session_key,
        "limit": (params or {}).get("limit", 200),
    }
    for source, target in (
        ("before", "before"),
        ("after", "after"),
        ("includeCanonical", "includeCanonical"),
        ("include_canonical", "includeCanonical"),
        ("includeSummaries", "includeSummaries"),
        ("include_summaries", "includeSummaries"),
    ):
        if isinstance(params, dict) and source in params:
            history_params[target] = params[source]

    history = await read_chat_history_v4(history_params, ctx)
    task_rows = await _list_task_rows(ctx, storage, session_key)
    task_state = _task_state_summary(task_rows)
    await _overlay_runtime_task_snapshot(ctx, session_key, task_state)
    await _attach_active_steer_capability(ctx, session_key, task_state)
    epoch = await _bootstrap_epoch(ctx.session_manager, storage, session, session_key)
    live_queued_ids = task_state.get("queued_task_ids")
    if isinstance(live_queued_ids, list):
        queued_count = len(live_queued_ids)
        active_task = task_state.get("active_task")
        running_count = int(
            isinstance(active_task, dict) and active_task.get("status") == "running"
        )
    else:
        queued_count = sum(
            1 for row in task_rows if _enum_value(getattr(row, "status", None)) == "queued"
        )
        running_count = sum(
            1 for row in task_rows if _enum_value(getattr(row, "status", None)) == "running"
        )
    agent_id = _effective_agent_id_for_session(session, session_key)
    agent_identity = await _bootstrap_agent_identity(ctx, agent_id)
    effective_model = _session_turn_model(ctx, session, agent_id)
    guest_safe = _is_remote_web_guest(ctx.principal, {})
    workspace: str | None = None
    project_snapshot: dict[str, Any] | None = None
    if not guest_safe:
        from opensquilla.agents.scope import resolve_agent_workspace_dir

        workspace_path = resolve_agent_workspace_dir(agent_id, ctx.config)
        default_workspace = str(workspace_path) if workspace_path is not None else None
        project_snapshot = await project_workspace_snapshot(storage, session)
        try:
            bootstrap_run_context, _workspace_guard = await authoritative_project_run_context(
                storage=storage,
                session_manager=ctx.session_manager,
                session=session,
                config=ctx.config,
                default_workspace=default_workspace,
            )
            workspace = bootstrap_run_context.workspace or default_workspace
        except ProjectWorkspaceStateError:
            snapshot_path = project_snapshot.get("path") if project_snapshot is not None else None
            workspace = str(snapshot_path) if isinstance(snapshot_path, str) else default_workspace
    from opensquilla.gateway.model_routing import (
        capture_model_routing_config,
        model_routing_snapshot,
    )

    routing = await _resolve_session_routing_snapshot(ctx, session_key)
    effective_routing_config = capture_model_routing_config(
        ctx.config,
        session_mode=routing["mode"],
        session_routing_revision=routing["revision"],
        session_routing_source=routing["source"],
    )
    overlay_live_config = getattr(effective_routing_config, "overlay_live_config", None)
    effective_runtime_config = (
        overlay_live_config(ctx.config)
        if callable(overlay_live_config)
        else effective_routing_config
    )

    metadata: dict[str, Any] = {
        "session_key": session_key,
        "session_id": session.session_id,
        "status": session.status,
        "agent_id": session.agent_id,
        "model": getattr(session, "model", None),
        "effective_model": effective_model,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "display_name": getattr(session, "display_name", None),
        "queue_mode": getattr(session, "queue_mode", None),
        **_derive_source_metadata(session),
    }
    if not guest_safe:
        metadata.update(
            {
                "workspace": workspace,
                "workspace_id": getattr(session, "workspace_id", None),
                "workspaceId": getattr(session, "workspace_id", None),
                "projectWorkspace": project_snapshot,
            }
        )
    get_current_plan = getattr(storage, "get_current_plan_revision", None)
    get_active_run = getattr(storage, "get_active_plan_run", None)
    current_plan = await get_current_plan(session_key) if callable(get_current_plan) else None
    active_plan_run = await get_active_run(session_key) if callable(get_active_run) else None
    from opensquilla.session.plans import plan_revision_snapshot, plan_run_snapshot

    return {
        "session": metadata,
        "agent_identity": agent_identity,
        "history": history,
        **task_state,
        "queue": {
            "mode": getattr(session, "queue_mode", None) or "followup",
            "queued_count": queued_count,
            "running_count": running_count,
        },
        "runtime": {
            "model_routing": model_routing_snapshot(effective_runtime_config),
        },
        "routing": routing,
        "collaboration": _plan_collaboration_snapshot(session),
        "currentPlan": (
            plan_revision_snapshot(current_plan, current=True) if current_plan is not None else None
        ),
        "activePlanRun": (
            plan_run_snapshot(active_plan_run) if active_plan_run is not None else None
        ),
        "planCapabilities": {
            "planMode": True,
            "implementation": ctx.task_runtime is not None,
            "newTaskImplementation": ctx.task_runtime is not None,
            "goalDriver": True,
        },
        "epoch": epoch,
        "stream_cursor": stream_cursor,
    }


class _GatewayAdmissionPrimitives(GatewayAdmissionRuntime):
    """Compose fixed storage, authority, content and runtime primitives."""

    def __init__(self, ctx: RpcContext) -> None:
        super().__init__(
            config=ctx.config,
            manager=ctx.session_manager,
            runtime=ctx.task_runtime,
            runner=ctx.turn_runner,
            is_owner=ctx.principal.is_owner,
            host_execute_allowed=principal_has_host_execute(ctx.principal),
            publish=partial(_emit_to_subscribers, ctx),
            normalize_terminal=_normalize_terminal_event_payload,
            session_model=partial(_session_turn_model, ctx),
        )
        self._native_sessions = ctx.session_manager
        self.sessions = (
            GatewayAdmissionSessions(ctx.session_manager)
            if ctx.session_manager is not None
            else None
        )
        self.runtime = ctx.task_runtime
        self._native_storage = get_session_storage(ctx.session_manager)
        self.storage = (
            GatewayAdmissionStorage(self._native_storage)
            if self._native_storage is not None
            else None
        )
        self.is_owner = ctx.principal.is_owner
        self.direct_registry = get_agent_task_registry()
        self.uploads = get_upload_store()
        attachments = getattr(ctx.config, "attachments", None)
        self.policy = AdmissionPolicy(
            media_root=media_root_from_config(ctx.config),
            persist_transcripts=bool(getattr(attachments, "persist_transcripts", True)),
            disk_budget_bytes=getattr(attachments, "transcript_disk_budget_bytes", None),
            opaque_max_bytes=getattr(attachments, "opaque_max_bytes", None),
            accept_opaque=bool(getattr(attachments, "accept_opaque", True)),
            project_run_mode=project_default_run_mode(ctx.config),
            default_run_mode=config_run_mode(ctx.config),
        )
        self.session_lock = partial(get_session_lock, ctx.turn_runner)
        self.effective_agent_id = _effective_agent_id_for_session
        self.new_session_key = _create_session_key
        self.collaboration_snapshot = _plan_collaboration_snapshot
        self.routing_snapshot = partial(_resolve_session_routing_snapshot, ctx)
        self._should_auto_title = partial(_should_auto_title, ctx)
        self._fork_session = partial(_fork_with_numbered_title, ctx)
        self._fork_title_allocation = partial(_fork_title_allocation_context, ctx)
        self._next_fork_title = partial(_next_fork_display_name, ctx)
        self.schedule_auto_title = partial(_schedule_auto_title, ctx)
        self.steer_metric = _emit_steer_metric
        self.positive_int = _coerce_positive_int
        self.workspace_error = partial(map_project_workspace_error, owner=self.is_owner)
        self.validate_initial_routing = partial(model_routing_patches, ctx.config)
        self._emit_disposition = partial(
            _publish_admission_disposition,
            ctx,
        )
        self._emit_forked = partial(_publish_admission_forked, ctx)
        self._emit_collaboration = partial(_publish_admission_collaboration, ctx)
        self._principal = ctx.principal
        self._clear_compaction = getattr(ctx.turn_runner, "clear_compacted_this_turn", None)
        self._artifact_binding = partial(
            bind_admission_artifact,
            media_root=self.policy.media_root,
            principal_actor_id=getattr(ctx.principal, "token_public_id", None),
            event_emitter_factory=partial(_artifact_state_event_emitter, ctx),
        )
        self._route_preparation = partial(
            prepare_admission_route,
            config=ctx.config,
            principal=ctx.principal,
            conn_id=ctx.conn_id,
            media_root=self.policy.media_root,
            preview_service=getattr(ctx, "artifact_preview_service", None),
            effective_agent_id=_effective_agent_id_for_session,
            guest_profile_factory=lambda task_id: _guest_profile_for_principal(
                ctx.principal, task_id, state_dir=ctx.config.state_dir
            ),
            event_emitter_factory=partial(_artifact_state_event_emitter, ctx),
            candidate_loop_supported=_desktop_artifact_bridge_supports_candidate_loop,
            source_only_context=_prompt_annotation_source_only_context,
        )
        self._run_mode_hint = partial(_trusted_run_mode_hint, ctx)
        self._elevated_hint = partial(_trusted_elevated_hint, ctx)

    def _require_storage(self, storage: AdmissionStorage) -> None:
        if storage is not self.storage or self._native_storage is None:
            raise ValueError("Admission storage is not bound to this operation")

    async def accepted_response(
        self,
        acceptance: AdmissionAcceptance,
        *,
        client_request_id: str,
        storage: AdmissionStorage,
        turn_context: dict[str, Any] | None = None,
        accepted_prompt_annotation_ids: Sequence[str] = (),
    ) -> AdmitTurnResult:
        self._require_storage(storage)
        if not isinstance(acceptance, TurnAcceptanceResult):
            raise TypeError("Accepted response requires a durable acceptance result")
        return await _accepted_turn_response(
            acceptance,
            client_request_id=client_request_id,
            storage=self._native_storage,
            turn_context=turn_context,
            accepted_prompt_annotation_ids=accepted_prompt_annotation_ids,
        )

    async def should_auto_title(
        self,
        storage: AdmissionStorage,
        session: AdmissionSessionIdentity,
        key: str,
        session_id: str,
    ) -> bool:
        self._require_storage(storage)
        return await self._should_auto_title(self._native_storage, session, key, session_id)

    async def fork_session(
        self,
        storage: AdmissionStorage,
        parent_key: str,
        child_key: str,
        *,
        explicit_title: str | None,
        fork_transcript: bool,
        status: str,
        fork_before_message_id: str,
    ) -> AdmissionSessionIdentity:
        self._require_storage(storage)
        with translate_admission_failure():
            node = await self._fork_session(
                self._native_storage,
                parent_key,
                child_key,
                explicit_title=explicit_title,
                fork_transcript=fork_transcript,
                status=SessionStatus(status),
                fork_before_message_id=fork_before_message_id,
            )
        if not isinstance(node, AdmissionSessionIdentity):
            raise TypeError("Fork did not return a session identity")
        return node

    @contextlib.asynccontextmanager
    async def fork_title_allocation(
        self, storage: AdmissionStorage, parent: AdmissionSessionIdentity
    ):
        self._require_storage(storage)
        with translate_admission_failure():
            async with self._fork_title_allocation(self._native_storage, parent):
                yield

    async def next_fork_title(
        self, storage: AdmissionStorage, parent: AdmissionSessionIdentity
    ) -> str:
        self._require_storage(storage)
        with translate_admission_failure():
            return await self._next_fork_title(self._native_storage, parent)

    def is_remote_guest(self, source: IncomingTurnSource) -> bool:
        return _is_remote_web_guest(self._principal, source_hint_from_turn(source))

    async def bind_artifact(
        self, command: AdmitTurn, *, key: str, session_id: str, session: Any
    ) -> ArtifactBinding:
        if self.storage is None or self._native_storage is None:
            raise KeyError("No session storage available")
        return await self._artifact_binding(
            command,
            key=key,
            session_id=session_id,
            session=session,
            storage=self._native_storage,
            load_followup_focus=partial(_load_followup_annotation_focus, self._native_storage),
        )

    async def prepare_route(
        self,
        command: AdmitTurn,
        *,
        session: Any,
        key: str,
        session_id: str,
        atomic_intent_plan: Any,
        binding: ArtifactBinding,
        workspace_guard: Any,
    ) -> PreparedRuntimeRoute:
        if self.storage is None or self.sessions is None or self._native_storage is None:
            raise KeyError("No session storage available")
        source = source_hint_from_turn(command.source)
        return await self._route_preparation(
            command,
            storage=self._native_storage,
            sessions=self._native_sessions,
            session=session,
            key=key,
            session_id=session_id,
            atomic_intent_plan=atomic_intent_plan,
            binding=binding,
            workspace_guard=workspace_guard,
            run_mode_hint=self._run_mode_hint(source),
            elevated_hint=self._elevated_hint(source),
            guest_safe=self.is_remote_guest(command.source),
            authority_scope=_INGRESS_TURN_AUTHORITY_SCOPE.get(),
        )

    @contextlib.asynccontextmanager
    async def explicit_ingress_intent(self, key: str):
        register = getattr(self.runtime, "explicit_ingress_intent", None)
        if callable(register):
            async with register(key):
                yield
        else:
            yield

    @contextlib.asynccontextmanager
    async def authority_scope(self):
        scope = _IngressTurnAuthorityScope()
        token = _INGRESS_TURN_AUTHORITY_SCOPE.set(scope)
        try:
            yield
        finally:
            _INGRESS_TURN_AUTHORITY_SCOPE.reset(token)
            await scope.close_untransferred()

    async def release_untransferred_authorities(self) -> None:
        scope = _INGRESS_TURN_AUTHORITY_SCOPE.get()
        if scope is not None:
            await scope.close_untransferred()
            scope.authorities.clear()

    def clear_compaction_marker(self, key: str) -> None:
        if callable(self._clear_compaction):
            self._clear_compaction(key)

    @staticmethod
    def turn_authority(envelope: Any) -> Any:
        return envelope.runtime_services.get("turn_authority_cleanup")

    @staticmethod
    def artifact_error(
        kind: str,
        cause: Exception | None = None,
        *,
        retryable: bool,
        operation: str = "turn_acceptance",
        session_key: str | None = None,
    ) -> RpcHandlerError:
        code = ArtifactProductErrorCode(kind.upper())
        if cause is None:
            return artifact_product_error(code, retryable=retryable)
        return logged_artifact_product_error(
            code,
            cause,
            operation=operation,
            retryable=retryable,
            session_key=session_key,
        )

    async def publish_forked(self, key: str) -> None:
        await self._emit_forked(key)

    async def publish_collaboration(self, key: str, collaboration: dict[str, Any]) -> None:
        await self._emit_collaboration(key, collaboration)

    async def publish_disposition(self, key: str, content: dict[str, Any]) -> None:
        await self._emit_disposition(key, content)


async def _publish_admission_forked(ctx: RpcContext, key: str) -> None:
    await _emit_to_subscribers(
        ctx,
        key,
        "sessions.changed",
        build_sessions_changed_payload(key, "forked", run_status="idle"),
    )


async def _publish_admission_collaboration(
    ctx: RpcContext,
    key: str,
    collaboration: dict[str, Any],
) -> None:
    await _emit_to_subscribers(
        ctx,
        key,
        "session.event.collaboration_mode",
        {
            "session_key": key,
            "collaboration": collaboration,
            "appliesTo": "current_turn",
        },
    )


async def _publish_admission_disposition(
    ctx: RpcContext,
    key: str,
    content: dict[str, Any],
) -> None:
    await _emit_to_subscribers(ctx, key, "session.event.input_disposition", content)


def build_turn_admission_application(ctx: RpcContext) -> TurnAdmission:
    return TurnAdmission(
        ingress=DurableTurnAdmission(cast(AdmissionPrimitives, _GatewayAdmissionPrimitives(ctx))),
        cancellation=TurnCancellation(
            _GatewayCancellationPorts(ctx),
            timing=CancellationTiming(
                response_seconds=_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS,
                cleanup_seconds=_ABORT_OWNED_CLEANUP_SECONDS,
                lookup_seconds=_ABORT_SESSION_LOOKUP_SECONDS,
                tree_passes=_ABORT_TREE_STABILIZATION_PASSES,
            ),
            clock=time.monotonic,
        ),
        steering=TurnSteering(
            GatewaySteeringPrimitives(
                session_manager=ctx.session_manager,
                task_runtime=ctx.task_runtime,
                turn_runner=ctx.turn_runner,
                emit_steer=partial(_publish_admission_steer, ctx),
                emit_disposition=partial(_publish_admission_disposition, ctx),
            )
        ),
    )


async def _publish_admission_steer(ctx: RpcContext, key: str, content: dict[str, Any]) -> None:
    await _emit_to_subscribers(ctx, key, "session.event.steer", content)


def build_gateway_turn_admission_adapter(ctx: RpcContext) -> GatewayTurnAdmissionAdapter:
    """Bind aliases to the same application, preserving request authority."""
    return GatewayTurnAdmissionAdapter(
        build_turn_admission_application(ctx),
        principal_role=str(ctx.principal.role),
        connection_id=ctx.conn_id,
        is_owner=ctx.principal.is_owner,
    )


def _session_turn_admission_adapter(ctx: RpcContext) -> GatewayTurnAdmissionAdapter:
    return build_gateway_turn_admission_adapter(ctx)


async def _handle_sessions_send_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _session_turn_admission_adapter(ctx).admit(params, surface="session")


async def _handle_sessions_abort_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _session_turn_admission_adapter(ctx).cancel(params, surface="session")


async def _handle_sessions_steer_v2_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _session_turn_admission_adapter(ctx).steer(params, durable=True)


async def _handle_sessions_steer_contract(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _session_turn_admission_adapter(ctx).steer(params, durable=False)


_handle_sessions_send_generated_contract = register_turn_admission_contract(
    _d,
    "sessions.send",
    _handle_sessions_send_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_abort_generated_contract = register_turn_admission_contract(
    _d,
    "sessions.abort",
    _handle_sessions_abort_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_steer_v2_generated_contract = register_turn_admission_contract(
    _d,
    "sessions.steer.v2",
    _handle_sessions_steer_v2_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_steer_generated_contract = register_turn_admission_contract(
    _d,
    "sessions.steer",
    _handle_sessions_steer_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


class _GatewayPendingInputQueuePort(GatewayPendingInputPrimitives, PendingInputQueuePort):
    """Concrete queue Port backed by the single durable SessionStorage path."""

    def __init__(self, context: RpcContext) -> None:
        self._context = context

    @property
    def storage(self) -> SessionStorage:
        return _pending_input_storage(self._context)

    @property
    def config(self) -> object:
        return self._context.config

    def owner_lock(self, pending_input_id: str) -> asyncio.Lock:
        return _pending_input_lock_for(pending_input_id)

    def session_lock(self, key: str):
        return get_session_lock(self._context.turn_runner, key) or contextlib.nullcontext()

    async def replay_dispatch(
        self,
        source_scope: str,
        key: str,
        request_id: str,
    ) -> PendingDispatchReplay | None:
        storage = self.storage
        replay = await storage.replay_turn_ingress_receipt(
            source_scope=source_scope,
            request_session_key=key,
            client_request_id=request_id,
        )
        if replay is None:
            return None
        result = await _accepted_turn_response(
            replay, client_request_id=request_id, storage=storage
        )
        return PendingDispatchReplay(
            replay.receipt.request_fingerprint,
            replay.receipt.session_id,
            cast(AdmitTurnResult, result),
        )

    async def list_items(self, key: str) -> list[PendingInputProjection]:
        rows = await _pending_input_storage(self._context).list_pending_chat_inputs(key)
        return [cast(PendingInputProjection, pending_input_projection(row)) for row in rows]

    async def reposition(
        self,
        key: str,
        pending_input_id: str,
        revision: int,
        position: int,
    ) -> PendingInputProjection:
        try:
            row = await _pending_input_storage(self._context).update_pending_chat_input(
                pending_input_id,
                session_key=key,
                expected_revision=revision,
                position=position,
            )
        except PendingChatInputNotFoundError as exc:
            raise PendingInputMissingError from exc
        except PendingChatInputConflictError as exc:
            raise PendingInputConflictError from exc
        return cast(PendingInputProjection, pending_input_projection(row))

    async def reorder_durable(
        self,
        key: str,
        revisions: tuple[PendingInputRevision, ...],
    ) -> list[PendingInputProjection]:
        try:
            rows = await _pending_input_storage(self._context).reorder_pending_chat_inputs(
                session_key=key,
                expected_revisions=[
                    (item.pending_input_id, item.expected_revision) for item in revisions
                ],
            )
        except PendingChatInputConflictError as exc:
            raise PendingInputConflictError from exc
        return [cast(PendingInputProjection, pending_input_projection(row)) for row in rows]

    @asynccontextmanager
    async def cancellation_lock(self, pending_input_id: str) -> AsyncIterator[None]:
        try:
            async with _pending_input_lock_for(pending_input_id):
                yield
        except PendingChatInputConflictError as exc:
            raise PendingCancellationConflictError from exc

    async def cancellation_material_scopes(self, key: str, pending_input_id: str) -> set[str]:
        storage = _pending_input_storage(self._context)
        row = await storage.get_pending_chat_input(pending_input_id)
        scopes = _pending_input_attachment_scopes(row)
        current = await _pending_input_current_session_id(storage, key)
        if current is not None:
            # Recover an owner materialized before an interrupted queue insert.
            scopes.add(current)
        return scopes

    async def cancel_durable(self, key: str, pending_input_id: str, revision: int | None) -> bool:
        return await _pending_input_storage(self._context).cancel_pending_chat_input(
            pending_input_id,
            session_key=key,
            expected_revision=revision,
        )

    async def cleanup_promotions(self, key: str, pending_input_id: str, scopes: set[str]) -> None:
        await _cleanup_unreferenced_pending_promotions(
            ctx=self._context,
            storage=_pending_input_storage(self._context),
            session_key=key,
            pending_input_id=pending_input_id,
            source_session_ids=scopes,
        )

    def cleanup_material(self, pending_input_id: str, scopes: set[str]) -> None:
        _cleanup_pending_input_scopes(
            ctx=self._context,
            pending_input_id=pending_input_id,
            session_ids=scopes,
        )


def _pending_input_queue_adapter(ctx: RpcContext) -> GatewayPendingInputQueueAdapter:
    return GatewayPendingInputQueueAdapter(
        _GatewayPendingInputQueuePort(ctx),
        turns=build_turn_admission_application(ctx),
        principal_role=str(getattr(ctx.principal, "role", "operator") or "operator"),
        is_owner=ctx.principal.is_owner,
    )


async def _handle_pending_inputs_enqueue_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).enqueue(params)


async def _handle_pending_inputs_list_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).list(params)


async def _handle_pending_inputs_update_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).update(params)


async def _handle_pending_inputs_reorder_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).reorder(params)


async def _handle_pending_inputs_cancel_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).cancel(params)


async def _handle_pending_inputs_dispatch_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).dispatch(params)


async def _handle_pending_inputs_steer_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _pending_input_queue_adapter(ctx).steer(params)


for _pending_method, _pending_implementation in (
    ("sessions.pending_inputs.enqueue", _handle_pending_inputs_enqueue_contract),
    ("sessions.pending_inputs.list", _handle_pending_inputs_list_contract),
    ("sessions.pending_inputs.update", _handle_pending_inputs_update_contract),
    ("sessions.pending_inputs.reorder", _handle_pending_inputs_reorder_contract),
    ("sessions.pending_inputs.cancel", _handle_pending_inputs_cancel_contract),
    ("sessions.pending_inputs.dispatch", _handle_pending_inputs_dispatch_contract),
    ("sessions.pending_inputs.steer", _handle_pending_inputs_steer_contract),
):
    register_pending_input_queue_contract(
        _d,
        _pending_method,
        _pending_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )


_SESSION_CONTROL_CONTRACT_IMPLEMENTATIONS = {
    "sessions.subscribe": _handle_sessions_subscribe,
    "sessions.unsubscribe": _handle_sessions_unsubscribe,
    "sessions.routing.get": _handle_sessions_routing_get,
    "sessions.routing.set": _handle_sessions_routing_set,
}

_SESSION_CONTROL_CONTRACT_HANDLERS = {
    method: register_session_control_contract(
        _d,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _SESSION_CONTROL_CONTRACT_IMPLEMENTATIONS.items()
}
