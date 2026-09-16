"""Shared v4 projection for session history reads.

Both chat.history and sessions.bootstrap use this adapter so pagination,
legacy projection, usage enrichment, and outcome recovery have one concrete
implementation without one RPC module importing another.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import structlog

from opensquilla.application.session_history import SessionHistoryQuery
from opensquilla.artifact_session import (
    ArtifactSessionService,
    MutationAttempt,
    MutationAttemptStatus,
    document_mutation_outcome_from_attempt,
)
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.gateway.adapters.session_history import (
    SessionHistoryStorageAdapter,
    parse_history_cursor,
)
from opensquilla.gateway.adapters.turn_admission import webchat_session_key
from opensquilla.gateway.rpc.registry import RpcContext, RpcUnavailableError
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.terminal_activity import (
    is_usage_accounting_barrier,
    safe_primary_user_message_id,
    safe_retry_after_ms,
    terminal_activity_snapshot,
    usage_barrier_replay_proof,
)
from opensquilla.session.storage import StorageBusyError, bounded_interactive_storage_reads
from opensquilla.session.terminal_reply import build_terminal_reply
from opensquilla.turn_outcome_projection import (
    extract_fork_terminal_outcome_projection,
    terminal_turn_outcome,
    turn_id_from_context,
)

log = structlog.get_logger(__name__)

_CHAT_HISTORY_DEFAULT_LIMIT = 50
_CHAT_HISTORY_MAX_LIMIT = 200
_CHAT_HISTORY_LOCK_BUDGET_SECONDS = 2.0
_CHAT_HISTORY_RETRY_AFTER_MS = 100

_TURN_USAGE_PROJECTION_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "total_tokens",
        "cost_usd",
        "billed_cost",
        "estimated_cost_component_usd",
        "cost_source",
        "missing_cost_entries",
        "coverage_status",
        "usage_unknown",
        "unknown_usage_events",
        "inputTokens",
        "outputTokens",
        "reasoningTokens",
        "cachedTokens",
        "cacheWriteTokens",
        "totalTokens",
        "costUsd",
        "billedCost",
        "estimatedCostComponentUsd",
        "costSource",
        "missingCostEntries",
        "coverageStatus",
        "usageUnknown",
        "unknownUsageEvents",
    }
)
_TURN_USAGE_PROJECTION_ALIASES = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "reasoning_tokens": "reasoningTokens",
    "cached_tokens": "cachedTokens",
    "cache_write_tokens": "cacheWriteTokens",
    "total_tokens": "totalTokens",
    "cost_usd": "costUsd",
    "billed_cost": "billedCost",
    "estimated_cost_component_usd": "estimatedCostComponentUsd",
    "cost_source": "costSource",
    "missing_cost_entries": "missingCostEntries",
    "coverage_status": "coverageStatus",
    "usage_unknown": "usageUnknown",
    "unknown_usage_events": "unknownUsageEvents",
}
_HISTORY_STRUCTURAL_RECEIPT_FIELDS = (
    ("model_usage_breakdown", "modelUsageBreakdown"),
    ("ensemble_trace", "ensembleTrace"),
    ("route_plan", "routePlan"),
)


def _history_structural_richness(value: object) -> tuple[int, int]:
    """Rank JSON-like structural receipts without interpreting their schema."""
    if isinstance(value, Mapping):
        nested = sum(_history_structural_richness(item)[0] for item in value.values())
        return nested + len(value), len(value)
    if isinstance(value, (list, tuple)):
        nested = sum(_history_structural_richness(item)[0] for item in value)
        return nested + len(value), len(value)
    if isinstance(value, str):
        return (1, len(value)) if value.strip() else (0, 0)
    return (1, 0) if value is not None else (0, 0)


def _history_route_plan_has_complete_snapshot(value: object) -> bool:
    """Whether a route-plan candidate carries a minimally complete v1 snapshot."""

    if not isinstance(value, Mapping):
        return False
    snapshot = value.get("router_tier_snapshot", value.get("routerTierSnapshot"))
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("version") != 1 or snapshot.get("request_kind") not in {
        "text",
        "image",
    }:
        return False
    tiers = snapshot.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return False
    seen: set[str] = set()
    for entry in tiers:
        if not isinstance(entry, Mapping):
            return False
        tier = str(entry.get("tier") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        execution_kind = entry.get("execution_kind")
        if (
            not tier
            or tier in seen
            or not model
            or execution_kind not in {"single_model", "ensemble"}
        ):
            return False
        seen.add(tier)
    return True


def _clear_history_usage_for_indexes(
    projected: list[object],
    indexes: list[int],
) -> None:
    for index in indexes:
        entry = copy.copy(projected[index])
        setattr(entry, "turn_usage", None)
        projected[index] = entry


def _require_chat_session_manager(ctx: RpcContext):
    if ctx.session_manager is None:
        raise RpcUnavailableError("Chat session manager not available")
    return ctx.session_manager


def _normalize_chat_history_limit(value: object) -> int:
    try:
        if isinstance(value, int):
            limit = value
        elif isinstance(value, str):
            limit = int(value)
        else:
            limit = _CHAT_HISTORY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _CHAT_HISTORY_DEFAULT_LIMIT
    return max(1, min(limit, _CHAT_HISTORY_MAX_LIMIT))


def _is_webchat_session_key(key: str) -> bool:
    parts = str(key or "").split(":")
    return (
        len(parts) >= 4
        and parts[0] == "agent"
        and bool(parts[1])
        and parts[2] == "webchat"
        and all(parts[3:])
    )


def _empty_chat_history_payload(limit: int) -> dict[str, Any]:
    return {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": limit,
        "canonical_available": False,
        # A missing WebChat key has an empty but complete transcript. Keep
        # canonical_available's compatibility meaning while distinguishing this
        # normal state from a temporary reader failure or lost legacy archive.
        "canonical_complete": True,
        "compaction_summaries": [],
        "turn_outcomes": [],
    }


def _chat_history_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


async def _chat_history_turn_outcomes(
    ctx: RpcContext,
    session_key: str,
    entries: list[object],
) -> list[dict[str, Any]]:
    """Return typed outcomes only for explicit turn ids present in this page."""

    entry_turns = [
        (entry, turn_id)
        for entry in entries
        if (turn_id := turn_id_from_context(getattr(entry, "turn_context", None)))
        is not None
    ]
    turn_ids = {turn_id for _entry, turn_id in entry_turns}
    if not turn_ids:
        return []

    outcomes_by_turn: dict[str, dict[str, Any]] = {}
    conflicting_projections: set[str] = set()
    for entry, turn_id in entry_turns:
        entry_session_id = getattr(entry, "session_id", None)
        entry_session_key = getattr(entry, "session_key", None)
        if (
            not isinstance(entry_session_id, str)
            or entry_session_key != session_key
            or turn_id in conflicting_projections
        ):
            continue
        projection = extract_fork_terminal_outcome_projection(
            getattr(entry, "turn_context", None),
            session_id=entry_session_id,
            session_key=session_key,
            turn_id=turn_id,
        )
        if projection is None:
            continue
        projection = dict(projection)
        projected_snapshot = projection.get("activity_snapshot")
        if projected_snapshot is not None:
            validated_snapshot = terminal_activity_snapshot(
                projected_snapshot,
                task_id=str(projection.get("task_id") or turn_id),
                turn_id=turn_id,
            )
            if validated_snapshot is None:
                projection.pop("activity_snapshot", None)
            else:
                projection["activity_snapshot"] = validated_snapshot
        previous = outcomes_by_turn.get(turn_id)
        if previous is not None and previous != projection:
            outcomes_by_turn.pop(turn_id, None)
            conflicting_projections.add(turn_id)
            continue
        outcomes_by_turn[turn_id] = projection

    def _sorted_outcomes() -> list[dict[str, Any]]:
        outcomes = list(outcomes_by_turn.values())
        outcomes.sort(
            key=lambda item: (
                int(item.get("started_at") or 0),
                str(item.get("task_id") or ""),
            )
        )
        return outcomes

    storage = get_session_storage(getattr(ctx, "session_manager", None))
    exact_tasks = getattr(storage, "get_agent_tasks_by_ids", None)
    get_task = getattr(storage, "get_agent_task", None)
    list_tasks = getattr(storage, "list_agent_tasks", None)
    rows: list[Any] = []
    try:
        if callable(exact_tasks):
            rows = await exact_tasks(sorted(turn_ids))
        elif callable(get_task):
            rows = [
                row
                for turn_id in sorted(turn_ids)
                if (row := await get_task(turn_id)) is not None
            ]
        elif callable(list_tasks):
            rows = await list_tasks(session_key=session_key)
    except Exception:  # noqa: BLE001 - history remains readable without outcomes.
        log.warning(
            "chat.history.turn_outcomes_failed",
            session_key=session_key,
            exc_info=True,
        )

    attempts: tuple[MutationAttempt, ...] = ()
    if storage is not None and callable(getattr(storage, "_write_transaction", None)):
        artifact_service: ArtifactSessionService | None = None
        try:
            artifact_service = await ArtifactSessionService.from_session_storage(storage)
            attempts = await artifact_service.list_mutation_attempts_by_turn_ids(
                session_key=session_key,
                turn_ids=sorted(turn_ids),
            )
        except Exception:  # noqa: BLE001 - transcript and task history remain readable.
            log.warning(
                "chat.history.document_mutation_outcomes_failed",
                session_key=session_key,
                exc_info=True,
            )
        finally:
            if artifact_service is not None:
                await artifact_service.close()

    attempts_by_turn_id = {attempt.turn_id: attempt for attempt in attempts}

    def with_ledger_facts(
        attempt: MutationAttempt,
        task_outcome: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = document_mutation_outcome_from_attempt(attempt)
        mutation_keys = (
            "documentMutationOutcome",
            "document_mutation_outcome",
            "documentMutation",
            "document_mutation",
        )
        prior = next(
            (
                task_outcome[key]
                for key in mutation_keys
                if isinstance(task_outcome.get(key), dict)
            ),
            None,
        )
        if isinstance(prior, dict):
            corrected = prior.get("corrected")
            if isinstance(corrected, bool):
                canonical["corrected"] = corrected
            proposal_attempts = prior.get("proposalAttempts")
            if (
                isinstance(proposal_attempts, int)
                and not isinstance(proposal_attempts, bool)
                and proposal_attempts >= 0
            ):
                canonical["proposalAttempts"] = proposal_attempts
        projected = {key: value for key, value in task_outcome.items() if key not in mutation_keys}
        projected["documentMutationOutcome"] = canonical
        return projected

    for row in rows:
        row_session_key = getattr(row, "session_key", None)
        if isinstance(row_session_key, str) and row_session_key != session_key:
            continue
        task_id = getattr(row, "task_id", None)
        details = getattr(row, "details", None)
        details = details if isinstance(details, dict) else {}
        turn_id = details.get("turn_id") or task_id
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            continue
        attempt = attempts_by_turn_id.pop(turn_id, None)
        status = getattr(row, "status", None)
        status = str(getattr(status, "value", status) or "")
        projected = outcomes_by_turn.get(turn_id)
        outcome = terminal_turn_outcome(status, details.get("turn_outcome"))
        if projected is None:
            if outcome is None:
                if attempt is None:
                    continue
                outcome = {
                    "kind": "unknown",
                    "reason": "mutation_ledger_with_nonterminal_task",
                }
            if attempt is not None:
                outcome = with_ledger_facts(attempt, outcome)
            projected = {
                "turn_id": turn_id,
                "task_id": task_id,
                "status": status,
                "started_at": getattr(row, "started_at", None),
                "finished_at": getattr(row, "finished_at", None),
                "outcome": outcome,
            }
            outcomes_by_turn[turn_id] = projected
        elif attempt is not None:
            existing_outcome = projected.get("outcome")
            projected["outcome"] = with_ledger_facts(
                attempt,
                existing_outcome if isinstance(existing_outcome, dict) else {},
            )
        accepted_routing = details.get("accepted_model_routing")
        if isinstance(accepted_routing, dict):
            accepted_mode = str(accepted_routing.get("effective_mode") or "").strip().lower()
            if accepted_mode in {"direct", "router", "ensemble"}:
                projected["accepted_routing_mode"] = accepted_mode
        snapshot = terminal_activity_snapshot(
            details.get("activity_snapshot"),
            task_id=str(task_id or turn_id),
            turn_id=turn_id,
        )
        if snapshot is not None:
            projected["activity_snapshot"] = snapshot
        error_class = getattr(row, "error_class", None)
        if is_usage_accounting_barrier(error_class):
            if outcome is None:
                outcome = terminal_turn_outcome(status, projected.get("outcome"))
            if outcome is None:
                continue
            replay_proof = usage_barrier_replay_proof(
                usage_call_index=details.get("usage_call_index"),
                no_prior_provider_dispatch=details.get(
                    "no_prior_provider_dispatch"
                ),
                replay_safe=details.get("replay_safe"),
            )
            projected["code"] = error_class
            projected["error_class"] = error_class
            projected["retryable"] = True
            projected.update(replay_proof)
            outcome.pop("user_message_id", None)
            outcome.pop("userMessageId", None)
            primary_user_message_id = safe_primary_user_message_id(
                details.get("persisted_user_message_id")
            )
            if primary_user_message_id is not None:
                projected["user_message_id"] = primary_user_message_id
                outcome["user_message_id"] = primary_user_message_id
            projected["terminal_message"] = build_terminal_reply(
                {
                    "status": status,
                    "terminal_reason": getattr(row, "terminal_reason", None),
                    "error_class": error_class,
                    "error_message": getattr(row, "error_message", None),
                    **replay_proof,
                }
            )
            retry_after_ms = safe_retry_after_ms(details.get("retry_after_ms"))
            if retry_after_ms is not None:
                projected["retry_after_ms"] = retry_after_ms
    for turn_id, attempt in attempts_by_turn_id.items():
        existing = outcomes_by_turn.get(turn_id)
        if existing is not None:
            existing_outcome = existing.get("outcome")
            existing["outcome"] = with_ledger_facts(
                attempt,
                existing_outcome if isinstance(existing_outcome, dict) else {},
            )
            continue
        # The durable side-effect fact remains useful after a crash even when
        # no task row survived. Keep the generic turn state explicitly unknown
        # instead of manufacturing a successful completion.
        outcomes_by_turn[turn_id] = {
            "turn_id": turn_id,
            "task_id": None,
            "status": "unknown",
            "started_at": attempt.created_at,
            "finished_at": (
                None if attempt.status is MutationAttemptStatus.RESERVED else attempt.updated_at
            ),
            "outcome": {
                "kind": "unknown",
                "reason": "mutation_ledger_without_task",
                "documentMutationOutcome": document_mutation_outcome_from_attempt(attempt),
            },
        }
    return _sorted_outcomes()


def _chat_history_cursor(entry: object | None) -> str | None:
    if entry is None:
        return None
    created_at = getattr(entry, "created_at", "")
    stable_id = getattr(entry, "id", None) or getattr(entry, "message_id", "")
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    return f"{created_at}|{stable_id}"


def _session_summary_to_chat_payload(summary: object) -> dict[str, Any]:
    return {
        "id": getattr(summary, "id", None),
        "compaction_id": getattr(summary, "compaction_id", None),
        "compaction_index": getattr(summary, "compaction_index", None),
        "trigger_reason": getattr(summary, "trigger_reason", None),
        "summary_text": getattr(summary, "summary_text", "") or "",
        "summary_format": getattr(summary, "summary_format", "") or "",
        "coverage_status": getattr(summary, "coverage_status", "") or "",
        "removed_count": getattr(summary, "removed_count", None),
        "kept_count": getattr(summary, "kept_count", None),
        "covered_through_id": getattr(summary, "covered_through_id", None),
        "created_at": getattr(summary, "created_at", None),
    }


def _annotate_transcript_attachment_downloads(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
) -> list[dict[str, Any]]:
    session_qs = quote(session_key, safe="")
    for msg in messages:
        attachments = msg.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            sha = attachment.get("sha256_ref")
            if not isinstance(sha, str) or not sha:
                continue
            if attachment.get("download_url"):
                continue
            name = str(attachment.get("name") or "attachment")
            mime = str(attachment.get("mime") or attachment.get("type") or "")
            attachment["download_url"] = (
                f"/api/v1/attachments/{quote(sha, safe='')}?sessionKey={session_qs}"
                f"&name={quote(name, safe='')}&mime={quote(mime, safe='')}"
            )
    return messages


async def _project_missing_history_usage(
    mgr: object,
    session_key: str,
    entries: list[object],
) -> list[object]:
    """Project ledger totals onto every historical assistant turn.

    Existing ``turn_usage`` is often only a partial receipt from the old
    publication path. Ledger totals are authoritative for numeric usage and
    coverage, while structural trace/breakdown/routing metadata stays intact.
    """

    indexes_by_turn: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if getattr(entry, "role", None) != "assistant":
            continue
        turn_id = turn_id_from_context(getattr(entry, "turn_context", None))
        if not turn_id:
            continue
        indexes_by_turn.setdefault(turn_id, []).append(index)
    if not indexes_by_turn:
        return entries

    storage = getattr(mgr, "storage", None)
    batch_project = getattr(storage, "get_turn_usage_projections", None)
    probe_continuation = getattr(storage, "get_turn_ids_continuing_after_cursor", None)
    get_session = getattr(mgr, "get_session", None)
    if not callable(batch_project) or not callable(get_session):
        return entries

    # A page is a contiguous keyset slice, so only rows after its last cursor
    # can hold a turn's terminal assistant row. Probing that suffix keeps the
    # newest page — the common read — from touching transcript rows at all.
    page_cursor = parse_history_cursor(_chat_history_cursor(entries[-1]))

    continuing: set[str] = set()
    try:
        session = await get_session(session_key)
        if session is None:
            return entries
        session_id = str(getattr(session, "session_id", "") or "")
        session_epoch = max(0, int(getattr(session, "epoch", 0) or 0))
        projections = await batch_project(
            session_id=session_id,
            session_epoch=session_epoch,
            turn_ids=list(indexes_by_turn),
        )
        if page_cursor is not None and callable(probe_continuation):
            created_at, entry_id = page_cursor
            continuing = set(
                await probe_continuation(
                    session_id=session_id,
                    created_at=created_at,
                    entry_id=entry_id,
                    turn_ids=list(indexes_by_turn),
                )
            )
    except Exception:  # noqa: BLE001 - usage fallback must not hide transcript history
        log.warning(
            "chat.history.usage_projection_failed",
            session_key=session_key,
            entry_count=len(entries),
            exc_info=True,
        )
        return entries
    if not projections:
        return entries

    projected = list(entries)
    for turn_id, indexes in indexes_by_turn.items():
        usage = projections.get(turn_id)
        if usage is None:
            continue
        if turn_id in continuing:
            # The terminal row sits on a later page, which will carry the whole
            # ledger total. Publishing it here too would bill the turn twice
            # once a client merges the pages.
            _clear_history_usage_for_indexes(projected, indexes)
            continue

        # Every row of this turn is inside the page, so its last row is the
        # terminal one. Damaged legacy history may still hold duplicates.
        index = indexes[-1]
        entry = copy.copy(projected[index])
        existing = getattr(entry, "turn_usage", None)
        existing_keys = set(existing) if isinstance(existing, dict) else set()
        if isinstance(existing, dict):
            merged_usage = dict(existing)
            for key in _TURN_USAGE_PROJECTION_FIELDS:
                if key in usage:
                    merged_usage[key] = usage[key]
                    alias = _TURN_USAGE_PROJECTION_ALIASES.get(key)
                    if alias is not None and alias in merged_usage:
                        merged_usage[alias] = usage[key]
        else:
            merged_usage = dict(usage)

        # Earlier duplicate assistant rows may carry only structural details
        # or a stale partial receipt. Move those non-accounting fields forward
        # and clear the old receipt, so one turn can never render two spends.
        structural_sources: list[dict[str, Any]] = []
        if isinstance(existing, dict):
            structural_sources.append(existing)
        duplicate_structural: dict[str, Any] = {}
        for duplicate_index in indexes[:-1]:
            duplicate = getattr(projected[duplicate_index], "turn_usage", None)
            if not isinstance(duplicate, dict):
                continue
            structural_sources.append(duplicate)
            for key, value in duplicate.items():
                if key in {"provider", "model"} and key not in duplicate_structural:
                    duplicate_structural[key] = copy.deepcopy(value)
                if key not in _TURN_USAGE_PROJECTION_FIELDS and key not in merged_usage:
                    merged_usage[key] = copy.deepcopy(value)
            duplicate_entry = copy.copy(projected[duplicate_index])
            setattr(duplicate_entry, "turn_usage", None)
            projected[duplicate_index] = duplicate_entry

        # A rebuilt continuation can publish a small terminal receipt after an
        # earlier row already persisted the complete ensemble structure. Keep
        # the richer structural receipt, while the numeric fields above remain
        # authoritative ledger projections. Write both aliases when history
        # contains both spellings so the chosen receipt is not split in two.
        for snake_key, camel_key in _HISTORY_STRUCTURAL_RECEIPT_FIELDS:
            candidates: list[object] = []
            present_keys: set[str] = set()
            for source in structural_sources:
                for key in (snake_key, camel_key):
                    if key in source:
                        candidates.append(source[key])
                        present_keys.add(key)
            if not candidates:
                continue
            richest = candidates[0]
            richest_score = (
                int(
                    snake_key == "route_plan"
                    and _history_route_plan_has_complete_snapshot(richest)
                ),
                *_history_structural_richness(richest),
            )
            for candidate in candidates[1:]:
                candidate_score = (
                    int(
                        snake_key == "route_plan"
                        and _history_route_plan_has_complete_snapshot(candidate)
                    ),
                    *_history_structural_richness(candidate),
                )
                if candidate_score > richest_score:
                    richest = candidate
                    richest_score = candidate_score
            for key in present_keys:
                merged_usage[key] = copy.deepcopy(richest)

        # Provider/model are useful when no historical row had them, but an
        # existing routed identity is structural metadata and must not be
        # replaced by the latest physical ledger leg.
        for key in ("provider", "model"):
            if key not in existing_keys and key in duplicate_structural:
                merged_usage[key] = duplicate_structural[key]
            if key not in merged_usage and key in usage:
                merged_usage[key] = usage[key]

        setattr(entry, "turn_usage", merged_usage)
        projected[index] = entry
    return projected


async def _chat_history_summaries(
    mgr: object,
    session_key: str,
    *,
    include_summaries: bool,
) -> list[dict[str, Any]]:
    """Return requested summaries without letting lock contention hide history."""

    if not include_summaries:
        return []
    getter = getattr(mgr, "get_summaries", None)
    if not callable(getter):
        return []
    try:
        with bounded_interactive_storage_reads():
            summaries = await getter(session_key)
    except StorageBusyError:
        # The message page is already available. Let callers retry the optional
        # summary metadata instead of converting a useful history response into
        # STORAGE_BUSY.
        return []
    except Exception:  # noqa: BLE001 - summaries remain optional display metadata
        return []
    return [_session_summary_to_chat_payload(summary) for summary in summaries or []]


async def read_chat_history_v4(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = webchat_session_key(raw_params.get("sessionKey"))
    limit = _normalize_chat_history_limit(raw_params.get("limit"))
    before = raw_params.get("before")
    after = raw_params.get("after")
    include_canonical = _chat_history_bool(
        raw_params.get("includeCanonical"),
        default=True,
    )
    include_summaries = _chat_history_bool(
        raw_params.get("includeSummaries"),
        default=True,
    )

    mgr = _require_chat_session_manager(ctx)
    history_adapter = SessionHistoryStorageAdapter(mgr)
    history_application = history_adapter.application()
    history_query = SessionHistoryQuery(
        session_key=session_key,
        limit=limit,
        before=parse_history_cursor(before),
        after=parse_history_cursor(after),
        include_canonical=include_canonical,
    )

    async def _load_page() -> tuple[
        list[object],
        bool,
        bool,
        bool,
        object | None,
        object | None,
    ]:
        page = await history_application.read_page(history_query)
        entries = list(page.entries)
        entries = await _project_missing_history_usage(mgr, session_key, entries)
        previous_entry, next_entry = await history_adapter.load_legacy_tool_projection_context(
            session_key,
            entries,
            canonical_available=page.canonical_available,
        )
        return (
            entries,
            page.has_more,
            page.canonical_available,
            page.canonical_complete,
            previous_entry,
            next_entry,
        )

    try:
        with bounded_interactive_storage_reads():
            history_lock = get_session_lock(ctx.turn_runner, session_key)
            if history_lock is None:
                (
                    page_entries,
                    has_more,
                    canonical_available,
                    canonical_complete,
                    previous_entry,
                    next_entry,
                ) = await _load_page()
            else:
                # Canonical reads and compaction rewrites share one aiosqlite
                # connection.  SQLite statements are snapshots, but a statement on
                # that same connection can still observe the connection's own
                # uncommitted archive/delete/reinsert work.  Use the short session
                # mutation lock so the page and its coverage metadata are read only
                # before or after a rewrite, never from its intermediate state.
                started = time.monotonic()
                acquired = False
                try:
                    try:
                        async with asyncio.timeout(_CHAT_HISTORY_LOCK_BUDGET_SECONDS):
                            await history_lock.acquire()
                    except TimeoutError as exc:
                        raise StorageBusyError(
                            "chat.history",
                            waited_ms=max(0, int((time.monotonic() - started) * 1000)),
                            retry_after_ms=_CHAT_HISTORY_RETRY_AFTER_MS,
                            stage="lock_acquire",
                            resource="session_mutation_lock",
                        ) from exc
                    acquired = True
                    (
                        page_entries,
                        has_more,
                        canonical_available,
                        canonical_complete,
                        previous_entry,
                        next_entry,
                    ) = await _load_page()
                finally:
                    if acquired:
                        history_lock.release()
    except KeyError:
        if _is_webchat_session_key(session_key):
            return _empty_chat_history_payload(limit)
        raise
    summaries = await _chat_history_summaries(
        mgr,
        session_key,
        include_summaries=include_summaries,
    )
    if summaries:
        history_scope = "compacted"
    elif has_more:
        history_scope = "latest_window"
    else:
        history_scope = "complete"

    messages = transcript_entries_to_chat_messages(
        page_entries,
        limit=None,
        previous_entry=previous_entry,
        next_entry=next_entry,
    )
    turn_outcomes = await _chat_history_turn_outcomes(
        ctx,
        session_key,
        page_entries,
    )
    return {
        "messages": _annotate_transcript_attachment_downloads(
            messages,
            session_key=session_key,
        ),
        "has_more": has_more,
        "oldest_cursor": _chat_history_cursor(page_entries[0]) if page_entries else None,
        "newest_cursor": _chat_history_cursor(page_entries[-1]) if page_entries else None,
        "history_scope": history_scope,
        "loaded_count": len(page_entries),
        "page_size": limit,
        "canonical_available": canonical_available,
        "canonical_complete": canonical_complete,
        "compaction_summaries": summaries,
        "turn_outcomes": turn_outcomes,
    }


__all__ = ["read_chat_history_v4"]
