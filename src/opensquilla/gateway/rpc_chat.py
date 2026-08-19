"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import structlog

from opensquilla.artifact_session import (
    ArtifactSessionService,
    MutationAttempt,
    MutationAttemptStatus,
    document_mutation_outcome_from_attempt,
)
from opensquilla.chat.conversation import ChatSendRequest, sessions_send_params
from opensquilla.chat.flattened_tool_markers import (
    has_flattened_used_tool_line,
    is_flattened_tool_result_dump,
)
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.chat.source import chat_source_metadata
from opensquilla.gateway.compaction_target import (
    effective_session_model,
    resolve_gateway_compaction_target,
    resolve_selected_compaction_provider,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.context_overflow import apply_context_overflow_policy
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.terminal_activity import (
    is_usage_accounting_barrier,
    safe_primary_user_message_id,
    safe_retry_after_ms,
    terminal_activity_snapshot,
    usage_barrier_replay_proof,
)
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import build_compaction_config_from_provider
from opensquilla.session.compaction_lifecycle import new_compaction_id
from opensquilla.session.keys import build_webchat_key, canonicalize_session_key, parse_agent_id
from opensquilla.session.storage import (
    StorageBusyError,
    bounded_interactive_storage_reads,
)
from opensquilla.session.terminal_reply import build_terminal_reply
from opensquilla.turn_outcome_projection import (
    extract_fork_terminal_outcome_projection,
    terminal_turn_outcome,
    turn_id_from_context,
)

_d = get_dispatcher()
log = structlog.get_logger(__name__)

_WEBCHAT_SESSION_KEY = build_webchat_key()
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


def _clear_history_usage_for_indexes(
    projected: list[object],
    indexes: list[int],
) -> None:
    for index in indexes:
        entry = copy.copy(projected[index])
        setattr(entry, "turn_usage", None)
        projected[index] = entry


def _canonical_webchat_session_key(value: object = None) -> str:
    """Map legacy WebChat defaults onto the canonical WebChat session."""
    raw = str(value or "").strip()
    if not raw or raw in {"default", "webchat:default", "unknown"}:
        return _WEBCHAT_SESSION_KEY
    if raw.startswith("sess-"):
        return f"agent:main:webchat:{raw[len('sess-') :]}"
    return canonicalize_session_key(raw)


def _requested_initial_collaboration_mode(params: dict[str, Any]) -> str | None:
    mode = params.get("collaborationMode")
    snake_mode = params.get("collaboration_mode")
    if mode is not None and snake_mode is not None and mode != snake_mode:
        raise ValueError("collaborationMode and collaboration_mode must match")
    if mode is None:
        mode = snake_mode
    if mode is None:
        return None
    if not isinstance(mode, str) or mode not in {"default", "plan"}:
        raise ValueError("collaborationMode must be default or plan")
    if params.get("intent") != "new_chat":
        raise ValueError("collaborationMode requires explicit new_chat intent")
    return mode


def _requested_initial_routing_mode(params: dict[str, Any]) -> str | None:
    """Read the first-turn-only durable model-routing selection."""

    mode = params.get("initialRoutingMode")
    snake_mode = params.get("initial_routing_mode")
    if mode is not None and snake_mode is not None and mode != snake_mode:
        raise ValueError("initialRoutingMode and initial_routing_mode must match")
    if mode is None:
        mode = snake_mode
    if mode is None:
        return None
    if not isinstance(mode, str) or mode not in {"direct", "router", "ensemble"}:
        raise ValueError("initialRoutingMode must be direct, router, or ensemble")
    if params.get("intent") != "new_chat":
        raise ValueError("initialRoutingMode requires explicit new_chat intent")
    return mode


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
            snapshot = terminal_activity_snapshot(
                details.get("activity_snapshot"),
                task_id=str(task_id or turn_id),
                turn_id=turn_id,
            )
            if snapshot is not None:
                projected["activity_snapshot"] = snapshot
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


def _chat_history_cursor_index(entries: list[object], cursor: object) -> int | None:
    raw = str(cursor or "").strip()
    if not raw:
        return None
    for idx, entry in enumerate(entries):
        if _chat_history_cursor(entry) == raw:
            return idx
    return None


def _chat_history_cursor_key(cursor: object) -> tuple[int, int] | None:
    raw = str(cursor or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, stable_id = raw.split("|", 1)
    try:
        return int(created_at), int(stable_id)
    except ValueError:
        return None


def _chat_history_page(
    entries: list[object],
    *,
    limit: int,
    before: object = None,
    after: object = None,
) -> tuple[list[object], bool]:
    if not entries:
        return [], False
    before_idx = _chat_history_cursor_index(entries, before)
    if before_idx is not None:
        end = before_idx
        start = max(0, end - limit)
        return entries[start:end], start > 0

    after_idx = _chat_history_cursor_index(entries, after)
    if after_idx is not None:
        start = min(len(entries), after_idx + 1)
        end = min(len(entries), start + limit)
        return entries[start:end], end < len(entries)

    if len(entries) <= limit:
        return entries, False
    return entries[-limit:], True


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


def _canonical_page_parts(page: object) -> tuple[list[object], bool, bool]:
    if isinstance(page, dict):
        entries = page.get("entries")
        has_more = page.get("has_more", False)
        canonical_complete = page.get("canonical_complete", True)
    elif isinstance(page, tuple):
        entries = page[0] if page else None
        has_more = page[1] if len(page) > 1 else False
        canonical_complete = page[2] if len(page) > 2 else True
    else:
        entries = getattr(page, "entries", None)
        has_more = getattr(page, "has_more", False)
        canonical_complete = getattr(page, "canonical_complete", True)
    if entries is None:
        raise TypeError("canonical transcript page is missing entries")
    return list(entries), bool(has_more), bool(canonical_complete)


async def _load_chat_history_page(
    mgr: object,
    session_key: str,
    *,
    limit: int,
    before: object = None,
    after: object = None,
    include_canonical: bool,
) -> tuple[list[object], bool, bool, bool]:
    if include_canonical:
        page_getter = getattr(mgr, "get_canonical_transcript_page", None)
        if callable(page_getter):
            try:
                page = await page_getter(
                    session_key,
                    limit=limit,
                    before=_chat_history_cursor_key(before),
                    after=_chat_history_cursor_key(after),
                )
                entries, has_more, canonical_complete = _canonical_page_parts(page)
                return entries, has_more, True, canonical_complete
            except StorageBusyError:
                raise
            except Exception:  # noqa: BLE001 - fall back to active transcript
                pass
        else:
            getter = getattr(mgr, "get_canonical_transcript", None)
            if callable(getter):
                try:
                    transcript = list(await getter(session_key))
                    entries, has_more = _chat_history_page(
                        transcript,
                        limit=limit,
                        before=before,
                        after=after,
                    )
                    return entries, has_more, True, True
                except StorageBusyError:
                    raise
                except Exception:  # noqa: BLE001 - fall back to active transcript
                    pass
    transcript_getter = getattr(mgr, "get_transcript", None)
    if not callable(transcript_getter):
        return [], False, False, False
    transcript = await transcript_getter(session_key)
    entries, has_more = _chat_history_page(
        list(transcript or []),
        limit=limit,
        before=before,
        after=after,
    )
    return entries, has_more, False, False


def _needs_legacy_tool_lookbehind(entry: object | None) -> bool:
    if entry is None or getattr(entry, "tool_call_id", None):
        return False
    role = str(getattr(entry, "role", "") or "").lower()
    content = str(getattr(entry, "content", "") or "")
    return role in {"tool", "user"} and is_flattened_tool_result_dump(content)


def _needs_legacy_tool_lookahead(entry: object | None) -> bool:
    if entry is None or getattr(entry, "tool_calls", None):
        return False
    role = str(getattr(entry, "role", "") or "").lower()
    content = str(getattr(entry, "content", "") or "")
    return role == "assistant" and has_flattened_used_tool_line(content)


async def _load_legacy_tool_projection_context(
    mgr: object,
    session_key: str,
    entries: list[object],
    *,
    canonical_available: bool,
) -> tuple[object | None, object | None]:
    """Load at most one adjacent row per page edge for legacy projection.

    The selected page remains the pagination/accounting unit. These bounded
    reads only provide enough context to recognize a marker/result pair split
    by a page boundary; neither row is added to the response page.
    """

    if not entries or not canonical_available:
        return None, None
    page_getter = getattr(mgr, "get_canonical_transcript_page", None)
    if not callable(page_getter):
        return None, None

    previous_entry = None
    next_entry = None
    oldest_cursor = _chat_history_cursor_key(_chat_history_cursor(entries[0]))
    newest_cursor = _chat_history_cursor_key(_chat_history_cursor(entries[-1]))
    if _needs_legacy_tool_lookbehind(entries[0]) and oldest_cursor is not None:
        try:
            page = await page_getter(
                session_key,
                limit=1,
                before=oldest_cursor,
                after=None,
            )
            candidates, _has_more, _complete = _canonical_page_parts(page)
        except StorageBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - optional read-time projection
            log.warning(
                "chat_history_legacy_projection_context_unavailable",
                edge="before",
                error_type=type(exc).__name__,
            )
            return None, None
        if candidates:
            candidate = candidates[-1]
            candidate_cursor = _chat_history_cursor_key(_chat_history_cursor(candidate))
            if candidate_cursor is not None and candidate_cursor < oldest_cursor:
                previous_entry = candidate

    if _needs_legacy_tool_lookahead(entries[-1]) and newest_cursor is not None:
        try:
            page = await page_getter(
                session_key,
                limit=1,
                before=None,
                after=newest_cursor,
            )
            candidates, _has_more, _complete = _canonical_page_parts(page)
        except StorageBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - optional read-time projection
            log.warning(
                "chat_history_legacy_projection_context_unavailable",
                edge="after",
                error_type=type(exc).__name__,
            )
            return None, None
        if candidates:
            candidate = candidates[0]
            candidate_cursor = _chat_history_cursor_key(_chat_history_cursor(candidate))
            if candidate_cursor is not None and candidate_cursor > newest_cursor:
                next_entry = candidate
    return previous_entry, next_entry


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
    page_cursor = _chat_history_cursor_key(_chat_history_cursor(entries[-1]))

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
            richest_score = _history_structural_richness(richest)
            for candidate in candidates[1:]:
                candidate_score = _history_structural_richness(candidate)
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


def _effective_compaction_model(session: object | None) -> str | None:
    return effective_session_model(session)


def _resolve_compaction_provider(ctx: RpcContext, session: object | None) -> object | None:
    return resolve_selected_compaction_provider(ctx, session)


async def _build_context_overflow_compaction_config(ctx: RpcContext, session_key: str):
    session = None
    storage = getattr(getattr(ctx, "session_manager", None), "_storage", None)
    if storage is not None:
        try:
            session = await storage.get_session(session_key)
        except Exception:  # noqa: BLE001
            session = None
    compaction_target = resolve_gateway_compaction_target(ctx, session)
    return build_compaction_config_from_provider(
        compaction_target.provider,
        model_override=compaction_target.model or _effective_compaction_model(session),
        compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
        compaction_plan=compaction_target.plan,
    )


async def _enforce_context_overflow(
    ctx: RpcContext,
    session_key: str,
    message: str,
    *,
    restricted_turn: bool = False,
) -> dict | None:
    """Apply the configured context-overflow policy before a turn runs.

    Returns a stable error envelope when the policy is REFUSE and the
    payload exceeds the budget; returns ``None`` for every other path
    (policy consults pass, HARD_TRUNCATE dropped some history in place,
    AUTO_SUMMARIZE kicked off a compaction). The caller short-circuits
    on a non-None return.
    """

    config = ctx.config if isinstance(ctx.config, GatewayConfig) else GatewayConfig()

    transcript: list = []
    if ctx.session_manager is not None:
        try:
            transcript = list(await ctx.session_manager.get_transcript(session_key))
        except Exception:  # noqa: BLE001 — missing transcript just means "no history"
            transcript = []

    # Per-session context-budget overrides are independent from runtime/request
    # timeout resolution, which happens in TurnRunner.
    # A session-scoped context_budget_tokens override is supported via
    # ctx.session_manager.get_config(session_key) if present.
    budget_override = None
    policy_override = None
    if ctx.session_manager is not None and hasattr(ctx.session_manager, "get_session_config"):
        try:
            session_cfg = await ctx.session_manager.get_session_config(session_key)
            if session_cfg is not None:
                budget_override = getattr(session_cfg, "context_budget_tokens", None)
                policy_override = getattr(session_cfg, "context_overflow_policy", None)
        except Exception:  # noqa: BLE001
            pass

    from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
    from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

    usage_scope = await build_session_usage_scope(
        getattr(ctx, "usage_event_sink", None),
        ctx.session_manager,
        session_key,
        run_kind="session_compaction",
    )
    root_operation_id = new_compaction_id()
    provider_request_correlation = None
    if not provider_request_correlation_disabled(config=config):
        try:
            session = await ctx.session_manager.get_session(session_key)
        except Exception:  # noqa: BLE001 - observability is best-effort
            session = None
        durable_session_id = getattr(session, "session_id", None)
        if isinstance(durable_session_id, str) and durable_session_id:
            provider_request_correlation = ProviderRequestCorrelation(
                session_id=durable_session_id,
                turn_id=root_operation_id,
                execution_id=uuid4().hex,
                call_kind="auxiliary.compaction",
            )
    with bind_usage_accounting_scope(usage_scope):
        outcome = await apply_context_overflow_policy(
            config=config,
            message=message,
            transcript=transcript,
            session_key=session_key,
            session_manager=ctx.session_manager,
            compaction_config=await _build_context_overflow_compaction_config(ctx, session_key),
            flush_service=getattr(ctx, "flush_service", None),
            compaction_marker=getattr(ctx, "turn_runner", None),
            policy_override=policy_override,
            budget_override=budget_override,
            provider_request_correlation=provider_request_correlation,
            root_operation_id=root_operation_id,
            restricted_turn=restricted_turn,
        )

    if outcome.refusal is not None:
        log.warning(
            "chat_send.context_overflow_refused",
            session_key=session_key,
            estimated_tokens=outcome.estimated_tokens,
            budget_tokens=outcome.budget_tokens,
        )
        return outcome.refusal

    if outcome.compacted_this_turn:
        marker = getattr(ctx, "turn_runner", None)
        mark = getattr(marker, "mark_compacted_this_turn", None)
        if callable(mark):
            mark(session_key)

    return None


@_d.method("chat.send", scope="operator.write")
async def _handle_chat_send(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")

    message = params["message"]
    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    agent_id = parse_agent_id(session_key)
    initial_collaboration_mode = _requested_initial_collaboration_mode(params)
    initial_routing_mode = _requested_initial_routing_mode(params)
    prompt_annotation_ids = params.get(
        "promptAnnotationIds",
        params.get("prompt_annotation_ids"),
    )
    document_context = params.get(
        "documentContext",
        params.get("document_context"),
    )
    if prompt_annotation_ids is not None:
        if not isinstance(prompt_annotation_ids, list):
            raise ValueError("params.promptAnnotationIds must be an array")
        if any(not isinstance(item, str) or not item.strip() for item in prompt_annotation_ids):
            raise ValueError("params.promptAnnotationIds must contain non-empty strings")
        prompt_annotation_ids = [item.strip() for item in prompt_annotation_ids]

    # Fresh-WebUI / smoke path: when no session manager is wired (webui
    # simulator, dispatcher-only boot), instant-accept without kicking off a
    # turn. This matches the roundtrip the WebUI observes on first paint
    # before the sessions engine is attached.
    if ctx.session_manager is None:
        if prompt_annotation_ids or document_context is not None:
            raise RpcUnavailableError("Artifact context requires durable session storage")
        if initial_collaboration_mode is not None or initial_routing_mode is not None:
            raise RpcUnavailableError(
                "Initial session controls require atomic turn acceptance"
            )
        return {"ok": True, "sessionKey": session_key, "instant_accept": True}

    mgr = _require_chat_session_manager(ctx)
    intent = params.get("intent")
    intent_was_provided = intent is not None
    requested_intent = intent
    if intent is None and (
        isinstance(params.get("workspaceId"), str) or isinstance(params.get("workspace_id"), str)
    ):
        # A project draft is always a first-turn request. Keeping this intent
        # stable on retries lets sessions.send consult the durable ingress
        # receipt before an already-created session can change the strategy.
        intent = "new_chat"

    # WebChat must accept the turn even when existing history is oversized.
    # Context shaping happens inside TurnRunner so it can produce a request-scoped
    # sendable view instead of making the RPC layer a terminal overflow gate.

    try:
        if intent != "new_chat":
            # Detect a draft without creating it yet. sessions.send folds the
            # session row into the same durable acceptance transaction as the
            # first message/task/receipt.
            storage = getattr(mgr, "storage", None) or getattr(mgr, "_storage", None)
            get_session = getattr(storage, "get_session", None)
            if callable(get_session):
                try:
                    if await get_session(session_key) is None:
                        intent = "new_chat"
                except Exception as exc:
                    raise RpcUnavailableError(f"Failed to inspect chat session: {exc}") from exc
            else:
                # Compatibility for minimal test/simulator managers that do
                # not expose storage: retain the historical initializer.
                try:
                    await mgr.get_or_create(
                        session_key=session_key,
                        agent_id=agent_id,
                        display_name="WebChat",
                    )
                except Exception as exc:
                    raise RpcUnavailableError(f"Failed to initialize chat session: {exc}") from exc

        from opensquilla.gateway.rpc_sessions import _handle_sessions_send

        incoming_source = params.get("_source")
        if not isinstance(incoming_source, dict):
            incoming_source = {}

        elevated_hint = incoming_source.get("elevated")
        run_mode_hint = incoming_source.get("runMode") or incoming_source.get("run_mode")
        attachments = params.get("attachments")
        extra: dict = {}
        for source_key, target_key in (
            ("noMemoryCapture", "noMemoryCapture"),
            ("no_memory_capture", "no_memory_capture"),
            ("inputProvenance", "inputProvenance"),
            ("input_provenance", "input_provenance"),
            ("inputProvenanceKind", "inputProvenanceKind"),
            ("input_provenance_kind", "input_provenance_kind"),
            ("provenance_kind", "provenance_kind"),
            ("runKind", "runKind"),
            ("run_kind", "run_kind"),
            ("queueMode", "queueMode"),
            ("queue_mode", "queue_mode"),
            ("forkBeforeMessageId", "forkBeforeMessageId"),
            ("fork_before_message_id", "fork_before_message_id"),
            ("clientRequestId", "clientRequestId"),
            ("client_request_id", "client_request_id"),
            ("clientMessageId", "clientMessageId"),
            ("client_message_id", "client_message_id"),
            ("surfaceId", "surfaceId"),
            ("surface_id", "surface_id"),
            ("workspaceId", "workspaceId"),
            ("workspace_id", "workspace_id"),
            ("promptAnnotationIds", "promptAnnotationIds"),
            ("prompt_annotation_ids", "promptAnnotationIds"),
            ("documentContext", "documentContext"),
            ("document_context", "documentContext"),
            ("initialRoutingMode", "initialRoutingMode"),
            ("initial_routing_mode", "initial_routing_mode"),
        ):
            if source_key in params:
                extra[target_key] = params[source_key]
        send_params = sessions_send_params(
            ChatSendRequest(
                session_key=session_key,
                message=message,
                attachments=attachments if isinstance(attachments, list) else [],
                display_text=params.get("displayText") if "displayText" in params else None,
                intent=cast(str, intent) if intent is not None else None,
                extra=extra,
            ),
            chat_source_metadata(
                caller_kind="web",
                channel_kind="webchat",
                channel_id=f"webchat:{session_key}",
                sender_id=ctx.principal.role,
                source_kind="webui",
                source_name="WebChat",
                elevated=elevated_hint if isinstance(elevated_hint, str) else None,
                run_mode=run_mode_hint if isinstance(run_mode_hint, str) else None,
            ),
        )
        # Keep the public handler params free of fingerprint-control fields.
        # The logical request fingerprint uses the caller's original intent,
        # while the actual send may use the internal ``continue`` ->
        # ``new_chat`` strategy to create a first session atomically.
        fingerprint_params = dict(send_params)
        if intent_was_provided:
            fingerprint_params["intent"] = requested_intent
        else:
            fingerprint_params.pop("intent", None)
        if initial_collaboration_mode is not None:
            # Both public spellings represent the same logical request. Keep
            # one canonical field in the durable idempotency fingerprint.
            fingerprint_params["initialCollaborationMode"] = initial_collaboration_mode
        if initial_routing_mode is not None:
            fingerprint_params["initialRoutingMode"] = initial_routing_mode
        if prompt_annotation_ids is not None:
            send_params["promptAnnotationIds"] = prompt_annotation_ids
            fingerprint_params["promptAnnotationIds"] = prompt_annotation_ids
        result = await _handle_sessions_send(
            send_params,
            ctx,
            fingerprint_params=fingerprint_params,
            initial_collaboration_mode=initial_collaboration_mode,
            initial_routing_mode=initial_routing_mode,
        )
        result_session_key = result.get("sessionKey") or result.get("key") or session_key
        return {"ok": True, "sessionKey": result_session_key, **result}
    except Exception:
        marker = getattr(ctx, "turn_runner", None)
        clear = getattr(marker, "clear_compacted_this_turn", None)
        if callable(clear):
            clear(session_key)
        raise


@_d.method("chat.abort", scope="operator.write")
async def _handle_chat_abort(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    # Fresh-WebUI / smoke path: abort always returns an ok envelope keyed by
    # sessionKey, regardless of whether a live task exists to cancel.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "aborted": False}
    _require_chat_session_manager(ctx)
    from opensquilla.gateway.rpc_sessions import _handle_sessions_abort

    abort_params = {
        "key": session_key,
        "source": raw_params.get("source") or "webui_abort",
    }
    task_id_present = "taskId" in raw_params or "task_id" in raw_params
    task_id = raw_params.get("taskId") or raw_params.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        abort_params["task_id"] = task_id.strip()
        # chat.abort task ids are always session-bound, even for clients that
        # predate the explicit scope marker.
        abort_params["scope"] = "task"
    elif (
        task_id_present
        or str(raw_params.get("scope") or "").strip().lower() == "task"
    ):
        abort_params["scope"] = "task"
    result = await _handle_sessions_abort(
        abort_params,
        ctx,
    )
    return {"sessionKey": session_key, **result}


@_d.method("chat.history", scope="operator.read")
async def _handle_chat_history(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
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

    async def _load_page() -> tuple[
        list[object],
        bool,
        bool,
        bool,
        object | None,
        object | None,
    ]:
        entries, has_more, canonical_available, canonical_complete = (
            await _load_chat_history_page(
                mgr,
                session_key,
                limit=limit,
                before=before,
                after=after,
                include_canonical=include_canonical,
            )
        )
        entries = await _project_missing_history_usage(mgr, session_key, entries)
        previous_entry, next_entry = await _load_legacy_tool_projection_context(
            mgr,
            session_key,
            entries,
            canonical_available=canonical_available,
        )
        return (
            entries,
            has_more,
            canonical_available,
            canonical_complete,
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


def _clarify_fields_to_text(fields: dict[str, object]) -> str:
    """Serialise a clarify-form submission into a ``key: value\\n`` reply.

    The synthetic message is fed back through ``chat.send`` so it
    traverses the regular meta-resolution pipeline:
      peek_awaiting → parse_clarify_reply (key:value mode) →
      try_claim_resume → DAG continues.

    Bools are rendered as ``true``/``false``; everything else uses
    Python's natural string representation. Empty / None values are
    skipped — they signal "optional field omitted".
    """
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


@_d.method("chat.clarify_submit", scope="operator.write")
async def _handle_chat_clarify_submit(params: dict | None, ctx: RpcContext) -> dict:
    """Accept a structured clarify-form submission from a Web UI surface.

    Params:
      ``sessionKey``  (str)  — same WebChat session that triggered the pause
      ``fields``      (dict) — ``{field_name: value}`` collected by the form
      ``run_id``      (str, optional) — awaiting run id for trace/log only;
                                          the awaiting branch in meta_resolution
                                          uses ``session_key`` for the CAS

    A request carrying ``request_id`` resolves the exact deferred tool call and
    continues its existing turn. Legacy Meta clarifications have no request id;
    those remain a cross-turn protocol and are fed through ``chat.send``.
    """
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, fields")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("params.fields must be a non-empty mapping")

    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    raw_request_id = params.get("request_id", params.get("requestId"))
    if raw_request_id is not None:
        request_id = str(raw_request_id).strip()
        if not request_id:
            raise ValueError("params.request_id must be a non-empty string")
        task_runtime = getattr(ctx, "task_runtime", None)
        resolve_user_input = getattr(task_runtime, "resolve_user_input", None)
        if not callable(resolve_user_input):
            raise RpcUnavailableError("Deferred user-input resolution is not available")
        result = await resolve_user_input(
            session_key=session_key,
            request_id=request_id,
            fields=fields,
        )
        log.info(
            "chat.clarify_submit.deferred",
            session_key=session_key,
            request_id=request_id,
            field_count=len(fields),
            replayed=bool(result.get("replayed")),
        )
        return {"sessionKey": session_key, **result}

    text = _clarify_fields_to_text(fields)

    run_id = params.get("run_id")
    log.info(
        "chat.clarify_submit.params",
        session_key=session_key,
        field_count=len(fields),
        run_id=run_id if isinstance(run_id, str) and run_id else None,
    )

    send_params: dict = {
        "message": text,
        "sessionKey": session_key,
        # meta_resolution's awaiting branch keys off session_key, not
        # intent — so we deliberately stay on the default "continue"
        # intent (SessionIntent enum rejects unknown values). The
        # provenance tag is the observability hook for distinguishing
        # form submits from typed replies downstream.
        "inputProvenance": {"kind": "clarify_form", "source": "webui"},
    }
    if isinstance(run_id, str) and run_id:
        send_params["_source"] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
            "channel_id": f"webchat:{session_key}",
            "source_kind": "webui",
            "source_name": "WebChat",
            "clarify_run_id": run_id,
        }
    result = await _handle_chat_send(send_params, ctx)
    return cast(dict, result)


@_d.method("chat.inject", scope="operator.admin")
async def _handle_chat_inject(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, role, content")
    for field in ("sessionKey", "role", "content"):
        if field not in params:
            raise ValueError(f"params.{field} is required")

    role = params["role"]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role}")

    session_key = _canonical_webchat_session_key(params["sessionKey"])

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = getattr(ctx.session_manager, "_storage", None)
    if storage is not None:
        existing = await storage.get_session(session_key)
        if existing is None:
            raise KeyError(f"Session not found: {session_key}")

    await ctx.session_manager.append_message(session_key, role=role, content=params["content"])
    return {"ok": True, "sessionKey": session_key}
