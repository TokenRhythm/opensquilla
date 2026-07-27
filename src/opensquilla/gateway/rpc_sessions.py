"""RPC handlers for the sessions domain."""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.agents.scope import default_workspace_dir, resolve_agent_workspace_dir
from opensquilla.artifacts import enrich_artifact_event_dict
from opensquilla.engine.cache_break_monitor import notify_compaction
from opensquilla.engine.start_turn import reserve_turn_via_runtime, start_turn_via_runtime
from opensquilla.gateway import attachment_ingest as _attachment_ingest
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.config import effective_agent_stream_idle_timeout_seconds
from opensquilla.gateway.input_normalization import (
    infer_normalized_input_from_attachments,
    materialize_generated_text_attachments,
    normalize_incoming_text,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_services import (
    get_session_epoch,
    get_session_lock,
    get_session_storage,
    set_session_epoch,
)
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.session_view import build_session_view_item, derive_transcript_title
from opensquilla.gateway.turn_ingress import (
    accepted_turn_payload,
    complete_durable_ingress,
    request_identity,
)
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.paths import media_root_from_config
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
)
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    get_run_context,
    run_context_from_origin_payload,
)
from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    run_mode_allowed_for_principal,
)
from opensquilla.session.compaction import (
    build_compaction_config_from_provider,
    call_compact_with_optional_config,
)
from opensquilla.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_PERSISTED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    compaction_memory_status,
    compaction_result_payload,
    durable_receipt_allows_destructive_compaction,
    flush_receipt_is_successful_flush,
    flush_receipt_status_for_compaction,
    flush_receipt_to_dict,
    flush_trigger_enabled,
    new_compaction_id,
    pre_compaction_flush_requires_safe_receipt,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id, parse_agent_id
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionStatus
from opensquilla.session.naming import (
    generate_session_title,
    is_naming_eligible,
    title_slot_is_empty,
)
from opensquilla.session.storage import (
    SessionStorage,
    StaleEpochError,
    StorageBusyError,
    TaskCollectionUnavailableError,
    TurnAcceptanceResult,
    TurnIngressConflictError,
)
from opensquilla.session.terminal_reply import (
    append_error_ref,
    build_terminal_reply,
    sanitize_agent_error,
)

_d = get_dispatcher()
log = structlog.get_logger(__name__)
_ELEVATED_MODES = frozenset({"full"})
_TRUSTED_ELEVATED_ALIASES = frozenset({"on", "bypass"})

if TYPE_CHECKING:
    from opensquilla.gateway.task_runtime import TaskRuntime

_ALLOWED_MEDIA_TYPES = _attachment_ingest.ALLOWED_MEDIA_TYPES
_MAX_ATTACHMENT_BYTES = _attachment_ingest.MAX_ATTACHMENT_BYTES
_MAX_STAGED_PDF_BYTES = _attachment_ingest.MAX_STAGED_PDF_BYTES
_MAX_TEXT_ATTACHMENT_BYTES = _attachment_ingest.TEXT_ATTACHMENT_BYTES
_MAX_TOTAL_ATTACHMENT_BYTES = _attachment_ingest.MAX_TOTAL_ATTACHMENT_BYTES
_MAX_ATTACHMENTS = _attachment_ingest.MAX_ATTACHMENTS


def _accepts_keyword_arg(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _build_session_flush_correlation(
    ctx: RpcContext,
    session_id: object,
) -> tuple[str, ProviderRequestCorrelation | None]:
    """Create one root operation and execution for a session-bound maintenance flush."""

    turn_id = uuid.uuid4().hex
    if (
        not isinstance(session_id, str)
        or not session_id
        or provider_request_correlation_disabled(config=ctx.config)
    ):
        return turn_id, None
    return (
        turn_id,
        ProviderRequestCorrelation(
            session_id=session_id,
            turn_id=turn_id,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.session_flush",
        ),
    )


async def _branch_with_session_mutation_lock(
    session_manager: Any,
    turn_runner: Any,
    parent_key: str,
    child_key: str,
    **kwargs: Any,
) -> Any:
    """Fork against the same parent write lock used by turns and compaction."""
    branch = session_manager.branch
    lock = get_session_lock(turn_runner, parent_key)
    if lock is None:
        return await branch(parent_key, child_key, **kwargs)
    if _accepts_keyword_arg(branch, "mutation_context"):
        return await branch(
            parent_key,
            child_key,
            mutation_context=lambda: lock,
            **kwargs,
        )
    # Preserve compatibility with older manager-like implementations that do
    # not yet expose the mutation-context seam.
    async with lock:
        return await branch(parent_key, child_key, **kwargs)


def _clean_cancel_source(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", ".", ":"} else "_" for ch in text)
    return (safe.strip("_") or default)[:80]


def _cancel_source_from_params(params: dict | None, default: str) -> str:
    return _clean_cancel_source((params or {}).get("source"), default)


async def _cancel_task_runtime(
    task_runtime: Any,
    *,
    session_key: str,
    task_id: str | None = None,
    source: str,
    reason: str,
) -> int:
    cancel = getattr(task_runtime, "cancel")
    kwargs: dict[str, Any] = {}
    if task_id and _accepts_keyword_arg(cancel, "task_id"):
        kwargs["task_id"] = task_id
    else:
        kwargs["session_key"] = session_key
    if _accepts_keyword_arg(cancel, "source"):
        kwargs["source"] = source
    if _accepts_keyword_arg(cancel, "reason"):
        kwargs["reason"] = reason
    return int(await cancel(**kwargs))


async def _durable_receipt_allows_covered_destructive_compaction(
    storage: Any,
    session_key: str,
    session_id: str | None,
    entries: list[Any],
) -> bool:
    if not entries:
        return True
    from opensquilla.memory.checkpoint import (
        checkpoint_coverage_hash,
        checkpoint_turn_id,
    )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return False
    receipts = await list_receipts(
        session_key=session_key,
        session_id=session_id,
        scope="checkpoint",
        status="checkpoint_saved",
        coverage_turn_id=checkpoint_turn_id(entries),
        coverage_hash=checkpoint_coverage_hash(entries),
        coverage_entry_count=len(entries),
        limit=1,
    )
    return any(durable_receipt_allows_destructive_compaction(receipt) for receipt in receipts)


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


_attachment_media_type = _attachment_ingest.attachment_media_type
_normalize_attachments = _attachment_ingest.normalize_attachments
_sniff_mime_from_bytes = _attachment_ingest.sniff_mime_from_bytes


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
        if run_mode_allowed_for_principal(run_mode, ctx.principal):
            return run_mode
        if run_mode == RunMode.FULL and not ctx.principal.is_owner:
            return RunMode.TRUSTED
        return None

    elevated = source_hint.get("elevated")
    if not isinstance(elevated, str):
        return None
    if not ctx.principal.is_owner:
        return None
    if elevated in _TRUSTED_ELEVATED_ALIASES:
        return RunMode.TRUSTED
    if elevated == "full":
        return RunMode.FULL
    return None


def _apply_run_context_route_metadata(
    route_envelope: Any,
    run_context: Any,
    *,
    principal_is_owner: bool,
) -> None:
    run_context_payload = run_context.to_origin_payload()
    filtered_run_context = run_context_from_origin_payload(
        run_context_payload,
        source="route_metadata",
        preserve_materialized_user_grants=True,
    )
    route_envelope.metadata["run_mode"] = run_context.run_mode.value
    # Preserve whether this is the default or an explicit per-session choice
    # for route consumers that need to display or audit its origin.
    route_envelope.metadata["run_mode_explicit"] = run_context.source != "default"
    route_envelope.metadata["sandbox_mounts"] = (
        filtered_run_context.to_origin_payload()["mounts"]
        if filtered_run_context is not None
        else []
    )
    route_envelope.metadata["sandbox_run_context"] = run_context_payload
    object.__setattr__(route_envelope, "sandbox_run_context_fresh", True)
    if run_context.run_mode.value == "full" and principal_is_owner:
        route_envelope.metadata["elevated"] = "full"


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


def _normalize_session_send_source_hint(params: dict[str, Any]) -> dict[str, Any]:
    raw_hint = params.get("_source")
    source_hint = dict(raw_hint) if isinstance(raw_hint, dict) else {}
    caller_kind = (
        str(source_hint.get("caller_kind") or source_hint.get("callerKind") or "").strip().lower()
    )
    channel_kind = (
        str(source_hint.get("channel_kind") or source_hint.get("channelKind") or "").strip().lower()
    )
    if caller_kind:
        source_hint.setdefault("caller_kind", caller_kind)
    if channel_kind:
        source_hint.setdefault("channel_kind", channel_kind)
    if caller_kind == "cli" or channel_kind == "cli":
        return source_hint
    source_hint.setdefault("caller_kind", "web")
    source_hint.setdefault("channel_kind", "web")
    return source_hint


_STREAM_IDLE_TIMEOUT_CODE = "stream_idle_timeout"
_STREAM_IDLE_TIMEOUT_MESSAGE = "Session event stream idle before terminal event"
_RESET_RUNTIME_SETTLE_SECONDS = 0.25
_RESET_RUNTIME_CANCEL_DRAIN_SECONDS = 2.0
_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS = 2.0
_ABORT_TREE_STABILIZATION_PASSES = 8
_ACTIVE_TASK_STATUSES = frozenset({"queued", "running"})


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
) -> None:
    if not task_ids or not hasattr(task_runtime, "wait"):
        return
    for task_id in task_ids:
        try:
            await asyncio.wait_for(
                task_runtime.wait(task_id),
                timeout=_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "sessions.abort.task_runtime_drain_timeout",
                session_key=session_key,
                task_id=task_id,
            )
        except Exception:
            log.warning(
                "sessions.abort.task_runtime_drain_failed",
                session_key=session_key,
                task_id=task_id,
            )


async def _drain_task_runtime_for_reset(task_runtime: Any, session_key: str) -> None:
    """Cancel live runtime work without racing a just-finished turn.

    The task runtime emits ``session.event.done`` from inside the turn handler,
    then marks the runtime task terminal immediately after the handler returns.
    A client that calls reset on the done event can arrive during that narrow
    post-done/pre-terminal window. Give running tasks a short chance to settle
    before issuing cancellation so reset does not append a false
    ``[interrupted]`` marker into the transcript being flushed.
    """
    has_runtime_listing = hasattr(task_runtime, "list") and hasattr(task_runtime, "wait")

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
            log.warning("sessions.reset.task_runtime_settle_failed", session_key=session_key)

    await _cancel_task_runtime(
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
            if _task_status_value(getattr(row, "status", None)) in _ACTIVE_TASK_STATUSES:
                await asyncio.wait_for(
                    task_runtime.wait(row.task_id),
                    timeout=_RESET_RUNTIME_CANCEL_DRAIN_SECONDS,
                )
    except TimeoutError:
        log.warning("sessions.reset.task_runtime_drain_timeout", session_key=session_key)
    except Exception:
        log.warning("sessions.reset.task_runtime_drain_failed", session_key=session_key)


def _optional_positive_timeout(config: Any, attr: str, default: float) -> float | None:
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


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


def _buffer_session_event(
    session_key: str,
    event_name: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if event_name.startswith("session.event."):
        return get_session_streams().record(session_key, event_name, payload)
    return dict(payload or {})


async def _resolve_attachments(
    validated: list[dict[str, Any]],
    store: Any | None = None,
    *,
    material_root: Any | None = None,
    session_id: str | None = None,
    disk_budget_bytes: int | None = None,
) -> list[dict[str, Any]]:
    resolved, _consumed = await _attachment_ingest.resolve_attachments(
        validated,
        store=store,
        material_root=material_root,
        session_id=session_id,
        disk_budget_bytes=disk_budget_bytes,
    )
    return resolved


def _validate_attachments(raw_attachments: Any) -> list[dict[str, Any]]:
    validated, _failures = _attachment_ingest.validate_attachments(
        raw_attachments,
        logger=log,
    )
    return validated


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _first_dict_value(*values: Any) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return None


def _normalize_memory_capture_controls(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize RPC/chat memory-capture controls onto snake_case fields."""

    source_hint = params.get("_source")
    if not isinstance(source_hint, dict):
        source_hint = {}

    no_memory_capture = _coerce_optional_bool(
        params.get("no_memory_capture", params.get("noMemoryCapture"))
    )
    if no_memory_capture is None:
        no_memory_capture = _coerce_optional_bool(
            source_hint.get("no_memory_capture", source_hint.get("noMemoryCapture"))
        )

    input_provenance = _first_dict_value(
        params.get("input_provenance"),
        params.get("inputProvenance"),
        source_hint.get("input_provenance"),
        source_hint.get("inputProvenance"),
    )
    provenance_kind = (
        params.get("input_provenance_kind")
        or params.get("inputProvenanceKind")
        or params.get("provenance_kind")
        or source_hint.get("input_provenance_kind")
        or source_hint.get("inputProvenanceKind")
        or source_hint.get("provenance_kind")
    )
    if input_provenance is None and provenance_kind:
        input_provenance = {"kind": str(provenance_kind)}
    elif input_provenance is not None and "kind" not in input_provenance and provenance_kind:
        input_provenance["kind"] = str(provenance_kind)

    run_kind = params.get("run_kind", params.get("runKind"))
    if run_kind is None:
        run_kind = source_hint.get("run_kind", source_hint.get("runKind"))

    return {
        "no_memory_capture": bool(no_memory_capture),
        "input_provenance": input_provenance,
        "run_kind": str(run_kind) if run_kind is not None and str(run_kind) else None,
    }


def _require_key(params: dict | None) -> str:
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    key = params["key"]
    if not isinstance(key, str):
        raise ValueError("params.key must be a string")
    return canonicalize_session_key(key)


def _optional_string_param(params: dict | None, *names: str) -> str | None:
    if not isinstance(params, dict):
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
    workspace = (
        context_payload.get("workspace") if isinstance(context_payload, dict) else None
    )
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


def _context_window_tokens(params: dict | None, ctx: RpcContext) -> int:
    raw: Any = None
    if isinstance(params, dict):
        raw = params.get("contextWindowTokens", params.get("context_window_tokens"))
    if raw is None:
        raw = getattr(ctx.config, "context_budget_tokens", 100_000)
    if isinstance(raw, bool):
        raise ValueError("contextWindowTokens must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("contextWindowTokens must be a positive integer") from exc
    if value <= 0:
        raise ValueError("contextWindowTokens must be a positive integer")
    return value


def _effective_compaction_model(session: Any | None) -> str | None:
    if session is None:
        return None
    return getattr(session, "model_override", None) or getattr(session, "model", None)


def _resolve_compaction_provider(ctx: RpcContext, session: Any | None) -> Any | None:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return None

    resolved_selector = selector
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            resolved_selector = clone()
        except Exception:  # noqa: BLE001
            resolved_selector = selector

    model = _effective_compaction_model(session)
    if model and resolved_selector is not selector:
        override = getattr(resolved_selector, "override_model", None)
        if callable(override):
            try:
                override(model)
            except Exception:  # noqa: BLE001
                pass

    resolver = getattr(resolved_selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except Exception:  # noqa: BLE001
        return None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _model_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    summary = {
        "task_id": getattr(row, "task_id", None),
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

    message = payload.get("message")
    error_message = payload.get("error_message")
    raw_message = error_message if isinstance(error_message, str) and error_message else message
    raw_text = raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
    code = payload.get("code")
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
    )
    return {
        **payload,
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
    return _task_summary(_sorted_task_rows(active)[0])


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


async def _list_task_rows(ctx: RpcContext, storage: Any | None, session_key: str) -> list[Any]:
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


def _is_ephemeral_webchat_session_key(key: str) -> bool:
    parts = key.split(":")
    return len(parts) == 4 and parts[0] == "agent" and parts[2] == "webchat" and bool(parts[3])


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


async def _resolve_session_node(storage: Any, key: str) -> Any:
    session = await storage.get_session(key)
    if session is not None:
        return session

    sessions = await storage.list_sessions(limit=500)
    matches: list[Any] = []
    for candidate in sessions:
        values = [
            getattr(candidate, "session_key", ""),
            getattr(candidate, "session_id", ""),
            getattr(candidate, "display_name", "") or "",
            getattr(candidate, "derived_title", "") or "",
        ]
        if any(str(value) == key or str(value).startswith(key) for value in values if value):
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(str(getattr(match, "session_key", "")) for match in matches[:5])
        raise ValueError(f"Ambiguous session id {key!r}; matches: {candidates}")
    raise KeyError(f"Session not found: {key}")


@_d.method("sessions.list", scope="operator.read")
async def _handle_sessions_list(params: dict | None, ctx: RpcContext) -> dict:
    """List all sessions."""
    now_ms = int(time.time() * 1000)

    if ctx.session_manager is None:
        return {"sessions": [], "count": 0, "ts": now_ms}

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return {"sessions": [], "count": 0, "ts": now_ms}

    limit = (params or {}).get("limit", 50)
    sessions = await storage.list_sessions(limit=limit)
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

    return {"sessions": result, "count": len(result), "ts": now_ms}


async def _titles_for_keys(
    storage: Any,
    keys: list[str],
    now_ms: int,
    channel_types: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve canonical session_key -> sidebar title for a small set of keys.

    Labels transcript (content) hits without rebuilding the whole session list.
    Bounded by the search limit, so this is a handful of point lookups.
    """
    unique = list(dict.fromkeys(canonicalize_session_key(k) for k in keys if k))
    sessions: list[Any] = []
    for key in unique:
        try:
            node = await storage.get_session(key)
        except Exception:
            node = None
        if node is not None:
            sessions.append(node)
    if not sessions:
        return {}
    transcript_titles = await _list_transcript_titles(storage, sessions)
    out: dict[str, str] = {}
    for node in sessions:
        view = build_session_view_item(
            node,
            entry_count=0,
            task_rows=[],
            now_ms=now_ms,
            transcript_title=transcript_titles.get(getattr(node, "session_id", ""), ""),
            channel_types=channel_types,
        )
        out[canonicalize_session_key(node.session_key)] = str(view.get("title") or "")
    return out


@_d.method("sessions.search", scope="operator.read")
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
    query = ""
    limit = 20
    if isinstance(params, dict):
        query = str(params.get("query") or "").strip()
        try:
            limit = int(params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
    limit = max(1, min(limit, 50))

    empty = {"sessions": [], "messages": [], "query": query, "ts": now_ms}
    if not query or ctx.session_manager is None:
        return empty
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return empty

    # Title hits.
    # Prefer the dedicated global query (matches every session, builds view rows
    # only for the matches). Fall back to a bounded recent scan for storage
    # doubles that don't implement it.
    title_search = getattr(storage, "search_sessions_by_title", None)
    if callable(title_search):
        title_sessions = await title_search(query, limit)
    else:
        needle = query.lower()
        recent = await storage.list_sessions(limit=200)
        title_sessions = [
            s
            for s in recent
            if needle
            in " ".join(
                p
                for p in (
                    str(getattr(s, "display_name", "") or ""),
                    str(getattr(s, "derived_title", "") or ""),
                    str(getattr(s, "subject", "") or ""),
                )
                if p
            ).lower()
        ][:limit]

    transcript_titles = await _list_transcript_titles(storage, title_sessions)
    session_hits: list[dict[str, Any]] = []
    title_keys: set[str] = set()
    channel_types = _channel_types_from_config(getattr(ctx, "config", None))
    for s in title_sessions:
        view = build_session_view_item(
            s,
            entry_count=0,
            task_rows=[],
            now_ms=now_ms,
            transcript_title=transcript_titles.get(getattr(s, "session_id", ""), ""),
            channel_types=channel_types,
        )
        title_keys.add(canonicalize_session_key(s.session_key))
        session_hits.append(
            {
                "key": s.session_key,
                "title": str(view.get("title") or ""),
                "effectiveAgentId": view.get("effectiveAgentId"),
                "surface": view.get("surface"),
                "updatedAt": view.get("updatedAt"),
            }
        )

    # Content hits.
    # ASCII queries use the ranked, indexed FTS path. Non-ASCII queries (CJK and
    # other scripts the FTS tokenizer can't segment) use a substring LIKE scan,
    # the only option for them. ASCII deliberately has NO LIKE fallback so a
    # common keystroke can never trigger an unbounded full-table content scan.
    has_like = hasattr(storage, "search_transcript_like")
    non_ascii = any(ord(ch) > 127 for ch in query)
    rows: list[dict[str, Any]] = []
    try:
        if non_ascii:
            if has_like:
                rows = await storage.search_transcript_like(query, limit=limit)
        else:
            rows = await storage.search_transcript(query, limit=limit)
    except Exception:
        log.warning("sessions.search.transcript_failed", exc_info=True)
        rows = []

    # One row per session, never repeating a session already shown as a title
    # hit, enriched with the session title via a small bounded lookup.
    pending: list[tuple[str, str, dict[str, Any]]] = []
    content_keys: set[str] = set()
    for row in rows:
        raw_key = str(row.get("session_key") or "")
        canon = canonicalize_session_key(raw_key)
        if not canon or canon in title_keys or canon in content_keys:
            continue
        content_keys.add(canon)
        pending.append((raw_key, canon, row))

    title_map = await _titles_for_keys(
        storage,
        [canon for _, canon, _ in pending],
        now_ms,
        channel_types=channel_types,
    )
    message_hits: list[dict[str, Any]] = []
    for raw_key, canon, row in pending:
        message_hits.append(
            {
                "key": raw_key,
                "title": title_map.get(canon, ""),
                "role": row.get("role"),
                "snippet": row.get("snippet") or "",
                "createdAt": row.get("created_at"),
            }
        )

    return {"sessions": session_hits, "messages": message_hits, "query": query, "ts": now_ms}


@_d.method("sessions.create", scope="operator.write")
async def _handle_sessions_create(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        params = {}
    agent_id = normalize_agent_id(params.get("agentId", "main"))
    display_name = params.get("displayName")
    message = params.get("message")
    model = _model_value(params.get("model")) or _agent_registry_model(ctx, agent_id)
    kind = params.get("kind") or params.get("sessionKind")
    if message is not None and not isinstance(message, str):
        raise ValueError("params.message must be a string")

    if not await _agent_registry_has(ctx, agent_id):
        raise RpcHandlerError(
            "agent.not_found",
            f"Agent '{agent_id}' does not exist",
            details={"agentId": agent_id},
        )

    if ctx.session_manager is None:
        if message:
            raise RpcUnavailableError("sessions.create(message=...) requires a session manager")
        key = _create_session_key(agent_id, kind)
        return {
            "key": key,
            "sessionId": key.rsplit(":", 1)[-1],
            "note": "session manager not available",
        }

    session = await ctx.session_manager.create(
        session_key=_create_session_key(agent_id, kind),
        agent_id=agent_id,
        display_name=display_name,
        model=model,
    )
    result = {"key": session.session_key, "sessionId": session.session_id}

    if message:
        _persisted = await ctx.session_manager.append_message(
            session.session_key,
            role="user",
            content=message,
        )
        if _persisted is not None and isinstance(_persisted.content, str):
            message = _persisted.content
        result["seededMessage"] = True

    return result


@_d.method("sessions.fork", scope="operator.write")
async def _handle_sessions_fork(params: dict | None, ctx: RpcContext) -> dict:
    """Fork a session into a new webchat-routable child with a copied transcript."""
    key = _require_key(params)
    assert isinstance(params, dict)
    title = params.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("params.title must be a string")
    before_message_id = _optional_string_param(
        params,
        "beforeMessageId",
        "before_message_id",
    )

    if ctx.session_manager is None:
        raise KeyError("No session manager available")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    parent = await storage.get_session(key)
    if parent is None:
        raise KeyError(f"Session not found: {key}")

    agent_id = _effective_agent_id_for_session(parent, key)
    child_key = _create_session_key(agent_id, "webchat")
    child = await _branch_with_session_mutation_lock(
        ctx.session_manager,
        ctx.turn_runner,
        key,
        child_key,
        fork_transcript=True,
        status=SessionStatus.DONE,
        fork_before_message_id=before_message_id,
    )

    display_name = title or getattr(parent, "display_name", None)
    if display_name:
        await ctx.session_manager.update(child.session_key, display_name=display_name)

    await _emit_to_subscribers(
        ctx,
        child.session_key,
        "sessions.changed",
        build_sessions_changed_payload(child.session_key, "forked", run_status="idle"),
    )

    return {"key": child.session_key, "parentKey": key}


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


async def _accepted_turn_response(
    result: TurnAcceptanceResult,
    *,
    client_request_id: str,
    storage: SessionStorage,
    turn_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = accepted_turn_payload(result, client_request_id=client_request_id)
    receipt = result.receipt
    payload["session_key"] = receipt.accepted_session_key
    payload["user_message_id"] = receipt.message_id
    if receipt.task_id is not None:
        payload["turn_id"] = receipt.task_id

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

    if isinstance(turn_context, dict):
        _apply_identity_context(turn_context)

    if receipt.task_id is None:
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


@_d.method("sessions.send", scope="operator.write")
async def _handle_sessions_send(
    params: dict | None,
    ctx: RpcContext,
    *,
    fingerprint_params: dict[str, Any] | None = None,
) -> dict:
    key = _require_key(params)
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")

    message_text: str = params["message"]
    source_hint = _normalize_session_send_source_hint(params)
    requested_client_message_id = _optional_string_param(
        params, "client_message_id", "clientMessageId"
    ) or _optional_string_param(source_hint, "client_message_id", "clientMessageId")
    requested_surface_id = _optional_string_param(
        params, "surface_id", "surfaceId"
    ) or _optional_string_param(source_hint, "surface_id", "surfaceId")
    incoming_attachments = params.get("attachments", [])
    normalized_input = normalize_incoming_text(
        message_text,
        source_hint=source_hint,
        attachments=incoming_attachments if isinstance(incoming_attachments, list) else [],
    )
    message_text = normalized_input.message_text
    semantic_message_text = normalized_input.semantic_message
    combined_attachments = [
        *normalized_input.generated_attachments,
        *(incoming_attachments if isinstance(incoming_attachments, list) else []),
    ]
    attachments_cfg = getattr(ctx.config, "attachments", None)
    persist_enabled = bool(getattr(attachments_cfg, "persist_transcripts", True))
    media_root = media_root_from_config(ctx.config)
    from opensquilla.session.models import SessionIntent

    try:
        session_intent = SessionIntent(params.get("intent", SessionIntent.CONTINUE.value))
    except ValueError as exc:
        raise ValueError(f"Invalid session intent: {params.get('intent')}") from exc
    fork_before_message_id = _optional_string_param(
        params,
        "forkBeforeMessageId",
        "fork_before_message_id",
    )
    if fork_before_message_id is not None and session_intent is not SessionIntent.CONTINUE:
        raise ValueError("forkBeforeMessageId cannot be combined with non-continue intent")

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage_candidate = get_session_storage(ctx.session_manager)
    if storage_candidate is None:
        raise KeyError("No session storage available")
    storage = cast(SessionStorage, storage_candidate)

    ingress_identity = request_identity(
        params,
        request_session_key=key,
        source_scope=_turn_source_scope(source_hint, ctx),
        fingerprint_params=fingerprint_params,
    )
    get_ingress_receipt = getattr(storage, "get_turn_ingress_receipt", None)
    if callable(get_ingress_receipt):
        previous_acceptance = await get_ingress_receipt(
            source_scope=ingress_identity.source_scope,
            request_session_key=ingress_identity.request_session_key,
            client_request_id=ingress_identity.client_request_id,
        )
        if previous_acceptance is not None:
            if (
                previous_acceptance.receipt.request_fingerprint
                != ingress_identity.request_fingerprint
            ):
                raise RpcHandlerError(
                    "IDEMPOTENCY_CONFLICT",
                    "clientRequestId was already used for a different turn",
                    retryable=False,
                    accepted=False,
                )
            return await _accepted_turn_response(
                previous_acceptance,
                client_request_id=ingress_identity.client_request_id,
                storage=storage,
            )

    task_runtime_candidate = cast("TaskRuntime | None", getattr(ctx, "task_runtime", None))
    prepare_intent = getattr(ctx.session_manager, "prepare_intent", None)
    create_kwargs: dict[str, Any] = {}
    if source_hint.get("caller_kind") == "web":
        create_kwargs["display_name"] = "WebChat"
    supports_atomic_intent = (
        fork_before_message_id is None
        and task_runtime_candidate is not None
        and callable(prepare_intent)
        and callable(getattr(storage, "accept_turn", None))
        and callable(getattr(task_runtime_candidate, "reserve", None))
        and callable(getattr(task_runtime_candidate, "activate", None))
        and callable(getattr(task_runtime_candidate, "abort_reservation", None))
    )

    async def _prepare_or_apply_intent() -> tuple[Any, Any | None]:
        existing_session = await storage.get_session(key)
        if existing_session is None and session_intent is SessionIntent.CONTINUE:
            raise KeyError(f"Session not found: {key}")
        if supports_atomic_intent:
            assert callable(prepare_intent)
            plan = await prepare_intent(
                key,
                session_intent,
                agent_id=_effective_agent_id_for_session(existing_session, key),
                **create_kwargs,
            )
            return plan.node, plan
        if "apply_intent" in dir(ctx.session_manager):
            applied_session, _intent_applied = await ctx.session_manager.apply_intent(
                key,
                session_intent,
                agent_id=_effective_agent_id_for_session(existing_session, key),
                **create_kwargs,
            )
            return applied_session, None
        if session_intent is not SessionIntent.CONTINUE:
            raise RuntimeError("Session intent handling requires SessionManager.apply_intent")
        return existing_session, None

    intent_lock = get_session_lock(ctx.turn_runner, key)
    if intent_lock is None:
        session, atomic_intent_plan = await _prepare_or_apply_intent()
    else:
        async with intent_lock:
            session, atomic_intent_plan = await _prepare_or_apply_intent()

    if fork_before_message_id is not None:
        parent_key = key
        parent_display_name = getattr(session, "display_name", None)
        agent_id = _effective_agent_id_for_session(session, parent_key)
        child_key = _create_session_key(agent_id, "webchat")
        prepare_prefix_branch = getattr(ctx.session_manager, "prepare_prefix_branch", None)
        if (
            callable(prepare_prefix_branch)
            and task_runtime_candidate is not None
            and callable(getattr(storage, "accept_turn", None))
            and callable(getattr(task_runtime_candidate, "reserve", None))
            and callable(getattr(task_runtime_candidate, "activate", None))
            and callable(getattr(task_runtime_candidate, "abort_reservation", None))
        ):

            async def _prepare_prefix_intent() -> Any:
                return await prepare_prefix_branch(
                    parent_key,
                    child_key,
                    fork_before_message_id=fork_before_message_id,
                    status=SessionStatus.DONE,
                )

            parent_lock = get_session_lock(ctx.turn_runner, parent_key)
            if parent_lock is None:
                atomic_intent_plan = await _prepare_prefix_intent()
            else:
                async with parent_lock:
                    atomic_intent_plan = await _prepare_prefix_intent()
            session = atomic_intent_plan.node
            key = child_key
        else:
            session = await _branch_with_session_mutation_lock(
                ctx.session_manager,
                ctx.turn_runner,
                parent_key,
                child_key,
                fork_transcript=True,
                status=SessionStatus.DONE,
                fork_before_message_id=fork_before_message_id,
            )
            key = child_key
            if parent_display_name:
                session = await ctx.session_manager.update(key, display_name=parent_display_name)
            await _emit_to_subscribers(
                ctx,
                key,
                "sessions.changed",
                build_sessions_changed_payload(key, "forked", run_status="idle"),
            )

    canonical_session_id = getattr(session, "session_id", None)
    session_id = (
        canonical_session_id
        if isinstance(canonical_session_id, str) and canonical_session_id
        else key.split(":")[-1] or key
    )
    generate_title = await _should_auto_title(ctx, storage, session, key, session_id)
    disk_budget = getattr(attachments_cfg, "transcript_disk_budget_bytes", None)
    opaque_cap = getattr(attachments_cfg, "opaque_max_bytes", None)
    try:
        ingested_attachments = await _attachment_ingest.ingest_attachments(
            message_text,
            combined_attachments,
            failure_mode="raise",
            material_root=media_root,
            session_id=session_id,
            disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
            accept_opaque=bool(getattr(attachments_cfg, "accept_opaque", True)),
            opaque_limit_bytes=opaque_cap if isinstance(opaque_cap, int) else None,
        )
    except _attachment_ingest.AttachmentResolutionError as exc:
        # A staged upload expired / was lost before this send. Surface a typed,
        # retryable error carrying the attachment index + uuid so the client can
        # re-upload and resend instead of hitting a generic INVALID_REQUEST dead
        # end. The uuid is intentionally NOT evicted (it is already gone).
        raise RpcHandlerError(
            exc.code,
            str(exc),
            details={
                "attachmentIndex": exc.attachment_index,
                "fileUuid": exc.file_uuid,
                "recovery": "reupload" if exc.recoverable else None,
            },
            retryable=exc.recoverable,
        ) from exc
    message_text = ingested_attachments.text
    raw_attachments = ingested_attachments.attachments
    inferred_normalized_input = None
    if normalized_input.metadata.get("guard_action") == "none":
        inferred_normalized_input = infer_normalized_input_from_attachments(
            message_text,
            raw_attachments,
        )
        if inferred_normalized_input is not None:
            message_text = inferred_normalized_input.message_text
            semantic_message_text = inferred_normalized_input.semantic_message

    normalization_metadata = (
        normalized_input.metadata
        if normalized_input.metadata.get("guard_action") != "none"
        else (
            inferred_normalized_input.metadata
            if inferred_normalized_input is not None
            and inferred_normalized_input.metadata.get("guard_action") != "none"
            else None
        )
    )
    if normalization_metadata is not None:
        raw_attachments = materialize_generated_text_attachments(
            raw_attachments,
            media_root=media_root,
            session_id=session_id,
            normalization_metadata=normalization_metadata,
            disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
        )
    # Evict consumed uuids only after the turn is accepted.
    _consumed_file_uuids: list[str] = list(ingested_attachments.consumed_file_uuids)
    log.info(
        "sessions.send.params",
        session_key=key,
        message_len=len(message_text),
        attachments_count=len(raw_attachments),
    )

    display_text = params.get("displayText") if source_hint.get("caller_kind") == "web" else None
    if display_text is not None and not isinstance(display_text, str):
        display_text = None
    if display_text is None and source_hint.get("caller_kind") == "web":
        from opensquilla.meta_preflight_protocol import (
            display_text_from_preflight_confirmation,
        )

        display_text = display_text_from_preflight_confirmation(message_text)
    provider_message_text = message_text
    if source_hint.get("caller_kind") == "web":
        from opensquilla.meta_preflight_protocol import (
            strip_preflight_confirmation_protocol_text,
        )

        stripped_message = strip_preflight_confirmation_protocol_text(message_text)
        if stripped_message is not None:
            provider_message_text = stripped_message.strip()

    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.gateway.routing import (
        build_cli_route_envelope,
        build_web_route_envelope,
    )

    agent_id = _effective_agent_id_for_session(session, key)
    workspace_path = resolve_agent_workspace_dir(agent_id, ctx.config)
    workspace_dir = str(workspace_path) if workspace_path is not None else None
    run_mode_hint = _trusted_run_mode_hint(ctx, source_hint)
    run_context = await get_run_context(
        ctx.session_manager,
        key,
        config=ctx.config,
        workspace=workspace_dir,
    )
    run_context = replace(
        run_context,
        run_mode=coerce_run_mode_for_principal(run_context.run_mode, ctx.principal),
    )
    if run_mode_hint is not None:
        run_context = replace(
            run_context,
            run_mode=run_mode_hint,
            source="request",
        )
    if source_hint.get("caller_kind") == "cli" or source_hint.get("channel_kind") == "cli":
        route_envelope = build_cli_route_envelope(
            session_key=key,
            agent_id=agent_id,
            source_name=source_hint.get("source_name") or "rpc",
            channel_id=source_hint.get("channel_id") or "cli:rpc",
            sender_id=source_hint.get("sender_id"),
            session_id=getattr(session, "session_id", None),
            principal_is_owner=ctx.principal.is_owner,
            run_mode=run_context.run_mode.value,
        )
    else:
        route_envelope = build_web_route_envelope(
            session_key=key,
            agent_id=agent_id,
            conn_id=ctx.conn_id,
            sender_id=source_hint.get("sender_id"),
            channel_id=source_hint.get("channel_id") or f"web:{ctx.conn_id}",
            source_name=source_hint.get("source_name") or "RPC",
            tool_source_kind=source_hint.get("source_kind"),
            session_id=getattr(session, "session_id", None),
            principal_is_owner=ctx.principal.is_owner,
        )
    _apply_run_context_route_metadata(
        route_envelope,
        run_context,
        principal_is_owner=ctx.principal.is_owner,
    )
    elevated_hint = _trusted_elevated_hint(ctx, source_hint)
    if elevated_hint is not None:
        route_envelope.metadata["elevated"] = elevated_hint

    capture_controls = _normalize_memory_capture_controls(params)
    input_provenance = capture_controls["input_provenance"]
    if input_provenance is not None:
        input_provenance = dict(input_provenance)
    else:
        input_provenance = dict(route_envelope.input_provenance)
    if normalization_metadata is not None:
        input_provenance["input_normalization"] = normalization_metadata
    if input_provenance != route_envelope.input_provenance:
        route_envelope = replace(
            route_envelope,
            input_provenance=input_provenance,
        )
    run_kind = capture_controls["run_kind"] or "session_turn"

    # Allocate the durable causal identity before persistence.  The same id is
    # handed to TaskRuntime, live events, bootstrap history, and every transcript
    # row produced by this turn.
    turn_id = uuid.uuid4().hex
    client_message_id = requested_client_message_id or uuid.uuid4().hex
    surface_id = (
        requested_surface_id
        or getattr(route_envelope, "channel_id", None)
        or str(getattr(route_envelope, "source_kind", "unknown"))
    )
    route_envelope = replace(
        route_envelope,
        metadata={
            **route_envelope.metadata,
            "client_message_id": client_message_id,
            "surface_id": surface_id,
            "turn_context_intent": "send",
            "turn_context_revision": 1,
        },
    )
    ingress_turn_context = {
        "turn_id": turn_id,
        "client_message_id": client_message_id,
        "surface_id": surface_id,
        "intent": "send",
        "disposition": "queued" if getattr(ctx, "task_runtime", None) is not None else "applied",
        "revision": 1,
    }

    task_runtime = task_runtime_candidate
    requested_mode = (
        params.get("queueMode")
        or params.get("queue_mode")
        or getattr(session, "queue_mode", None)
        or "followup"
    )
    runtime_mode = "interrupt" if requested_mode == "steer" else requested_mode
    if atomic_intent_plan is not None and atomic_intent_plan.action == "reset":
        # A reset rotates the session identity. Any old-key task must be stopped
        # only after that rotation commits so it cannot append into the new epoch.
        runtime_mode = "interrupt"
    atomic_runtime_acceptance = (
        task_runtime is not None
        and atomic_intent_plan is not None
        and callable(getattr(task_runtime, "reserve", None))
        and callable(getattr(task_runtime, "activate", None))
        and callable(getattr(task_runtime, "abort_reservation", None))
        and callable(getattr(task_runtime, "collect_admission", None))
        and callable(getattr(storage, "accept_turn", None))
        and callable(getattr(ctx.session_manager, "prepare_message", None))
        and (
            runtime_mode != "collect"
            or callable(getattr(task_runtime, "try_collect_atomically", None))
        )
    )

    if atomic_runtime_acceptance:
        assert task_runtime is not None
        assert atomic_intent_plan is not None
        atomic_task_runtime = task_runtime
        persist_content = message_text
        if raw_attachments or display_text is not None:
            from opensquilla.gateway.transcripts import (
                build_transcript_attachment_envelope,
            )

            if raw_attachments and hasattr(ctx.session_manager, "stamp_user_text"):
                stamped = ctx.session_manager.stamp_user_text(message_text)
                if isinstance(stamped, str):
                    message_text = stamped
            persist_content, _writes = build_transcript_attachment_envelope(
                text=message_text,
                display_text=display_text,
                attachments=raw_attachments,
                session_id=session_id,
                media_root=media_root,
                persist_enabled=persist_enabled,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
            )

        persisted_entry, expected_epoch = await ctx.session_manager.prepare_message(
            key,
            role="user",
            content=persist_content,
            turn_context=ingress_turn_context,
            session_node=session,
        )
        if (
            not raw_attachments
            and display_text is None
            and isinstance(persisted_entry.content, str)
        ):
            message_text = persisted_entry.content

        from opensquilla.gateway.task_runtime import TaskQueueFullError

        async def _accept_task_record(
            task_record: AgentTaskRecord,
            *,
            merge_into_task: bool = False,
        ) -> TurnAcceptanceResult:
            reset_archive_writer = None
            if atomic_intent_plan.action == "reset":
                write_session_archive = getattr(
                    ctx.session_manager,
                    "write_session_archive",
                    None,
                )
                if not callable(write_session_archive):
                    raise RuntimeError("Reset requires durable session archive support")

                async def reset_archive_writer(snapshot: Any) -> None:
                    await write_session_archive(
                        snapshot.node,
                        list(snapshot.entries),
                        list(snapshot.summaries),
                    )

            return await storage.accept_turn(
                persisted_entry,
                expected_epoch=expected_epoch,
                updated_at=int(time.time() * 1000),
                task_record=task_record,
                source_scope=ingress_identity.source_scope,
                request_session_key=ingress_identity.request_session_key,
                client_request_id=ingress_identity.client_request_id,
                request_fingerprint=ingress_identity.request_fingerprint,
                session_node=(
                    atomic_intent_plan.node
                    if atomic_intent_plan.action in {"create", "reset", "fork"}
                    else None
                ),
                reset_from_session_id=(
                    atomic_intent_plan.previous_session_id
                    if atomic_intent_plan.action == "reset"
                    else None
                ),
                reset_archive_writer=reset_archive_writer,
                initial_transcript_entries=(
                    atomic_intent_plan.initial_transcript_entries
                    if atomic_intent_plan.action == "fork"
                    else ()
                ),
                merge_into_task=merge_into_task,
            )

        async def _commit_and_activate() -> TurnAcceptanceResult:
            if runtime_mode == "collect" and atomic_intent_plan.action == "continue":

                async def _persist_collection(
                    handle: Any,
                    details: dict[str, Any],
                ) -> TurnAcceptanceResult:
                    collected_context = {
                        **ingress_turn_context,
                        "turn_id": handle.task_id,
                        "target_turn_id": handle.task_id,
                        "revision": max(
                            2,
                            int(ingress_turn_context.get("revision", 1)) + 1,
                        ),
                    }
                    persisted_entry.turn_context = collected_context
                    task_record = AgentTaskRecord(
                        task_id=handle.task_id,
                        session_key=handle.session_key,
                        agent_id=route_envelope.agent_id,
                        source_kind=route_envelope.source_kind.value,
                        queue_mode="collect",
                        run_kind=run_kind,
                        status=AgentTaskStatus.QUEUED,
                        details=details,
                    )
                    return await _accept_task_record(
                        task_record,
                        merge_into_task=True,
                    )

                collected = await atomic_task_runtime.try_collect_atomically(
                    envelope=route_envelope,
                    message=provider_message_text,
                    attachments=raw_attachments,
                    run_kind=run_kind,
                    no_memory_capture=bool(capture_controls["no_memory_capture"]),
                    semantic_message=semantic_message_text,
                    persisted_user_message_id=persisted_entry.message_id,
                    message_count=1,
                    persist=_persist_collection,
                )
                if collected is not None:
                    _handle, collected_acceptance = collected
                    return cast(TurnAcceptanceResult, collected_acceptance)

            reservation = await reserve_turn_via_runtime(
                atomic_task_runtime,
                route_envelope,
                provider_message_text,
                attachments=raw_attachments,
                mode=runtime_mode,
                run_kind=run_kind,
                no_memory_capture=bool(capture_controls["no_memory_capture"]),
                semantic_message=semantic_message_text,
                turn_id=turn_id,
            )
            try:
                acceptance = await _accept_task_record(reservation.task_record)
            except BaseException:
                await atomic_task_runtime.abort_reservation(reservation)
                raise

            if acceptance.replayed:
                await atomic_task_runtime.abort_reservation(reservation)
                return acceptance

            if atomic_intent_plan.action == "reset":
                set_cached_epoch = getattr(ctx.session_manager, "set_cached_epoch", None)
                if callable(set_cached_epoch):
                    set_cached_epoch(key, expected_epoch)
            try:
                await atomic_task_runtime.activate(
                    reservation,
                    persisted_user_message_id=acceptance.receipt.message_id,
                    fresh_user_session=acceptance.fresh_user_session,
                )
            except Exception as exc:  # noqa: BLE001 - acceptance already committed.
                log.error(
                    "sessions.send.activation_failed",
                    session_key=key,
                    task_id=acceptance.receipt.task_id,
                    error=str(exc),
                    exc_info=True,
                )
                if acceptance.receipt.task_id:
                    try:
                        await storage.update_agent_task(
                            acceptance.receipt.task_id,
                            status="failed",
                            finished_at=int(time.time() * 1000),
                            terminal_reason="activation_failed",
                            error_class=type(exc).__name__,
                            error_message=str(exc),
                        )
                    except Exception:  # noqa: BLE001 - preserve accepted response.
                        log.exception(
                            "sessions.send.activation_failure_record_failed",
                            task_id=acceptance.receipt.task_id,
                        )
                if not reservation.activated:
                    try:
                        await atomic_task_runtime.abort_reservation(reservation)
                    except Exception:  # noqa: BLE001 - preserve accepted response.
                        log.exception(
                            "sessions.send.activation_abort_failed",
                            task_id=acceptance.receipt.task_id,
                        )
                acceptance = replace(
                    acceptance,
                    task_status=AgentTaskStatus.FAILED,
                )
            return acceptance

        async def _commit_with_session_admission() -> TurnAcceptanceResult:
            # Serialize the full durable commit -> runtime activation boundary
            # for every queue mode. In particular, a reset/interrupt must not
            # overtake a committed-but-inert continue reservation: interrupt
            # activation can only cancel tasks that have crossed activation.
            async with atomic_task_runtime.collect_admission(route_envelope.session_key):
                return await _commit_and_activate()

        try:
            acceptance = await complete_durable_ingress(_commit_with_session_admission())
        except TaskQueueFullError as exc:
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "QUEUE_FULL",
                "The session task queue is full. Try again after queued work completes.",
                details={
                    "session_key": exc.session_key,
                    "max_pending": exc.max_pending,
                },
                retryable=True,
                accepted=False,
            ) from exc
        except StorageBusyError as exc:
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "STORAGE_BUSY",
                "Session storage is temporarily busy. Retry this send.",
                details={
                    "operation": exc.operation,
                    "waited_ms": exc.waited_ms,
                },
                retryable=True,
                retry_after_ms=exc.retry_after_ms,
                accepted=False,
            ) from exc
        except StaleEpochError as exc:
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "SESSION_CHANGED",
                "The session changed while this turn was being accepted. Retry the send.",
                retryable=True,
                accepted=False,
            ) from exc
        except TurnIngressConflictError as exc:
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "IDEMPOTENCY_CONFLICT",
                str(exc),
                retryable=False,
                accepted=False,
            ) from exc
        except TaskCollectionUnavailableError as exc:
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "COLLECT_RACE",
                "The queued task started before this message could be collected. Retry it.",
                retryable=True,
                accepted=False,
            ) from exc
        except sqlite3.IntegrityError as exc:
            if atomic_intent_plan.action != "create" or "sessions.session_key" not in str(exc):
                raise
            _consumed_file_uuids = []
            raise RpcHandlerError(
                "SESSION_CONFLICT",
                "Another request created this session first. Start a new chat and retry.",
                retryable=False,
                accepted=False,
            ) from exc

        if not acceptance.replayed:
            notify_message_appended = getattr(ctx.session_manager, "notify_message_appended", None)
            if callable(notify_message_appended):
                try:
                    notify_message_appended(persisted_entry)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_notify_failed",
                        session_key=key,
                        task_id=acceptance.receipt.task_id,
                    )
            reset_archive = acceptance.reset_archive_snapshot
            if reset_archive is not None:
                write_session_archive = getattr(ctx.session_manager, "write_session_archive", None)
                if callable(write_session_archive):
                    try:
                        await write_session_archive(
                            reset_archive.node,
                            list(reset_archive.entries),
                            list(reset_archive.summaries),
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_archive_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                        )
            if (
                atomic_intent_plan.action == "fork"
                and atomic_intent_plan.previous_session_id is not None
            ):
                copy_fork_materials = getattr(ctx.session_manager, "_copy_fork_materials", None)
                if callable(copy_fork_materials):
                    try:
                        await copy_fork_materials(
                            atomic_intent_plan.previous_session_id,
                            session_id,
                            key,
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_fork_copy_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                        )
                try:
                    await _emit_to_subscribers(
                        ctx,
                        key,
                        "sessions.changed",
                        build_sessions_changed_payload(key, "forked", run_status="idle"),
                    )
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_fork_event_failed",
                        session_key=key,
                        task_id=acceptance.receipt.task_id,
                    )

        if _consumed_file_uuids:
            from opensquilla.gateway.uploads import get_upload_store

            upload_store = get_upload_store()
            for file_uuid in _consumed_file_uuids:
                try:
                    await upload_store.evict(file_uuid)
                except Exception:  # noqa: BLE001 - eviction is best-effort
                    log.warning("uploads.evict_failed_post_turn uuid=%s", file_uuid[:8])
        if not acceptance.replayed:
            try:
                _schedule_auto_title(
                    ctx,
                    key,
                    semantic_message_text or message_text,
                    enabled=generate_title,
                    session_id=session_id,
                    root_turn_id=acceptance.receipt.task_id,
                )
            except Exception:  # noqa: BLE001 - turn is already accepted.
                log.exception(
                    "sessions.send.post_accept_title_failed",
                    session_key=key,
                    task_id=acceptance.receipt.task_id,
                )
        return await _accepted_turn_response(
            acceptance,
            client_request_id=ingress_identity.client_request_id,
            storage=storage,
            turn_context=(persisted_entry.turn_context if not acceptance.replayed else None),
        )

    # 1. Persist user message to transcript (include attachment metadata).
    # Hold the per-session lock used by /reset so a concurrent reset cannot
    # tear the append and leak an orphan user turn into the cleared transcript.
    _persist_lock = get_session_lock(ctx.turn_runner, key)
    legacy_persisted_entry: Any = None
    fresh_user_session = False

    async def _persist_user_message() -> None:
        nonlocal message_text, legacy_persisted_entry, fresh_user_session
        from opensquilla.session.turn_context import turn_context_scope

        get_transcript = getattr(ctx.session_manager, "get_transcript", None)
        if callable(get_transcript):
            fresh_user_session = not bool(await get_transcript(key))
        if raw_attachments or display_text is not None:
            from opensquilla.gateway.transcripts import (
                build_transcript_attachment_envelope,
            )

            # Stamp up-front so both the stored envelope and the LLM path agree.
            if raw_attachments and hasattr(ctx.session_manager, "stamp_user_text"):
                _stamped = ctx.session_manager.stamp_user_text(message_text)
                if isinstance(_stamped, str):
                    message_text = _stamped

            persist_content, _writes = build_transcript_attachment_envelope(
                text=message_text,
                display_text=display_text,
                attachments=raw_attachments,
                session_id=session_id,
                media_root=media_root,
                persist_enabled=persist_enabled,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
            )
            with turn_context_scope(ingress_turn_context):
                legacy_persisted_entry = await ctx.session_manager.append_message(
                    key,
                    role="user",
                    content=persist_content,
                )
        else:
            with turn_context_scope(ingress_turn_context):
                legacy_persisted_entry = await ctx.session_manager.append_message(
                    key,
                    role="user",
                    content=message_text,
                )
            if legacy_persisted_entry is not None and isinstance(
                legacy_persisted_entry.content, str
            ):
                message_text = legacy_persisted_entry.content

    if _persist_lock is None:
        await _persist_user_message()
    else:
        async with _persist_lock:
            await _persist_user_message()

    user_message_id = getattr(legacy_persisted_entry, "message_id", None)

    async def _rollback_persisted_user_message(reason: str) -> tuple[str | None, bool]:
        message_id = getattr(legacy_persisted_entry, "message_id", None)
        if not message_id or not hasattr(ctx.session_manager, "remove_message"):
            return message_id, False
        try:
            if _persist_lock is None:
                removed = await ctx.session_manager.remove_message(key, message_id)
            else:
                async with _persist_lock:
                    removed = await ctx.session_manager.remove_message(key, message_id)
        except Exception as rb_exc:  # noqa: BLE001 — rollback is best-effort
            log.warning(
                "sessions.send.rollback_failed",
                session_key=key,
                message_id=message_id,
                reason=reason,
                error=str(rb_exc),
            )
            return message_id, False
        if removed:
            log.info(
                "sessions.send.rollback_succeeded",
                session_key=key,
                message_id=message_id,
                reason=reason,
            )
        return message_id, bool(removed)

    task_runtime = getattr(ctx, "task_runtime", None)
    if task_runtime is not None:
        requested_mode = (
            params.get("queueMode")
            or params.get("queue_mode")
            or getattr(session, "queue_mode", None)
            or "followup"
        )
        runtime_mode = "interrupt" if requested_mode == "steer" else requested_mode
        try:
            handle = await start_turn_via_runtime(
                task_runtime,
                route_envelope,
                provider_message_text,
                attachments=raw_attachments,
                mode=runtime_mode,
                run_kind=run_kind,
                no_memory_capture=bool(capture_controls["no_memory_capture"]),
                semantic_message=semantic_message_text,
                persisted_user_message_id=getattr(legacy_persisted_entry, "message_id", None),
                fresh_user_session=fresh_user_session,
                turn_id=turn_id,
            )
        except Exception as exc:
            # Ensure the uuid eviction does NOT fire on this
            # path. The locked semantic mandates that any rejection /
            # rollback / queue-full leaves the uuid alive until TTL so
            # the user can retry against the same uuid.
            _consumed_file_uuids = []  # noqa: F841 — explicit no-evict marker
            from opensquilla.gateway.task_runtime import TaskQueueFullError

            if not isinstance(exc, TaskQueueFullError):
                raise

            # Roll back the just-appended user turn so a retry doesn't leave
            # a ghost message in the transcript. If rollback fails (e.g.
            # storage error under load), surface a non-retryable error and
            # hand the orphan message_id to the client as an idempotency
            # token — clients must dedup before retrying.
            orphan_id, rollback_ok = await _rollback_persisted_user_message("queue_full")

            if rollback_ok:
                raise RpcHandlerError(
                    "QUEUE_FULL",
                    "The session task queue is full. Try again after queued work completes.",
                    details={
                        "session_key": exc.session_key,
                        "max_pending": exc.max_pending,
                        "rollback_message_id": orphan_id,
                    },
                    retryable=True,
                    accepted=False,
                ) from exc
            raise RpcHandlerError(
                "QUEUE_FULL_DIRTY",
                (
                    "The session task queue is full and the just-appended user "
                    "turn could not be rolled back. The transcript now contains "
                    "an orphan message; clients must dedup by orphan_message_id "
                    "before retrying."
                ),
                details={
                    "session_key": exc.session_key,
                    "max_pending": exc.max_pending,
                    "orphan_message_id": orphan_id,
                    "remediation": "client must dedup by message_id before retry",
                },
                retryable=False,
                accepted=True,
            ) from exc
        if handle.task_id != turn_id:
            # ``collect`` coalesces this durable prompt into an already queued
            # runtime turn. TaskRuntime has rebound the stored row; project and
            # return that same canonical identity instead of the unused
            # preallocation so live consumers and a later hydrate agree.
            turn_id = handle.task_id
            ingress_turn_context = {
                **ingress_turn_context,
                "turn_id": turn_id,
                "target_turn_id": turn_id,
                "revision": max(2, int(ingress_turn_context.get("revision", 1)) + 1),
            }
        # Eviction hook: turn was accepted into the runtime,
        # post-resolution + post-engine-acceptance. Evict consumed uuids
        # so memory does not linger for the full TTL window. Locked
        # semantic mandates this fires ONLY here on the success path.
        if _consumed_file_uuids:
            from opensquilla.gateway.uploads import get_upload_store

            _store = get_upload_store()
            for _u in _consumed_file_uuids:
                try:
                    await _store.evict(_u)
                except Exception:  # noqa: BLE001 — eviction is best-effort
                    log.warning("uploads.evict_failed_post_turn uuid=%s", _u[:8])
        _schedule_auto_title(
            ctx,
            key,
            semantic_message_text or message_text,
            enabled=generate_title,
            session_id=session_id,
            root_turn_id=turn_id,
        )
        await _emit_to_subscribers(
            ctx,
            key,
            "session.event.input_disposition",
            {
                "session_key": key,
                "user_message_id": user_message_id,
                **ingress_turn_context,
            },
        )
        return {
            "status": "accepted",
            "key": key,
            "session_key": key,
            "session_id": session_id,
            "task_id": handle.task_id,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "user_message_id": user_message_id,
            "surface_id": surface_id,
        }

    # 2. Run agent turn in background via TurnRunner
    def _turn_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        enriched.setdefault("session_id", session_id)
        enriched.setdefault("turn_id", turn_id)
        enriched.setdefault("client_message_id", client_message_id)
        if user_message_id:
            enriched.setdefault("user_message_id", user_message_id)
        enriched.setdefault("surface_id", surface_id)
        return enriched

    async def _run() -> None:
        _terminal_emitted = False

        def _current_task() -> asyncio.Task | None:
            task = asyncio.current_task()
            return task if isinstance(task, asyncio.Task) else None

        def _mark_started() -> None:
            task = _current_task()
            if task is not None:
                setattr(task, "_opensquilla_started", True)

        async def _emit_terminal_once(event_name: str, payload: dict[str, Any]) -> None:
            nonlocal _terminal_emitted
            task = _current_task()
            if _terminal_emitted or (
                task is not None and getattr(task, "_opensquilla_terminal_emitted", False)
            ):
                return
            _terminal_emitted = True
            if task is not None:
                setattr(task, "_opensquilla_terminal_emitted", True)
            payload = _normalize_terminal_event_payload(event_name, payload)
            await _emit_to_subscribers(
                ctx,
                key,
                event_name,
                _turn_event_payload(payload),
            )

        try:
            _mark_started()
            from opensquilla.session.turn_context import turn_context_scope

            turn_scope = turn_context_scope(
                {
                    **ingress_turn_context,
                    "disposition": "applied",
                }
            )
            turn_scope.__enter__()
            if ctx.turn_runner is None:
                log.error("sessions.send.no_turn_runner", session_key=key)
                await ctx.session_manager.append_message(
                    key, role="system", content="Error: No turn runner available"
                )
                await _emit_terminal_once(
                    "session.event.error",
                    {"message": "No turn runner available", "code": "no_turn_runner"},
                )
                return

            from opensquilla.engine.stream_wrappers import wrap_stream
            from opensquilla.gateway.routing import tool_context_from_envelope
            from opensquilla.permissions import configured_default_elevated

            workspace_strict = getattr(ctx.config, "workspace_strict", None)
            if not isinstance(workspace_strict, bool):
                workspace_strict = bool(workspace_dir)
            tool_ctx = tool_context_from_envelope(
                route_envelope,
                is_owner=ctx.principal.is_owner,
                workspace_dir=workspace_dir,
                workspace_strict=workspace_strict,
                default_elevated=configured_default_elevated(ctx.config),
            )
            raw_stream = ctx.turn_runner.run(
                provider_message_text,
                key,
                tool_context=tool_ctx,
                agent_id=agent_id,
                model=_session_turn_model(ctx, session, agent_id),
                attachments=raw_attachments,
                session_intent=session_intent.value,
                input_provenance=route_envelope.input_provenance,
                run_kind=run_kind,
                no_memory_capture=capture_controls["no_memory_capture"],
                semantic_message=semantic_message_text,
                fresh_user_session=fresh_user_session,
                root_turn_id=turn_id,
            )
            raw_stream_idle_timeout = effective_agent_stream_idle_timeout_seconds(ctx.config)
            stream_idle_timeout: float | None = (
                raw_stream_idle_timeout if raw_stream_idle_timeout > 0 else None
            )
            heartbeat_interval = _optional_positive_timeout(
                ctx.config, "agent_stream_heartbeat_interval_seconds", 15.0
            )
            async for event in wrap_stream(
                raw_stream,
                idle_timeout=stream_idle_timeout,
                heartbeat_interval=heartbeat_interval,
                heartbeat_message="Agent run is still active",
            ):
                event_dict = asdict(event)
                event_kind = event_dict.pop("kind", event.__class__.__name__)
                if event_kind == "artifact":
                    event_dict = enrich_artifact_event_dict(event_dict)
                if event_kind in ("done", "error"):
                    await _emit_terminal_once(f"session.event.{event_kind}", event_dict)
                else:
                    await _emit_to_subscribers(
                        ctx,
                        key,
                        f"session.event.{event_kind}",
                        _turn_event_payload(event_dict),
                    )

            await _emit_to_subscribers(
                ctx,
                key,
                "sessions.changed",
                _turn_event_payload(build_sessions_changed_payload(key, "turn_complete")),
            )
        except asyncio.CancelledError:
            log.info("sessions.send.aborted", session_key=key)
            try:
                await _emit_terminal_once("session.event.done", {"reason": "aborted"})
            except Exception:
                pass
        except TimeoutError:
            log.warning("sessions.send.stream_idle_timeout", session_key=key)
            timeout_message = build_terminal_reply(
                {
                    "status": "timeout",
                    "terminal_reason": "timeout",
                    "error_class": _STREAM_IDLE_TIMEOUT_CODE,
                    "error_message": _STREAM_IDLE_TIMEOUT_MESSAGE,
                }
            )
            await ctx.session_manager.append_message(key, role="system", content=timeout_message)
            await _emit_terminal_once(
                "session.event.error",
                {"message": _STREAM_IDLE_TIMEOUT_MESSAGE, "code": _STREAM_IDLE_TIMEOUT_CODE},
            )
        except Exception as exc:
            error_code, error_message = sanitize_agent_error(
                {
                    "status": "failed",
                    "terminal_reason": "error",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
                fallback_error_class="agent_error",
                fallback_error_message=str(exc) or "Agent error",
            )
            event_code = error_code if error_code == "provider_request_too_large" else "agent_error"
            log.error("sessions.send.agent_failed", session_key=key, error=str(exc), exc_info=True)
            await ctx.session_manager.append_message(
                key,
                role="system",
                content=f"Error: {error_message}",
            )
            await _emit_terminal_once(
                "session.event.error",
                {"message": error_message, "code": event_code},
            )
        finally:
            if "turn_scope" in locals():
                turn_scope.__exit__(None, None, None)
            if not _terminal_emitted:
                try:
                    await _emit_terminal_once(
                        "session.event.error",
                        {"message": "Agent task terminated unexpectedly", "code": "task_cancelled"},
                    )
                except Exception:
                    pass

    task = asyncio.create_task(_run())
    setattr(task, "_opensquilla_started", False)
    setattr(task, "_opensquilla_terminal_emitted", False)
    get_agent_task_registry().register(key, task)
    await _emit_to_subscribers(
        ctx,
        key,
        "session.event.input_disposition",
        {
            "session_key": key,
            "user_message_id": user_message_id,
            **ingress_turn_context,
        },
    )
    # Same eviction semantic as the task_runtime success path: the turn was
    # accepted into a background TurnRunner task, so consumed uuids can be
    # evicted from the upload store rather than waiting out the TTL window.
    if _consumed_file_uuids:
        from opensquilla.gateway.uploads import get_upload_store

        _store = get_upload_store()
        for _u in _consumed_file_uuids:
            try:
                await _store.evict(_u)
            except Exception:  # noqa: BLE001 — eviction is best-effort
                log.warning("uploads.evict_failed_post_turn uuid=%s", _u[:8])
    _schedule_auto_title(
        ctx,
        key,
        semantic_message_text or message_text,
        enabled=generate_title,
        session_id=session_id,
        root_turn_id=turn_id,
    )
    return {
        "status": "accepted",
        "key": key,
        "session_key": key,
        "session_id": session_id,
        "turn_id": turn_id,
        "client_message_id": client_message_id,
        "user_message_id": user_message_id,
        "surface_id": surface_id,
    }


@_d.method("sessions.steer", scope="operator.write")
async def _handle_sessions_steer(params: dict | None, ctx: RpcContext) -> dict:
    """Inject text into the active turn, with a durable follow-up fallback."""

    key = _require_key(params)
    if not isinstance(params, dict) or not isinstance(params.get("message"), str):
        raise ValueError("params.message is required")
    raw_message = params["message"]
    if not raw_message.strip():
        raise ValueError("params.message must not be blank")
    if ctx.session_manager is None:
        raise KeyError("No session manager available")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")

    task_runtime = getattr(ctx, "task_runtime", None)
    active_task_id = getattr(task_runtime, "active_task_id", None)
    steer = getattr(task_runtime, "steer", None)
    if not callable(active_task_id) or not callable(steer):
        return {"status": "unavailable", "accepted": False, "key": key}
    current_turn_id = await active_task_id(key)
    if not current_turn_id:
        return {"status": "idle", "accepted": False, "key": key}

    source_hint = _normalize_session_send_source_hint(params)
    normalized = normalize_incoming_text(raw_message, source_hint=source_hint, attachments=[])
    if normalized.generated_attachments:
        raise ValueError("Steering does not support generated attachments")
    message_text = normalized.message_text
    semantic_message = normalized.semantic_message
    client_message_id = (
        _optional_string_param(params, "client_message_id", "clientMessageId") or uuid.uuid4().hex
    )
    surface_id = _optional_string_param(params, "surface_id", "surfaceId") or str(
        source_hint.get("channel_id") or f"web:{ctx.conn_id}"
    )

    persisted_entry: Any = None

    async def _persist() -> None:
        nonlocal persisted_entry, message_text
        from opensquilla.session.turn_context import turn_context_scope

        with turn_context_scope(
            {
                "turn_id": current_turn_id,
                "client_message_id": client_message_id,
                "surface_id": surface_id,
                "intent": "steer",
                "disposition": "steering",
                "target_turn_id": current_turn_id,
                "revision": 1,
            }
        ):
            persisted_entry = await ctx.session_manager.append_message(
                key,
                role="user",
                content=message_text,
            )
        if persisted_entry is not None and isinstance(persisted_entry.content, str):
            message_text = persisted_entry.content

    persist_lock = get_session_lock(ctx.turn_runner, key)
    if persist_lock is None:
        await _persist()
    else:
        async with persist_lock:
            await _persist()
    user_message_id = getattr(persisted_entry, "message_id", None)

    accepted_turn_id = await steer(
        key,
        message_text,
        semantic_message=semantic_message,
        persisted_user_message_id=user_message_id,
        client_message_id=client_message_id,
        surface_id=surface_id,
    )
    if not accepted_turn_id:
        # The turn crossed its terminal boundary between the optimistic active
        # check and the append. Roll back so the caller can honestly queue the
        # text through sessions.send without leaving a duplicate transcript row.
        remove_message = getattr(ctx.session_manager, "remove_message", None)
        removed = False
        rollback_error: str | None = None
        if user_message_id and callable(remove_message):
            try:
                removed = bool(await remove_message(key, user_message_id))
            except Exception as exc:  # noqa: BLE001 - classify dirty rollback
                rollback_error = str(exc)
        if removed:
            return {
                "status": "idle",
                "accepted": False,
                "key": key,
            }

        # The durable row still exists.  Returning the ordinary idle response
        # would make TUI enqueue the same text through sessions.send and create
        # a duplicate.  Mark the orphan explicitly, emit the causal failure,
        # and fail closed with the same dirty-rollback semantics used by
        # sessions.send's QUEUE_FULL_DIRTY path.
        rejected_context = {
            "turn_id": current_turn_id,
            "client_message_id": client_message_id,
            "surface_id": surface_id,
            "intent": "steer",
            "disposition": "rejected",
            "target_turn_id": current_turn_id,
            "revision": 2,
        }
        update_turn_context = getattr(
            ctx.session_manager,
            "update_message_turn_context",
            None,
        )
        if user_message_id and callable(update_turn_context):
            try:
                updated = bool(
                    await update_turn_context(
                        key,
                        user_message_id,
                        rejected_context,
                    )
                )
                if not updated:
                    log.warning(
                        "sessions.steer.dirty_context_update_missed",
                        session_key=key,
                        message_id=user_message_id,
                    )
            except Exception:  # noqa: BLE001 - RPC error below remains authoritative
                log.warning(
                    "sessions.steer.dirty_context_update_failed",
                    session_key=key,
                    message_id=user_message_id,
                    exc_info=True,
                )
        try:
            await _emit_to_subscribers(
                ctx,
                key,
                "session.event.input_disposition",
                {
                    "session_key": key,
                    "user_message_id": user_message_id,
                    **rejected_context,
                    "failure_code": "STEER_RACE_DIRTY",
                    "retryable": False,
                    "fallback_safe": False,
                },
            )
        except Exception:  # noqa: BLE001 - explicit RPC error still reaches caller
            log.warning(
                "sessions.steer.dirty_disposition_emit_failed",
                session_key=key,
                message_id=user_message_id,
                exc_info=True,
            )
        log.warning(
            "sessions.steer.rollback_failed",
            session_key=key,
            message_id=user_message_id,
            error=rollback_error,
        )
        raise RpcHandlerError(
            "STEER_RACE_DIRTY",
            (
                "The active turn ended and the just-appended steer input could "
                "not be rolled back. The transcript contains a rejected orphan; "
                "automatic queue fallback is disabled to prevent duplication."
            ),
            details={
                "session_key": key,
                "orphan_message_id": user_message_id,
                "target_turn_id": current_turn_id,
                "fallback_safe": False,
                "remediation": "dedup by orphan_message_id before resending",
            },
            retryable=False,
        )

    accepted_context = {
        "turn_id": accepted_turn_id,
        "client_message_id": client_message_id,
        "surface_id": surface_id,
        "intent": "steer",
        # Acceptance reserves the input for the next safe boundary.  The task
        # runtime advances this to ``applied`` only after the engine drains it,
        # or to ``promoted``/``rejected`` if the turn ends first.
        "disposition": "steering",
        "target_turn_id": accepted_turn_id,
        "revision": 1,
    }
    update_turn_context = getattr(
        ctx.session_manager,
        "update_message_turn_context",
        None,
    )
    if user_message_id and callable(update_turn_context):
        try:
            await update_turn_context(key, user_message_id, accepted_context)
        except Exception:  # noqa: BLE001 - steer is already accepted in runtime
            log.warning(
                "sessions.steer.context_update_failed",
                session_key=key,
                message_id=user_message_id,
                exc_info=True,
            )

    try:
        await _emit_to_subscribers(
            ctx,
            key,
            "session.event.steer",
            {
                "session_key": key,
                "turn_id": accepted_turn_id,
                "client_message_id": client_message_id,
                "user_message_id": user_message_id,
                "surface_id": surface_id,
                "disposition": "next_safe_boundary",
            },
        )
        await _emit_to_subscribers(
            ctx,
            key,
            "session.event.input_disposition",
            {
                "session_key": key,
                "user_message_id": user_message_id,
                **accepted_context,
            },
        )
    except Exception:  # noqa: BLE001 - runtime acceptance is authoritative
        log.warning(
            "sessions.steer.accepted_event_emit_failed",
            session_key=key,
            message_id=user_message_id,
            exc_info=True,
        )
    return {
        "status": "accepted",
        "accepted": True,
        "key": key,
        "turn_id": accepted_turn_id,
        "client_message_id": client_message_id,
        "user_message_id": user_message_id,
        "surface_id": surface_id,
        "disposition": "next_safe_boundary",
    }


async def _emit_to_subscribers(
    ctx: RpcContext,
    session_key: str,
    event_name: str,
    payload: dict,
) -> None:
    """Send an event to all connections subscribed to a session's messages."""
    from opensquilla.gateway.websocket import get_registry

    # Inject current epoch into session.event.* and sessions.changed
    # payloads so the frontend _isStaleEpoch guard can filter pre-reset frames.
    # Read from the in-process cache on SessionManager (populated by reset path) to
    # avoid a DB SELECT on every high-frequency event such as text_delta.
    if event_name.startswith("session.event.") or event_name == "sessions.changed":
        session_manager = getattr(ctx, "session_manager", None)
        cached_epoch = get_session_epoch(session_manager, session_key)
        if cached_epoch is not None:
            payload = {**payload, "epoch": cached_epoch}
        else:
            storage = get_session_storage(session_manager)
            if storage is not None and hasattr(storage, "get_epoch"):
                try:
                    epoch = await storage.get_epoch(session_key)
                    # Populate cache for subsequent emits.
                    set_session_epoch(session_manager, session_key, epoch)
                    payload = {**payload, "epoch": epoch}
                except Exception:
                    pass  # best-effort; never block event delivery

    send_payload = _buffer_session_event(session_key, event_name, payload)

    sub_mgr = getattr(ctx, "subscription_manager", None)
    if sub_mgr is None:
        return

    registry = get_registry()
    conn_ids = sub_mgr.get_message_subscribers(session_key)

    # For session-level events, also include session subscribers
    if event_name.startswith("sessions."):
        conn_ids = conn_ids | sub_mgr.get_session_subscribers()

    for conn_id in conn_ids:
        conn = registry.get(conn_id)
        if conn is not None:
            try:
                await conn.send_event(event_name, send_payload)
            except Exception:
                log.warning("emit.send_failed", conn_id=conn_id, ws_event=event_name)


@_d.method("sessions.abort", scope="operator.write")
async def _handle_sessions_abort(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)

    if ctx.session_manager is None:
        return {"aborted": False, "key": key}

    storage = get_session_storage(ctx.session_manager)
    if storage:
        session = await storage.get_session(key)
        if session is None:
            raise KeyError(f"Session not found: {key}")

    task_runtime = getattr(ctx, "task_runtime", None)
    if task_runtime is not None:
        from opensquilla.gateway.approval_queue import get_approval_queue
        from opensquilla.gateway.subagent_announce import (
            cancel_background_completion_for_session,
        )

        requested_task_id = _optional_string_param(params, "task_id", "taskId")
        if requested_task_id is not None:
            await cancel_background_completion_for_session(key)
            get_approval_queue().resolve_pending_for_session(key, approved=False)
            active_task_ids = await _active_task_runtime_ids(task_runtime, key)
            if active_task_ids and requested_task_id not in active_task_ids:
                return {"aborted": False, "key": key}
            active_task_ids = (requested_task_id,)
            cancelled_count = await _cancel_task_runtime(
                task_runtime,
                session_key=key,
                task_id=requested_task_id,
                source=_cancel_source_from_params(params, "sessions_abort"),
                reason="user_abort",
            )
            if cancelled_count > 0:
                await _drain_cancelled_task_runtime(
                    task_runtime,
                    session_key=key,
                    task_ids=active_task_ids,
                )
            return {"aborted": cancelled_count > 0, "key": key}

        cancel_source = _cancel_source_from_params(params, "sessions_abort")
        approval_queue = get_approval_queue()
        processed_keys: set[str] = set()
        cancel_requested_task_ids: set[str] = set()
        cancelled_tasks = 0
        cancelled_session_keys: set[str] = set()
        cancelled_groups = 0
        resolved_approvals = 0

        # Re-scan after each drained batch. A child may have committed a nested
        # spawn immediately before receiving cancellation; the next pass picks
        # that session up before the abort is considered complete.
        for pass_index in range(_ABORT_TREE_STABILIZATION_PASSES):
            tree_keys = await _session_tree_keys(ctx.session_manager, key)
            new_keys = [
                session_key
                for session_key in tree_keys
                if session_key not in processed_keys
            ]
            drains: list[tuple[str, tuple[str, ...]]] = []
            cancelled_this_pass = 0
            for session_key in tree_keys:
                first_visit = session_key in new_keys
                if first_visit:
                    processed_keys.add(session_key)
                    cancelled_groups += await cancel_background_completion_for_session(
                        session_key
                    )
                active_task_ids = await _active_task_runtime_ids(task_runtime, session_key)
                new_active_task_ids = tuple(
                    task_id
                    for task_id in active_task_ids
                    if task_id not in cancel_requested_task_ids
                )
                if not first_visit and not new_active_task_ids:
                    continue
                cancelled_count = await _cancel_task_runtime(
                    task_runtime,
                    session_key=session_key,
                    source=cancel_source,
                    reason="user_abort",
                )
                cancelled_tasks += cancelled_count
                cancelled_this_pass += cancelled_count
                resolved_approvals += approval_queue.resolve_pending_for_session(
                    session_key,
                    approved=False,
                )
                if cancelled_count > 0:
                    cancel_requested_task_ids.update(new_active_task_ids)
                    cancelled_session_keys.add(session_key)
                    drains.append((session_key, new_active_task_ids))

            for session_key, active_task_ids in drains:
                await _drain_cancelled_task_runtime(
                    task_runtime,
                    session_key=session_key,
                    task_ids=active_task_ids,
                )
            if pass_index > 0 and not new_keys and cancelled_this_pass == 0:
                break
        else:
            log.warning(
                "sessions.abort.tree_stabilization_exhausted",
                session_key=key,
                passes=_ABORT_TREE_STABILIZATION_PASSES,
            )

        aborted = any((cancelled_tasks, cancelled_groups, resolved_approvals))
        if aborted:
            await _emit_to_subscribers(
                ctx,
                key,
                "sessions.changed",
                build_sessions_changed_payload(
                    key,
                    "task_terminal",
                    run_status="cancelled",
                    last_task={
                        "status": "cancelled",
                        "terminal_reason": "user_abort",
                    },
                ),
            )
        return {
            "aborted": aborted,
            "key": key,
            "cancelled_tasks": cancelled_tasks,
            "cancelled_sessions": len(cancelled_session_keys),
        }

    # Cancel running agent task via registry
    registry = get_agent_task_registry()
    task = registry.get(key)
    cancelled = registry.cancel(key)

    if (
        cancelled
        and task is not None
        and not getattr(task, "_opensquilla_started", True)
        and not getattr(task, "_opensquilla_terminal_emitted", False)
    ):
        setattr(task, "_opensquilla_terminal_emitted", True)
        await _emit_to_subscribers(ctx, key, "session.event.done", {"reason": "aborted"})

    return {"aborted": cancelled, "key": key}


@_d.method("sessions.patch", scope="operator.admin")
async def _handle_sessions_patch(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")

    update_values: dict[str, Any] = {}
    assert isinstance(params, dict)
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


@_d.method("sessions.reset", scope="operator.write")
async def _handle_sessions_reset(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Synchronous session reset with FlushReceipt.

    Sequence when ``ctx.flush_service`` is wired:
    1. Drain any in-flight turn task so the per-session lock is free.
    2. Acquire the per-session lock for the whole snapshot → flush → rotate
       window (prevents a late turn write after flush).
    3. Snapshot the transcript, execute the flush, then rotate via
       ``apply_intent(RESET_SAME_KEY)``.

    When ``ctx.flush_service`` is None (kill-switch path), falls back to
    PR2-pre behavior: no flush, no ``flush_receipt`` field in the response.
    """
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.memory.session_flush import FlushReceipt
    from opensquilla.session.models import SessionIntent

    key = _require_key(params)

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    task_runtime = getattr(ctx, "task_runtime", None)
    # Drain MUST run before any branch that clears session state — including the
    # flush_service=None (kill-switch) path.  Skipping drain here would let a
    # still-running turn write its final message into the transcript *after*
    # apply_intent has rotated the session_id, producing an orphaned transcript
    # entry that is never flushed and never visible to the new session.
    # force=True does not bypass this: the operator wants a clean slate, not a
    # corrupted one.  drain() is idempotent when no task is running.
    if task_runtime is not None:
        await _drain_task_runtime_for_reset(task_runtime, key)

    force = bool((params or {}).get("force", False))

    registry = get_agent_task_registry()
    active = registry.get(key)
    if active is not None and not active.done():
        registry.cancel(key)
        try:
            await asyncio.wait_for(active, timeout=2.0)
        except TimeoutError:
            log.warning("sessions.reset.drain_timeout", session_key=key)
        except asyncio.CancelledError:
            log.debug("sessions.reset.drain_cancelled", session_key=key)
        except Exception as exc:  # noqa: BLE001
            log.warning("sessions.reset.drain_failed", session_key=key, error=str(exc))

    turn_runner = ctx.turn_runner
    lock = get_session_lock(turn_runner, key)

    async def _run_locked() -> dict[str, Any]:
        session = await storage.get_session(key)
        if session is None:
            raise KeyError(f"Session not found: {key}")
        previous_session_id = session.session_id
        previous_epoch = int(getattr(session, "epoch", 0) or 0)
        agent_id = normalize_agent_id(getattr(session, "agent_id", None) or "main")

        transcript = await ctx.session_manager.get_transcript(key)
        reset_flush_enabled = flush_trigger_enabled(ctx.config, "session_reset")

        if not reset_flush_enabled:
            updated, rotated = await ctx.session_manager.apply_intent(
                key,
                SessionIntent.RESET_SAME_KEY,
            )
            new_epoch = await _ensure_and_emit_reset_epoch(
                ctx, storage, key, previous_epoch=previous_epoch
            )
            return {
                "key": key,
                "reset": True,
                "rotated": rotated,
                "previous_session_id": previous_session_id,
                "session_id": updated.session_id,
                "epoch": new_epoch,
            }

        if ctx.flush_service is None:
            # Fail-closed when flush is unavailable: refuse to clear a non-empty
            # transcript without an explicit admin override or a covering
            # checkpoint receipt. The whole read -> gate -> rotate window stays
            # under the same per-session lock used by sends.
            if transcript and not force:
                checkpoint_safe = await _durable_receipt_allows_covered_destructive_compaction(
                    storage,
                    key,
                    previous_session_id,
                    transcript,
                )
                if not checkpoint_safe:
                    raise RpcHandlerError(
                        code="flush_unavailable",
                        message=(
                            "Reset aborted: flush service is unavailable and the "
                            "transcript is non-empty. Re-run with force=true (admin) "
                            "to discard without backup."
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
                    message="force=true on sessions.reset requires operator.admin scope.",
                    details={"key": key, "session_id": previous_session_id},
                )

            updated, rotated = await ctx.session_manager.apply_intent(
                key,
                SessionIntent.RESET_SAME_KEY,
            )
            new_epoch = await _ensure_and_emit_reset_epoch(
                ctx, storage, key, previous_epoch=previous_epoch
            )
            return {
                "key": key,
                "reset": True,
                "rotated": rotated,
                "previous_session_id": previous_session_id,
                "session_id": updated.session_id,
                "epoch": new_epoch,
            }

        if not transcript:
            updated, rotated = await ctx.session_manager.apply_intent(
                key, SessionIntent.RESET_SAME_KEY
            )
            new_epoch = await _ensure_and_emit_reset_epoch(
                ctx, storage, key, previous_epoch=previous_epoch
            )
            receipt = FlushReceipt(
                mode="skipped",
                flushed_paths=[],
                slug=None,
                message_count=0,
                duration_ms=0,
                raw_reason=None,
                error=None,
            )
            return _reset_response(
                key,
                rotated,
                previous_session_id,
                updated.session_id,
                receipt,
                new_epoch,
            )

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
            if (
                flush_correlation is not None
                and _accepts_keyword_arg(
                    ctx.flush_service.execute,
                    "provider_request_correlation",
                )
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
                code="flush_disk_error",
                message=f"Reset aborted: flush failed ({receipt.error})",
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
            transcript,
        )
        memory_status = compaction_memory_status(
            receipt,
            deterministic_receipt_safe=durable_receipt_safe,
            required=True,
        )
        if not memory_status.allows_destructive_compaction:
            flush_status = flush_receipt_status_for_compaction(receipt, ctx.config)
            raise RpcHandlerError(
                code="flush_disk_error",
                message=(
                    f"Reset aborted: flush status {flush_status!r} is not sufficient "
                    "for destructive reset."
                ),
                details={
                    "flush_receipt": receipt.to_dict(),
                    "key": key,
                    "session_id": previous_session_id,
                    "reason": "destructive_reset_requires_safe_flush",
                    "flush_receipt_status": flush_status,
                    "memory_safety_status": memory_status.safety_status,
                    "semantic_memory_status": memory_status.semantic_status,
                },
            )

        updated, rotated = await ctx.session_manager.apply_intent(key, SessionIntent.RESET_SAME_KEY)
        new_epoch = await _ensure_and_emit_reset_epoch(
            ctx, storage, key, previous_epoch=previous_epoch
        )
        return _reset_response(
            key,
            rotated,
            previous_session_id,
            updated.session_id,
            receipt,
            new_epoch,
        )

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


async def _ensure_and_emit_reset_epoch(
    ctx: RpcContext,
    storage: Any,
    session_key: str,
    *,
    previous_epoch: int,
) -> int:
    """Broadcast the manager's reset epoch, incrementing only as a fallback.

    ``SessionManager._rotate_session_id`` normally increments before rotating
    to fence stale writers. Older/test managers may not, and the manager keeps
    reset best-effort if that increment fails, so this RPC performs one durable
    increment only when the stored epoch did not advance.
    """
    increment_fn = getattr(storage, "increment_epoch", None)
    if not callable(increment_fn):
        return 0
    new_epoch = previous_epoch
    get_session = getattr(storage, "get_session", None)
    if callable(get_session):
        try:
            current = await get_session(session_key)
            new_epoch = int(getattr(current, "epoch", previous_epoch) or 0)
        except Exception:
            new_epoch = previous_epoch
    try:
        if new_epoch <= previous_epoch:
            # Durable commit happens inside increment_epoch before it returns.
            new_epoch = int(await increment_fn(session_key))
    except Exception:
        log.warning("sessions.reset.epoch_increment_failed", session_key=session_key)
        return 0
    # Invalidate / update the in-process epoch cache so subsequent _emit_to_subscribers
    # calls read the new epoch without hitting the DB.
    session_manager = getattr(ctx, "session_manager", None)
    set_session_epoch(session_manager, session_key, new_epoch)
    # Emit after the storage commit — failure here is non-fatal; epoch is already
    # persisted and the client will re-sync on next reconnect.
    try:
        await _emit_to_subscribers(
            ctx,
            session_key,
            "session.epoch_changed",
            {"key": session_key, "epoch": new_epoch},
        )
    except Exception:
        log.warning(
            "sessions.reset.epoch_emit_failed",
            session_key=session_key,
            new_epoch=new_epoch,
        )
    return new_epoch


def _reset_response(
    key: str,
    rotated: bool,
    previous_session_id: str,
    session_id: str,
    receipt: Any,
    epoch: int = 0,
) -> dict[str, Any]:
    return {
        "key": key,
        "reset": True,
        "rotated": rotated,
        "previous_session_id": previous_session_id,
        "session_id": session_id,
        "epoch": epoch,
        "flush_receipt": flush_receipt_to_dict(receipt),
    }


@_d.method("sessions.delete", scope="operator.write")
async def _handle_sessions_delete(params: dict | None, ctx: RpcContext) -> dict:
    """Delete one or more sessions. Accepts {key} for single or {keys} for bulk."""
    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    # Support both single key and bulk keys
    keys: list[str] = []
    if isinstance(params, dict):
        if "keys" in params:
            keys = params["keys"]
        elif "key" in params:
            keys = [params["key"]]

    if not keys:
        raise ValueError("params.key or params.keys is required")

    deleted: list[str] = []
    errors: list[str] = []
    for k in keys:
        try:
            canonical_key = canonicalize_session_key(k)
            lock = get_session_lock(ctx.turn_runner, canonical_key)
            if lock is None:
                await storage.delete_session(canonical_key)
            else:
                async with lock:
                    await storage.delete_session(canonical_key)
            deleted.append(k)
        except Exception as exc:
            errors.append(f"{k}: {exc}")

    return {"deleted": deleted, "errors": errors}


@_d.method("sessions.contextCompact", scope="operator.write")
async def _handle_sessions_context_compact(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)
    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    context_window_tokens = _context_window_tokens(params, ctx)
    custom_instructions = (params or {}).get("instructions")
    if custom_instructions is not None and not isinstance(custom_instructions, str):
        raise RpcHandlerError(
            code="INVALID_PARAMS",
            message="instructions must be a string when provided.",
            details={"field": "instructions"},
        )
    turn_runner = ctx.turn_runner
    lock = get_session_lock(turn_runner, key)

    async def _publish_manual_compaction_event(**payload: Any) -> None:
        status = str(payload.get("status") or "")
        reason = payload.get("reason") or payload.get("skip_reason")
        event_payload = {
            "source": "manual",
            "phase": "manual",
            "context_window_tokens": context_window_tokens,
            **compaction_effect_payload(
                status=status,
                source="manual",
                reason=str(reason) if reason is not None else None,
                user_visible=True,
            ),
            **payload,
        }
        notify_compaction(key, notify_listeners=False, **event_payload)
        await _emit_to_subscribers(
            ctx,
            key,
            "session.event.compaction",
            dict(event_payload),
        )

    async def _run_locked() -> dict[str, Any]:
        receipt = None
        flush_receipt_status: str | None = None
        compaction_id = new_compaction_id()
        storage = get_session_storage(ctx.session_manager)
        session = None
        if storage is not None:
            session = await storage.get_session(key)
            if session is None:
                if _is_ephemeral_webchat_session_key(key):
                    await _publish_manual_compaction_event(
                        status="started",
                        **compaction_lifecycle_payload(
                            compaction_id,
                            COMPACTION_TRIGGERED_EVENT,
                        ),
                    )
                    await _publish_manual_compaction_event(
                        status="skipped",
                        reason="empty_ephemeral_webchat_session",
                        **compaction_lifecycle_payload(
                            compaction_id,
                            COMPACTION_TRIGGERED_EVENT,
                        ),
                    )
                    return {
                        "key": key,
                        "compacted": False,
                        "status": "skipped",
                        "reason": "empty_ephemeral_webchat_session",
                        "skip_reason": "empty_ephemeral_webchat_session",
                        "applied": False,
                        "durability": "none",
                        "user_visible": True,
                        "mode": "summary",
                        "summary_len": 0,
                        "summary_source": "none",
                        "context_window_tokens": context_window_tokens,
                        "tokens_before": 0,
                        "tokens_after": 0,
                        "remaining_budget_tokens": context_window_tokens,
                        "removed_count": 0,
                        "kept_count": 0,
                        "chunk_count": 0,
                        "coverage_status": "unknown",
                        "missing_obligation_count": 0,
                        "critical_carry_forward_count": 0,
                        "state_kind": "text",
                    }
                raise KeyError(f"Session not found: {key}")
        elif hasattr(ctx.session_manager, "get_session"):
            session = await ctx.session_manager.get_session(key)
            if session is None:
                raise KeyError(f"Session not found: {key}")
        durable_session_id = getattr(session, "session_id", None)
        compaction_correlation = (
            ProviderRequestCorrelation(
                session_id=durable_session_id,
                turn_id=compaction_id,
                execution_id=uuid.uuid4().hex,
                call_kind="auxiliary.compaction",
            )
            if isinstance(durable_session_id, str)
            and durable_session_id
            and not provider_request_correlation_disabled(config=ctx.config)
            else None
        )
        flush_correlation = derive_provider_request_correlation(
            compaction_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.session_flush",
        )
        await _publish_manual_compaction_event(
            status="started",
            **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
        )
        transcript = []
        flush_enabled = flush_trigger_enabled(ctx.config, "manual")
        try:
            if flush_enabled:
                get_transcript = getattr(ctx.session_manager, "get_transcript", None)
                if not callable(get_transcript):
                    log.warning(
                        "sessions.context_compact.flush_skipped",
                        key=key,
                        reason="transcript_reader_unavailable",
                    )
                    flush_enabled = False
                else:
                    transcript = await get_transcript(key)

            if flush_enabled and transcript:
                if ctx.flush_service is None:
                    log.warning(
                        "sessions.context_compact.flush_skipped",
                        key=key,
                        reason="flush_service_unavailable",
                    )
                    flush_receipt_status = flush_receipt_status_for_compaction(
                        None,
                        ctx.config,
                    )
                else:
                    agent_id = normalize_agent_id(getattr(session, "agent_id", None) or "main")
                    memory_cfg = getattr(getattr(ctx, "config", None), "memory", None)
                    raw_timeout = getattr(
                        memory_cfg,
                        "flush_background_timeout_seconds",
                        120.0,
                    )
                    try:
                        flush_timeout = max(float(raw_timeout), 0.0)
                    except (TypeError, ValueError):
                        flush_timeout = 120.0
                    try:
                        flush_kwargs: dict[str, Any] = {
                            "agent_id": agent_id,
                            "timeout": flush_timeout,
                            "message_window": 0,
                            "segment_mode": "auto",
                            "raw_capture_policy": "required",
                            "turn_id": compaction_id,
                        }
                        if (
                            flush_correlation is not None
                            and _accepts_keyword_arg(
                                ctx.flush_service.execute,
                                "provider_request_correlation",
                            )
                        ):
                            flush_kwargs["provider_request_correlation"] = (
                                flush_correlation
                            )
                        receipt = await ctx.flush_service.execute(
                            transcript,
                            key,
                            **flush_kwargs,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "sessions.context_compact.flush_failed",
                            key=key,
                            error=str(exc),
                        )
                        flush_receipt_status = flush_receipt_status_for_compaction(
                            None,
                            ctx.config,
                        )
                    else:
                        flush_receipt_status = flush_receipt_status_for_compaction(
                            receipt,
                            ctx.config,
                        )
                        if not flush_receipt_is_successful_flush(receipt):
                            log.warning(
                                "sessions.context_compact.flush_degraded",
                                key=key,
                                flush_receipt_status=flush_receipt_status,
                                flush_receipt=flush_receipt_to_dict(receipt),
                            )
                        else:
                            log.info(
                                "sessions.context_compact.flush_done",
                                key=key,
                                flush_receipt_status=flush_receipt_status,
                                flush_receipt=flush_receipt_to_dict(receipt),
                            )

            if (
                flush_enabled
                and transcript
                and pre_compaction_flush_requires_safe_receipt(ctx.config)
            ):
                durable_receipt_safe = False
                if storage is not None:
                    durable_receipt_safe = (
                        await _durable_receipt_allows_covered_destructive_compaction(
                            storage,
                            key,
                            getattr(session, "session_id", None) if session else None,
                            transcript,
                        )
                    )
                memory_status = compaction_memory_status(
                    receipt,
                    deterministic_receipt_safe=durable_receipt_safe,
                    required=flush_enabled,
                )
                if not memory_status.allows_destructive_compaction:
                    raise RpcHandlerError(
                        code="CONTEXT_FLUSH_FAILED",
                        message=(
                            "Manual compaction aborted: flush receipt is not sufficient "
                            "for destructive compaction."
                        ),
                        details={
                            "flush_receipt": flush_receipt_to_dict(receipt),
                            "key": key,
                            "session_id": getattr(session, "session_id", None),
                            "reason": "destructive_manual_compact_requires_safe_flush",
                            "flush_receipt_status": flush_receipt_status,
                            "memory_safety_status": memory_status.safety_status,
                            "semantic_memory_status": memory_status.semantic_status,
                        },
                    )

            compaction_config = build_compaction_config_from_provider(
                _resolve_compaction_provider(ctx, session),
                model_override=_effective_compaction_model(session),
                compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
            )

            chunk_count = 0
            coverage_status = "unknown"
            missing_obligation_count = 0
            critical_carry_forward_count = 0
            state_kind = "text"
            quality_report: dict[str, Any] = {}
            skip_reason = ""
            compact_with_result = getattr(ctx.session_manager, "compact_with_result", None)
            if callable(compact_with_result):
                compact_kwargs: dict[str, Any] = {
                    "custom_instructions": custom_instructions,
                }
                if _accepts_keyword_arg(compact_with_result, "compaction_id"):
                    compact_kwargs["compaction_id"] = compaction_id
                if _accepts_keyword_arg(compact_with_result, "trigger_reason"):
                    compact_kwargs["trigger_reason"] = "manual"
                if flush_receipt_status is not None and _accepts_keyword_arg(
                    compact_with_result, "flush_receipt_status"
                ):
                    compact_kwargs["flush_receipt_status"] = flush_receipt_status
                if (
                    compaction_correlation is not None
                    and _accepts_keyword_arg(
                        compact_with_result,
                        "provider_request_correlation",
                    )
                ):
                    compact_kwargs["provider_request_correlation"] = (
                        compaction_correlation
                    )
                result = await compact_with_result(
                    key,
                    context_window_tokens,
                    compaction_config,
                    **compact_kwargs,
                )
                summary = getattr(result, "summary", "") or ""
                removed_count = int(getattr(result, "removed_count", 0) or 0)
                summary_source = getattr(result, "summary_source", "unknown") or "unknown"
                kept_count = len(getattr(result, "kept_entries", []) or [])
                tokens_before = int(getattr(result, "tokens_before", 0) or 0)
                tokens_after = int(getattr(result, "tokens_after", 0) or 0)
                remaining_budget_tokens = int(getattr(result, "remaining_budget_tokens", 0) or 0)
                chunk_count = int(getattr(result, "chunks_processed", 0) or 0)
                coverage_status = str(getattr(result, "coverage_status", "unknown") or "unknown")
                skip_reason = str(getattr(result, "skip_reason", "") or "")
                missing_obligation_count = len(getattr(result, "missing_obligations", None) or [])
                critical_carry_forward_count = len(
                    getattr(result, "critical_carry_forward", None) or []
                )
                state_kind = str(getattr(result, "summary_format", "text") or "text")
                quality_report = dict(getattr(result, "quality_report", None) or {})
                if removed_count > 0 and summary:
                    for event in (
                        COMPACTION_CHUNK_SUMMARIZED_EVENT,
                        COMPACTION_SUMMARY_VERIFIED_EVENT,
                    ):
                        observed_payload = compaction_lifecycle_payload(compaction_id, event)
                        observed_payload.update(compaction_result_payload(result))
                        await _publish_manual_compaction_event(
                            status="observed",
                            **observed_payload,
                        )
            else:
                compact = ctx.session_manager.compact
                summary = await call_compact_with_optional_config(
                    compact,
                    key,
                    context_window_tokens,
                    compaction_config,
                    provider_request_correlation=compaction_correlation,
                )
                removed_count = 1 if summary else 0
                summary_source = "unknown"
                skip_reason = "" if summary else "empty_summary"
                kept_count = 0
                tokens_before = 0
                tokens_after = 0
                remaining_budget_tokens = 0
        except asyncio.CancelledError:
            await _publish_manual_compaction_event(
                status="cancelled",
                message="Compaction was cancelled.",
                **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
            )
            raise
        except Exception as exc:
            await _publish_manual_compaction_event(
                status="failed",
                message=str(exc),
                **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
            )
            raise
        payload = {
            "key": key,
            "compacted": removed_count > 0,
            "applied": removed_count > 0,
            "durability": "durable" if removed_count > 0 else "none",
            "user_visible": True,
            "mode": "summary",
            "summary_len": len(summary),
            "summary_source": summary_source,
            "context_window_tokens": context_window_tokens,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "remaining_budget_tokens": remaining_budget_tokens,
            "removed_count": removed_count,
            "kept_count": kept_count,
            "chunk_count": chunk_count,
            "coverage_status": coverage_status,
            "missing_obligation_count": missing_obligation_count,
            "critical_carry_forward_count": critical_carry_forward_count,
            "state_kind": state_kind,
        }
        if quality_report:
            payload["quality_report"] = quality_report
        if not removed_count:
            payload["skip_reason"] = skip_reason or "empty_summary"
            payload["reason"] = payload["skip_reason"]
        if receipt is not None:
            payload["flush_receipt"] = flush_receipt_to_dict(receipt)
        if flush_receipt_status is not None:
            payload["flush_receipt_status"] = flush_receipt_status
        final_event = (
            COMPACTION_PERSISTED_EVENT if removed_count > 0 else COMPACTION_TRIGGERED_EVENT
        )
        final_lifecycle_payload = compaction_lifecycle_payload(compaction_id, final_event)
        final_lifecycle_payload.pop("coverage_status", None)
        final_status = "completed" if removed_count > 0 else "skipped"
        final_payload: dict[str, Any] = {}
        if removed_count <= 0:
            final_payload["reason"] = skip_reason or "empty_summary"
        await _publish_manual_compaction_event(
            status=final_status,
            **final_payload,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            remaining_budget_tokens=remaining_budget_tokens,
            removed_count=removed_count,
            kept_count=kept_count,
            chunk_count=chunk_count,
            coverage_status=coverage_status,
            missing_obligation_count=missing_obligation_count,
            critical_carry_forward_count=critical_carry_forward_count,
            state_kind=state_kind,
            quality_report=quality_report,
            summary_len=len(summary),
            summary_source=summary_source,
            flush_receipt_status=flush_receipt_status,
            **final_lifecycle_payload,
        )
        return payload

    async def _run_accounted() -> dict[str, Any]:
        from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
        from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

        usage_scope = await build_session_usage_scope(
            getattr(ctx, "usage_event_sink", None),
            ctx.session_manager,
            key,
            run_kind="session_compaction",
        )
        with bind_usage_accounting_scope(usage_scope):
            return await _run_locked()

    if lock is None:
        return await _run_accounted()
    async with lock:
        return await _run_accounted()


@_d.method("sessions.compact", scope="operator.write")
async def _handle_sessions_compact(params: dict | None, ctx: RpcContext) -> dict:
    return cast(dict, await _handle_sessions_context_compact(params, ctx))


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
                    if (
                        flush_correlation is not None
                        and _accepts_keyword_arg(
                            ctx.flush_service.execute,
                            "provider_request_correlation",
                        )
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


@_d.method("sessions.subscribe", scope="operator.read")
async def _handle_sessions_subscribe(params: dict | None, ctx: RpcContext) -> None:
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.subscribe_sessions(ctx.conn_id)
    return None


@_d.method("sessions.unsubscribe", scope="operator.read")
async def _handle_sessions_unsubscribe(params: dict | None, ctx: RpcContext) -> None:
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.unsubscribe_sessions(ctx.conn_id)
    return None


@_d.method("sessions.messages.subscribe", scope="operator.read")
async def _handle_sessions_messages_subscribe(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.subscribe_messages(ctx.conn_id, key)

    replay = get_session_streams().replay(key, _optional_stream_seq(params))
    replayed_count = 0
    if subscription_mgr is not None and replay.events:
        from opensquilla.gateway.websocket import get_registry

        conn = get_registry().get(ctx.conn_id)
        if conn is not None:
            for event in replay.events:
                await conn.send_event(
                    event.event_name,
                    event.payload,
                    meta={"replayed": True},
                )
                replayed_count += 1

    storage = get_session_storage(getattr(ctx, "session_manager", None))
    task_rows = await _list_task_rows(ctx, storage, key)
    task_state = _task_state_summary(task_rows)
    from opensquilla.gateway.subagent_announce import (
        active_background_completion_group_ids,
    )

    active_task_group_ids = await active_background_completion_group_ids(key)

    return {
        "subscribed": subscription_mgr is not None,
        "key": key,
        "current_stream_seq": replay.current_stream_seq,
        "replay_complete": replay.replay_complete,
        "replay_gap_reason": replay.gap_reason,
        "replayed_count": replayed_count,
        "active_task_group_ids": active_task_group_ids,
        **task_state,
    }


@_d.method("sessions.messages.unsubscribe", scope="operator.read")
async def _handle_sessions_messages_unsubscribe(params: dict | None, ctx: RpcContext) -> None:
    key = _require_key(params)
    subscription_mgr = getattr(ctx, "subscription_manager", None)
    if subscription_mgr is not None:
        subscription_mgr.unsubscribe_messages(ctx.conn_id, key)
    return None


@_d.method("sessions.preview", scope="operator.read")
async def _handle_sessions_preview(params: dict | None, ctx: RpcContext) -> dict:
    keys = (params or {}).get("keys")
    limit = (params or {}).get("limit", 50)
    now_ms = int(time.time() * 1000)

    if ctx.session_manager is None:
        return {"ts": now_ms, "previews": []}

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        return {"ts": now_ms, "previews": []}

    if keys:
        sessions = []
        for k in keys:
            s = await storage.get_session(k)
            if s is not None:
                sessions.append(s)
    else:
        sessions = await storage.list_sessions(limit=limit)

    previews = []
    for s in sessions:
        title = (
            getattr(s, "display_name", None)
            or getattr(s, "derived_title", None)
            or s.session_id[:8]
        )
        last_msg = ""
        try:
            transcript = await storage.get_transcript(s.session_id, limit=-1)
            if transcript:
                # Find the last user or assistant message for preview
                for entry in reversed(transcript):
                    if entry.role in ("user", "assistant") and entry.content:
                        last_msg = entry.content[:120]
                        break
        except Exception:
            pass
        previews.append(
            {
                "key": s.session_key,
                "title": title,
                "lastMessage": last_msg,
                "updatedAt": getattr(s, "updated_at", now_ms),
            }
        )

    return {"ts": now_ms, "previews": previews}


@_d.method("sessions.resolve", scope="operator.read")
async def _handle_sessions_resolve(params: dict | None, ctx: RpcContext) -> dict:
    key = _require_key(params)

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise KeyError("No session storage available")

    session = await _resolve_session_node(storage, key)

    return {
        "session_key": session.session_key,
        "session_id": session.session_id,
        "status": session.status,
        "agent_id": session.agent_id,
        "model": getattr(session, "model", None),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


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

    session = await _resolve_session_node(storage, key)
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

    # Local import avoids making rpc_chat/rpc_sessions module registration
    # order part of the public RPC contract.
    from opensquilla.gateway.rpc_chat import _handle_chat_history

    history = await _handle_chat_history(history_params, ctx)
    task_rows = await _list_task_rows(ctx, storage, session_key)
    task_state = _task_state_summary(task_rows)
    epoch = await _bootstrap_epoch(ctx.session_manager, storage, session, session_key)
    queued_count = sum(
        1 for row in task_rows if _enum_value(getattr(row, "status", None)) == "queued"
    )
    running_count = sum(
        1 for row in task_rows if _enum_value(getattr(row, "status", None)) == "running"
    )
    agent_id = _effective_agent_id_for_session(session, session_key)
    agent_identity = await _bootstrap_agent_identity(ctx, agent_id)
    effective_model = _session_turn_model(ctx, session, agent_id)
    from opensquilla.agents.scope import resolve_agent_workspace_dir

    workspace_path = resolve_agent_workspace_dir(agent_id, ctx.config)
    workspace = str(workspace_path) if workspace_path is not None else None
    from opensquilla.gateway.model_routing import model_routing_snapshot

    metadata = {
        "session_key": session_key,
        "session_id": session.session_id,
        "status": session.status,
        "agent_id": session.agent_id,
        "model": getattr(session, "model", None),
        "effective_model": effective_model,
        "workspace": workspace,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "display_name": getattr(session, "display_name", None),
        "queue_mode": getattr(session, "queue_mode", None),
        **_derive_source_metadata(session),
    }
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
            "model_routing": model_routing_snapshot(ctx.config),
        },
        "epoch": epoch,
        "stream_cursor": stream_cursor,
    }
