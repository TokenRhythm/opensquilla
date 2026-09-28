"""Context-overflow policy enforcement.

Helpers consulted by the gateway's chat entry-point before the turn is
handed off to the engine. The policy layer is deliberately small and
synchronous where possible — it either:

* returns :data:`PROCEED_NORMALLY` → the caller continues as today, or
* returns :class:`OverflowOutcome` carrying an error envelope (for REFUSE)
  or bookkeeping counters (for HARD_TRUNCATE / AUTO_SUMMARIZE) that the
  caller can use to shape the downstream turn.

The three policies:

* ``auto_summarize`` — checkpoint the transcript, compact once, then proceed
  only if post-compaction token evidence proves the next call fits.
* ``hard_truncate`` — drop oldest transcript entries from the in-memory
  history list until the estimated token count is under budget. The
  caller uses the shortened list.
* ``refuse`` — short-circuit with a stable error envelope; the caller
  must not invoke the provider.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

import structlog

from opensquilla.engine.cache_break_monitor import notify_compaction
from opensquilla.gateway.config import ContextOverflowPolicy, GatewayConfig
from opensquilla.provider.model_catalog import resolve_effective_context_window, shared_catalog
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
)
from opensquilla.session.compaction import (
    CompactionConfig,
    arm_compaction_deadline,
    await_compaction_phase,
    call_compact_with_optional_config,
    estimate_entry_model_replay_tokens,
)
from opensquilla.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_PERSISTED_EVENT,
    COMPACTION_REPLAYED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    CompactionLifecycleResult,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    compaction_result_payload,
    durable_receipt_allows_destructive_compaction,
    new_compaction_id,
)
from opensquilla.session.context_view import build_compaction_context_records
from opensquilla.session.tokenizer import estimate_tokens

log = structlog.get_logger(__name__)


def _accepts_keyword_arg(func: Any, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    if name in signature.parameters:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
    )


@dataclass
class OverflowOutcome:
    """Result of applying a context-overflow policy for one turn."""

    policy: ContextOverflowPolicy
    over_budget: bool = False
    estimated_tokens: int = 0
    budget_tokens: int = 0
    # Only populated for REFUSE: stable error envelope shaped like the
    # tool-failure envelope so UI code has one rendering path.
    refusal: dict[str, Any] | None = None
    # Only populated for HARD_TRUNCATE: how many transcript entries were
    # dropped to fit under budget.
    truncated_entries: int = 0
    # Only populated for AUTO_SUMMARIZE: whether compaction was triggered.
    summarized: bool = False
    retried: bool = False
    reason: str | None = None
    tokens_after: int | None = None
    remaining_budget_tokens: int | None = None
    removed_count: int = 0
    kept_count: int = 0
    summary_len: int = 0
    summary_source: str = "unknown"
    lifecycle: CompactionLifecycleResult | None = None
    compacted_this_turn: bool = False
    # Possibly mutated history. HARD_TRUNCATE shortens this list in place.
    trimmed_history: list[Any] = field(default_factory=list)


def _estimate_payload_tokens(message: str, transcript: list[Any]) -> int:
    """Estimate the token cost of (history + new message).

    Uses the shared :func:`opensquilla.session.tokenizer.estimate_tokens` so
    the budget comparison is apples-to-apples with on-disk bookkeeping.
    """

    total = estimate_tokens(message or "")
    for entry in transcript or []:
        total += estimate_entry_model_replay_tokens(entry)
    return total


def _build_refusal_envelope(
    estimated: int,
    budget: int,
    reason: str = "context_overflow",
    *,
    error_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape the REFUSE error payload the way UI/tool-error callers expect."""

    error = {
        "code": "context_overflow",
        "reason": reason,
    }
    if error_details:
        error.update(error_details)
    return {
        "status": "error",
        "error_class": "context_overflow",
        "user_message": (
            "Your conversation is too long for the model. "
            "Please start a new session or remove some earlier messages."
        ),
        "retry_allowed": False,
        "estimated_tokens": estimated,
        "budget_tokens": budget,
        "reason": reason,
        "error": error,
    }


async def _estimate_session_payload_tokens(
    message: str,
    transcript: list[Any],
    *,
    session_manager: Any | None = None,
    session_key: str = "",
    fallback_summary: str = "",
) -> int:
    total = _estimate_payload_tokens(message, transcript)
    summaries: list[Any] = []
    get_summaries = getattr(session_manager, "get_summaries", None)
    if callable(get_summaries):
        try:
            summaries = await get_summaries(session_key)
        except Exception as exc:  # pragma: no cover - optional estimate input
            log.warning(
                "context_overflow.summary_estimate_failed",
                session_key=session_key,
                error=str(exc),
            )
            summaries = []

    context_states: list[Any] = []
    get_context_states = getattr(session_manager, "get_context_states", None)
    if callable(get_context_states):
        try:
            context_states = await get_context_states(session_key)
        except Exception as exc:  # pragma: no cover - optional estimate input
            log.warning(
                "context_overflow.context_state_estimate_failed",
                session_key=session_key,
                error=str(exc),
            )
            context_states = []

    if summaries or context_states:
        records = build_compaction_context_records(
            context_states=context_states,
            summaries=summaries,
        )
        if records:
            for record in records:
                total += estimate_tokens(record.text)
            return total

        if summaries:
            return total

    if fallback_summary:
        total += estimate_tokens(str(fallback_summary))
    return total


async def _record_checkpoint_before_compaction(
    session_manager: Any,
    session_key: str,
    transcript: list[Any],
    *,
    turn_id: str,
    source: str,
    compaction_config: CompactionConfig | None = None,
) -> bool:
    if not transcript:
        return False
    method = getattr(type(session_manager), "record_memory_checkpoint", None)
    if method is None:
        method = getattr(
            getattr(session_manager, "__dict__", {}),
            "get",
            lambda *_: None,
        )("record_memory_checkpoint")
    if not callable(method):
        return False
    checkpoint_method = session_manager.record_memory_checkpoint
    kwargs: dict[str, Any] = {"turn_id": turn_id, "source": source}
    if compaction_config is not None and _accepts_keyword_arg(
        checkpoint_method,
        "compaction_config",
    ):
        kwargs["compaction_config"] = compaction_config
    receipt = await checkpoint_method(session_key, list(transcript), **kwargs)
    if not durable_receipt_allows_destructive_compaction(receipt):
        raise RuntimeError("Memory checkpoint did not confirm a durable transcript backup")
    return True


# Envelope shape note:
# This UI-facing refusal shares the common tool-failure fields, but it
# intentionally carries overflow-specific metadata as extra keys.


async def apply_context_overflow_policy(
    *,
    config: GatewayConfig,
    message: str,
    transcript: list[Any],
    session_key: str,
    session_manager: Any | None = None,
    compaction_config: Any | None = None,
    compaction_marker: Any | None = None,
    policy_override: ContextOverflowPolicy | None = None,
    budget_override: int | None = None,
    provider_request_correlation: ProviderRequestCorrelation | None = None,
    root_operation_id: str | None = None,
) -> OverflowOutcome:
    """Apply the gateway's overflow policy to the upcoming turn.

    Parameters
    ----------
    config:
        The gateway config. Policy comes from ``context_overflow_policy``;
        absent an explicit budget override, provider/model metadata and
        ``llm.context_window_tokens`` supply the effective context window.
    message:
        The new user message.
    transcript:
        The existing session transcript (list of ``TranscriptEntry``-like
        objects with a ``content`` attribute).
    session_key:
        Used for logging and as the handle passed to
        ``session_manager.compact`` for the AUTO_SUMMARIZE branch.
    session_manager:
        Optional session manager used to run compaction when the policy
        is AUTO_SUMMARIZE. When None (e.g. in unit tests) the AUTO
        branch degrades to a best-effort "drop oldest, retry" proxy so
        the turn can still proceed.
    compaction_config:
        Optional provider-backed config passed through to
        ``session_manager.compact`` for AUTO_SUMMARIZE.
    policy_override / budget_override:
        Test + per-session knobs.

    Returns
    -------
    OverflowOutcome
        ``over_budget=False`` means the caller can proceed unchanged.
        For REFUSE, the caller must return ``outcome.refusal``; for
        HARD_TRUNCATE, the caller should use ``outcome.trimmed_history``
        instead of the original transcript.
    """

    policy = policy_override or config.context_overflow_policy
    if budget_override is not None:
        budget = budget_override
    else:
        budget, _ = resolve_effective_context_window(
            shared_catalog(),
            config.llm.model,
            provider=config.llm.provider,
            global_override=config.llm.context_window_tokens,
        )
    estimated = _estimate_payload_tokens(message, transcript)

    outcome = OverflowOutcome(
        policy=policy,
        estimated_tokens=estimated,
        budget_tokens=budget,
        trimmed_history=list(transcript or []),
    )

    if estimated <= budget:
        return outcome

    outcome.over_budget = True
    log.info(
        "context_overflow.triggered",
        session_key=session_key,
        policy=policy.value,
        estimated_tokens=estimated,
        budget_tokens=budget,
    )

    if policy == ContextOverflowPolicy.REFUSE:
        outcome.reason = "context_overflow"
        outcome.refusal = _build_refusal_envelope(estimated, budget, outcome.reason)
        return outcome

    if policy == ContextOverflowPolicy.HARD_TRUNCATE:
        # Drop oldest transcript entries until estimated tokens fit.
        trimmed = list(transcript or [])
        while trimmed and _estimate_payload_tokens(message, trimmed) > budget:
            trimmed.pop(0)
            outcome.truncated_entries += 1
        outcome.trimmed_history = trimmed
        log.info(
            "context_overflow.hard_truncate",
            session_key=session_key,
            dropped=outcome.truncated_entries,
            remaining=len(trimmed),
        )
        return outcome

    # ContextOverflowPolicy.AUTO_SUMMARIZE
    if session_manager is not None:
        checkpoint_failed = False
        compaction_id = root_operation_id or new_compaction_id()
        durable_commit_won = False
        committed_result = None
        try:
            marker_has = getattr(compaction_marker, "has_compacted_this_turn", None)
            if callable(marker_has) and marker_has(session_key):
                compacted_transcript = await session_manager.get_transcript(session_key)
                post_estimate = await _estimate_session_payload_tokens(
                    message,
                    compacted_transcript,
                    session_manager=session_manager,
                    session_key=session_key,
                )
                outcome.tokens_after = post_estimate
                outcome.remaining_budget_tokens = max(budget - post_estimate, 0)
                if post_estimate <= budget and post_estimate < estimated:
                    outcome.summarized = True
                    outcome.retried = True
                    return outcome
                outcome.reason = "compaction_insufficient"
                outcome.refusal = _build_refusal_envelope(post_estimate, budget, outcome.reason)
                return outcome

            effective_compaction_config = compaction_config or CompactionConfig()
            arm_compaction_deadline(
                effective_compaction_config,
                operation_id=compaction_id,
            )
            notify_compaction(
                session_key,
                source="automatic",
                phase="gateway_auto_summarize",
                status="started",
                tokens_before=estimated,
                context_window_tokens=budget,
                heartbeat_interval_seconds=(effective_compaction_config.heartbeat_interval_seconds),
                **compaction_effect_payload(status="started"),
                **compaction_lifecycle_payload(
                    compaction_id,
                    COMPACTION_TRIGGERED_EVENT,
                ),
            )
            try:
                await _record_checkpoint_before_compaction(
                    session_manager,
                    session_key,
                    list(transcript or []),
                    turn_id=compaction_id,
                    source="gateway_auto_summarize",
                    compaction_config=effective_compaction_config,
                )
            except Exception:
                checkpoint_failed = True
                raise
            compaction_result = None
            compact_with_result = getattr(session_manager, "compact_with_result", None)
            if callable(compact_with_result):
                compact_kwargs: dict[str, Any] = {}
                if _accepts_keyword_arg(compact_with_result, "compaction_id"):
                    compact_kwargs["compaction_id"] = compaction_id
                if _accepts_keyword_arg(compact_with_result, "trigger_reason"):
                    compact_kwargs["trigger_reason"] = "gateway_auto_summarize"
                if provider_request_correlation is not None and _accepts_keyword_arg(
                    compact_with_result,
                    "provider_request_correlation",
                ):
                    compact_kwargs["provider_request_correlation"] = provider_request_correlation
                compaction_result = await await_compaction_phase(
                    compact_with_result(
                        session_key,
                        budget,
                        effective_compaction_config,
                        **compact_kwargs,
                    ),
                    effective_compaction_config,
                    phase="summarizing",
                )
                summary = getattr(compaction_result, "summary", "") or ""
                outcome.removed_count = int(getattr(compaction_result, "removed_count", 0) or 0)
                outcome.kept_count = len(getattr(compaction_result, "kept_entries", []) or [])
                outcome.summary_source = str(
                    getattr(compaction_result, "summary_source", "unknown") or "unknown"
                )
                durable_commit_won = bool(outcome.removed_count > 0 and summary)
                if durable_commit_won:
                    committed_result = compaction_result
            else:
                summary = await await_compaction_phase(
                    call_compact_with_optional_config(
                        session_manager.compact,
                        session_key,
                        budget,
                        effective_compaction_config,
                        provider_request_correlation=provider_request_correlation,
                    ),
                    effective_compaction_config,
                    phase="summarizing",
                )
                outcome.removed_count = 1 if summary else 0
                durable_commit_won = bool(summary)
            if (
                compaction_result is not None
                and int(getattr(compaction_result, "removed_count", 0) or 0) > 0
                and bool(getattr(compaction_result, "summary", "") or "")
            ):
                for event in (
                    COMPACTION_CHUNK_SUMMARIZED_EVENT,
                    COMPACTION_SUMMARY_VERIFIED_EVENT,
                ):
                    observed_payload = compaction_lifecycle_payload(compaction_id, event)
                    observed_payload.update(compaction_result_payload(compaction_result))
                    notify_compaction(
                        session_key,
                        source="automatic",
                        phase="gateway_auto_summarize",
                        status="observed",
                        context_window_tokens=budget,
                        **compaction_effect_payload(status="observed"),
                        **observed_payload,
                    )
            compacted_transcript = await await_compaction_phase(
                session_manager.get_transcript(session_key),
                effective_compaction_config,
                phase="verifying",
            )
            post_estimate = await await_compaction_phase(
                _estimate_session_payload_tokens(
                    message,
                    compacted_transcript,
                    session_manager=session_manager,
                    session_key=session_key,
                    fallback_summary=str(summary or ""),
                ),
                effective_compaction_config,
                phase="verifying",
            )
            outcome.tokens_after = post_estimate
            outcome.remaining_budget_tokens = max(budget - post_estimate, 0)
            outcome.summary_len = len(str(summary or ""))

            if post_estimate > budget or post_estimate >= estimated:
                outcome.reason = "compaction_insufficient"
                outcome.refusal = _build_refusal_envelope(post_estimate, budget, outcome.reason)
                durable_applied = (
                    compaction_result is not None
                    and int(getattr(compaction_result, "removed_count", 0) or 0) > 0
                    and bool(getattr(compaction_result, "summary", "") or "")
                )
                outcome.lifecycle = CompactionLifecycleResult(
                    compacted=durable_applied,
                    refused=True,
                    reason=outcome.reason,
                    tokens_before=estimated,
                    tokens_after=post_estimate,
                    remaining_budget_tokens=outcome.remaining_budget_tokens,
                    removed_count=outcome.removed_count,
                    kept_count=outcome.kept_count,
                    summary_len=outcome.summary_len,
                    summary_source=outcome.summary_source,
                )
                log.warning(
                    "context_overflow.auto_summarize_refused",
                    session_key=session_key,
                    reason=outcome.reason,
                    tokens_after=post_estimate,
                )
                failed_payload = {
                    "tokens_before": estimated,
                    "tokens_after": post_estimate,
                    "remaining_budget_tokens": outcome.remaining_budget_tokens,
                    "removed_count": outcome.removed_count,
                    "kept_count": outcome.kept_count,
                    "summary_len": outcome.summary_len,
                    "summary_source": outcome.summary_source,
                }
                if compaction_result is not None:
                    failed_payload.update(
                        compaction_result_payload(
                            compaction_result,
                            tokens_before=estimated,
                            tokens_after=post_estimate,
                            remaining_budget_tokens=outcome.remaining_budget_tokens,
                        )
                    )
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="gateway_auto_summarize",
                    status="failed",
                    reason=outcome.reason,
                    request_status="refused",
                    context_window_tokens=budget,
                    **compaction_effect_payload(
                        status="failed",
                        reason=outcome.reason,
                        applied=durable_applied,
                        durability="durable" if durable_applied else None,
                    ),
                    **failed_payload,
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_PERSISTED_EVENT,
                    ),
                )
                return outcome

            outcome.summarized = True
            outcome.retried = True
            outcome.compacted_this_turn = True
            outcome.lifecycle = CompactionLifecycleResult(
                compacted=True,
                refused=False,
                tokens_before=estimated,
                tokens_after=post_estimate,
                remaining_budget_tokens=outcome.remaining_budget_tokens,
                removed_count=outcome.removed_count,
                kept_count=outcome.kept_count,
                summary_len=outcome.summary_len,
                summary_source=outcome.summary_source,
            )
            log.info(
                "context_overflow.auto_summarize_ok",
                session_key=session_key,
                tokens_before=estimated,
                tokens_after=post_estimate,
                remaining_budget_tokens=outcome.remaining_budget_tokens,
                summary_source=outcome.summary_source,
            )
            completed_payload = {
                "tokens_before": estimated,
                "tokens_after": post_estimate,
                "remaining_budget_tokens": outcome.remaining_budget_tokens,
                "removed_count": outcome.removed_count,
                "kept_count": outcome.kept_count,
                "summary_len": outcome.summary_len,
                "summary_source": outcome.summary_source,
            }
            if compaction_result is not None:
                completed_payload.update(
                    compaction_result_payload(
                        compaction_result,
                        tokens_before=estimated,
                        tokens_after=post_estimate,
                        remaining_budget_tokens=outcome.remaining_budget_tokens,
                    )
                )
            notify_compaction(
                session_key,
                source="automatic",
                phase="gateway_auto_summarize",
                status="completed",
                context_window_tokens=budget,
                **compaction_effect_payload(status="completed"),
                **completed_payload,
                **compaction_lifecycle_payload(compaction_id, COMPACTION_REPLAYED_EVENT),
            )
        except asyncio.CancelledError:
            if durable_commit_won:
                reconciled_payload: dict[str, Any] = {
                    "tokens_before": estimated,
                    "removed_count": outcome.removed_count,
                    "kept_count": outcome.kept_count,
                    "summary_source": outcome.summary_source,
                    "cancellation_reconciled": True,
                }
                if committed_result is not None:
                    reconciled_payload.update(
                        compaction_result_payload(
                            committed_result,
                            tokens_before=estimated,
                        )
                    )
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="gateway_auto_summarize",
                    status="completed",
                    reason="cancelled_after_commit",
                    context_window_tokens=budget,
                    **compaction_effect_payload(status="completed"),
                    **reconciled_payload,
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_PERSISTED_EVENT,
                    ),
                )
            else:
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="gateway_auto_summarize",
                    status="cancelled",
                    reason="cancelled",
                    tokens_before=estimated,
                    context_window_tokens=budget,
                    **compaction_effect_payload(status="cancelled"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort
            if checkpoint_failed:
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="checkpointing",
                    status="failed",
                    reason="checkpoint_failed",
                    message=str(exc),
                    tokens_before=estimated,
                    context_window_tokens=budget,
                    **compaction_effect_payload(status="failed"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                raise
            trimmed = list(transcript or [])
            while trimmed and _estimate_payload_tokens(message, trimmed) > budget:
                trimmed.pop(0)
                outcome.truncated_entries += 1
            post_estimate = _estimate_payload_tokens(message, trimmed)
            outcome.trimmed_history = trimmed
            outcome.tokens_after = post_estimate
            outcome.remaining_budget_tokens = max(budget - post_estimate, 0)
            if post_estimate <= budget and post_estimate < estimated:
                outcome.reason = "emergency_ephemeral"
                outcome.refusal = None
                outcome.retried = True
                outcome.lifecycle = CompactionLifecycleResult(
                    compacted=False,
                    refused=False,
                    reason=outcome.reason,
                    tokens_before=estimated,
                    tokens_after=post_estimate,
                    remaining_budget_tokens=outcome.remaining_budget_tokens,
                )
            else:
                outcome.reason = "compaction_failed"
                outcome.refusal = _build_refusal_envelope(estimated, budget, outcome.reason)
            if durable_commit_won:
                outcome.lifecycle = CompactionLifecycleResult(
                    compacted=True,
                    refused=outcome.refusal is not None,
                    reason=outcome.reason,
                    tokens_before=estimated,
                    tokens_after=outcome.tokens_after,
                    remaining_budget_tokens=outcome.remaining_budget_tokens,
                    removed_count=outcome.removed_count,
                    kept_count=outcome.kept_count,
                    summary_len=outcome.summary_len,
                    summary_source=outcome.summary_source,
                )
            log.warning(
                "context_overflow.auto_summarize_failed",
                session_key=session_key,
                error=str(exc),
                emergency_ephemeral=outcome.reason == "emergency_ephemeral",
            )
            terminal_status = (
                "completed"
                if durable_commit_won
                else (
                    "emergency_ephemeral" if outcome.reason == "emergency_ephemeral" else "failed"
                )
            )
            terminal_reason = (
                "post_commit_verification_failed" if durable_commit_won else outcome.reason
            )
            notify_compaction(
                session_key,
                source="automatic",
                phase="gateway_auto_summarize",
                status=terminal_status,
                message=str(exc),
                reason=terminal_reason,
                request_reason=outcome.reason,
                tokens_before=estimated,
                tokens_after=outcome.tokens_after,
                remaining_budget_tokens=outcome.remaining_budget_tokens,
                context_window_tokens=budget,
                **compaction_effect_payload(
                    status=terminal_status,
                    reason=terminal_reason,
                    applied=durable_commit_won or None,
                    durability="durable" if durable_commit_won else None,
                ),
                **compaction_lifecycle_payload(
                    compaction_id,
                    (
                        COMPACTION_PERSISTED_EVENT
                        if durable_commit_won
                        else COMPACTION_TRIGGERED_EVENT
                    ),
                ),
            )
    else:
        # No session manager wired in — degrade to drop-oldest proxy so
        # the turn still fits. This path is exercised by unit tests; the
        # production gateway always wires a real session manager.
        trimmed = list(transcript or [])
        while trimmed and _estimate_payload_tokens(message, trimmed) > budget:
            trimmed.pop(0)
            outcome.truncated_entries += 1
        outcome.trimmed_history = trimmed
        outcome.summarized = False
        outcome.retried = True
        log.info(
            "context_overflow.auto_summarize_proxy",
            session_key=session_key,
            dropped=outcome.truncated_entries,
        )

    return outcome
