"""TurnRunner: shared agent orchestration layer.

Single convergence point for all entry points (Web UI, CLI, Channel).
Extracted from gateway/rpc_sessions.py:_run_agent_turn() closure.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import contextvars
import copy
import hashlib
import inspect
import json
import math
import os
import platform
import re
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, Literal, SupportsInt, TypeGuard, cast
from urllib.parse import urlsplit

import structlog

from opensquilla.artifacts import artifact_marker
from opensquilla.attachment_refs import (
    is_attachment_ref,
    make_attachment_ref,
    read_attachment_ref_bytes,
    transcript_material_path,
)
from opensquilla.attachment_workspace import (
    AttachmentWorkspaceMaterializer,
    render_attachment_material_marker,
    workspace_attachment_budget_from_config,
)
from opensquilla.bootstrap_types import BootstrapFileReport
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES as _ALLOWED_ENGINE_MEDIA_TYPES,
)
from opensquilla.contracts.attachments import (
    DOCX_MIME as _DOCX_MIME,
)
from opensquilla.contracts.attachments import (
    EMAIL_ATTACHMENT_MIMES as _EMAIL_ATTACHMENT_MIMES,
)
from opensquilla.contracts.attachments import (
    IMAGE_ATTACHMENT_MIMES as _IMAGE_ATTACHMENT_MIMES,
)
from opensquilla.contracts.attachments import (
    MAX_ATTACHMENTS as _MAX_ATTACHMENT_COUNT,
)
from opensquilla.contracts.attachments import (
    MBOX_MIME as _MBOX_MIME,
)
from opensquilla.contracts.attachments import (
    MSG_MIME as _MSG_MIME,
)
from opensquilla.contracts.attachments import (
    OFFICE_ATTACHMENT_MIMES as _OFFICE_ATTACHMENT_MIMES,
)
from opensquilla.contracts.attachments import (
    OPAQUE_MIME as _OPAQUE_MIME,
)
from opensquilla.contracts.attachments import (
    PPTX_MIME as _PPTX_MIME,
)
from opensquilla.contracts.attachments import (
    TEXT_ATTACHMENT_MIMES as _ENGINE_TEXT_FAMILY_MIMES,
)
from opensquilla.contracts.attachments import (
    XLSX_MIME as _XLSX_MIME,
)
from opensquilla.contracts.attachments import (
    attachment_size_limit_for_mime as _attachment_size_limit_for_mime,
)
from opensquilla.contracts.attachments import (
    can_stage_attachment_mime as _can_stage_attachment_mime,
)
from opensquilla.contracts.attachments import (
    normalize_attachment_mime as _normalize_attachment_mime,
)
from opensquilla.engine.agent import Agent, ToolHandler
from opensquilla.engine.agent_injection import PendingInputProvider
from opensquilla.engine.cache_break_monitor import notify_compaction
from opensquilla.engine.hooks import (
    CompactionHook,
    DefaultTraceEmitterHook,
    TurnEvent,
    TurnHook,
    TurnHookContext,
)
from opensquilla.engine.outcome import outcome_from_error, turn_outcome_details
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.pricing import PriceEntry, lookup_price
from opensquilla.engine.router_decision import build_router_decision_event
from opensquilla.engine.turn_policy import resolve_turn_policy
from opensquilla.engine.turn_runner import (
    AgentBootstrapStage,
    AgentBootstrapStageInput,
    AttachmentStage,
    AttachmentStageInput,
    CompactionAndHistoryStage,
    CompactionAndHistoryStageInput,
    InputStage,
    InputStageInput,
    PromptAssemblerStage,
    PromptAssemblerStageInput,
    ProviderAndToolsStage,
    ProviderAndToolsStageInput,
    StreamConsumerStage,
    StreamConsumerStageInput,
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.turn_runner.harness import (
    _PromptReportBuilderAdapter,
    _RequestContextPrependAdapter,
    _TurnRunnerAgentConfigBuilderAdapter,
    _TurnRunnerAgentFactoryAdapter,
    _TurnRunnerAgentRunAdapter,
    _TurnRunnerAttachmentMessageBuilderAdapter,
    _TurnRunnerCompactionPersistAdapter,
    _TurnRunnerExtraContextAdapter,
    _TurnRunnerHistoryLoaderAdapter,
    _TurnRunnerMemoryFingerprintAdapter,
    _TurnRunnerMemorySnapshotAdapter,
    _TurnRunnerMemorySnapshotRefreshAdapter,
    _TurnRunnerMemorySyncNotifyAdapter,
    _TurnRunnerModelCatalogAdapter,
    _TurnRunnerPipelineExecutionAdapter,
    _TurnRunnerPreflightCompactionAdapter,
    _TurnRunnerPromptAssemblerAdapter,
    _TurnRunnerPromptConfigResolverAdapter,
    _TurnRunnerProviderResolverAdapter,
    _TurnRunnerRouterContextAdapter,
    _TurnRunnerSessionIdResolverAdapter,
    _TurnRunnerSessionTotalsAdapter,
    _TurnRunnerSkillCatalogResolverAdapter,
    _TurnRunnerSystemPromptRefreshAdapter,
    _TurnRunnerT3UpgradeCompactionAdapter,
    _TurnRunnerTimeoutBudgetAdapter,
    _TurnRunnerToolBuilderAdapter,
    _TurnRunnerTranscriptAppendAdapter,
    _TurnRunnerTurnErrorPersistAdapter,
    _TurnRunnerTurnMemoryCaptureAdapter,
    _TurnRunnerUsageTelemetryAdapter,
)
from opensquilla.engine.turn_runner.stream_consumer_stage import _StreamState
from opensquilla.engine.types import (
    AgentConfig,
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    RouterControlReplayEvent,
    ThinkingLevel,
    ToolResultEvent,
    WarningEvent,
)
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageAccountingUnavailableError,
    UsageEventSink,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
    provider_accounts_physical_usage,
)
from opensquilla.execution_status import (
    mark_execution_status_truncated,
    normalize_execution_status,
)
from opensquilla.memory.session_flush import SessionFlushService
from opensquilla.observability.decision_log import (
    DecisionEntry,
    PipelineStepRecord,
    SavingsTelemetry,
    build_intent_summary,
    build_vision_followup_gate_reason_code,
    compute_hashes,
    write_decision_entry,
)
from opensquilla.observability.prompt_report import PromptReport, build_prompt_report
from opensquilla.observability.trace import TraceContext, TraceEvent, write_trace_event
from opensquilla.observability.turn_call_log import TurnCallLogger, is_turn_call_log_enabled
from opensquilla.paths import media_root_from_config
from opensquilla.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from opensquilla.provider import (
    ProviderFailureKind,
    ProviderHeartbeatEvent,
    ProviderRecoveryAction,
    ProviderRetryTransition,
    classify_provider_error,
    decide_recovery_action,
    prepare_provider_retry_after_failure,
)
from opensquilla.provider.cache_affinity import (
    CacheAffinityReceipt as _RouterDynamicCacheAffinityReceipt,
)
from opensquilla.provider.cache_affinity import (
    build_cache_affinity_receipt,
    build_cache_domain_guard,
)
from opensquilla.provider.model_catalog import resolve_effective_context_window
from opensquilla.provider.protocol import (
    ProviderRetryScopeError,
    reserve_provider_retry_physical_request,
    validate_provider_chat_request,
)
from opensquilla.provider.types import (
    DoneEvent as ProviderDoneEvent,
)
from opensquilla.provider.types import (
    EnsembleProgressEvent as ProviderEnsembleProgressEvent,
)
from opensquilla.router_control import (
    RouterControlHoldStore,
    render_router_control_prompt_block,
)
from opensquilla.router_tiers import HIGHEST_TEXT_TIER, normalize_text_tier, tier_index
from opensquilla.safety import injection_guard, permission_matrix, sandbox, tool_tiers
from opensquilla.sandbox.run_mode import RunMode, display_name, execution_target, normalize_run_mode
from opensquilla.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_PERSISTED_EVENT,
    COMPACTION_REPLAYED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    compaction_memory_status,
    compaction_result_payload,
    durable_receipt_allows_destructive_compaction,
    flush_receipt_allows_destructive_compaction,
    flush_receipt_is_successful_flush,
    flush_receipt_status_for_compaction,
    flush_trigger_enabled,
    mark_compaction_flush_status_with_retry,
    new_compaction_id,
    pre_compaction_flush_requires_safe_receipt,
)
from opensquilla.session.context_view import (
    build_compaction_context_records,
    build_provider_compaction_context,
)
from opensquilla.session.cost_rollup import (
    normalize_event_cost_source,
)
from opensquilla.session.keys import (
    allows_private_memory_prompt_injection,
    canonicalize_session_key,
    is_subagent_key,
    normalize_agent_id,
)
from opensquilla.session.terminal_reply import (
    append_error_ref,
    build_terminal_reply,
    sanitize_agent_error,
)
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

if TYPE_CHECKING:
    from opensquilla.engine.routing.health import ProviderHealthLedger
    from opensquilla.persistence.meta_run_writer import MetaRunWriter

# Stable user-facing envelope for LLM timeouts.
_LLM_TIMEOUT_ENVELOPE: dict[str, Any] = {
    "status": "error",
    "error_class": "llm_timeout",
    "user_message": "The model took too long to respond. Please try again.",
    "retry_allowed": True,
}
_DEFAULT_AGENT_RUNTIME_TIMEOUT_SECONDS: float = 48 * 60 * 60
_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS: float = 120.0
_DEFAULT_LLM_TIMEOUT_SECONDS: float = _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
_WEB_CHAT_META_EXEMPT_KEYS: Final[frozenset[str]] = frozenset(
    {"meta_match", "meta_launch", "meta_resume"}
)
_ROUTER_PREV_ASSISTANT_MAX_CHARS: Final[int] = 8000
_ROUTER_HISTORY_USER_MAX_CHARS: Final[int] = 8000
_ROUTER_HISTORY_USER_MAX_TURNS: Final[int] = 4
_CONTEXT_SUMMARY_MARKER: Final[str] = "[Context Summary]"
_COMPACTION_SUMMARY_CONTEXT_HEADER: Final[str] = "[Compacted Session Summaries]"
_COMPACTION_SUMMARY_CONTEXT_MAX_CHARS: Final[int] = 16_000
_DEFAULT_PREFLIGHT_COMPACT_RATIO: Final[float] = 0.85
_COMPACTION_FAILURE_LIMIT: Final[int] = 3
_COMPACTION_CIRCUIT_COOLDOWN_SECONDS: Final[float] = 300.0
_T3_NOT_APPLICABLE: Final[str] = "not_applicable"
_T3_HANDLED: Final[str] = "handled"
_T3_FLUSH_FAILED: Final[str] = "flush_failed"
_T3_COMPACT_FAILED: Final[str] = "compact_failed"
_IMAGE_GENERATION_TOOL_NAMES: Final[frozenset[str]] = frozenset({"image_generate"})
_ARTIFACT_DELIVERY_FAILURE_MARKER: Final[str] = "File delivery failed:"
_ARTIFACT_DELIVERY_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"publish_artifact", "create_pptx"}
)
_ARTIFACT_DELIVERY_FAILURE_MAX_CHARS: Final[int] = 360
_HOOKS_FEATURE_ENV: Final[str] = "OPENSQUILLA_HOOKS"
_FIXED_FOUR_TIER_V2_MAX_OUTSTANDING_JOBS: Final[int] = 8
_FIXED_FOUR_TIER_V2_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
_FIXED_FOUR_TIER_V2_RELEASE_TIMEOUT_SECONDS: Final[float] = 5.0


def _is_materializable_attachment_mime(mime: Any) -> bool:
    # Everything except rendered images lands in the workspace so the agent's
    # tools can reach it; rendered images travel to the provider as vision
    # blocks instead. Non-rendered image labels (image/tiff, image/svg+xml…)
    # are opaque, so their only representation is the workspace copy.
    normalized = _normalize_attachment_mime(mime)
    return normalized is not None and normalized not in _IMAGE_ATTACHMENT_MIMES


def collect_invoked_skills(
    turn_segments: list[dict],
    *,
    extra_first: list[str] | None = None,
) -> list[str]:
    """Collect skill names from skill_view/meta_invoke tool segments."""

    seen: set[str] = set()
    result: list[str] = []
    for name in extra_first or []:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            result.append(name)
    for segment in turn_segments:
        tool_name = segment.get("name")
        if tool_name not in {"skill_view", "meta_invoke"}:
            continue
        skill_name = (segment.get("input") or {}).get("name")
        if not isinstance(skill_name, str) or not skill_name or skill_name in seen:
            continue
        seen.add(skill_name)
        result.append(skill_name)
    return result


def _hooks_mode_from_env() -> str:
    """Resolve the active hook mode from the ``OPENSQUILLA_HOOKS`` env var.

    Returns ``"legacy"`` only when explicitly set to ``legacy``
    (case-insensitive); any other value (including unset) returns ``"new"``.
    The default flipped to ``new`` after the equivalence harness showed zero
    divergence between legacy and hook paths across the engine and tools test
    suites. ``OPENSQUILLA_HOOKS=legacy`` remains as an escape hatch for one
    release cycle so any unforeseen drift can be diagnosed without rolling
    back code.
    """

    raw = os.environ.get(_HOOKS_FEATURE_ENV, "").strip().lower()
    return "legacy" if raw == "legacy" else "new"


def _is_deepseek_model_id(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("deepseek") or "/deepseek" in normalized


# Tools that are safe to run concurrently within a single LLM turn.
# Any tool name absent from this set is treated as mutex (serial dispatch).
_SAFE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "agents_list",
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep_search",
        "image",
        "list_dir",
        "memory_get",
        "memory_search",
        "pdf",
        "read_file",
        "read_spreadsheet",
        "session_search",
        "session_status",
        "sessions_history",
        "sessions_list",
        "skill_list",
        "skill_search_community",
        "skill_view",
        "tts",
        "web_discover",
        "web_fetch",
        "web_search",
    }
)

_ToolConcurrencyMode = Literal["mutex", "concurrent", "keyed", "predicate"]


@dataclass(frozen=True)
class _ToolConcurrencyPolicy:
    mode: _ToolConcurrencyMode
    key: Hashable | None = None
    max_inflight: int | None = None
    limit_key: Hashable | None = None


_MUTEX_TOOL_POLICY = _ToolConcurrencyPolicy(mode="mutex")
_CONCURRENT_TOOL_POLICY = _ToolConcurrencyPolicy(mode="concurrent")
# Image analysis crosses a provider boundary. Keep slide-thumbnail bursts below
# the generic safe-tool cap so compatible vision endpoints are not saturated.
_IMAGE_ANALYSIS_TOOL_POLICY = _ToolConcurrencyPolicy(
    mode="concurrent",
    max_inflight=2,
    limit_key=("media", "image_analysis"),
)


def _get_tool_concurrency_policy(
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    parent_session_key: str | None = None,
) -> _ToolConcurrencyPolicy:
    if tool_name == "image":
        return _IMAGE_ANALYSIS_TOOL_POLICY
    if tool_name in _SAFE_TOOL_NAMES:
        return _CONCURRENT_TOOL_POLICY
    if tool_name == "sessions_send":
        session_key = (arguments or {}).get("session_key")
        if isinstance(session_key, str) and session_key.strip():
            return _ToolConcurrencyPolicy(
                mode="keyed",
                key=("sessions_send", session_key.strip()),
            )
        return _MUTEX_TOOL_POLICY
    if tool_name == "sessions_spawn":
        from opensquilla.tools.types import current_tool_context  # noqa: PLC0415

        ctx = current_tool_context.get()
        parent_key = parent_session_key or (ctx.session_key if ctx is not None else None)
        if parent_key:
            return _ToolConcurrencyPolicy(
                mode="keyed",
                key=("sessions_spawn", parent_key),
            )
        return _MUTEX_TOOL_POLICY
    return _MUTEX_TOOL_POLICY


# Per-call-chain owner tracking for session-lock re-entry detection.
# A ContextVar is copied into child asyncio Tasks created while a turn is
# running, which matters for stream wrappers such as heartbeat_stream. Treating
# the lock id as the ownership token lets those child tasks enter without
# self-deadlocking while unrelated tasks still see their own context values.
_SESSION_LOCK_OWNER: contextvars.ContextVar[dict[int, asyncio.Task[Any]]] = contextvars.ContextVar(
    "_session_lock_owner"
)
_SESSION_LOCK_BYPASS_ONLY: contextvars.ContextVar[set[int] | None] = contextvars.ContextVar(
    "_session_lock_bypass_only",
    default=None,
)
# Gateway TaskRuntime installs the routing config captured when a turn is
# accepted.  ContextVar keeps concurrent sessions isolated without mutating the
# shared TurnRunner or GatewayConfig instances. Standalone/direct callers never
# set it and continue to read the runner's live config exactly as before.
_ACCEPTED_TURN_CONFIG: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "_accepted_turn_config",
    default=None,
)

# One router_single route freezes the two catalog budgets used for ranking.
# The ContextVar carries those values across the pipeline -> AgentBootstrap
# boundary without sharing mutable per-turn state between concurrent sessions.
_ROUTER_SINGLE_FROZEN_CATALOG: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar(
        "_router_single_frozen_catalog",
        default=None,
    )
)


def _consume_router_single_frozen_catalog(
    provider: str,
    model: str,
) -> tuple[int, int, Any] | None:
    """Consume one exact router_single catalog snapshot."""

    frozen = _ROUTER_SINGLE_FROZEN_CATALOG.get()
    if frozen is None:
        return None
    _ROUTER_SINGLE_FROZEN_CATALOG.set(None)
    frozen_provider = str(frozen.get("provider") or "").strip().casefold()
    frozen_model = str(frozen.get("model") or "").strip()
    if (
        frozen_provider != str(provider or "").strip().casefold()
        or frozen_model != str(model or "").strip()
    ):
        raise RuntimeError("router_single frozen catalog identity drifted")
    max_tokens = frozen.get("max_tokens")
    context_window = frozen.get("context_window")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
        or isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        raise RuntimeError("router_single frozen catalog budget is invalid")
    return max_tokens, context_window, frozen.get("capabilities")


@contextlib.contextmanager
def accepted_turn_config_scope(config: Any | None) -> Any:
    """Use one acceptance-time routing snapshot for the enclosed turn."""

    if config is None:
        yield
        return
    token = _ACCEPTED_TURN_CONFIG.set(config)
    try:
        yield
    finally:
        _ACCEPTED_TURN_CONFIG.reset(token)


def _compute_route_input_savings_usd(
    max_price_per_m: float,
    routed_price_per_m: float,
    input_tokens: int,
) -> float:
    """49b7e08 squilla-router savings formula: input-price delta times input tokens."""
    return round(max(0.0, (max_price_per_m - routed_price_per_m) * input_tokens / 1_000_000), 6)


@dataclass(frozen=True)
class _SavingsBaseline:
    model: str = ""
    price: PriceEntry = field(default_factory=lambda: PriceEntry(0.0, 0.0))
    cost_usd: float = 0.0


@dataclass(frozen=True)
class _ComprehensiveTurnSavings:
    pct: float = 0.0
    usd: float = 0.0
    baseline_model: str = ""
    baseline_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0


@dataclass
class _CompactionFailureState:
    count: int = 0
    opened_at: float | None = None


@dataclass
class _EmergencyCompactionOverride:
    summary: str
    kept_entries: list[Any]
    reason: str
    compaction_id: str


def _non_negative_int(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, str | bytes | bytearray | SupportsInt):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _token_cost_usd(input_tokens: float, output_tokens: float, price: PriceEntry) -> float:
    return (
        max(0.0, float(input_tokens)) * price.input_per_m / 1_000_000
        + max(0.0, float(output_tokens)) * price.output_per_m / 1_000_000
    )


def _tier_value(tier: object, key: str, default: object = None) -> object:
    if isinstance(tier, Mapping):
        return tier.get(key, default)
    return getattr(tier, key, default)


def _iter_text_tier_models(tiers: object) -> list[str]:
    if not isinstance(tiers, Mapping):
        return []
    models: list[str] = []
    for tier in tiers.values():
        if bool(_tier_value(tier, "image_only", False)):
            continue
        model = str(_tier_value(tier, "model", "") or "").strip()
        if model:
            models.append(model)
    return models


def _select_savings_baseline_model(
    tiers: object,
    baseline_input_tokens: float,
    baseline_output_tokens: float,
) -> _SavingsBaseline:
    best = _SavingsBaseline(cost_usd=-1.0)
    for model in _iter_text_tier_models(tiers):
        price = lookup_price(model)
        cost_usd = _token_cost_usd(baseline_input_tokens, baseline_output_tokens, price)
        if cost_usd > best.cost_usd:
            best = _SavingsBaseline(model=model, price=price, cost_usd=cost_usd)
    if best.cost_usd < 0:
        return _SavingsBaseline()
    return best


def _short_output_savings_rate(metadata: Mapping[str, Any], estimated_pct: float) -> float:
    prompt_policy = str(metadata.get("prompt_policy") or "").strip().upper()
    active = prompt_policy == "P0" or bool(metadata.get("short_reply_active"))
    if not active:
        return 0.0
    try:
        rate = float(estimated_pct)
    except (TypeError, ValueError):
        return 0.0
    if rate <= 0.0 or rate >= 1.0:
        return 0.0
    return rate


def _restored_output_side_tokens(
    actual_output_side_tokens: int,
    metadata: Mapping[str, Any],
    estimated_output_savings_pct: float,
) -> float:
    rate = _short_output_savings_rate(metadata, estimated_output_savings_pct)
    if rate <= 0.0 or actual_output_side_tokens <= 0:
        return float(actual_output_side_tokens)
    return actual_output_side_tokens / (1.0 - rate)


def _turn_used_ensemble(event: DoneEvent, metadata: Mapping[str, Any]) -> bool:
    """True when any part of the turn ran through the ensemble provider."""
    if metadata.get("ensemble_enabled"):
        return True
    return getattr(event, "ensemble_trace", None) is not None


def _compute_comprehensive_turn_savings(
    event: DoneEvent,
    metadata: Mapping[str, Any],
    tiers: object,
    routed_model: str,
    *,
    estimated_output_savings_pct: float = 0.03,
) -> _ComprehensiveTurnSavings:
    """Estimate per-turn savings from token counts and model prices only."""
    if _turn_used_ensemble(event, metadata):
        # Ensemble turns have no single-model counterfactual: the turn's token
        # totals are multiplied by the member fan-out while the routed-model
        # price covers only one member, so the formula below would report a
        # large saving on a turn that deliberately spends more for quality.
        return _ComprehensiveTurnSavings()
    actual_input_tokens = _non_negative_int(event.input_tokens)
    actual_output_side_tokens = _non_negative_int(event.output_tokens) + _non_negative_int(
        event.reasoning_tokens
    )
    tool_tokens_saved = _non_negative_int(metadata.get("tool_projection_tokens_saved"))
    baseline_input_tokens = actual_input_tokens + tool_tokens_saved
    baseline_output_tokens = _restored_output_side_tokens(
        actual_output_side_tokens,
        metadata,
        estimated_output_savings_pct,
    )

    baseline = _select_savings_baseline_model(
        tiers,
        baseline_input_tokens,
        baseline_output_tokens,
    )
    routed_price = lookup_price(routed_model or event.model)
    actual_cost_usd = _token_cost_usd(
        actual_input_tokens,
        actual_output_side_tokens,
        routed_price,
    )

    if baseline.cost_usd <= 0.0:
        return _ComprehensiveTurnSavings(
            baseline_model=baseline.model,
            baseline_cost_usd=max(0.0, baseline.cost_usd),
            actual_cost_usd=actual_cost_usd,
        )

    savings_usd = round(max(0.0, baseline.cost_usd - actual_cost_usd), 6)
    savings_pct = 0.0
    if savings_usd > 0.0:
        savings_pct = round(max(0.0, min(99.9, (savings_usd / baseline.cost_usd) * 100)), 1)

    return _ComprehensiveTurnSavings(
        pct=savings_pct,
        usd=savings_usd,
        baseline_model=baseline.model,
        baseline_cost_usd=baseline.cost_usd,
        actual_cost_usd=actual_cost_usd,
    )


def _normalize_capture_kind(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(".", "_").replace(":", "_")


# Boot-path initialization of the safety baseline. All four submodules
# are imported here so tool dispatch and ingress guards can consult them
# without late imports.
#
# The tuple pins the imports to module scope so the linter does not drop them
# as "unused" — dispatch paths reach these modules via attribute lookup at
# call time, not through named references in this file. Keeping the reference
# explicit makes the load-time invariant legible to readers.
_SAFETY_MODULES: Final[tuple[Any, ...]] = (
    injection_guard,
    tool_tiers,
    permission_matrix,
    sandbox,
)

log = structlog.get_logger(__name__)


def _accepts_keyword_arg(callable_obj: Any, name: str) -> bool:
    """Return True when callable accepts `name` explicitly or via `**kwargs`."""
    params = inspect.signature(callable_obj).parameters
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _strip_context_summary_marker(content: str) -> str:
    """Return summary text from a legacy transcript summary marker."""
    if content.startswith(_CONTEXT_SUMMARY_MARKER):
        return content[len(_CONTEXT_SUMMARY_MARKER) :].lstrip("\r\n")
    return content


def _subagent_terminal_history_notice(entry: Any) -> str | None:
    """Render trusted non-success subagent completions for the next model turn."""
    if getattr(entry, "role", None) != "system":
        return None
    if getattr(entry, "provenance_kind", None) != "internal_system":
        return None
    if getattr(entry, "provenance_source_tool", None) != "subagent_completion":
        return None
    content = getattr(entry, "content", None)
    if not isinstance(content, str) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "subagent_completion":
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"cancelled", "failed", "timeout", "abandoned"}:
        return None
    child_session_key = str(payload.get("child_session_key") or "unknown")[:200]
    terminal_reason = str(payload.get("terminal_reason") or status)[:120]
    return (
        "[Trusted runtime status] "
        f'Subagent {child_session_key} finished with status "{status}" '
        f'(reason: "{terminal_reason}"). It is no longer running. '
        "Do not wait for it or call sessions_yield for it. Continue from this terminal "
        "state unless the user asks to start a replacement subagent."
    )


def _format_compaction_summary_context(summary_texts: list[str]) -> str | None:
    """Render durable summaries as request-scoped context, newest context preserved."""
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in summary_texts:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    if not deduped:
        return None

    blocks = [f"[Summary {idx}]\n{text}" for idx, text in enumerate(deduped, start=1)]
    rendered = f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n" + "\n\n".join(blocks)
    if len(rendered) <= _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS:
        return rendered
    tail_budget = (
        _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS - len(_COMPACTION_SUMMARY_CONTEXT_HEADER) - 80
    )
    tail_budget = max(1000, tail_budget)
    return (
        f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n"
        "[Earlier compaction summary context truncated to fit request budget.]\n"
        f"{rendered[-tail_budget:]}"
    )


def _prepend_request_context_prompt(
    existing_request_context: str | None,
    prepended_context: str | None,
) -> str | None:
    """Place session summary context before volatile per-turn context."""
    if not prepended_context or not prepended_context.strip():
        return existing_request_context
    if not existing_request_context or not existing_request_context.strip():
        return prepended_context.strip()
    return f"{prepended_context.strip()}\n\n{existing_request_context.strip()}"


_MAX_TOOL_RESULT_CHARS = 2000
_MAX_TOOL_RESULT_METADATA_VALUE_CHARS = 256
_MAX_PERSISTED_TOOL_SOURCES = 12
_MAX_PERSISTED_TOOL_ARGUMENT_FIELD_CHARS = 4096
_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS = 512
_PERSISTED_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
_TOOL_ARGUMENT_PAYLOAD_FIELDS: Final[dict[str, frozenset[str]]] = {
    "write_file": frozenset({"content"}),
    "edit_file": frozenset({"old_text", "new_text"}),
}
_TOOL_RESULT_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "budget_clamped",
        "cache_status",
        "domain_limited_count",
        "duplicate_count",
        "provider",
        "query",
        "fallback_from",
        "fetch_failed_count",
        "fetched_count",
        "error",
        "error_class",
        "error_kind",
        "mode",
        "recency_degraded",
        "recency_supported",
        "returned_chars",
        "selected_provider",
    }
)
_SENTINELS: Final[frozenset[str]] = frozenset({"NO_REPLY", "HEARTBEAT_OK"})
_HEARTBEAT_ACK_TOKEN: Final[str] = "HEARTBEAT_OK"
_HEARTBEAT_THINK_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<think>.*?</think>",
    re.DOTALL,
)
_HEARTBEAT_UNCLOSED_THINK_RE: Final[re.Pattern[str]] = re.compile(
    r"<think>.*\Z",
    re.DOTALL,
)
_HEARTBEAT_FINAL_TAG_RE: Final[re.Pattern[str]] = re.compile(r"</?final>")
_THINKING_ALIASES: Final[dict[str, str]] = {
    "x-high": "xhigh",
    "x_high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "extra high": "xhigh",
    "highest": "high",
    "max": "high",
    "on": "low",
    "true": "medium",
    "none": "off",
    "false": "off",
}


def _truncate_json_string(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return value[: max_chars - 1] + "…"


def _compact_json_for_tool_result_preview(
    value: Any,
    *,
    max_string_chars: int,
    max_list_items: int,
) -> Any:
    """Return a JSON-serializable preview that keeps structure bounded."""

    if isinstance(value, str):
        return _truncate_json_string(value, max_string_chars)
    if isinstance(value, list):
        return [
            _compact_json_for_tool_result_preview(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            for item in value[:max_list_items]
        ]
    if isinstance(value, dict):
        return {
            str(key): _compact_json_for_tool_result_preview(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            for key, item in value.items()
        }
    return value


def _bounded_tool_result_metadata(
    parsed: Mapping[str, Any],
) -> dict[str, str | int | float | bool | None]:
    """Return bounded scalar metadata safe to store beside capped result text."""

    metadata: dict[str, str | int | float | bool | None] = {}
    for key in _TOOL_RESULT_METADATA_KEYS:
        if key not in parsed:
            continue
        _add_bounded_tool_result_metadata(metadata, key, parsed[key])

    diagnostics = parsed.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        for key in _TOOL_RESULT_METADATA_KEYS:
            if key not in diagnostics or key in metadata:
                continue
            _add_bounded_tool_result_metadata(metadata, key, diagnostics[key])

        diagnostic_attempts = diagnostics.get("provider_attempts")
        if "provider_attempt_count" not in metadata and isinstance(
            diagnostic_attempts, list | tuple
        ):
            metadata["provider_attempt_count"] = len(diagnostic_attempts)

    attempts = parsed.get("provider_attempts")
    if isinstance(attempts, list | tuple):
        metadata["provider_attempt_count"] = len(attempts)

    return metadata


def _add_bounded_tool_result_metadata(
    metadata: dict[str, str | int | float | bool | None],
    key: str,
    value: Any,
) -> None:
    if isinstance(value, str):
        metadata[key] = _truncate_json_string(
            value,
            _MAX_TOOL_RESULT_METADATA_VALUE_CHARS,
        )
    elif isinstance(value, int | float | bool) or value is None:
        metadata[key] = value


def _json_tool_result_preview(parsed: Any, original_chars: int, max_chars: int) -> str:
    """Build a bounded, valid-JSON preview for persisted transcript display.

    Tool results are often structured JSON consumed by the web UI. A plain
    prefix slice can turn them into invalid JSON and hide top-level metadata
    such as the active search provider. This helper prefers a valid JSON
    preview with explicit truncation metadata while keeping the historical
    transcript size cap.
    """

    if isinstance(parsed, dict):
        base: dict[str, Any] = dict(parsed)
    else:
        base = {"value": parsed}
    base["result_truncated"] = True
    base["result_original_chars"] = original_chars

    for max_list_items in (5, 3, 2, 1, 0):
        for max_string_chars in (512, 256, 128, 64, 32, 16):
            compacted = _compact_json_for_tool_result_preview(
                base,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            rendered = json.dumps(compacted, ensure_ascii=False, indent=2)
            if len(rendered) <= max_chars:
                return rendered

    fallback: dict[str, Any] = {
        "result_truncated": True,
        "result_original_chars": original_chars,
    }
    if isinstance(parsed, dict):
        fallback.update(_bounded_tool_result_metadata(parsed))
    rendered = json.dumps(fallback, ensure_ascii=False, indent=2)
    if len(rendered) <= max_chars:
        return rendered
    return json.dumps({"result_truncated": True}, ensure_ascii=False)


def _persisted_web_search_sources(parsed: Any) -> list[dict[str, Any]]:
    if not isinstance(parsed, Mapping):
        return []
    candidates = parsed.get("sources")
    if not isinstance(candidates, list | tuple):
        candidates = parsed.get("results")
    if not isinstance(candidates, list | tuple):
        return []

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = _persisted_web_search_source(candidate)
        if source is None:
            continue
        key = str(source.get("url") or "").split("#", 1)[0]
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append(source)
        if len(sources) >= _MAX_PERSISTED_TOOL_SOURCES:
            break
    return sources


def _persisted_web_search_source(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    url = _persisted_source_url(candidate.get("url") or candidate.get("final_url"))
    if url is None:
        return None

    source: dict[str, Any] = {"url": url}
    canonical_url = _persisted_source_url(candidate.get("canonical_url"))
    if canonical_url is not None:
        source["canonical_url"] = canonical_url
    title = _persisted_source_text(candidate.get("title"), max_chars=256)
    if title:
        source["title"] = title
    domain = _persisted_source_text(candidate.get("domain"), max_chars=128)
    if not domain:
        domain = _domain_from_source_url(url)
    if domain:
        source["domain"] = domain
    provider = _persisted_source_text(candidate.get("provider"), max_chars=64)
    if provider:
        source["provider"] = provider
    rank = candidate.get("rank")
    if isinstance(rank, int):
        source["rank"] = rank
    fetched = candidate.get("fetched")
    if isinstance(fetched, bool):
        source["fetched"] = fetched
    fetch_status = _persisted_source_text(candidate.get("fetch_status"), max_chars=64)
    if fetch_status:
        source["fetch_status"] = fetch_status
    return source


def _persisted_source_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or url.endswith("…"):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return url


def _persisted_source_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return _truncate_json_string(value.strip(), max_chars)


def _domain_from_source_url(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _tool_argument_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _persisted_tool_argument_projection(
    *,
    tool_name: str,
    tool_use_id: str,
    field: str,
    value_text: str,
    path_hint: Any,
) -> str:
    lines = [
        _PERSISTED_TOOL_ARGUMENT_PROJECTION_PREFIX.rstrip("\n"),
        f"tool: {tool_name}",
        f"tool_use_id: {tool_use_id}",
        f"field: {field}",
        f"original_chars: {len(value_text)}",
        f"sha256: {hashlib.sha256(value_text.encode('utf-8')).hexdigest()}",
    ]
    if isinstance(path_hint, str) and path_hint.strip():
        lines.append(f"path: {path_hint.strip()}")
    lines.extend(
        [
            "head:",
            value_text[:_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS],
            "tail:",
            value_text[-_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS:],
        ]
    )
    return "\n".join(lines)


def _persisted_tool_use_input(
    tool_name: str,
    tool_use_id: str,
    arguments: dict[str, Any],
    *,
    max_field_chars: int = _MAX_PERSISTED_TOOL_ARGUMENT_FIELD_CHARS,
) -> dict[str, Any]:
    """Create the transcript-safe input for persisted file-writing tool calls."""

    payload_fields = _TOOL_ARGUMENT_PAYLOAD_FIELDS.get(tool_name)
    if not payload_fields:
        return arguments

    projected = dict(arguments)
    changed = False
    path_hint = projected.get("path")
    for argument_name in payload_fields:
        if argument_name not in projected:
            continue
        value_text = _tool_argument_text(projected[argument_name])
        if len(value_text) <= max_field_chars:
            continue
        projected[argument_name] = _persisted_tool_argument_projection(
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            field=argument_name,
            value_text=value_text,
            path_hint=path_hint,
        )
        changed = True

    return projected if changed else arguments


def _persisted_tool_result_segment(
    event: ToolResultEvent,
    *,
    max_chars: int = _MAX_TOOL_RESULT_CHARS,
) -> dict[str, Any]:
    """Create the transcript `tool_result` segment for a streamed event."""

    result = event.result
    segment: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": event.tool_use_id,
        "name": event.tool_name,
        "result": result,
        "is_error": event.is_error,
    }
    if event.execution_status is not None:
        segment["execution_status"] = normalize_execution_status(event.execution_status)

    parsed_result: Any = None
    parsed_result_available = False
    if event.tool_name == "web_search" or len(result) > max_chars:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed_result = json.loads(result)
            parsed_result_available = True
    if event.tool_name == "web_search" and parsed_result_available:
        sources = _persisted_web_search_sources(parsed_result)
        if sources:
            segment["sources"] = sources
    if len(result) <= max_chars:
        return segment

    segment["result_truncated"] = True
    segment["result_original_chars"] = len(result)
    if "execution_status" in segment:
        segment["execution_status"] = mark_execution_status_truncated(segment["execution_status"])
    if not parsed_result_available:
        segment["result"] = result[:max_chars]
        return segment

    parsed = parsed_result
    if isinstance(parsed, dict):
        segment.update(_bounded_tool_result_metadata(parsed))
        sources = _persisted_web_search_sources(parsed)
        if sources:
            segment["sources"] = sources
    segment["result"] = _json_tool_result_preview(parsed, len(result), max_chars)
    return segment


def _artifact_delivery_failure_summary(event: ToolResultEvent) -> str | None:
    if event.tool_name not in _ARTIFACT_DELIVERY_TOOL_NAMES or not event.is_error:
        return None
    raw = event.result.strip()
    summary = raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        candidate = (
            parsed.get("user_message")
            or parsed.get("message")
            or parsed.get("error")
            or parsed.get("error_class")
        )
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
    summary = " ".join(summary.split())
    if len(summary) > _ARTIFACT_DELIVERY_FAILURE_MAX_CHARS:
        summary = summary[: _ARTIFACT_DELIVERY_FAILURE_MAX_CHARS - 3].rstrip() + "..."
    return summary or f"{event.tool_name} failed"


def _artifact_delivery_result_name(event: ToolResultEvent) -> str | None:
    try:
        parsed = json.loads(event.result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    artifact = parsed.get("artifact")
    if not isinstance(artifact, dict):
        return None
    name = artifact.get("name")
    return name if isinstance(name, str) and name else None


def _artifact_delivery_effective_publish_name(
    arguments: dict[str, Any],
    raw_target: str,
) -> str | None:
    """Mirror publish_artifact's effective public filename calculation."""

    try:
        target_name = Path(raw_target).name
        raw_name = arguments.get("name")
        requested_name = raw_name if isinstance(raw_name, str) else None
        artifact_name = (requested_name or target_name).strip() or target_name
        if requested_name and not Path(artifact_name).suffix and Path(target_name).suffix:
            artifact_name = f"{artifact_name}{Path(target_name).suffix}"
    except (OSError, RuntimeError, ValueError):
        return None
    return artifact_name or None


def _artifact_delivery_target_keys(
    event: ToolResultEvent,
    *,
    tool_context: ToolContext | None = None,
    include_publish_name: bool = False,
) -> tuple[str, ...]:
    if event.tool_name not in _ARTIFACT_DELIVERY_TOOL_NAMES:
        return ()
    arguments = event.arguments if isinstance(event.arguments, dict) else {}
    from opensquilla.engine.artifact_delivery import (
        artifact_delivery_name_target_key,
        artifact_delivery_publish_target_key,
    )

    if event.tool_name == "publish_artifact":
        raw_target = arguments.get("path")
        if not isinstance(raw_target, str):
            return ()
        path_key = artifact_delivery_publish_target_key(
            raw_target,
            workspace_dir=tool_context.workspace_dir if tool_context is not None else None,
        )
        keys = [path_key] if path_key is not None else []
        if not include_publish_name:
            if "name" in arguments and isinstance(arguments.get("name"), str):
                artifact_name = _artifact_delivery_effective_publish_name(
                    arguments,
                    raw_target,
                )
                if artifact_name is not None:
                    return (artifact_delivery_name_target_key(artifact_name),)
            return tuple(keys)

        artifact_name = _artifact_delivery_result_name(event)
        if artifact_name is None:
            artifact_name = _artifact_delivery_effective_publish_name(
                arguments,
                raw_target,
            )
        if artifact_name is not None:
            keys.append(artifact_delivery_name_target_key(artifact_name))
        return tuple(dict.fromkeys(keys))

    effective_name = _artifact_delivery_result_name(event) if not event.is_error else None
    if effective_name is None:
        raw_name = arguments.get("name") or "generated.pptx"
        if not isinstance(raw_name, str):
            return ()
        # Match create_pptx's public name normalization: it publishes a basename
        # and appends .pptx when omitted.
        effective_name = Path(raw_name).name.strip()
        if not effective_name or effective_name in {".", ".."}:
            effective_name = "generated.pptx"
        if not effective_name.lower().endswith(".pptx"):
            effective_name = f"{effective_name}.pptx"
    name_key = artifact_delivery_name_target_key(effective_name)
    keys = [name_key]
    if not event.is_error and tool_context is not None and tool_context.workspace_dir:
        root_path_key = artifact_delivery_publish_target_key(
            name_key.removeprefix("name:"),
            workspace_dir=tool_context.workspace_dir,
        )
        if root_path_key is not None:
            keys.append(root_path_key)
    return tuple(dict.fromkeys(keys))


def _artifact_delivery_failure_notice(*, partial: bool = False) -> str:
    if partial:
        return (
            f"{_ARTIFACT_DELIVERY_FAILURE_MARKER} some generated files were attached, "
            "but at least one file could not be attached. Ask me to resend the "
            "missing file after I correct or regenerate it."
        )
    return (
        f"{_ARTIFACT_DELIVERY_FAILURE_MARKER} no downloadable file was attached "
        "to this response. Ask me to resend the file after I correct or regenerate it."
    )


def _cancelled_partial_response_text(
    partial_text: str,
    artifacts: list[dict[str, Any]],
) -> str:
    partial_text = partial_text.rstrip()
    if artifacts:
        names = [
            str(item.get("name") or item.get("filename") or "").strip()
            for item in artifacts
            if isinstance(item, dict)
        ]
        named = [name for name in names if name]
        delivered = (
            "The generated file was delivered: " + ", ".join(named) + "."
            if named
            else "The generated file was delivered."
        )
        return f"{partial_text}\n\n{delivered}" if partial_text else delivered
    return f"{partial_text}\n\n[interrupted]" if partial_text else "[interrupted]"


async def _finish_required_cancel_cleanup(awaitable: Awaitable[Any]) -> Any:
    """Finish required turn cleanup without forwarding repeated cancellation."""

    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _fixed_route_visible_transcript_text(role: str, content: Any) -> str:
    """Return classifier-safe visible text from one persisted transcript row."""

    from opensquilla.engine.steps.inject_time_prefix import TIME_PREFIX_RE

    text = str(content or "")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            visible = payload.get("text")
            is_user_envelope = role == "user" and "attachments" in payload
            is_assistant_envelope = role == "assistant" and "artifacts" in payload
            if isinstance(visible, str) and (is_user_envelope or is_assistant_envelope):
                text = visible
    if role == "user":
        text = TIME_PREFIX_RE.sub("", text, count=1)
    return text.strip()


_FIXED_ROUTE_CLARIFICATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:clarif(?:y|ication)|ask[_ -]?user|need[_ -]?input)\b|"
    r"could you (?:clarify|provide)|please (?:specify|provide)|clarify which|"
    r"能否|请\s*提供|需要(?:更多|具体).{0,8}信息)",
    re.IGNORECASE,
)
_FIXED_ROUTE_FAILURE_OUTCOME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:error|fail(?:ed|ure)?|timeout|abort(?:ed)?|cancel(?:led|ed)?)\b|"
    r"错误|失败|超时|取消)",
    re.IGNORECASE,
)


def _fixed_route_tool_call_names(value: Any) -> tuple[str, ...]:
    """Extract only observable tool names from a persisted assistant row."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    names: list[str] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name") or raw.get("tool_name")
        function = raw.get("function")
        if not name and isinstance(function, Mapping):
            name = function.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def _fixed_route_previous_outcome(
    *,
    route: Any | None,
    assistant_entry: Any | None,
    assistant_text: str | None,
) -> Literal["success", "failure", "clarification", "unknown"]:
    """Derive the prior outcome from durable route and assistant evidence.

    Route failure is authoritative.  A successful route may still have ended
    by asking the user for missing information, which is a distinct training
    label.  Rows written before fixed routing existed have no route record, so
    their persisted stop/outcome fields are used when present.
    """

    execution_status = str(getattr(route, "execution_status", "") or "").casefold()
    if execution_status in {"failed", "cancelled", "canceled"}:
        return "failure"

    signals: list[str] = []
    for raw_container in (
        getattr(assistant_entry, "turn_context", None),
        getattr(assistant_entry, "turn_usage", None),
    ):
        if not isinstance(raw_container, Mapping):
            continue
        for key in (
            "agent_loop_stop_reason",
            "stop_reason",
            "outcome",
            "execution_status",
            "error_code",
        ):
            value = raw_container.get(key)
            if isinstance(value, str) and value.strip():
                signals.append(value.strip())

    tool_names = _fixed_route_tool_call_names(getattr(assistant_entry, "tool_calls", None))
    clarification_signal = " ".join((*signals, *tool_names, assistant_text or ""))
    if _FIXED_ROUTE_CLARIFICATION_RE.search(clarification_signal):
        return "clarification"

    if execution_status == "succeeded":
        return "success"
    if any(_FIXED_ROUTE_FAILURE_OUTCOME_RE.search(signal) for signal in signals):
        return "failure"
    if signals:
        return "success"
    return "unknown"


_FIXED_ROUTE_NATIVE_USAGE_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credentials",
        "headers",
        "messages",
        "prompt",
        "request",
        "request_body",
        "request_id",
        "access_token",
        "refresh_token",
        "secret",
    }
)


def _fixed_route_provider_native_usage(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact provider-native usage evidence for route audit."""

    if depth > 6:
        return None
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:128]:
            key = str(raw_key)
            normalized = key.strip().casefold().replace("-", "_")
            if normalized in _FIXED_ROUTE_NATIVE_USAGE_SENSITIVE_KEYS or any(
                marker in normalized
                for marker in ("api_key", "authorization", "credential", "secret")
            ):
                continue
            sanitized[key] = _fixed_route_provider_native_usage(
                raw_value,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, list | tuple):
        return [
            _fixed_route_provider_native_usage(item, depth=depth + 1) for item in list(value)[:128]
        ]
    if isinstance(value, str):
        return value[:4096]
    if value is None or isinstance(value, bool | int | float):
        return copy.deepcopy(value)
    return None


def _should_add_artifact_delivery_failure_notice(
    *,
    failure_summaries: list[str],
    turn_artifacts: list[dict[str, Any]],
    final_text: str,
) -> bool:
    if not failure_summaries:
        return False
    return _ARTIFACT_DELIVERY_FAILURE_MARKER not in final_text


_SUBAGENT_TASK_PROTOCOL: Final[str] = (
    "You are a spawned subagent. Execute only the delegated task and return "
    "a compact result for the parent agent to use. Prefer a direct answer; "
    "call tools only when the task explicitly requires external state, files, "
    "network data, or tool output. If the delegated task asks you to reply with "
    "an exact phrase, only reply, output a sentinel token, or avoid explanation, "
    "Do not call tools and return exactly that requested text. Do not treat "
    "uppercase sentinel-like strings as shell commands, filenames, or config keys."
)


def _should_use_selector_fallback(provider_name: str, event: ProviderErrorEvent) -> bool:
    kind = classify_provider_error(
        provider_name=provider_name,
        status_code=int(event.code) if str(event.code).isdigit() else None,
        raw_code=event.code,
        message=event.message,
    )
    return decide_recovery_action(kind) in {
        ProviderRecoveryAction.FALLBACK_PROVIDER,
        ProviderRecoveryAction.RETRY_THEN_FALLBACK,
    }


def _report_credential_pool_failure(
    provider_name: str,
    turn_metadata: dict[str, Any] | None,
    event: ProviderErrorEvent,
) -> bool:
    """Park a pool-served profile key on rate-limit / credits / auth failures.

    No-op unless this turn's provider was resolved through a profile
    credential pool (the non-secret ``credential_pool`` stamp written at
    resolution time) and the tier ProviderConfig was actually applied
    (``routed_provider_applied`` names the same provider — instance
    ``provider_name`` is not used because openai-compatible backends share
    the generic ``"openai"`` name). The pool manager additionally ignores
    kinds other than RATE_LIMITED / INSUFFICIENT_CREDITS / AUTH_INVALID and
    sessions it never pinned. Never raises: credential bookkeeping must not
    break the turn loop.
    """
    if not turn_metadata:
        return False
    pool_info = turn_metadata.get("credential_pool")
    if not isinstance(pool_info, dict):
        return False
    pool_provider = str(pool_info.get("provider") or "")
    if not pool_provider:
        return False
    if str(turn_metadata.get("routed_provider_applied") or "") != pool_provider:
        return False
    should_purge = False
    try:
        kind = classify_provider_error(
            provider_name=provider_name,
            status_code=int(event.code) if str(event.code).isdigit() else None,
            raw_code=event.code,
            message=event.message,
        )
        if kind not in {
            ProviderFailureKind.RATE_LIMITED,
            ProviderFailureKind.INSUFFICIENT_CREDITS,
            ProviderFailureKind.AUTH_INVALID,
        }:
            return False
        should_purge = True
        from opensquilla.gateway.llm_runtime import profile_credential_pools

        profile_credential_pools().report_failure(
            pool_provider,
            str(pool_info.get("session_key") or ""),
            kind,
            retry_after_seconds=getattr(event, "retry_after_s", None),
        )
        return True
    except Exception:  # noqa: BLE001 — credential bookkeeping only
        log.debug("credential_pool.report_failed", provider=pool_provider)
        return should_purge


def _normalize_heartbeat_text(
    text: str,
    *,
    run_kind: str,
    heartbeat_ack_max_chars: int,
) -> str:
    stripped = text.strip()
    if stripped in _SENTINELS:
        log.debug("turn_runner.sentinel_suppressed", sentinel=stripped)
        return ""
    if run_kind != "heartbeat":
        return text

    normalized = _HEARTBEAT_THINK_BLOCK_RE.sub("", text)
    normalized = _HEARTBEAT_UNCLOSED_THINK_RE.sub("", normalized)
    normalized = _HEARTBEAT_FINAL_TAG_RE.sub("", normalized)
    if normalized != text:
        text = normalized.strip()
        stripped = text.strip()

    if stripped in _SENTINELS:
        log.debug("turn_runner.sentinel_suppressed", sentinel=stripped)
        return ""

    def _suppressed(payload: str) -> bool:
        return len(payload.strip()) <= heartbeat_ack_max_chars

    if stripped.startswith(_HEARTBEAT_ACK_TOKEN):
        remainder = stripped[len(_HEARTBEAT_ACK_TOKEN) :].strip()
        if _suppressed(remainder):
            return ""

    if stripped.endswith(_HEARTBEAT_ACK_TOKEN):
        remainder = stripped[: -len(_HEARTBEAT_ACK_TOKEN)].strip()
        if _suppressed(remainder):
            return ""

    return text


def _drop_unpaired_tool_use_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paired_ids = {
        segment.get("tool_use_id")
        for segment in segments
        if isinstance(segment, dict) and segment.get("type") == "tool_result"
    }
    return [
        segment
        for segment in segments
        if not (
            isinstance(segment, dict)
            and segment.get("type") == "tool_use"
            and segment.get("tool_use_id") not in paired_ids
        )
    ]


def _valid_router_dynamic_route_identity(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    provider, separator, model = value.partition(":")
    return (
        separator == ":"
        and provider == provider.strip()
        and model == model.strip()
        and bool(provider)
        and bool(model)
    )


def _router_dynamic_decision_projection(
    selection_plan: object,
) -> dict[str, Any] | None:
    """Build the public runtime audit projection for one dynamic route."""

    if not isinstance(selection_plan, Mapping):
        return None
    decision_id = selection_plan.get("decision_id")
    selected_proposers = selection_plan.get("selected_P")
    selected_aggregator = selection_plan.get("selected_A")
    session = selection_plan.get("session")
    if (
        selection_plan.get("strategy") != "router_dynamic"
        or selection_plan.get("selection_mode") != "router_dynamic"
        or not isinstance(decision_id, str)
        or not decision_id.strip()
        or decision_id != decision_id.strip()
        or not isinstance(selected_proposers, list)
        or not selected_proposers
        or any(
            not _valid_router_dynamic_route_identity(identity) for identity in selected_proposers
        )
        or len(set(selected_proposers)) != len(selected_proposers)
        or not _valid_router_dynamic_route_identity(selected_aggregator)
        or not isinstance(session, Mapping)
    ):
        return None

    projection = {
        "decision_id": decision_id,
        "ranking_version": selection_plan.get("ranking_version"),
        "registry_snapshot_version": selection_plan.get("registry_snapshot_version"),
        "registry_snapshot_hash": selection_plan.get("registry_snapshot_hash"),
        "selected_P": list(selected_proposers),
        "selected_A": selected_aggregator,
        "effective_tier": selection_plan.get("effective_tier"),
        "session": copy.deepcopy(dict(session)),
    }
    thinking_assignment = selection_plan.get("thinking_assignment")
    if isinstance(thinking_assignment, Mapping):
        executed_assignment = selection_plan.get("executed_thinking_assignment")
        unsupported_fallbacks = selection_plan.get("unsupported_level_fallbacks")
        policy_versions = selection_plan.get("policy_versions")
        if (
            unsupported_fallbacks is not None
            and (
                not isinstance(unsupported_fallbacks, Sequence)
                or isinstance(unsupported_fallbacks, (str, bytes, bytearray))
            )
        ) or (policy_versions is not None and not isinstance(policy_versions, Mapping)):
            return None
        projection.update(
            {
                "thinking_assignment": copy.deepcopy(dict(thinking_assignment)),
                "executed_thinking_assignment": (
                    copy.deepcopy(dict(executed_assignment))
                    if isinstance(executed_assignment, Mapping)
                    else None
                ),
                "assignment_reasons": copy.deepcopy(selection_plan.get("assignment_reasons")),
                "unsupported_level_fallbacks": copy.deepcopy(list(unsupported_fallbacks or [])),
                "policy_versions": copy.deepcopy(dict(policy_versions or {})),
            }
        )
    for audit_field in (
        "retry_parent_decision_id",
        "retry_excluded_proposer_identities",
        "task_analysis_reused",
        "task_analysis_reuse",
        "retry_routing",
    ):
        if audit_field in selection_plan:
            projection[audit_field] = copy.deepcopy(selection_plan[audit_field])
    return projection


@dataclass(frozen=True, slots=True)
class _RouterDynamicCacheAffinityPolicy:
    """Runtime-only projection of the validated ranking policy."""

    topology: Literal["single", "multiple"]
    ttl_seconds: float
    route_cache_max_entries: int
    source: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class _RouterDynamicCacheAffinityReceiptBatch:
    turn_id: str
    decision_id: str
    provider_instance_token: str
    provider_instance_generation: int
    chat_call_id: str
    chat_call_sequence: int
    runtime_generation: int
    topology: Literal["single", "multiple"]
    receipts: tuple[_RouterDynamicCacheAffinityReceipt, ...]

    def __repr__(self) -> str:
        return (
            "_RouterDynamicCacheAffinityReceiptBatch("
            f"turn_id={self.turn_id!r}, decision_id={self.decision_id!r}, "
            f"chat_call_sequence={self.chat_call_sequence!r}, "
            f"runtime_generation={self.runtime_generation!r}, "
            f"receipt_count={len(self.receipts)})"
        )


@dataclass(frozen=True, slots=True)
class _RouterDynamicCacheAffinityCollectionContext:
    turn_id: str
    decision_id: str
    provider_instance_token: str
    provider_instance_generation: int
    session_key: str
    session_epoch: int
    selection_generation: int
    topology: Literal["single", "multiple"]


@dataclass(frozen=True, slots=True)
class _RouterDynamicCacheAffinityStateKey:
    session_key: str
    session_epoch: int
    topology: Literal["single", "multiple"]


@dataclass(slots=True, repr=False)
class _RouterDynamicCacheAffinitySessionState:
    receipts: dict[
        tuple[Literal["single", "proposer", "aggregator"], str, str],
        _RouterDynamicCacheAffinityReceipt,
    ] = field(default_factory=dict)


@dataclass(slots=True, repr=False)
class _RouterDynamicCacheAffinityPendingSidecar:
    context: _RouterDynamicCacheAffinityCollectionContext
    policy: _RouterDynamicCacheAffinityPolicy
    active_provider_instance_token: str
    active_provider_instance_generation: int
    latest_chat_sequence: int = -1
    latest_batch: _RouterDynamicCacheAffinityReceiptBatch | None = None


@dataclass(frozen=True, slots=True)
class _RouterDynamicCacheRerouteResult:
    provider: Any
    resolved_model: str
    provider_name: str
    active_provider_id: str


@dataclass(frozen=True, slots=True)
class _RouterDynamicCacheReroutePlan:
    session_key: str
    selection_generation: int
    reroute_without_affinity: Callable[[], _RouterDynamicCacheRerouteResult]
    finalize_observability: Callable[[], None] | None = None


def _router_dynamic_cache_affinity_policy(
    ranking_config: Mapping[str, Any],
    *,
    topology: Literal["single", "multiple"],
) -> _RouterDynamicCacheAffinityPolicy | None:
    """Return the enabled policy without touching helpers on the absent path."""

    session = ranking_config.get("session")
    if not isinstance(session, Mapping):
        return None
    if not isinstance(session.get("kv_cache_affinity"), Mapping):
        return None

    from opensquilla.provider.ranking_router import (
        router_dynamic_cache_affinity_policy,
    )

    policy = router_dynamic_cache_affinity_policy(
        ranking_config,
        topology=topology,
    )
    if policy is None:
        return None
    raw_ttl = policy.get("ttl_seconds")
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int | float):
        return None
    ttl_seconds = float(raw_ttl)
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0.0:
        return None
    raw_max_entries = session.get("route_cache_max_entries")
    if (
        isinstance(raw_max_entries, bool)
        or not isinstance(raw_max_entries, int)
        or raw_max_entries <= 0
    ):
        return None
    return _RouterDynamicCacheAffinityPolicy(
        topology=topology,
        ttl_seconds=ttl_seconds,
        route_cache_max_entries=raw_max_entries,
        source=policy,
    )


def _router_dynamic_cache_price_quote_resolver(request: object) -> object | None:
    """Resolve one frozen no-network quote without inventing provenance."""

    from opensquilla.engine.pricing import resolve_cache_price_quote_exact

    try:
        return resolve_cache_price_quote_exact(
            provider=str(getattr(request, "provider")),
            model_id=str(getattr(request, "model_id")),
            endpoint_scope=str(getattr(request, "endpoint_scope")),
            upstream_scope=str(getattr(request, "upstream_scope")),
            ranking_price_source=str(getattr(request, "ranking_price_source")),
            ranking_input_per_million=getattr(
                request,
                "ranking_input_per_million",
            ),
            ranking_output_per_million=getattr(
                request,
                "ranking_output_per_million",
            ),
        )
    except Exception:  # noqa: BLE001 - optional cost evidence fails closed
        return None


def _router_dynamic_cache_domain_guard(
    provider_config: object,
    *,
    session_epoch: int,
    upstream: str,
    provider_routing_strict: bool,
    chat_config: object,
    credential_namespace_token: object | None,
) -> object | None:
    provider = str(getattr(provider_config, "provider", "") or "").strip()
    model = str(getattr(provider_config, "model", "") or "").strip()
    normalized_upstream = str(upstream or "").strip().casefold()
    thinking_enabled = bool(getattr(chat_config, "thinking", False))
    raw_thinking_level = getattr(chat_config, "thinking_level", None)
    effective_thinking_level = str(
        getattr(raw_thinking_level, "value", raw_thinking_level) or ""
    ).strip()
    if not thinking_enabled:
        effective_thinking_level = "off"
    elif not effective_thinking_level:
        effective_thinking_level = "enabled"
    raw_thinking_budget = getattr(chat_config, "thinking_budget_tokens", None)
    thinking_budget_tokens = (
        0
        if not thinking_enabled
        else (
            raw_thinking_budget
            if type(raw_thinking_budget) is int and raw_thinking_budget >= 0
            else None
        )
    )
    return build_cache_domain_guard(
        session_epoch=session_epoch,
        role="single",
        topology="single",
        provider=provider,
        requested_model=model,
        base_url=str(getattr(provider_config, "base_url", "") or ""),
        upstream_provider=normalized_upstream,
        provider_routing_strict=provider_routing_strict,
        allow_fallbacks=not provider_routing_strict,
        thinking_enabled=thinking_enabled,
        effective_thinking_level=effective_thinking_level,
        thinking_budget_tokens=thinking_budget_tokens,
        credential_namespace_token=credential_namespace_token,
    )


def _router_dynamic_model_matches_frozen_alias(
    requested_model: str,
    actual_model: str,
    actual_model_aliases: Sequence[str],
) -> bool:
    if not isinstance(actual_model_aliases, Sequence) or isinstance(
        actual_model_aliases,
        (str, bytes, bytearray),
    ):
        return False
    normalized_aliases: set[str] = {requested_model.casefold()}
    for alias in actual_model_aliases:
        if not isinstance(alias, str) or not alias.strip():
            return False
        normalized_aliases.add(alias.strip().casefold())
    return bool(actual_model and actual_model.casefold() in normalized_aliases)


def _router_dynamic_actual_identity_matches(
    event: ProviderDoneEvent,
    provider_config: object,
    *,
    actual_model_aliases: Sequence[str] = (),
) -> tuple[str, str] | None:
    actual_provider = str(getattr(event, "provider", "") or "").strip().casefold()
    actual_model = str(getattr(event, "model", "") or "").strip()
    requested_provider = str(getattr(provider_config, "provider", "") or "").strip().casefold()
    requested_model = str(getattr(provider_config, "model", "") or "").strip()
    if (
        not actual_provider
        or not requested_provider
        or not requested_model
        or actual_provider != requested_provider
        or not _router_dynamic_model_matches_frozen_alias(
            requested_model,
            actual_model,
            actual_model_aliases,
        )
    ):
        return None
    # Alias equivalence was proven against the route's frozen registry row.
    # Canonicalize before crossing into the neutral receipt contract, which
    # intentionally requires exact requested/actual identity equality.
    requested_identity = f"{requested_provider}:{requested_model}"
    return (
        requested_identity,
        requested_identity,
    )


_RouterSingleCleanupKey = tuple[str, str, str]
_ROUTER_SINGLE_CLEANUP_LOCK = threading.Lock()
_ROUTER_SINGLE_PENDING_CLEANUPS: dict[_RouterSingleCleanupKey, set[asyncio.Future[Any]]] = {}
_ROUTER_SINGLE_POISONED_CLEANUPS: set[_RouterSingleCleanupKey] = set()
_ROUTER_SINGLE_FINISHED_CLEANUPS: set[asyncio.Future[Any]] = set()


def _router_single_cleanup_key(
    provider: str,
    model: str,
    upstream: str,
) -> _RouterSingleCleanupKey:
    return (
        str(provider or "").strip().casefold(),
        str(model or "").strip(),
        str(upstream or "").strip().casefold(),
    )


def _router_single_cleanup_block_reason(key: _RouterSingleCleanupKey) -> str:
    """Return why another physical request must not overlap this deployment."""

    with _ROUTER_SINGLE_CLEANUP_LOCK:
        if key in _ROUTER_SINGLE_POISONED_CLEANUPS:
            return "router_single_cleanup_poisoned"
        if _ROUTER_SINGLE_PENDING_CLEANUPS.get(key):
            return "router_single_cleanup_pending"
    return ""


def _mark_router_single_cleanup_poisoned(key: _RouterSingleCleanupKey) -> None:
    with _ROUTER_SINGLE_CLEANUP_LOCK:
        _ROUTER_SINGLE_POISONED_CLEANUPS.add(key)


def _settle_router_single_cleanup(
    key: _RouterSingleCleanupKey,
    future: asyncio.Future[Any],
) -> bool:
    """Publish close proof, poisoning the deployment on any failed cleanup."""

    succeeded = False
    if future.done():
        try:
            future.result()
        except BaseException:  # cleanup failure must be consumed and retained
            pass
        else:
            succeeded = True
    with _ROUTER_SINGLE_CLEANUP_LOCK:
        pending = _ROUTER_SINGLE_PENDING_CLEANUPS.get(key)
        if pending is not None:
            pending.discard(future)
            if not pending:
                _ROUTER_SINGLE_PENDING_CLEANUPS.pop(key, None)
        _ROUTER_SINGLE_FINISHED_CLEANUPS.discard(future)
        if not succeeded:
            _ROUTER_SINGLE_POISONED_CLEANUPS.add(key)
    return succeeded


def _track_router_single_cleanup(
    key: _RouterSingleCleanupKey,
    future: asyncio.Future[Any],
) -> None:
    """Register physical cleanup before its first cancellable await."""

    with _ROUTER_SINGLE_CLEANUP_LOCK:
        _ROUTER_SINGLE_PENDING_CLEANUPS.setdefault(key, set()).add(future)

    def _done(done: asyncio.Future[Any]) -> None:
        with _ROUTER_SINGLE_CLEANUP_LOCK:
            owner_finished = done in _ROUTER_SINGLE_FINISHED_CLEANUPS
        if owner_finished:
            _settle_router_single_cleanup(key, done)

    future.add_done_callback(_done)


def _finish_router_single_cleanup(
    key: _RouterSingleCleanupKey,
    future: asyncio.Future[Any],
) -> None:
    """Open the gate only after health settlement and physical cleanup."""

    with _ROUTER_SINGLE_CLEANUP_LOCK:
        _ROUTER_SINGLE_FINISHED_CLEANUPS.add(future)
        cleanup_done = future.done()
    if cleanup_done:
        _settle_router_single_cleanup(key, future)


class _RouterSingleDirectProvider:
    """Single-route physical dispatch guard around one ordinary provider."""

    _STREAM_CLOSE_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        provider: Any,
        provider_config: Any,
        *,
        health_ledger: ProviderHealthLedger | None,
        absolute_deadline: float | None,
        frozen_catalog: Mapping[str, Any],
        enforces_routed_thinking_policy: bool,
        turn_metadata: dict[str, Any] | None = None,
        deployment_version: str | None = None,
        cache_affinity_context: _RouterDynamicCacheAffinityCollectionContext | None = None,
        cache_affinity_receipt_sink: (
            Callable[[_RouterDynamicCacheAffinityReceiptBatch], None] | None
        ) = None,
        cache_affinity_generation_getter: Callable[[], int] | None = None,
        cache_affinity_actual_model_aliases: Sequence[str] = (),
        cache_affinity_credential_namespace_token: object | None = None,
    ) -> None:
        from opensquilla.provider.deployment import (
            canonicalize_provider_routing_upstream,
        )

        self._provider = provider
        self._provider_config = provider_config
        self._health_ledger = health_ledger
        self._absolute_deadline = absolute_deadline
        self._router_single_frozen_catalog = dict(frozen_catalog)
        self._enforces_routed_thinking_policy = bool(enforces_routed_thinking_policy)
        self._turn_metadata = turn_metadata
        self._deployment_version = str(deployment_version or "").strip() or None
        self._cache_affinity_context = cache_affinity_context
        self._cache_affinity_receipt_sink = cache_affinity_receipt_sink
        self._cache_affinity_generation_getter = cache_affinity_generation_getter
        self._cache_affinity_actual_model_aliases = cache_affinity_actual_model_aliases
        self._cache_affinity_credential_namespace_token = cache_affinity_credential_namespace_token
        self._cache_affinity_chat_sequence = 0
        self._local_dispatch_blocked = False
        # Route-level physical evidence spans every Agent chat iteration (for
        # example an initial tool-use response followed by the final answer).
        # It is monotonic: a later local/zero-request failure cannot erase an
        # earlier confirmed provider call.
        self._fixed_route_confirmed_physical_requests = 0
        self._fixed_route_executed_provider: str | None = None
        self._fixed_route_executed_model: str | None = None
        self._upstream = canonicalize_provider_routing_upstream(
            provider_config.provider_routing.get(provider_config.model, "")
        )
        self._cleanup_key = _router_single_cleanup_key(
            self.active_provider_id,
            self.active_model_id,
            self._upstream,
        )

    def _update_fixed_route_dispatch(self, **updates: Any) -> None:
        metadata = self._turn_metadata
        if metadata is None:
            return
        trace_value = metadata.get("fixed_four_tier_v2_decision")
        if not isinstance(trace_value, Mapping):
            return
        trace = copy.deepcopy(dict(trace_value))
        dispatch_value = trace.get("dispatch")
        dispatch = (
            copy.deepcopy(dict(dispatch_value)) if isinstance(dispatch_value, Mapping) else {}
        )
        dispatch.update(updates)
        trace["dispatch"] = dispatch
        metadata["fixed_four_tier_v2_decision"] = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    @property
    def active_provider_id(self) -> str:
        return str(self._provider_config.provider or "")

    @property
    def active_model_id(self) -> str:
        return str(self._provider_config.model or "")

    @property
    def provider_config(self) -> Any:
        return self._provider_config

    @property
    def router_single_frozen_catalog(self) -> dict[str, Any]:
        return dict(self._router_single_frozen_catalog)

    @property
    def enforces_routed_thinking_policy(self) -> bool:
        return self._enforces_routed_thinking_policy

    @property
    def retry_failed_call_safe(self) -> bool:
        if self._local_dispatch_blocked:
            return False
        return getattr(self._provider, "retry_failed_call_safe", True) is not False

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        return self._chat(messages, tools=tools, config=config)

    def _remaining_seconds(self) -> float | None:
        if self._absolute_deadline is None:
            return None
        return self._absolute_deadline - time.monotonic()

    def _begin_health_attempt(self) -> dict[str, Any]:
        cleanup_reason = _router_single_cleanup_block_reason(self._cleanup_key)
        if cleanup_reason:
            return {
                "allowed": False,
                "tracked": False,
                "state": "cleanup_blocked",
                "reason": cleanup_reason,
            }
        begin_attempt = getattr(self._health_ledger, "begin_attempt", None)
        if not callable(begin_attempt):
            return {"allowed": True, "tracked": False}
        admission = begin_attempt(
            self.active_provider_id,
            self.active_model_id,
            upstream=self._upstream,
            never_strand_exempt=False,
        )
        if not isinstance(admission, Mapping):
            raise RuntimeError("provider health admission returned invalid evidence")
        return {**dict(admission), "tracked": True}

    def _cancel_health_attempt(self, admission: Mapping[str, Any]) -> None:
        if admission.get("tracked") is not True:
            return
        cancel_attempt = getattr(self._health_ledger, "cancel_attempt", None)
        if not callable(cancel_attempt):
            return
        cancel_attempt(
            self.active_provider_id,
            self.active_model_id,
            upstream=self._upstream,
            lease_token=admission.get("lease_token"),
        )

    def _record_health_failure(
        self,
        admission: Mapping[str, Any],
        event: ProviderErrorEvent,
    ) -> None:
        if admission.get("tracked") is not True:
            return
        record_failure = getattr(self._health_ledger, "record_failure", None)
        if not callable(record_failure):
            return
        kind = classify_provider_error(
            provider_name=self.active_provider_id,
            status_code=int(event.code) if str(event.code).isdigit() else None,
            raw_code=event.code,
            message=event.message,
        )
        record_failure(
            self.active_provider_id,
            self.active_model_id,
            kind,
            retry_after_s=getattr(event, "retry_after_s", None),
            upstream=self._upstream,
            lease_token=admission.get("lease_token"),
        )

    def _record_health_success(self, admission: Mapping[str, Any]) -> None:
        if admission.get("tracked") is not True:
            return
        record_success = getattr(self._health_ledger, "record_success", None)
        if not callable(record_success):
            return
        record_success(
            self.active_provider_id,
            self.active_model_id,
            upstream=self._upstream,
            attempt_started_at=admission.get("started_at"),
            lease_token=admission.get("lease_token"),
        )

    async def _close_stream_with_cleanup_reserve(
        self,
        stream: Any,
        *,
        require_aclose: bool,
        cleanup_ownership: list[asyncio.Future[Any]],
    ) -> bool:
        """Close one stream, gating only when no terminal/EOF proof exists."""

        try:
            close = getattr(stream, "aclose", None)
        except BaseException as exc:  # descriptor access is a provider boundary
            if require_aclose:
                _mark_router_single_cleanup_poisoned(self._cleanup_key)
            log.warning(
                "router_single.direct_stream_close_failed",
                provider=self.active_provider_id,
                model=self.active_model_id,
                error_type=type(exc).__name__,
            )
            return not require_aclose
        if not callable(close):
            if require_aclose:
                _mark_router_single_cleanup_poisoned(self._cleanup_key)
            return not require_aclose
        try:
            close_future = asyncio.ensure_future(close())
        except BaseException as exc:  # provider close construction is untrusted
            if require_aclose:
                _mark_router_single_cleanup_poisoned(self._cleanup_key)
            log.warning(
                "router_single.direct_stream_close_failed",
                provider=self.active_provider_id,
                model=self.active_model_id,
                error_type=type(exc).__name__,
            )
            return not require_aclose

        if require_aclose:
            # Register before the first cancellable await. A timeout or caller
            # cancellation transfers ownership to the process-level deployment
            # gate until the physical close task really terminates.
            _track_router_single_cleanup(self._cleanup_key, close_future)
            cleanup_ownership.append(close_future)
        else:
            # Terminal/EOF already proves the physical request ended. The
            # adapter's optional aclose is best-effort resource cleanup only.
            def _consume(done: asyncio.Future[Any]) -> None:
                try:
                    done.result()
                except BaseException:
                    pass

            close_future.add_done_callback(_consume)
        try:
            await asyncio.wait_for(
                asyncio.shield(close_future),
                timeout=self._STREAM_CLOSE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "router_single.direct_stream_close_timeout",
                provider=self.active_provider_id,
                model=self.active_model_id,
            )
            if not require_aclose:
                close_future.cancel()
            return not require_aclose
        except asyncio.CancelledError:
            if not require_aclose:
                close_future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask result
            log.warning(
                "router_single.direct_stream_close_failed",
                provider=self.active_provider_id,
                model=self.active_model_id,
                error_type=type(exc).__name__,
            )
            return not require_aclose
        return True

    async def _chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self._local_dispatch_blocked = False
        affinity_context = self._cache_affinity_context
        generation_getter = self._cache_affinity_generation_getter
        if (
            affinity_context is not None
            and generation_getter is not None
            and generation_getter() != affinity_context.selection_generation
        ):
            self._local_dispatch_blocked = True
            yield ProviderErrorEvent(
                message=("router_dynamic cache continuity changed before dispatch"),
                code="router_dynamic_cache_generation_changed",
                request_started=False,
                physical_request_count=0,
            )
            return
        remaining = self._remaining_seconds()
        if remaining is not None and remaining <= 0:
            self._local_dispatch_blocked = True
            yield ProviderErrorEvent(
                message="router_single absolute deadline expired before dispatch",
                code="router_single_absolute_deadline",
                request_started=False,
                physical_request_count=0,
            )
            return

        effective_config = config
        if remaining is not None:
            model_copy = getattr(config, "model_copy", None)
            if callable(model_copy):
                configured_timeout = float(getattr(config, "timeout", remaining) or remaining)
                effective_config = model_copy(
                    update={"timeout": min(configured_timeout, remaining)}
                )
        remaining = self._remaining_seconds()
        if remaining is not None and remaining <= 0:
            self._local_dispatch_blocked = True
            yield ProviderErrorEvent(
                message="router_single absolute deadline expired before dispatch",
                code="router_single_absolute_deadline",
                request_started=False,
                physical_request_count=0,
            )
            return

        # Admission is deliberately adjacent to the lazy provider call. A
        # rejected half-open lease is a zero-request terminal failure and the
        # direct route never selects another model in response.
        try:
            admission = self._begin_health_attempt()
        except Exception:
            self._local_dispatch_blocked = True
            yield ProviderErrorEvent(
                message="router_single health admission is unavailable",
                code="router_single_health_admission_unavailable",
                request_started=False,
                physical_request_count=0,
            )
            return
        if admission.get("allowed") is not True:
            self._local_dispatch_blocked = True
            rejected_updates: dict[str, Any] = {
                "health_admission": "rejected",
                "health_reason": str(admission.get("reason") or "unknown"),
                "last_call_request_started": False,
                "last_call_physical_request_count": 0,
                "execution_evidence": "health_admission_rejected",
            }
            if self._fixed_route_confirmed_physical_requests == 0:
                rejected_updates.update(
                    physical_request_started=False,
                    physical_request_count=0,
                    executed_provider=None,
                    executed_model=None,
                )
            self._update_fixed_route_dispatch(**rejected_updates)
            yield ProviderErrorEvent(
                message="router_single selected deployment is not healthy",
                code=str(admission.get("reason") or "router_single_health_admission_rejected"),
                request_started=False,
                physical_request_count=0,
            )
            return

        self._update_fixed_route_dispatch(
            health_admission="allowed",
            health_reason=str(admission.get("reason") or "allowed"),
            last_call_request_started=False,
            last_call_physical_request_count=0,
        )

        stream: Any = None
        settled = False
        physical_started = False
        audit_physical_started: bool | None = False
        audit_physical_count = 0
        close_attempted = False
        close_proven = False
        terminal_event: Any = None
        terminal_observed_at_monotonic: float | None = None
        stream_boundary_observed = False
        cleanup_ownership: list[asyncio.Future[Any]] = []
        affinity_batch: _RouterDynamicCacheAffinityReceiptBatch | None = None
        affinity_batch_published = False
        affinity_chat_call_id = ""
        affinity_chat_call_sequence = -1
        affinity_physical_attempt_id = ""
        affinity_runtime_generation = -1

        def incomplete_stream_event(message: str) -> ProviderErrorEvent:
            return ProviderErrorEvent(
                message=message,
                code="incomplete_stream",
                request_started=True,
                physical_request_count=1,
            )

        def explicit_zero_request_error(event: ProviderErrorEvent) -> bool:
            count = event.physical_request_count
            valid_zero_count = count is None or (
                isinstance(count, int) and not isinstance(count, bool) and count == 0
            )
            return event.request_started is False and valid_zero_count

        def record_dispatch_evidence(event: Any) -> None:
            """Update audit identity only from an observed provider event."""

            nonlocal audit_physical_count, audit_physical_started

            def confirm_physical_requests(count: int) -> int:
                """Merge per-chat evidence into the monotonic route total."""

                nonlocal audit_physical_count
                confirmed = max(1, count)
                if confirmed > audit_physical_count:
                    self._fixed_route_confirmed_physical_requests += (
                        confirmed - audit_physical_count
                    )
                    audit_physical_count = confirmed
                return self._fixed_route_confirmed_physical_requests

            def retain_or_clear_route_identity() -> dict[str, Any]:
                if self._fixed_route_confirmed_physical_requests > 0:
                    return {
                        "physical_request_started": True,
                        "physical_request_count": (self._fixed_route_confirmed_physical_requests),
                        "executed_provider": self._fixed_route_executed_provider,
                        "executed_model": self._fixed_route_executed_model,
                    }
                if self._turn_metadata is not None:
                    self._turn_metadata.pop("executed_provider", None)
                    self._turn_metadata.pop("executed_model", None)
                return {
                    "physical_request_started": False,
                    "physical_request_count": 0,
                    "executed_provider": None,
                    "executed_model": None,
                }

            if isinstance(event, ProviderErrorEvent):
                if explicit_zero_request_error(event):
                    audit_physical_started = False
                    self._update_fixed_route_dispatch(
                        **retain_or_clear_route_identity(),
                        last_call_request_started=False,
                        last_call_physical_request_count=0,
                        execution_evidence="provider_zero_request_error",
                    )
                    return
                count = event.physical_request_count
                if event.request_started is True or (
                    isinstance(count, int) and not isinstance(count, bool) and count > 0
                ):
                    confirmed_count = count if isinstance(count, int) and count > 0 else 1
                    audit_physical_started = True
                    route_count = confirm_physical_requests(confirmed_count)
                    executed_provider = str(
                        getattr(event, "provider", "") or self.active_provider_id
                    )
                    executed_model = str(getattr(event, "model", "") or self.active_model_id)
                    self._fixed_route_executed_provider = executed_provider
                    self._fixed_route_executed_model = executed_model
                    if self._turn_metadata is not None:
                        self._turn_metadata["executed_provider"] = executed_provider
                        self._turn_metadata["executed_model"] = executed_model
                    self._update_fixed_route_dispatch(
                        physical_request_started=True,
                        physical_request_count=route_count,
                        last_call_request_started=True,
                        last_call_physical_request_count=confirmed_count,
                        execution_evidence="provider_error_request_started",
                        executed_provider=executed_provider,
                        executed_model=executed_model,
                    )
                    return
                audit_physical_started = None
                unknown_updates: dict[str, Any] = {
                    "last_call_request_started": None,
                    "last_call_physical_request_count": None,
                    "execution_evidence": "provider_error_request_unknown",
                }
                if self._fixed_route_confirmed_physical_requests == 0:
                    if self._turn_metadata is not None:
                        self._turn_metadata.pop("executed_provider", None)
                        self._turn_metadata.pop("executed_model", None)
                    unknown_updates.update(
                        physical_request_started=None,
                        physical_request_count=None,
                        executed_provider=None,
                        executed_model=None,
                    )
                self._update_fixed_route_dispatch(
                    **unknown_updates,
                )
                return
            event_kind = str(getattr(event, "kind", "") or "")
            if event_kind not in {
                "text_delta",
                "reasoning_delta",
                "tool_use_start",
                "tool_use_delta",
                "tool_use_end",
                "done",
            }:
                return
            audit_physical_started = True
            route_count = confirm_physical_requests(1)
            executed_provider = str(getattr(event, "provider", "") or self.active_provider_id)
            executed_model = str(getattr(event, "model", "") or self.active_model_id)
            self._fixed_route_executed_provider = executed_provider
            self._fixed_route_executed_model = executed_model
            if self._turn_metadata is not None:
                self._turn_metadata["executed_provider"] = executed_provider
                self._turn_metadata["executed_model"] = executed_model
            self._update_fixed_route_dispatch(
                physical_request_started=True,
                physical_request_count=route_count,
                last_call_request_started=True,
                last_call_physical_request_count=audit_physical_count,
                execution_evidence=f"provider_{event_kind}",
                executed_provider=executed_provider,
                executed_model=executed_model,
                requested_deployment_version=self._deployment_version,
                executed_deployment_version=None,
                deployment_version_attested=False,
            )

        async def close_once(*, require_aclose: bool) -> bool:
            nonlocal close_attempted, close_proven
            if close_attempted:
                return close_proven
            close_attempted = True
            close_proven = stream is None or await self._close_stream_with_cleanup_reserve(
                stream,
                require_aclose=require_aclose,
                cleanup_ownership=cleanup_ownership,
            )
            return close_proven

        def finish_cleanup_ownership() -> None:
            while cleanup_ownership:
                _finish_router_single_cleanup(
                    self._cleanup_key,
                    cleanup_ownership.pop(),
                )

        try:
            if affinity_context is not None and self._cache_affinity_receipt_sink is not None:
                self._cache_affinity_chat_sequence += 1
                affinity_chat_call_sequence = self._cache_affinity_chat_sequence
                affinity_chat_call_id = uuid.uuid4().hex
                affinity_physical_attempt_id = uuid.uuid4().hex
                affinity_runtime_generation = affinity_context.selection_generation
            stream = self._provider.chat(
                messages,
                tools=tools,
                config=effective_config,
            )

            async def forward() -> AsyncIterator[Any]:
                nonlocal physical_started
                nonlocal stream_boundary_observed
                nonlocal terminal_event
                nonlocal terminal_observed_at_monotonic
                iterator = stream.__aiter__()
                while True:
                    # Crossing the first __anext__ boundary is the earliest
                    # reliable evidence that a lazy provider may have started
                    # its physical request.
                    if not physical_started:
                        physical_started = True
                        awaiting_updates: dict[str, Any] = {
                            "health_admission": "allowed",
                            "last_call_request_started": None,
                            "last_call_physical_request_count": None,
                            "execution_evidence": "awaiting_first_provider_event",
                            "requested_deployment_version": self._deployment_version,
                            "executed_deployment_version": None,
                            "deployment_version_attested": False,
                        }
                        if self._fixed_route_confirmed_physical_requests == 0:
                            awaiting_updates.update(
                                physical_request_started=None,
                                physical_request_count=None,
                                executed_provider=None,
                                executed_model=None,
                            )
                        self._update_fixed_route_dispatch(
                            **awaiting_updates,
                        )
                    try:
                        event = await iterator.__anext__()
                    except StopAsyncIteration:
                        stream_boundary_observed = True
                        return
                    record_dispatch_evidence(event)
                    is_terminal = isinstance(event, ProviderErrorEvent) or (
                        getattr(event, "kind", "") == "done"
                    )
                    if is_terminal:
                        # Stop at the protocol terminal instead of probing for
                        # another event. The terminal proves the physical
                        # boundary; optional aclose remains bounded resource
                        # cleanup and completes before the terminal is exposed.
                        terminal_event = event
                        terminal_observed_at_monotonic = time.monotonic()
                        stream_boundary_observed = True
                        return
                    yield event

            remaining = self._remaining_seconds()
            if remaining is None:
                async for event in forward():
                    yield event
            elif remaining <= 0:
                self._local_dispatch_blocked = True
                self._cancel_health_attempt(admission)
                settled = True
                yield ProviderErrorEvent(
                    message="router_single absolute deadline expired before dispatch",
                    code="router_single_absolute_deadline",
                    request_started=False,
                    physical_request_count=0,
                )
            else:
                try:
                    async with asyncio.timeout(remaining):
                        async for event in forward():
                            yield event
                except TimeoutError:
                    self._local_dispatch_blocked = True
                    timeout_event = ProviderErrorEvent(
                        message="router_single absolute deadline expired",
                        code="router_single_absolute_deadline",
                        # Conservative operational evidence retained for
                        # retry/cleanup governance. The fixed-route audit below
                        # separately records unknown until a provider event
                        # proves a physical request.
                        request_started=physical_started,
                        physical_request_count=1 if physical_started else 0,
                    )
                    if audit_physical_started is not True:
                        timeout_updates: dict[str, Any] = {
                            "last_call_request_started": None,
                            "last_call_physical_request_count": None,
                            "execution_evidence": "provider_timeout_request_unknown",
                        }
                        if self._fixed_route_confirmed_physical_requests == 0:
                            timeout_updates.update(
                                physical_request_started=None,
                                physical_request_count=None,
                                executed_provider=None,
                                executed_model=None,
                            )
                        self._update_fixed_route_dispatch(**timeout_updates)
                    await close_once(require_aclose=physical_started)
                    if physical_started and not settled:
                        # The absolute deadline cancelled an in-flight physical
                        # request. Count it as a transient deployment failure so
                        # a half-open probe is not released as a local cancel.
                        self._record_health_failure(
                            admission,
                            ProviderErrorEvent(
                                message="router_single provider request timeout",
                                code="timeout",
                                request_started=True,
                                physical_request_count=1,
                            ),
                        )
                        settled = True
                    finish_cleanup_ownership()
                    yield timeout_event
            if not settled:
                await close_once(require_aclose=not stream_boundary_observed)
                if terminal_event is None:
                    self._record_health_failure(
                        admission,
                        incomplete_stream_event("provider stream ended before terminal event"),
                    )
                    settled = True
                elif not close_proven:
                    self._local_dispatch_blocked = True
                    failure = incomplete_stream_event("provider stream could not be closed")
                    self._record_health_failure(admission, failure)
                    settled = True
                    yield failure
                elif isinstance(terminal_event, ProviderErrorEvent):
                    if explicit_zero_request_error(terminal_event):
                        self._cancel_health_attempt(admission)
                    else:
                        self._record_health_failure(admission, terminal_event)
                    settled = True
                    yield terminal_event
                else:
                    self._record_health_success(admission)
                    settled = True
                    if (
                        affinity_context is not None
                        and self._cache_affinity_receipt_sink is not None
                    ):
                        receipts: tuple[_RouterDynamicCacheAffinityReceipt, ...] = ()
                        if isinstance(terminal_event, ProviderDoneEvent):
                            actual_identity = _router_dynamic_actual_identity_matches(
                                terminal_event,
                                self._provider_config,
                                actual_model_aliases=(self._cache_affinity_actual_model_aliases),
                            )
                            domain_guard = _router_dynamic_cache_domain_guard(
                                self._provider_config,
                                session_epoch=affinity_context.session_epoch,
                                upstream=self._upstream,
                                provider_routing_strict=bool(
                                    getattr(
                                        self._provider,
                                        "_provider_routing_strict",
                                        False,
                                    )
                                ),
                                chat_config=effective_config,
                                credential_namespace_token=(
                                    self._cache_affinity_credential_namespace_token
                                ),
                            )
                            receipt = (
                                build_cache_affinity_receipt(
                                    physical_attempt_id=(affinity_physical_attempt_id),
                                    role="single",
                                    topology="single",
                                    execution_slot=0,
                                    requested_identity=actual_identity[0],
                                    actual_identity=actual_identity[1],
                                    cache_domain_guard=domain_guard,
                                    cached_tokens=terminal_event.cached_tokens,
                                    cache_write_tokens=(terminal_event.cache_write_tokens),
                                    observed_at_monotonic=(terminal_observed_at_monotonic),
                                )
                                if actual_identity is not None
                                else None
                            )
                            if receipt is not None:
                                receipts = (receipt,)
                        affinity_batch = _RouterDynamicCacheAffinityReceiptBatch(
                            turn_id=affinity_context.turn_id,
                            decision_id=affinity_context.decision_id,
                            provider_instance_token=(affinity_context.provider_instance_token),
                            provider_instance_generation=(
                                affinity_context.provider_instance_generation
                            ),
                            chat_call_id=affinity_chat_call_id,
                            chat_call_sequence=affinity_chat_call_sequence,
                            runtime_generation=affinity_runtime_generation,
                            topology="single",
                            receipts=receipts,
                        )
                    yield terminal_event
        except Exception:
            if audit_physical_started is not True:
                audit_physical_started = None
                if (
                    self._fixed_route_confirmed_physical_requests == 0
                    and self._turn_metadata is not None
                ):
                    self._turn_metadata.pop("executed_provider", None)
                    self._turn_metadata.pop("executed_model", None)
                exception_updates: dict[str, Any] = {
                    "last_call_request_started": None,
                    "last_call_physical_request_count": None,
                    "execution_evidence": "provider_stream_exception_unknown",
                }
                if self._fixed_route_confirmed_physical_requests == 0:
                    exception_updates.update(
                        physical_request_started=None,
                        physical_request_count=None,
                        executed_provider=None,
                        executed_model=None,
                    )
                self._update_fixed_route_dispatch(**exception_updates)
            if physical_started and not settled:
                try:
                    await close_once(require_aclose=True)
                finally:
                    self._record_health_failure(
                        admission,
                        incomplete_stream_event("provider stream raised before terminal event"),
                    )
                    settled = True
            raise
        finally:
            try:
                await close_once(require_aclose=(physical_started and not stream_boundary_observed))
            finally:
                if not settled:
                    if physical_started and not stream_boundary_observed and not close_proven:
                        self._record_health_failure(
                            admission,
                            incomplete_stream_event("provider stream could not be closed"),
                        )
                        settled = True
                    else:
                        self._cancel_health_attempt(admission)
                finish_cleanup_ownership()
                if affinity_batch is not None and not affinity_batch_published:
                    affinity_batch_published = True
                    receipt_sink = self._cache_affinity_receipt_sink
                    if receipt_sink is not None:
                        try:
                            receipt_sink(affinity_batch)
                        except Exception:  # noqa: BLE001 - evidence must not fail a turn
                            log.warning(
                                "router_single.cache_affinity_receipt_sink_failed",
                                provider=self.active_provider_id,
                                model=self.active_model_id,
                                exc_info=True,
                            )


class _SelectorFallbackProvider:
    """Provider wrapper that switches to selector fallback on pre-content errors."""

    def __init__(
        self,
        provider: Any,
        selector: Any,
        turn_metadata: dict[str, Any] | None = None,
        *,
        health_ledger: ProviderHealthLedger | None = None,
        cache_affinity_credential_failure_callback: Callable[[], None] | None = None,
    ) -> None:
        self._provider = provider
        self._selector = selector
        self._selector_fallback_admission = (
            getattr(provider, "selector_fallback_allowed", None)
            if getattr(
                provider,
                "selector_fallback_governance_active",
                False,
            )
            is True
            else None
        )
        self._selector_canary_route_blocked = False
        self._turn_metadata = turn_metadata
        # Opt-in provider health ledger (engine/routing/health.py). None —
        # the default everywhere today — makes every ledger hook below a
        # no-op, keeping the default fallback path byte-identical.
        self._health_ledger = health_ledger
        self._cache_affinity_credential_failure_callback = (
            cache_affinity_credential_failure_callback
        )
        self._pending_retry_metadata_update: dict[str, Any] | None = None
        self._retry_scope_provider_bindings: dict[str, Any | None] = {}
        self._retry_scope_local_remaining: dict[str, int | None] = {}
        self._retry_scope_handoff_sources: dict[str, object] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def begin_provider_retry_scope(
        self,
        scope_id: str,
        *,
        max_additional_physical_requests: int = 3,
    ) -> bool:
        """Bind the wrapper scope to the provider instance active at begin."""

        if (
            isinstance(max_additional_physical_requests, bool)
            or not isinstance(max_additional_physical_requests, int)
            or max_additional_physical_requests < 0
        ):
            raise ValueError("max_additional_physical_requests must be a non-negative integer")

        if scope_id in self._retry_scope_provider_bindings:
            return False
        handoff_source = self._retry_scope_handoff_sources.get(scope_id)
        handoff_remaining = self._retry_scope_local_remaining.get(scope_id)
        handoff_active = (
            handoff_source is not None
            and isinstance(handoff_remaining, int)
            and not isinstance(handoff_remaining, bool)
            and handoff_remaining >= 0
        )
        effective_max = (
            min(max_additional_physical_requests, handoff_remaining)
            if handoff_active
            else max_additional_physical_requests
        )
        provider = self.primary
        begin = getattr(provider, "begin_provider_retry_scope", None)
        if callable(begin):
            result = begin(
                scope_id,
                max_additional_physical_requests=effective_max,
            )
            if result is False or result not in (None, True):
                return result
            self._retry_scope_provider_bindings[scope_id] = provider
        else:
            self._retry_scope_provider_bindings[scope_id] = None
        # This mirror is the one wrapper-level ledger used by both selector
        # fallback and Agent retries. Native providers remain authoritative
        # for their internal recoveries, so a delegated reservation must pass
        # both this mirror and the bound provider's own scope.
        self._retry_scope_local_remaining[scope_id] = effective_max
        self._retry_scope_handoff_sources.pop(scope_id, None)
        return True

    def end_provider_retry_scope(self, scope_id: str) -> bool:
        """End the scope on the exact primary instance that accepted it."""

        if scope_id not in self._retry_scope_provider_bindings:
            return False
        self._retry_scope_local_remaining.pop(scope_id, None)
        provider = self._retry_scope_provider_bindings.pop(scope_id)
        if provider is None:
            return True
        end = getattr(provider, "end_provider_retry_scope", None)
        if not callable(end):
            return True
        return end(scope_id)

    def reserve_provider_retry_physical_request(
        self,
        scope_id: str,
        *,
        physical_request_count: int = 1,
    ) -> bool:
        """Reserve from the one ledger shared by fallback and Agent retries."""

        if (
            isinstance(physical_request_count, bool)
            or not isinstance(physical_request_count, int)
            or physical_request_count <= 0
        ):
            raise ValueError("physical_request_count must be a positive integer")
        if scope_id not in self._retry_scope_provider_bindings:
            return False
        remaining = self._retry_scope_local_remaining.get(scope_id)
        if (
            not isinstance(remaining, int)
            or isinstance(remaining, bool)
            or remaining < physical_request_count
        ):
            return False
        provider = self._retry_scope_provider_bindings[scope_id]
        if provider is not None:
            reserve = getattr(
                provider,
                "reserve_provider_retry_physical_request",
                None,
            )
            if not callable(reserve):
                # A native scope without a reservation hook may recover only
                # inside that provider. Starting another physical request at
                # this wrapper would create an independent second ledger.
                return False
            if (
                reserve(
                    scope_id,
                    physical_request_count=physical_request_count,
                )
                is not True
            ):
                return False
        self._retry_scope_local_remaining[scope_id] = remaining - physical_request_count
        return True

    def can_handoff_provider_retry_scope_to(
        self,
        replacement_provider: object,
        scope_id: str,
    ) -> bool:
        """Prove a legacy wrapper transition retains the exact same ledger."""

        return bool(
            isinstance(replacement_provider, _SelectorFallbackProvider)
            and scope_id in self._retry_scope_provider_bindings
            and self._retry_scope_provider_bindings[scope_id] is None
            and replacement_provider._retry_scope_handoff_sources.get(scope_id) is self
            and replacement_provider._retry_scope_local_remaining
            is self._retry_scope_local_remaining
        )

    @property
    def primary(self) -> Any:
        """Provider currently owned by this selector/accounting wrapper."""

        return self._provider

    @property
    def accounts_physical_usage(self) -> bool:
        """The wrapper, rather than Agent, owns every selector chain leg."""

        return True

    @property
    def retry_failed_call_safe(self) -> bool:
        """Whether replaying the currently active provider call is safe."""

        return getattr(self._provider, "retry_failed_call_safe", True) is not False

    @property
    def prepare_retry_after_failure(
        self,
    ) -> Callable[[ProviderErrorEvent], ProviderRetryTransition | None] | None:
        """Expose retry replacement only while the active provider supports it."""

        prepare = getattr(self.primary, "prepare_retry_after_failure", None)
        return self._prepare_retry_after_failure if callable(prepare) else None

    def _prepare_retry_after_failure(
        self,
        event: ProviderErrorEvent,
    ) -> ProviderRetryTransition | None:
        """Delegate a zero-request roster replacement without dropping wrappers."""

        transition = prepare_provider_retry_after_failure(
            self.primary,
            event,
        )
        if transition is None:
            return None

        if not isinstance(transition.source_plan, Mapping) or not isinstance(
            transition.target_plan,
            Mapping,
        ):
            return None
        source_plan = copy.deepcopy(dict(transition.source_plan))
        target_plan = copy.deepcopy(dict(transition.target_plan))
        source_audit = _router_dynamic_decision_projection(source_plan)
        target_audit = _router_dynamic_decision_projection(target_plan)
        if source_audit is None or target_audit is None:
            return None
        source_decision_id = source_audit["decision_id"]
        target_decision_id = target_audit["decision_id"]
        source_parent_decision_id = source_plan.get("retry_parent_decision_id")
        if "retry_parent_decision_id" in source_plan:
            expected_parent_decision_id = source_parent_decision_id
        else:
            expected_parent_decision_id = source_decision_id
        target_parent_decision_id = target_plan.get("retry_parent_decision_id")
        target_exclusions = target_plan.get("retry_excluded_proposer_identities")
        target_retry_routing = target_plan.get("retry_routing")
        transition_exclusions = list(transition.excluded_identities)
        if (
            not isinstance(expected_parent_decision_id, str)
            or not expected_parent_decision_id.strip()
            or expected_parent_decision_id != expected_parent_decision_id.strip()
            or target_decision_id == source_decision_id
            or target_parent_decision_id != expected_parent_decision_id
            or not isinstance(target_exclusions, list)
            or not target_exclusions
            or target_exclusions != transition_exclusions
            or any(
                not _valid_router_dynamic_route_identity(identity) for identity in target_exclusions
            )
            or len(set(target_exclusions)) != len(target_exclusions)
            or target_plan.get("task_analysis_reused") is not True
            or not isinstance(target_plan.get("task_analysis_reuse"), Mapping)
            or not isinstance(target_retry_routing, Mapping)
            or target_retry_routing.get("parent_decision_id") != target_parent_decision_id
            or target_retry_routing.get("task_analysis_reused") is not True
            or target_retry_routing.get("excluded_proposer_identities") != transition_exclusions
        ):
            return None

        metadata = self._turn_metadata
        if metadata is not None:
            pending = metadata.get("router_dynamic_pending_route_plan")
            if isinstance(pending, Mapping) and pending.get("decision_id") != source_decision_id:
                return None

        replacement = _SelectorFallbackProvider(
            transition.replacement_provider,
            self._selector,
            self._turn_metadata,
            health_ledger=self._health_ledger,
        )
        transferable_scope_ids = {
            scope_id
            for scope_id, provider in self._retry_scope_provider_bindings.items()
            if provider is None
            and isinstance(
                self._retry_scope_local_remaining.get(scope_id),
                int,
            )
        }
        if transferable_scope_ids:
            replacement._retry_scope_local_remaining = self._retry_scope_local_remaining
            replacement._retry_scope_handoff_sources = {
                scope_id: self for scope_id in transferable_scope_ids
            }
        wrapped_transition = ProviderRetryTransition(
            replacement_provider=replacement,
            reason=transition.reason,
            source_roster_fingerprint=(transition.source_roster_fingerprint),
            target_roster_fingerprint=(transition.target_roster_fingerprint),
            excluded_identities=tuple(transition.excluded_identities),
            source_plan=source_plan,
            target_plan=target_plan,
            setup_physical_request_count=(transition.setup_physical_request_count),
        )
        if metadata is not None:
            replacement._pending_retry_metadata_update = copy.deepcopy(
                {
                    "router_dynamic_pending_route_plan": target_plan,
                    "router_dynamic_decision": target_audit,
                    "ensemble_decision_id": target_decision_id,
                }
            )
        return wrapped_transition

    def _activate_pending_retry_metadata(self) -> None:
        update = self._pending_retry_metadata_update
        if update is None:
            return
        self._pending_retry_metadata_update = None
        if self._turn_metadata is not None:
            self._turn_metadata.update(copy.deepcopy(update))

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "provider_name", "")

    @property
    def active_provider_id(self) -> str:
        """Configured identity of the selector deployment serving this turn."""
        return str(getattr(self._selector, "active_provider_id", "") or self.provider_name)

    @property
    def active_model_id(self) -> str:
        """Configured model identity of the selector's active chain link."""

        current_config = getattr(self._selector, "current_config", None)
        return str(getattr(current_config, "model", "") or "")

    def disable_provider_state_replay(self) -> None:
        """Rebuild the active fallback chain without provider-private replay."""
        disable = getattr(self._selector, "disable_provider_state_replay", None)
        if not callable(disable):
            return
        disable()
        self._provider = self._selector.resolve()

    def _realign_routed_model_after_fallback(self) -> None:
        """Failover changed the running model — telemetry must follow.

        Same invariant as the explicit-model realignment in
        PromptAssemblerStage: ``routed_model`` (read by RouterDecisionEvent
        and comprehensive-savings pricing) must name the model that actually
        runs, and route-savings figures computed for the abandoned model no
        longer apply.
        """
        metadata = self._turn_metadata
        if metadata is None:
            return
        current_config = getattr(self._selector, "current_config", None)
        metadata["executed_provider"] = str(
            getattr(current_config, "provider", "")
            or getattr(self._selector, "active_provider_id", "")
            or self.provider_name
        )
        model = str(getattr(current_config, "model", "") or "")
        metadata["executed_model"] = model
        if not model or metadata.get("routed_model") in (None, model):
            return
        metadata["routed_model"] = model
        for savings_key in (
            "savings_pct",
            "savings_max_price_per_m",
            "savings_routed_price_per_m",
        ):
            if savings_key in metadata:
                metadata[savings_key] = 0.0

    def _note_fallback_hop(self) -> None:
        """Count each selector fallback actually taken this turn.

        Read at turn finalize by the router decision record
        (engine/steps/router_decision_record.py) so persisted rows report
        how many hops away from the routed model the executed one is.
        """
        metadata = self._turn_metadata
        if metadata is None:
            return
        try:
            metadata["router_fallback_hops"] = int(metadata.get("router_fallback_hops") or 0) + 1
            metadata.setdefault("router_fallback_reason", "selector_fallback")
        except Exception:  # noqa: BLE001 — telemetry only
            pass

    def _active_deployment(self) -> tuple[str, str]:
        """(provider id, model) of the selector's currently-active chain link."""
        provider_id = str(getattr(self._selector, "active_provider_id", "") or self.provider_name)
        current_config = getattr(self._selector, "current_config", None)
        model = str(getattr(current_config, "model", "") or "")
        return provider_id, model

    def _record_health_failure(self, event: ProviderErrorEvent) -> None:
        """Feed one pre-content provider error into the opt-in health ledger."""
        ledger = self._health_ledger
        if ledger is None:
            return
        provider_id, model = self._active_deployment()
        if not provider_id and not model:
            return
        kind = classify_provider_error(
            provider_name=provider_id,
            status_code=int(event.code) if str(event.code).isdigit() else None,
            raw_code=event.code,
            message=event.message,
        )
        ledger.record_failure(
            provider_id,
            model,
            kind,
            retry_after_s=getattr(event, "retry_after_s", None),
        )

    def _record_health_success(self) -> None:
        """A user-visible response clears the deployment's strike count."""
        ledger = self._health_ledger
        if ledger is None:
            return
        provider_id, model = self._active_deployment()
        if not provider_id and not model:
            return
        ledger.record_success(provider_id, model)

    def _skip_benched_fallbacks(
        self,
        provider: Any,
    ) -> tuple[Any, bool, int]:
        """Resolve past benched fallbacks without bypassing canary admission.

        Uses :meth:`ProviderHealthLedger.eligible` with the remaining chain as
        the candidate set, so the ledger's never-strand exemption applies: when
        every remaining deployment is benched, the current one is reported
        eligible and no hop is taken. Returns the final local provider, whether
        a canary hop was blocked, and the number of health-skip hops. The caller
        remains the sole owner of the externally visible route commit.
        """
        ledger = self._health_ledger
        if ledger is None:
            return provider, False, 0
        remaining_chain = getattr(self._selector, "remaining_chain", None)
        has_fallback = getattr(self._selector, "has_fallback", None)
        next_fallback = getattr(self._selector, "next_fallback", None)
        if remaining_chain is None or has_fallback is None or next_fallback is None:
            return provider, False, 0
        current_provider = provider
        skipped_hops = 0
        while True:
            candidates = [
                (str(getattr(cfg, "provider", "")), str(getattr(cfg, "model", "")))
                for cfg in remaining_chain()
            ]
            if not candidates:
                return current_provider, False, skipped_hops
            provider_id, model = candidates[0]
            if ledger.eligible(provider_id, model, candidates):
                return current_provider, False, skipped_hops
            if not has_fallback():
                return current_provider, False, skipped_hops
            if self._live_canary_policy_blocks_next_fallback():
                self._selector_canary_route_blocked = True
                return current_provider, True, skipped_hops
            try:
                next_provider = next_fallback()
            except Exception:  # noqa: BLE001 — a failed hop must not break the turn
                return current_provider, False, skipped_hops
            if self._live_canary_policy_blocks_active_fallback():
                self._selector_canary_route_blocked = True
                return current_provider, True, skipped_hops
            current_provider = next_provider
            skipped_hops += 1

    def _routed_thinking_policy_blocks_fallback(self) -> bool:
        return bool(
            getattr(
                self._provider,
                "enforces_routed_thinking_policy",
                False,
            )
        )

    def _live_canary_policy_blocks_next_fallback(self) -> bool:
        """Fail closed before a selector advances onto a live canary route."""

        if self._selector_canary_route_blocked:
            return True
        allows = self._selector_fallback_admission
        if not callable(allows):
            return False
        remaining_chain = getattr(self._selector, "remaining_chain", None)
        if not callable(remaining_chain):
            return True
        try:
            candidates = list(remaining_chain())
        except Exception:
            return True
        # ModelSelector exposes the active deployment followed by untried
        # fallbacks. A governance-aware primary must not allow an unknown hop.
        if len(candidates) < 2:
            return True
        next_config = candidates[1]
        return (
            allows(
                getattr(next_config, "provider", ""),
                getattr(next_config, "model", ""),
            )
            is not True
        )

    def _live_canary_policy_blocks_active_fallback(self) -> bool:
        """Recheck the route produced by plugin/static selector mutation."""

        if self._selector_canary_route_blocked:
            return True
        allows = self._selector_fallback_admission
        if not callable(allows):
            return False
        current_config = getattr(self._selector, "current_config", None)
        if current_config is None:
            return True
        return (
            allows(
                getattr(current_config, "provider", ""),
                getattr(current_config, "model", ""),
            )
            is not True
        )

    def _managed_policy_blocks_fallback(self) -> bool:
        return bool(
            self._routed_thinking_policy_blocks_fallback()
            or self._live_canary_policy_blocks_next_fallback()
        )

    def fallback_after_invalid_response(self, reason: str) -> bool:
        if self._managed_policy_blocks_fallback():
            return False
        try:
            fallback_provider = self._selector.next_fallback_after_failure(RuntimeError(reason))
        except Exception:
            return False
        if self._live_canary_policy_blocks_active_fallback():
            self._selector_canary_route_blocked = True
            return False
        fallback_provider, blocked, skipped_hops = self._skip_benched_fallbacks(fallback_provider)
        if blocked:
            return False
        self._provider = fallback_provider
        for _ in range(1 + skipped_hops):
            self._note_fallback_hop()
        self._realign_routed_model_after_fallback()
        return True

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        return self._chat(messages, tools=tools, config=config)

    async def _chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        dispatch_generation_guard = getattr(
            self._provider,
            "_router_dynamic_cache_dispatch_generation_guard",
            None,
        )

        def dispatch_generation_is_current() -> bool:
            if not callable(dispatch_generation_guard):
                return True
            try:
                return dispatch_generation_guard() is True
            except Exception:  # noqa: BLE001 - missing proof must fail closed
                return False

        async def generation_blocked_stream() -> AsyncIterator[Any]:
            yield ProviderErrorEvent(
                message=("router_dynamic cache continuity changed before dispatch"),
                code="router_dynamic_cache_generation_changed",
                request_started=False,
                physical_request_count=0,
            )

        if not dispatch_generation_is_current():
            async for blocked_event in generation_blocked_stream():
                yield blocked_event
            return
        validation_error = validate_provider_chat_request(self._provider, messages)
        if validation_error is not None:
            yield validation_error
            return

        emitted_user_visible_content = False
        pre_text_buffer: list[Any] = []

        def drain_pre_text_buffer() -> list[Any]:
            drained = list(pre_text_buffer)
            pre_text_buffer.clear()
            return drained

        active_provider = self._provider
        active_provider_id, active_model = self._active_deployment()
        active_usage_snapshot = getattr(
            active_provider,
            "usage_accounting_snapshot",
            None,
        )

        def dispatch_primary_stream() -> AsyncIterator[Any]:
            # account_provider_stream may await durable accounting setup before
            # invoking this lazy factory. Re-prove continuity immediately beside
            # provider.chat so compaction invalidation in that window cannot
            # dispatch the stale affinity route.
            if not dispatch_generation_is_current():
                return generation_blocked_stream()
            # A prepared retry route is only observable once this replacement is
            # actually committed to a physical provider call. Merely obtaining
            # an async iterator (or failing local request validation/accounting
            # setup) must leave the source route metadata intact.
            self._activate_pending_retry_metadata()
            return active_provider.chat(messages, tools=tools, config=config)

        primary_stream = account_provider_stream(
            dispatch_primary_stream,
            provider=active_provider_id,
            model=active_model,
            usage_snapshot=(active_usage_snapshot if callable(active_usage_snapshot) else None),
        )
        try:
            async for event in primary_stream:
                # Provider control events must cross this provider-domain wrapper
                # unchanged and in real time.  Agent is the sole Provider→Engine
                # normalization boundary.  Neither event counts as user-visible
                # content, so a later pre-content error may still select fallback.
                if isinstance(event, (ProviderHeartbeatEvent, ProviderEnsembleProgressEvent)):
                    yield event
                    continue
                if isinstance(event, ProviderErrorEvent):
                    credential_provider_name = self.provider_name
                    if (
                        self._turn_metadata is not None
                        and self._turn_metadata.get("_router_single_provider_finalized") is True
                    ):
                        configured_provider, _ = self._active_deployment()
                        credential_provider_name = configured_provider or credential_provider_name
                    credential_failed = _report_credential_pool_failure(
                        credential_provider_name,
                        self._turn_metadata,
                        event,
                    )
                    if credential_failed:
                        callback = self._cache_affinity_credential_failure_callback
                        if callback is not None:
                            try:
                                callback()
                            except Exception:  # noqa: BLE001 - evidence is fail-closed
                                log.debug("router_single.cache_affinity_credential_purge_failed")
                if emitted_user_visible_content:
                    yield event
                    continue

                if (
                    isinstance(event, ProviderErrorEvent)
                    and event.code == "router_dynamic_cache_generation_changed"
                ):
                    for buffered_event in drain_pre_text_buffer():
                        yield buffered_event
                    yield event
                    return

                if isinstance(event, ProviderErrorEvent) and _should_use_selector_fallback(
                    self.provider_name, event
                ):
                    if self._managed_policy_blocks_fallback():
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    self._record_health_failure(event)
                    try:
                        fallback_provider = self._selector.next_fallback_after_failure(
                            RuntimeError(event.message)
                        )
                    except Exception:
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    if self._live_canary_policy_blocks_active_fallback():
                        self._selector_canary_route_blocked = True
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    fallback_provider, blocked, skipped_hops = self._skip_benched_fallbacks(
                        fallback_provider
                    )
                    if blocked:
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    # Prove the failed physical leg closed before reserving or
                    # dispatching another potentially billable request.
                    await primary_stream.aclose()
                    if self._live_canary_policy_blocks_active_fallback():
                        self._selector_canary_route_blocked = True
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    active_scope_ids = tuple(self._retry_scope_provider_bindings)
                    if len(active_scope_ids) > 1:
                        for buffered_event in drain_pre_text_buffer():
                            yield buffered_event
                        yield event
                        return
                    if active_scope_ids:
                        try:
                            reserve_provider_retry_physical_request(
                                self,
                                active_scope_ids[0],
                            )
                        except ProviderRetryScopeError:
                            for buffered_event in drain_pre_text_buffer():
                                yield buffered_event
                            yield event
                            return
                    # The usage envelope is lazy and may await durable setup
                    # before invoking its stream factory. Keep the final live
                    # status check inside that factory, immediately beside
                    # provider.chat, and commit externally visible route
                    # metadata only after that check passes.
                    fallback_provider_id, fallback_model = self._active_deployment()
                    fallback_usage_snapshot = getattr(
                        fallback_provider,
                        "usage_accounting_snapshot",
                        None,
                    )
                    fallback_config = config
                    model_copy = getattr(config, "model_copy", None)
                    if callable(model_copy):
                        fallback_config = model_copy(
                            update={"allow_provider_stream_fallback": False}
                        )

                    async def canary_blocked_stream() -> AsyncIterator[Any]:
                        yield ProviderErrorEvent(
                            message=("selector fallback was blocked by live canary governance"),
                            code="ensemble_canary_fallback_blocked",
                            request_started=False,
                            physical_request_count=0,
                        )

                    def dispatch_fallback_stream() -> AsyncIterator[Any]:
                        # The fallback accounting envelope is independently
                        # lazy. Re-prove the original affinity generation before
                        # committing fallback metadata or calling the provider.
                        if not dispatch_generation_is_current():
                            return generation_blocked_stream()
                        if self._live_canary_policy_blocks_active_fallback():
                            self._selector_canary_route_blocked = True
                            return canary_blocked_stream()
                        self._provider = fallback_provider
                        for _ in range(1 + skipped_hops):
                            self._note_fallback_hop()
                        self._realign_routed_model_after_fallback()
                        return fallback_provider.chat(
                            messages,
                            tools=tools,
                            config=fallback_config,
                        )

                    fallback_stream = account_provider_stream(
                        dispatch_fallback_stream,
                        provider=fallback_provider_id,
                        model=fallback_model,
                        usage_snapshot=(
                            fallback_usage_snapshot if callable(fallback_usage_snapshot) else None
                        ),
                    )
                    try:
                        async for fallback_event in fallback_stream:
                            yield fallback_event
                    finally:
                        await fallback_stream.aclose()
                    return

                if _is_non_empty_provider_text_delta(event):
                    for buffered_event in drain_pre_text_buffer():
                        yield buffered_event
                    emitted_user_visible_content = True
                    self._record_health_success()
                    yield event
                    continue

                if getattr(event, "kind", "") == "done":
                    for buffered_event in drain_pre_text_buffer():
                        yield buffered_event
                    yield event
                    continue

                if isinstance(event, ProviderErrorEvent):
                    for buffered_event in drain_pre_text_buffer():
                        yield buffered_event
                    yield event
                    continue

                pre_text_buffer.append(event)
        finally:
            await primary_stream.aclose()

        for buffered_event in drain_pre_text_buffer():
            yield buffered_event

    async def list_models(self) -> list[Any]:
        return list(await self._provider.list_models())


def _is_non_empty_provider_text_delta(event: Any) -> bool:
    """Return True only once a provider event carries user-visible text."""
    return getattr(event, "kind", "") == "text_delta" and bool(getattr(event, "text", ""))


@dataclass
class MemorySnapshot:
    """Frozen memory content for stable system prompt prefixes."""

    memory_md: str | None = None
    daily_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class BootstrapSnapshot:
    """Frozen workspace bootstrap files for stable per-session prompt prefixes."""

    workspace_files: dict[str, str] = field(default_factory=dict)
    report: list[BootstrapFileReport] = field(default_factory=list)


_PDF_ATTACHMENT_TEXT_LIMIT = 200_000
_TEXT_ATTACHMENT_TEXT_LIMIT = 200_000
_PREVIEW_ONLY_TEXT_ATTACHMENT_CHARS = 4_000
_PREVIEW_ONLY_TEXT_ATTACHMENT_LINES = 80

_XML_ATTR_ESCAPES = {
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    '"': "&quot;",
    "'": "&apos;",
}


def _xml_escape_attr(value: str) -> str:
    """XML-escape characters that would break an HTML/XML attribute value.

    Matches the file-context wrapper escaping contract.
    """

    return "".join(_XML_ATTR_ESCAPES.get(ch, ch) for ch in value)


def _sanitize_attachment_filename(value: Any, fallback: str = "attachment") -> str:
    """Strip path separators, newlines/tabs, and trim; fall back if empty."""

    if not isinstance(value, str):
        return fallback
    cleaned = value.replace("\x00", "")
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return cleaned or fallback


def _escape_file_block_content(value: str) -> str:
    """Escape literal ``</file>`` and ``<file `` substrings inside payloads.

    Without this, a user-supplied CSV / markdown body containing the wrapper
    sentinel could be mis-parsed by the model as the boundary of a *different*
    attachment, enabling prompt-injection. The replacement is XML-entity
    style so the payload remains human-readable in the prompt.
    """

    import re as _re

    # Order matters: do the close-tag pattern first so we don't double-escape
    # the prefix it shares with the open-tag pattern.
    out = _re.sub(r"<\s*/\s*file\s*>", "&lt;/file&gt;", value, flags=_re.IGNORECASE)
    out = _re.sub(r"<\s*file\b", "&lt;file", out, flags=_re.IGNORECASE)
    return out


def _render_file_context_block(filename: str, mime: str, content: str) -> str:
    """Render a ``<file name="…" mime="…">\\n<content>\\n</file>`` envelope."""

    safe_name = _xml_escape_attr(_sanitize_attachment_filename(filename))
    safe_mime = _xml_escape_attr(mime)
    safe_content = _escape_file_block_content(content)
    return f'<file name="{safe_name}" mime="{safe_mime}">\n{safe_content}\n</file>'


def _truncate_attachment_text(text: str, *, limit: int = _PDF_ATTACHMENT_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[attachment text truncated: {len(text)} chars total]"


def _preview_attachment_text(
    text: str,
    *,
    char_limit: int = _PREVIEW_ONLY_TEXT_ATTACHMENT_CHARS,
    line_limit: int = _PREVIEW_ONLY_TEXT_ATTACHMENT_LINES,
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    preview = "".join(lines[:line_limit])
    truncated = len(lines) > line_limit
    if len(preview) > char_limit:
        preview = preview[:char_limit]
        truncated = True
    elif len(text) > len(preview):
        truncated = True
    return preview, truncated


def _attachment_ref_material_path(
    attachment: dict[str, Any],
    *,
    media_root: Path | None,
) -> str | None:
    path = attachment.get("_material_path")
    if isinstance(path, str) and path:
        return path
    if media_root is None or not is_attachment_ref(attachment):
        return None
    scope = attachment.get("scope")
    sha = attachment.get("sha256") or attachment.get("material_id")
    if not isinstance(scope, str) or not isinstance(sha, str):
        return None
    try:
        return str(transcript_material_path(media_root, scope, sha))
    except ValueError:
        return None


def _render_preview_only_attachment_text(
    attachment: dict[str, Any],
    *,
    filename: str,
    mime: str,
    raw_bytes: bytes,
    media_root: Path | None,
) -> str:
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "[attachment unavailable: declared text content is not valid UTF-8]"

    preview, truncated = _preview_attachment_text(decoded)
    material_path = _attachment_ref_material_path(attachment, media_root=media_root)
    estimated_tokens = attachment.get("_material_estimated_tokens")
    estimated_line = (
        f"estimated_tokens: {estimated_tokens}"
        if isinstance(estimated_tokens, int)
        else "estimated_tokens: unknown"
    )
    path_line = f"path: {material_path}" if material_path else "path: unavailable"
    read_hint = (
        f'read_full: use read_file(path="{material_path}", offset=1, limit=200) '
        "and adjust offset/limit as needed."
        if material_path
        else "read_full: material path unavailable."
    )
    truncation = (
        f"\n\n[attachment preview truncated: {len(decoded)} chars total]" if truncated else ""
    )
    return (
        "[large text attachment materialized]\n"
        f"name: {filename}\n"
        f"mime: {mime}\n"
        f"size_bytes: {len(raw_bytes)}\n"
        f"{estimated_line}\n"
        f"{path_line}\n"
        f"{read_hint}\n\n"
        "preview:\n"
        f"{preview}"
        f"{truncation}"
    )


def _extract_pdf_attachment_text(raw_bytes: bytes, filename: str) -> str:
    """Extract text from a PDF attachment before it reaches any provider.

    PDFs are converted into plain text context so provider-specific document
    block handling cannot silently drop files that an adapter does not know how
    to encode.
    """

    import io

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValueError("PDF text extraction requires pdfplumber") from exc

    try:
        page_texts: list[str] = []
        with pdfplumber.open(io.BytesIO(raw_bytes)) as doc:
            for index, page in enumerate(doc.pages, start=1):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    page_texts.append(f"--- Page {index} ---\n{page_text}")
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises several parser errors
        raise ValueError(f"PDF attachment {filename!r} could not be read: {exc}") from exc

    extracted = "\n\n".join(page_texts).strip()
    if not extracted:
        raise ValueError(f"PDF attachment {filename!r} has no extractable text")
    return _truncate_attachment_text(extracted)


# Office documents are zip containers. Guard against decompression bombs by
# rejecting archives whose declared uncompressed payload is implausibly large
# before handing the bytes to a parser.
_OFFICE_DECOMPRESSED_LIMIT = 200 * 1024 * 1024
_XLSX_MAX_ROWS_PER_SHEET = 1000
_XLSX_MAX_COLS = 64


def _office_zip_guard(raw_bytes: bytes, filename: str) -> None:
    # Measure the *actual* inflated size by streaming each member, not the
    # central-directory ``file_size`` (which the uploader controls and can lie
    # about). Reads in bounded chunks and aborts as soon as the running total
    # crosses the limit, so a decompression bomb never inflates past the cap.
    import io
    import zipfile

    chunk_size = 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            total = 0
            for info in archive.infolist():
                with archive.open(info) as member:
                    while True:
                        block = member.read(chunk_size)
                        if not block:
                            break
                        total += len(block)
                        if total > _OFFICE_DECOMPRESSED_LIMIT:
                            raise ValueError(
                                f"office attachment {filename!r} decompresses beyond "
                                f"the {_OFFICE_DECOMPRESSED_LIMIT} byte safety limit"
                            )
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - zipfile raises several error types
        raise ValueError(
            f"office attachment {filename!r} is not a readable OOXML container: {exc}"
        ) from exc


def _extract_docx_text(raw_bytes: bytes) -> str:
    import io

    from docx import Document

    document = Document(io.BytesIO(raw_bytes))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def _extract_xlsx_text(raw_bytes: bytes) -> str:
    import io

    from openpyxl import load_workbook  # type: ignore[import-untyped]

    workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        sheet_blocks: list[str] = []
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                if row_index >= _XLSX_MAX_ROWS_PER_SHEET:
                    rows.append(f"[sheet truncated at {_XLSX_MAX_ROWS_PER_SHEET} rows]")
                    break
                cells = ["" if value is None else str(value) for value in row[:_XLSX_MAX_COLS]]
                if any(cells):
                    rows.append(",".join(cells))
            if rows:
                sheet_blocks.append(f"=== Sheet: {sheet.title} ===\n" + "\n".join(rows))
        return "\n\n".join(sheet_blocks).strip()
    finally:
        workbook.close()


def _extract_pptx_text(raw_bytes: bytes) -> str:
    import io

    from pptx import Presentation

    presentation = Presentation(io.BytesIO(raw_bytes))
    slide_blocks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if text:
                    lines.append(text)
        notes = ""
        if slide.has_notes_slide:
            notes_frame = slide.notes_slide.notes_text_frame
            if notes_frame is not None:
                notes = notes_frame.text.strip()
        block = f"--- Slide {index} ---"
        if lines:
            block += "\n" + "\n".join(lines)
        if notes:
            block += f"\n[Notes]\n{notes}"
        slide_blocks.append(block)
    return "\n\n".join(slide_blocks).strip()


_OFFICE_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    _DOCX_MIME: _extract_docx_text,
    _XLSX_MIME: _extract_xlsx_text,
    _PPTX_MIME: _extract_pptx_text,
}


def _extract_office_attachment_text(raw_bytes: bytes, filename: str, media_type: str) -> str:
    """Extract text from an OOXML office attachment before it reaches any provider.

    docx/xlsx/pptx are zip containers that no provider adapter can encode, so they
    are converted to bounded plain-text context, mirroring the PDF path.
    """

    extractor = _OFFICE_EXTRACTORS.get(media_type)
    if extractor is None:  # pragma: no cover - guarded by the allow-list
        raise ValueError(f"unsupported office media type {media_type!r}")
    _office_zip_guard(raw_bytes, filename)
    try:
        extracted = extractor(raw_bytes).strip()
    except ValueError:
        raise
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValueError(f"office text extraction requires a missing dependency: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - parsers raise many error types
        raise ValueError(f"office attachment {filename!r} could not be read: {exc}") from exc
    if not extracted:
        raise ValueError(f"office attachment {filename!r} has no extractable text")
    return _truncate_attachment_text(extracted)


_EMAIL_MAX_MESSAGES = 50


def _strip_html_to_text(html: str) -> str:
    """Conservative HTML -> text for email bodies.

    Drops script/style/head blocks entirely (no execution, no leakage), turns
    block tags into newlines, strips remaining tags, and unescapes entities.
    """

    import html as _html_mod
    import re

    hidden_block_re = re.compile(
        r"(?is)<(script|style|head)\b(?:[^>]*>.*?(?:</\s*\1\s*>|$)|[^>]*$)"
    )
    cleaned = hidden_block_re.sub(" ", html)
    cleaned = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = _html_mod.unescape(cleaned)
    lines = [line.strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _render_one_email(message: Any) -> str:
    headers: list[str] = []
    for label in ("From", "To", "Cc", "Subject", "Date"):
        value = message.get(label)
        if value:
            headers.append(f"{label}: {value}")

    body_text = ""
    try:
        body_part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - defensive against malformed parts
        body_part = None
    if body_part is not None:
        try:
            content = body_part.get_content()
        except Exception:  # noqa: BLE001
            content = ""
        if not isinstance(content, str):
            content = ""
        if body_part.get_content_type() == "text/html":
            body_text = _strip_html_to_text(content)
        else:
            body_text = content

    attachment_lines: list[str] = []
    try:
        for part in message.iter_attachments():
            name = part.get_filename() or "(unnamed)"
            attachment_lines.append(f"  - {name} ({part.get_content_type()})")
    except Exception:  # noqa: BLE001
        pass

    rendered = "\n".join(headers)
    if body_text.strip():
        rendered += "\n\n" + body_text.strip()
    if attachment_lines:
        rendered += "\n\n[attachments]\n" + "\n".join(attachment_lines)
    return rendered.strip()


def _extract_email_text(raw_bytes: bytes, media_type: str) -> str:
    import email
    import re
    from email import policy

    # Trust the resolved media type: the gateway sniffer/guard already settle
    # eml-vs-mbox, so a .eml whose body happens to start with "From " is not
    # mis-routed through the mbox splitter.
    is_mbox = media_type == _MBOX_MIME
    if is_mbox:
        chunks = re.split(rb"(?m)^From .*\n", raw_bytes)
        messages = [chunk for chunk in chunks if chunk.strip()][:_EMAIL_MAX_MESSAGES]
        rendered: list[str] = []
        for index, chunk in enumerate(messages, start=1):
            message = email.message_from_bytes(chunk, policy=policy.default)
            rendered.append(f"--- Message {index} ---\n{_render_one_email(message)}")
        return "\n\n".join(rendered).strip()

    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    return _render_one_email(message)


def _extract_msg_text(raw_bytes: bytes) -> str:
    import io

    try:
        import extract_msg
    except ImportError as exc:
        raise ValueError(
            "Outlook .msg extraction requires the optional 'extract-msg' package "
            "(install opensquilla[msg])"
        ) from exc

    message = extract_msg.openMsg(io.BytesIO(raw_bytes))
    try:
        headers: list[str] = []
        for label, value in (
            ("From", getattr(message, "sender", None)),
            ("To", getattr(message, "to", None)),
            ("Cc", getattr(message, "cc", None)),
            ("Subject", getattr(message, "subject", None)),
            ("Date", getattr(message, "date", None)),
        ):
            if value:
                headers.append(f"{label}: {value}")

        body = getattr(message, "body", None) or ""
        if not body:
            html_body = getattr(message, "htmlBody", None)
            if isinstance(html_body, bytes):
                html_body = html_body.decode("utf-8", "replace")
            if isinstance(html_body, str) and html_body:
                body = _strip_html_to_text(html_body)

        attachment_lines: list[str] = []
        for part in getattr(message, "attachments", None) or []:
            name = (
                getattr(part, "longFilename", None)
                or getattr(part, "shortFilename", None)
                or "(unnamed)"
            )
            attachment_lines.append(f"  - {name}")
    finally:
        try:
            message.close()
        except Exception:  # noqa: BLE001
            pass

    rendered = "\n".join(headers)
    if isinstance(body, str) and body.strip():
        rendered += "\n\n" + body.strip()
    if attachment_lines:
        rendered += "\n\n[attachments]\n" + "\n".join(attachment_lines)
    return rendered.strip()


def _extract_email_attachment_text(raw_bytes: bytes, filename: str, media_type: str) -> str:
    """Extract text from an email attachment.

    .eml/.mbox use the stdlib email/mailbox parsers (zero dependency); .msg uses
    the optional extract-msg package and degrades gracefully if it is absent.
    """

    try:
        if media_type == _MSG_MIME:
            extracted = _extract_msg_text(raw_bytes).strip()
        else:
            extracted = _extract_email_text(raw_bytes, media_type).strip()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - email parsers raise many error types
        raise ValueError(f"email attachment {filename!r} could not be read: {exc}") from exc
    if not extracted:
        raise ValueError(f"email attachment {filename!r} has no extractable text")
    return _truncate_attachment_text(extracted)


# Strong past-tense / perfect-aspect phrases that signal the model is claiming
# to have produced an image. Only checked when ``image_generate`` is available
# and was not invoked. Future-tense ("I'll draw…", "给你画…") is intentionally
# excluded — those express intent and are often followed by an actual tool call
# in the same or next iteration; flagging them is noisy.
_IMAGE_CLAIM_PATTERNS = (
    # Chinese: perfect aspect / demonstrative past
    "已生成图片",
    "生成了图片",
    "画了一张",
    "这是生成的图",
    "已为您生成",
    "已经画好",
    "绘制好了",
    # English: past / perfect tense
    "generated an image",
    "i have created the image",
    "i've created the image",
    "i have generated the image",
    "i've generated the image",
    # Specific "here is/here's the image I …" — require the "I" pronoun to
    # avoid matching "here's the image you uploaded".
    "here is the image i",
    "here's the image i",
    # Markdown embed of a fake generated asset.
    "![generated",
)


def _claims_image_without_tool_use(
    final_text: str,
    tool_defs: list[Any],
    turn_segments: list[dict],
) -> bool:
    """Detect: model claimed image generation but never called image_generate.

    Returns True only when the tool was *available* (so we know the model had
    the option) and *not called* in this turn yet the final text matches a claim
    pattern. Used to surface a non-persistent UI warning; never writes to transcript.
    """
    tool_names = {getattr(td, "name", "") for td in tool_defs}
    if "image_generate" not in tool_names:
        return False
    had_image_call = any(
        isinstance(seg, dict)
        and seg.get("type") == "tool_use"
        and seg.get("name") == "image_generate"
        for seg in turn_segments
    )
    if had_image_call:
        return False
    if not final_text:
        return False
    lowered = final_text.lower()
    return any(p.lower() in lowered for p in _IMAGE_CLAIM_PATTERNS)


def _resolve_identity_prompt_mode(config: object) -> str:
    """Resolve the identity/system prompt mode from gateway config.

    ``auto`` preserves the historical behavior: full prompt by default, with
    memory-only tool surfaces using the minimal prompt. Any explicit prompt
    mode overrides that compatibility logic.
    """
    allowed_modes = {
        "auto",
        "full",
        "minimal",
        "none",
        "headless_source_edit",
        "headless_repo_coding_scaffold",
    }
    env_prompt_mode = os.environ.get("OPENSQUILLA_PROMPT_MODE", "").strip()
    if env_prompt_mode:
        if env_prompt_mode not in allowed_modes:
            raise ValueError(
                "OPENSQUILLA_PROMPT_MODE must be one of: " + ", ".join(sorted(allowed_modes))
            )
        return env_prompt_mode

    prompt_cfg = getattr(config, "prompt", None)
    prompt_mode = str(getattr(prompt_cfg, "mode", "auto") or "auto")
    if prompt_mode not in allowed_modes:
        raise ValueError("prompt.mode must be one of: " + ", ".join(sorted(allowed_modes)))
    if prompt_mode != "auto":
        return prompt_mode

    tools_cfg = getattr(config, "tools", None)
    if getattr(tools_cfg, "profile", None) == "memory_only":
        return "minimal"
    return "full"


_PATCH_EVIDENCE_PROTOCOL_ENV = "OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL"
_PATCH_EVIDENCE_PROTOCOL_ON = {"on", "1", "true", "yes"}
_PATCH_EVIDENCE_PROTOCOL_OFF = {"off", "0", "false", "no"}


def _resolve_patch_evidence_protocol(config: object) -> bool:
    """Resolve the opt-in Patch Evidence Protocol prompt flag.

    ``OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL`` ("on"/"off") overrides
    ``prompt.patch_evidence_protocol`` from gateway config; default is off.
    Unrecognized env values raise instead of being silently ignored so a
    run manifest cannot record an override the run did not actually apply.
    """
    env_value = os.environ.get(_PATCH_EVIDENCE_PROTOCOL_ENV, "").strip().lower()
    if env_value:
        if env_value in _PATCH_EVIDENCE_PROTOCOL_ON:
            return True
        if env_value in _PATCH_EVIDENCE_PROTOCOL_OFF:
            return False
        raise ValueError(
            f"{_PATCH_EVIDENCE_PROTOCOL_ENV} must be one of: "
            + ", ".join(sorted(_PATCH_EVIDENCE_PROTOCOL_ON | _PATCH_EVIDENCE_PROTOCOL_OFF))
        )

    prompt_cfg = getattr(config, "prompt", None)
    return bool(getattr(prompt_cfg, "patch_evidence_protocol", False))


_FINALIZE_EVIDENCE_GATE_ENV = "OPENSQUILLA_FINALIZE_EVIDENCE_GATE"
_FINALIZE_EVIDENCE_GATE_ON = {"on", "1", "true", "yes"}
_FINALIZE_EVIDENCE_GATE_OFF = {"off", "0", "false", "no"}


def _resolve_finalize_evidence_gate(config: object) -> bool:
    """Resolve the opt-in finalize-time red-evidence gate prompt flag.

    ``OPENSQUILLA_FINALIZE_EVIDENCE_GATE`` ("on"/"off") overrides
    ``prompt.finalize_evidence_gate`` from gateway config; default is off.
    The same env var also enables the loop-side gate (see
    engine.turn_runner.agent_bootstrap_stage). Unrecognized env values raise
    instead of being silently ignored so a run manifest cannot record an
    override the run did not actually apply.
    """
    env_value = os.environ.get(_FINALIZE_EVIDENCE_GATE_ENV, "").strip().lower()
    if env_value:
        if env_value in _FINALIZE_EVIDENCE_GATE_ON:
            return True
        if env_value in _FINALIZE_EVIDENCE_GATE_OFF:
            return False
        raise ValueError(
            f"{_FINALIZE_EVIDENCE_GATE_ENV} must be one of: "
            + ", ".join(sorted(_FINALIZE_EVIDENCE_GATE_ON | _FINALIZE_EVIDENCE_GATE_OFF))
        )

    prompt_cfg = getattr(config, "prompt", None)
    return bool(getattr(prompt_cfg, "finalize_evidence_gate", False))


_LEGACY_PROMPT_STYLE_ENV = "OPENSQUILLA_LEGACY_PROMPT_STYLE"
_LEGACY_PROMPT_STYLE_ON = {"on", "1", "true", "yes"}
_LEGACY_PROMPT_STYLE_OFF = {"off", "0", "false", "no"}


def _resolve_legacy_prompt_style(config: object) -> bool:
    """Resolve the opt-in legacy prompt style flag.

    ``OPENSQUILLA_LEGACY_PROMPT_STYLE`` ("on"/"off") overrides
    ``prompt.legacy_prompt_style`` from gateway config; default is off and
    keeps the current prompt wording byte-identical. Unrecognized env values
    raise instead of being silently ignored so a run manifest cannot record
    an override the run did not actually apply.
    """
    env_value = os.environ.get(_LEGACY_PROMPT_STYLE_ENV, "").strip().lower()
    if env_value:
        if env_value in _LEGACY_PROMPT_STYLE_ON:
            return True
        if env_value in _LEGACY_PROMPT_STYLE_OFF:
            return False
        raise ValueError(
            f"{_LEGACY_PROMPT_STYLE_ENV} must be one of: "
            + ", ".join(sorted(_LEGACY_PROMPT_STYLE_ON | _LEGACY_PROMPT_STYLE_OFF))
        )

    prompt_cfg = getattr(config, "prompt", None)
    return bool(getattr(prompt_cfg, "legacy_prompt_style", False))


class TurnRunner:
    """Orchestrates a complete agent turn: provider → tools → prompt → pipeline → Agent.

    Uses supplied per-session locking and owns transcript persistence.
    All entry points (Web RPC, CLI, Channel) converge here.

    Lock ordering invariant:
        TurnRunner no longer owns an internal lock dict.
        Per-session locks are supplied by an external ``session_lock_provider``
        (``Callable[[str], asyncio.Lock]``) injected at construction time.

        Gateway path: provider = ``TaskRuntime._get_session_lock_for_turn``.
        It returns the short write lock used for transcript/session state
        mutation. TaskRuntime owns a separate execution lock and marks the
        call chain so ``TurnRunner.run()`` skips its legacy coarse acquire while
        append adapters still acquire the write lock.

        CLI / standalone path: provider = ``_standalone_lock_provider`` from
        ``build_turn_runner_from_services``, which maintains its own dict.

        The old model/approval-wide write lock is eliminated on the gateway
        path. External I/O must stay outside the write lock.
    """

    def __init__(
        self,
        provider_selector: Any,
        tool_registry: Any | None = None,
        session_manager: Any | None = None,
        skill_loader: Any | None = None,
        usage_tracker: Any | None = None,
        config: Any | None = None,
        memory_sync_managers: dict[str, Any] | None = None,
        model_catalog: Any | None = None,
        memory_retrievers: dict[str, Any] | None = None,
        turn_capture_services: dict[str, Any] | None = None,
        session_flush_service: SessionFlushService | None = None,
        session_lock_provider: Callable[[str], asyncio.Lock] | None = None,
        diagnostics_state: Any | None = None,
        turn_hooks: Sequence[TurnHook] | None = None,
        compaction_hooks: Sequence[CompactionHook] | None = None,
        meta_run_writer: MetaRunWriter | None = None,
        turn_error_writer: Any | None = None,
        provider_call_observer: Callable[..., None] | None = None,
        usage_event_sink: UsageEventSink | None = None,
    ) -> None:
        self._provider_selector = provider_selector
        self._tool_registry = tool_registry
        self._session_manager = session_manager
        self._skill_loader = skill_loader
        self._usage_tracker = usage_tracker
        self._config = config
        self._last_agent_max_iterations_source = "AgentConfig default"
        self._memory_sync_managers = memory_sync_managers
        self._model_catalog = model_catalog
        self._memory_retrievers = memory_retrievers
        self._turn_capture_services = turn_capture_services
        self._session_flush_service = session_flush_service
        self._diagnostics_state = diagnostics_state
        self._meta_run_writer = meta_run_writer
        self._turn_error_writer = turn_error_writer
        self._usage_event_sink = usage_event_sink
        # Populated alongside the existing session-id lookup so live usage
        # events retain reset fencing without a second storage round trip.
        self._usage_session_epoch_by_key: dict[str, int] = {}
        # Optional gateway-injected provider-call observer (latency/health
        # sampling). Threaded onto AgentConfig via AgentBootstrapStage; None
        # keeps the engine gateway-agnostic.
        self._provider_call_observer = provider_call_observer
        self._router_control_hold_store = RouterControlHoldStore()
        # TurnHook surface. The default trace hook reproduces the inline trace
        # event behavior while keeping the event sink replaceable at construction.
        if turn_hooks is None:
            self._turn_hooks: tuple[TurnHook, ...] = (DefaultTraceEmitterHook(),)
        else:
            self._turn_hooks = tuple(turn_hooks)
        # CompactionHook surface. CompactionAndHistoryStage fans
        # before/after-compact events out through these hooks. Empty tuple by
        # default means compaction runs with no hook fan-out.
        self._compaction_hooks: tuple[CompactionHook, ...] = (
            tuple(compaction_hooks) if compaction_hooks else ()
        )
        # Per-session lock provider.
        # Gateway path: task_runtime._get_session_lock_for_turn (wired in boot.py).
        # CLI/standalone path: _standalone_lock_provider from build_turn_runner_from_services.
        # Test/direct-construction path: fallback dict created here inside a closure.
        # TurnRunner no longer owns a named per-session lock dict as an instance attribute.
        # The lock dict lives entirely in the provider closure.
        if session_lock_provider is None:
            _fallback_locks: dict[str, asyncio.Lock] = {}

            def _fallback_provider(key: str) -> asyncio.Lock:
                return _fallback_locks.setdefault(key, asyncio.Lock())

            session_lock_provider = _fallback_provider
        self._session_lock_provider = session_lock_provider
        # Frozen memory snapshots keyed by (agent_id, session_key).
        # Captured at session start, refreshed on write/compaction.
        self._memory_snapshots: dict[tuple[str, str], MemorySnapshot] = {}
        # Frozen bootstrap snapshots keyed by (agent_id, session_key, context_mode).
        # Captured on first prompt assembly so bootstrap-source edits do not
        # churn the cacheable prefix mid-session.
        self._bootstrap_snapshots: dict[tuple[str, str, str], BootstrapSnapshot] = {}
        self._compaction_failures: dict[str, _CompactionFailureState] = {}
        self._turn_compaction_attempted_sessions: set[str] = set()
        self._turn_compacted_sessions: set[str] = set()
        self._active_pre_compaction_flush_tasks: dict[str, asyncio.Task] = {}
        self._emergency_compaction_overrides: dict[str, _EmergencyCompactionOverride] = {}
        # Bounded, non-persistent continuity state for Step2 session intent.
        # It stores route identifiers only; prompt and candidate content never
        # enter this cache.
        self._router_dynamic_last_routes: dict[str, dict[str, Any]] = {}
        # Stateless classifier facades keyed by accepted four_tier_mapping config hash.
        # Durable task state lives in the session DB, never in this cache.
        # The lock also guards a model runner while it is executing, so a hot
        # configuration swap cannot close an in-flight native session.
        self._fixed_four_tier_v2_router_lock = threading.RLock()
        self._fixed_four_tier_v2_routers: OrderedDict[str, Any] = OrderedDict()
        # Native registered-model inference is serialized on a runner-owned
        # executor. Using the event loop's shared default executor here lets a
        # burst of requests occupy every worker while they all wait on the same
        # model lock, starving unrelated ``asyncio.to_thread`` work.
        self._fixed_four_tier_v2_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="opensquilla-fixed-four-tier",
        )
        self._fixed_four_tier_v2_lifecycle_lock = threading.RLock()
        self._fixed_four_tier_v2_pending_futures: set[concurrent.futures.Future[Any]] = set()
        self._fixed_four_tier_v2_release_future: concurrent.futures.Future[Any] | None = None
        self._fixed_four_tier_v2_close_future: concurrent.futures.Future[Any] | None = None
        self._fixed_four_tier_v2_router_cache_populated = False
        # Bound running + queued request work. Maintenance jobs are separately
        # coalesced and never wait for admission on the event-loop thread.
        self._fixed_four_tier_v2_admission = threading.BoundedSemaphore(
            _FIXED_FOUR_TIER_V2_MAX_OUTSTANDING_JOBS
        )
        self._fixed_four_tier_v2_close_timeout_seconds = _FIXED_FOUR_TIER_V2_CLOSE_TIMEOUT_SECONDS
        self._fixed_four_tier_v2_close_timeout_reported = False
        self._fixed_four_tier_v2_release_timeout_seconds = (
            _FIXED_FOUR_TIER_V2_RELEASE_TIMEOUT_SECONDS
        )
        self._fixed_four_tier_v2_release_timeout_future: concurrent.futures.Future[Any] | None = (
            None
        )
        # Optional KV-affinity evidence is intentionally isolated from B5 route
        # continuity. Both containers are process-local and non-persistent, but
        # this state is epoch/topology keyed and never enters TurnContext metadata.
        self._router_dynamic_cache_affinity: (
            OrderedDict[
                _RouterDynamicCacheAffinityStateKey,
                _RouterDynamicCacheAffinitySessionState,
            ]
            | None
        ) = None
        self._router_dynamic_cache_affinity_sidecars: (
            dict[
                tuple[str, str],
                _RouterDynamicCacheAffinityPendingSidecar,
            ]
            | None
        ) = None
        self._router_dynamic_cache_affinity_generation: dict[str, int] | None = None
        self._router_dynamic_cache_affinity_epoch_by_key: dict[str, int] | None = None
        self._router_dynamic_cache_affinity_generation_clock = 0
        self._router_dynamic_cache_affinity_control_max_entries: int | None = None
        self._router_dynamic_cache_affinity_compaction_remove: Callable[[], None] | None = None
        self._router_dynamic_cache_affinity_compaction_finalizer: weakref.finalize | None = None
        self._router_dynamic_cache_affinity_session_delete_remove: Callable[[], None] | None = None
        self._router_dynamic_cache_affinity_session_delete_finalizer: weakref.finalize | None = None
        # A TurnRunner may serve concurrent sessions, but every live canary
        # turn in the process must share one same-host durable rollout ledger.
        # Bind the state path on first explicit enablement; a hot state_dir
        # change is fail-closed until the runner is restarted.
        self._canary_rollout_ledger_lock = threading.Lock()
        self._canary_rollout_ledger: Any | None = None
        self._canary_rollout_ledger_path: Path | None = None
        self._canary_rollout_ledger_path_drifted = False
        # TurnRunner stage decomposition InputStage instance. Holds no per-turn state;
        # constructed once. Active unconditionally as of.
        self._input_stage = InputStage(extra_ctx=_TurnRunnerExtraContextAdapter())
        # TurnRunner stage decomposition ProviderAndToolsStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        self._provider_and_tools_stage = ProviderAndToolsStage(
            provider_resolver=_TurnRunnerProviderResolverAdapter(self),
            tool_builder=_TurnRunnerToolBuilderAdapter(self),
            skill_catalog_resolver=_TurnRunnerSkillCatalogResolverAdapter(self),
        )
        # TurnRunner stage decomposition PromptAssemblerStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        self._prompt_assembler_stage = PromptAssemblerStage(
            prompt_assembler=_TurnRunnerPromptAssemblerAdapter(self),
            pipeline_executor=_TurnRunnerPipelineExecutionAdapter(self),
            router_context=_TurnRunnerRouterContextAdapter(self),
            prompt_config_resolver=_TurnRunnerPromptConfigResolverAdapter(self),
            prompt_report_builder=_PromptReportBuilderAdapter(),
            session_id_resolver=_TurnRunnerSessionIdResolverAdapter(self),
            memory_fingerprint=_TurnRunnerMemoryFingerprintAdapter(self),
        )
        # TurnRunner stage decomposition AgentBootstrapStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        self._agent_bootstrap_stage = AgentBootstrapStage(
            timeout_budget=_TurnRunnerTimeoutBudgetAdapter(self),
            model_catalog=_TurnRunnerModelCatalogAdapter(self),
            agent_config_builder=_TurnRunnerAgentConfigBuilderAdapter(self),
            memory_snapshot=_TurnRunnerMemorySnapshotAdapter(self),
            agent_factory=_TurnRunnerAgentFactoryAdapter(self),
            provider_call_observer=self._provider_call_observer,
        )
        # TurnRunner stage decomposition CompactionAndHistoryStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        self._compaction_and_history_stage = CompactionAndHistoryStage(
            t3_upgrade=_TurnRunnerT3UpgradeCompactionAdapter(self),
            preflight=_TurnRunnerPreflightCompactionAdapter(self),
            history_loader=_TurnRunnerHistoryLoaderAdapter(self),
            request_context_prepender=_RequestContextPrependAdapter(),
            compaction_hooks=self._compaction_hooks,
        )
        # TurnRunner stage decomposition AttachmentStage instance. Holds no per-turn
        # state. Active unconditionally as of.
        self._attachment_stage = AttachmentStage(
            builder=_TurnRunnerAttachmentMessageBuilderAdapter(self),
        )
        # TurnRunner stage decomposition StreamConsumerStage instance. Holds no
        # per-turn state. Active unconditionally as of. The
        # warning transformer binds ``self._handle_runtime_warning`` as
        # a one-method callable; the recording-fake discipline applies
        # identically to a Protocol-shaped port.
        self._stream_consumer_stage = StreamConsumerStage(
            agent_run=_TurnRunnerAgentRunAdapter(),
            compaction_persist=_TurnRunnerCompactionPersistAdapter(self),
            memory_snapshot_refresh=_TurnRunnerMemorySnapshotRefreshAdapter(self),
            system_prompt_refresh=_TurnRunnerSystemPromptRefreshAdapter(self),
            memory_sync_notify=_TurnRunnerMemorySyncNotifyAdapter(),
            warning_transformer=self._handle_runtime_warning,
            compaction_hooks=self._compaction_hooks,
        )
        # TurnRunner stage decomposition TurnFinalizerStage instance. Holds no
        # per-turn state. Active unconditionally as of. Adapter
        # contracts:
        #   * TranscriptAppendPort folds the ``token_count`` introspect
        #     and the ``session_manager is None`` guard.
        #   * TurnMemoryCapturePort forwards verbatim; the stage owns
        #     the log-and-continue try/except.
        #   * SessionTotalsPort inlines the post-DoneEvent cost rollup
        #     bit-identically to the legacy slice.
        #   * TurnErrorPersistPort forwards verbatim; the helper owns
        #     its own try/except + None guards.
        self._turn_finalizer_stage = TurnFinalizerStage(
            transcript_append=_TurnRunnerTranscriptAppendAdapter(self),
            turn_memory_capture=_TurnRunnerTurnMemoryCaptureAdapter(self),
            session_totals=_TurnRunnerSessionTotalsAdapter(self),
            turn_error_persist=_TurnRunnerTurnErrorPersistAdapter(self),
            usage_telemetry=_TurnRunnerUsageTelemetryAdapter(self),
        )

    def _turn_config(self) -> Any:
        """Return live config with this turn's accepted routing values overlaid."""

        accepted = _ACCEPTED_TURN_CONFIG.get()
        if accepted is None:
            return self._config
        overlay_live_config = getattr(accepted, "overlay_live_config", None)
        if callable(overlay_live_config):
            return overlay_live_config(self._config)
        # Compatibility for direct callers that still install a complete
        # config object in accepted_turn_config_scope().
        return accepted

    def _track_fixed_four_tier_v2_future_locked(
        self,
        future: concurrent.futures.Future[Any],
        *,
        admitted_request: bool = False,
    ) -> None:
        """Track one private-executor job while the lifecycle lock is held."""

        self._fixed_four_tier_v2_pending_futures.add(future)

        def _discard(done: concurrent.futures.Future[Any]) -> None:
            with self._fixed_four_tier_v2_lifecycle_lock:
                self._fixed_four_tier_v2_pending_futures.discard(done)
                if admitted_request:
                    self._fixed_four_tier_v2_admission.release()

        future.add_done_callback(_discard)

    def _submit_fixed_four_tier_v2_job(
        self,
        callback: Callable[[], Any],
    ) -> concurrent.futures.Future[Any]:
        """Submit model work without consuming the event loop's default pool."""

        with self._fixed_four_tier_v2_lifecycle_lock:
            if self._fixed_four_tier_v2_close_future is not None:
                raise RuntimeError("TurnRunner fixed four-tier runtime is closed")
            if not self._fixed_four_tier_v2_admission.acquire(blocking=False):
                raise RuntimeError("TurnRunner fixed four-tier runtime queue is full")
            try:
                future = self._fixed_four_tier_v2_executor.submit(callback)
            except BaseException:
                self._fixed_four_tier_v2_admission.release()
                raise
            self._track_fixed_four_tier_v2_future_locked(
                future,
                admitted_request=True,
            )
            return future

    async def _run_fixed_four_tier_v2_job(self, callback: Callable[[], Any]) -> Any:
        """Await private model work, cancelling only jobs that have not started."""

        future = self._submit_fixed_four_tier_v2_job(callback)
        wrapped = asyncio.wrap_future(future)

        def _consume_unobserved_failure(done: asyncio.Future[Any]) -> None:
            if not done.cancelled():
                done.exception()

        # A running native call can finish after its disconnected waiter. Keep
        # its proxy observed so a later exception does not become an unhandled
        # event-loop warning.
        wrapped.add_done_callback(_consume_unobserved_failure)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # Queued work is removable and immediately returns its admission
            # slot. A running native call cannot be killed safely; cancel()
            # returns False and the single worker drains it before maintenance.
            future.cancel()
            raise

    def _close_fixed_four_tier_v2_resource(self, resource: Any, *, phase: str) -> None:
        """Close one model owner without breaking routing or shutdown."""

        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - model cleanup is deliberately fail-open.
            log.warning(
                "fixed_four_tier_v2.router_close_failed",
                phase=phase,
                resource_type=type(resource).__name__,
                exc_info=True,
            )

    def _close_cached_fixed_four_tier_v2_routers(self, *, phase: str) -> None:
        """Synchronously clear cached routers on the private model worker."""

        with self._fixed_four_tier_v2_router_lock:
            routers = tuple(self._fixed_four_tier_v2_routers.values())
            self._fixed_four_tier_v2_routers.clear()
            with self._fixed_four_tier_v2_lifecycle_lock:
                self._fixed_four_tier_v2_router_cache_populated = False
            for router in routers:
                self._close_fixed_four_tier_v2_resource(router, phase=phase)

    async def _release_fixed_four_tier_v2_routers(self) -> None:
        """Queue cache release after prior native work when the mode is left."""

        with self._fixed_four_tier_v2_lifecycle_lock:
            if self._fixed_four_tier_v2_close_future is not None:
                return
            release_future = self._fixed_four_tier_v2_release_future
            if (
                release_future is self._fixed_four_tier_v2_release_timeout_future
                and release_future is not None
                and not release_future.done()
            ):
                # The first mode-switch request already paid the bounded wait.
                # Do not impose the same delay on every subsequent non-fixed turn.
                return
            if release_future is None or release_future.done():
                pending_model_work = any(
                    not future.done()
                    for future in self._fixed_four_tier_v2_pending_futures
                    if future is not release_future
                )
                if not self._fixed_four_tier_v2_router_cache_populated and not pending_model_work:
                    return
                release_future = self._fixed_four_tier_v2_executor.submit(
                    lambda: self._close_cached_fixed_four_tier_v2_routers(
                        phase="selection_mode_changed"
                    )
                )
                self._fixed_four_tier_v2_release_future = release_future
                self._fixed_four_tier_v2_release_timeout_future = None
                self._track_fixed_four_tier_v2_future_locked(release_future)

        wrapped = asyncio.wrap_future(release_future)

        def _consume_unobserved_failure(done: asyncio.Future[Any]) -> None:
            if not done.cancelled():
                done.exception()

        wrapped.add_done_callback(_consume_unobserved_failure)
        try:
            await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=self._fixed_four_tier_v2_release_timeout_seconds,
            )
        except TimeoutError:
            with self._fixed_four_tier_v2_lifecycle_lock:
                should_report = (
                    self._fixed_four_tier_v2_release_timeout_future is not release_future
                )
                self._fixed_four_tier_v2_release_timeout_future = release_future
                outstanding_jobs = sum(
                    not future.done()
                    for future in self._fixed_four_tier_v2_pending_futures
                    if future is not release_future
                )
            if should_report:
                log.warning(
                    "fixed_four_tier_v2.release_timed_out",
                    timeout_seconds=self._fixed_four_tier_v2_release_timeout_seconds,
                    outstanding_jobs=outstanding_jobs,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - mode-switch cleanup is fail-open.
            log.warning(
                "fixed_four_tier_v2.release_failed",
                exc_info=True,
            )

    def _shutdown_fixed_four_tier_v2_executor_no_wait(self) -> None:
        """Reject new executor work without joining its current worker."""

        try:
            self._fixed_four_tier_v2_executor.shutdown(
                wait=False,
                cancel_futures=False,
            )
        except Exception:  # noqa: BLE001 - teardown remains best effort.
            log.warning(
                "fixed_four_tier_v2.executor_close_failed",
                exc_info=True,
            )

    def _shutdown_fixed_four_tier_v2_runtime(self) -> None:
        """Run at the tail of the private queue and stop its executor."""

        try:
            self._close_cached_fixed_four_tier_v2_routers(phase="turn_runner_close")
        finally:
            # This method itself runs on the executor worker. ``wait=False``
            # marks it shut down without attempting to join the current thread.
            self._shutdown_fixed_four_tier_v2_executor_no_wait()

    async def aclose(self) -> None:
        """Drain native model work and release runner-owned resources once."""

        with self._fixed_four_tier_v2_lifecycle_lock:
            close_future = self._fixed_four_tier_v2_close_future
            if close_future is None:
                close_future = self._fixed_four_tier_v2_executor.submit(
                    self._shutdown_fixed_four_tier_v2_runtime
                )
                self._fixed_four_tier_v2_close_future = close_future
                self._track_fixed_four_tier_v2_future_locked(close_future)

        wrapped = asyncio.wrap_future(close_future)

        def _consume_unobserved_failure(done: asyncio.Future[Any]) -> None:
            if not done.cancelled():
                done.exception()

        wrapped.add_done_callback(_consume_unobserved_failure)
        try:
            # Shielding preserves the queued drain if the shutdown caller is
            # cancelled. A bounded wait prevents one wedged native call from
            # hanging gateway shutdown, while the queued cleanup remains the
            # sole owner allowed to close its model after that call returns.
            await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=self._fixed_four_tier_v2_close_timeout_seconds,
            )
        except TimeoutError:
            with self._fixed_four_tier_v2_lifecycle_lock:
                should_report = not self._fixed_four_tier_v2_close_timeout_reported
                self._fixed_four_tier_v2_close_timeout_reported = True
                outstanding_jobs = sum(
                    not future.done()
                    for future in self._fixed_four_tier_v2_pending_futures
                    if future is not close_future
                )
            if should_report:
                log.warning(
                    "fixed_four_tier_v2.runtime_close_timed_out",
                    timeout_seconds=self._fixed_four_tier_v2_close_timeout_seconds,
                    outstanding_jobs=outstanding_jobs,
                )
            # Mark the executor shut down now so even private/direct submitters
            # cannot extend the queue. cancel_futures=False preserves the tail
            # cleanup job, which closes the runner only after native work exits.
            self._shutdown_fixed_four_tier_v2_executor_no_wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - ServiceContainer teardown is fail-open.
            log.warning(
                "fixed_four_tier_v2.runtime_close_failed",
                exc_info=True,
            )

    async def close(self) -> None:
        """Compatibility alias for async resource owners."""

        await self.aclose()

    def _fixed_four_tier_v2_router_for_config(self, ensemble_cfg: Any) -> Any:
        """Return the router bound to one accepted configuration snapshot."""

        from opensquilla.engine.routing.fixed_four_tier_v2 import FixedFourTierV2Router

        route_cfg = getattr(ensemble_cfg, "four_tier_mapping", None)
        if route_cfg is None:
            raise RuntimeError("four_tier_mapping configuration is unavailable")
        dump = getattr(route_cfg, "model_dump", None)
        payload = dump(mode="json") if callable(dump) else vars(route_cfg)
        config_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        with self._fixed_four_tier_v2_router_lock:
            router = self._fixed_four_tier_v2_routers.pop(config_hash, None)
            if router is None:
                default_tier = cast(
                    Any,
                    str(getattr(route_cfg, "default_new_task_tier", "c1") or "c1"),
                )
                classifier_cfg = getattr(route_cfg, "classifier", None)
                classifier_backend = str(getattr(classifier_cfg, "backend", "") or "")
                if not classifier_backend:
                    raise RuntimeError("four_tier_mapping classifier configuration is unavailable")
                classifier = None
                if classifier_backend == "registered_model":
                    from opensquilla.engine.routing.registered_model import (
                        RegisteredModelClassifier,
                    )

                    classifier = RegisteredModelClassifier(
                        artifact_root=str(getattr(classifier_cfg, "artifact_root", "") or ""),
                        metadata_db=str(getattr(classifier_cfg, "metadata_db", "") or ""),
                        model_set_id=str(getattr(classifier_cfg, "model_set_id", "") or ""),
                        expected_manifest_hash=str(
                            getattr(classifier_cfg, "expected_manifest_hash", "") or ""
                        ),
                        allow_candidate=bool(getattr(classifier_cfg, "allow_candidate", False)),
                    )
                elif classifier_backend != "random_mock":
                    raise RuntimeError(
                        f"unsupported four_tier_mapping classifier backend: {classifier_backend}"
                    )
                try:
                    router = FixedFourTierV2Router(
                        intent_classifier=classifier,
                        tier_classifier=classifier,
                        mock_seed=(
                            getattr(classifier_cfg, "seed", None)
                            if classifier_backend == "random_mock"
                            else None
                        ),
                        default_new_task_tier=default_tier,
                        intent_min_confidence=float(
                            getattr(route_cfg, "intent_min_confidence", 0.5)
                        ),
                        tier_min_confidence=float(getattr(route_cfg, "tier_min_confidence", 0.5)),
                        min_margin=float(getattr(route_cfg, "min_margin", 0.05)),
                        policy_config=payload,
                    )
                except BaseException:
                    if classifier is not None:
                        self._close_fixed_four_tier_v2_resource(
                            classifier,
                            phase="router_construction_rollback",
                        )
                    raise
            self._fixed_four_tier_v2_routers[config_hash] = router
            with self._fixed_four_tier_v2_lifecycle_lock:
                self._fixed_four_tier_v2_router_cache_populated = True
            # A BERT model set can own several large native sessions. Keep one
            # accepted configuration resident and deterministically release a
            # replaced runner instead of retaining the previous mock-era LRU=8.
            while len(self._fixed_four_tier_v2_routers) > 1:
                _, evicted = self._fixed_four_tier_v2_routers.popitem(last=False)
                self._close_fixed_four_tier_v2_resource(
                    evicted,
                    phase="config_replaced",
                )
            return router

    def _persistent_canary_rollout_ledger(self, turn_config: Any) -> Any | None:
        """Return the runner-owned ledger only for explicit live enablement."""

        ensemble_config = getattr(turn_config, "llm_ensemble", None)
        rollout_config = getattr(ensemble_config, "canary_rollout", None)
        auto_rollback = getattr(rollout_config, "auto_rollback", None)
        if getattr(auto_rollback, "enabled", False) is not True:
            return None
        raw_state_dir = str(getattr(turn_config, "state_dir", "") or "").strip()
        if not raw_state_dir:
            return None
        try:
            state_dir = Path(raw_state_dir).expanduser().resolve(strict=False)
            from opensquilla.canary_rollout import (
                CanaryRolloutLedger,
            )

            ledger_path = CanaryRolloutLedger.default_path(state_dir)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        with self._canary_rollout_ledger_lock:
            if self._canary_rollout_ledger_path_drifted:
                return None
            if self._canary_rollout_ledger_path is None:
                self._canary_rollout_ledger_path = ledger_path
            elif self._canary_rollout_ledger_path != ledger_path:
                self._canary_rollout_ledger_path_drifted = True
                self._canary_rollout_ledger = None
                return None
            if self._canary_rollout_ledger is None:
                try:
                    self._canary_rollout_ledger = CanaryRolloutLedger(ledger_path)
                except (OSError, RuntimeError, TypeError, ValueError):
                    return None
            return self._canary_rollout_ledger

    @property
    def router_control_hold_store(self) -> RouterControlHoldStore:
        """Session-keyed router-control hold store consulted by the router step.

        This is the same instance forwarded into the turn loop through
        ``initial_metadata["router_control_hold_store"]`` (and onto the
        ``router_control`` tool context), so operator RPCs that read or write
        holds here directly affect the routing of subsequent turns.
        """
        return self._router_control_hold_store

    def has_compacted_this_turn(self, session_key: str) -> bool:
        return session_key in self._turn_compacted_sessions

    def mark_compacted_this_turn(self, session_key: str) -> None:
        self._turn_compacted_sessions.add(session_key)

    def has_attempted_compaction_this_turn(self, session_key: str) -> bool:
        return session_key in self._turn_compaction_attempted_sessions

    def mark_compaction_attempted_this_turn(self, session_key: str) -> None:
        self._turn_compaction_attempted_sessions.add(session_key)

    def clear_compacted_this_turn(self, session_key: str) -> None:
        self._turn_compacted_sessions.discard(session_key)

    def clear_compaction_turn_state(self, session_key: str) -> None:
        self._turn_compaction_attempted_sessions.discard(session_key)
        self._turn_compacted_sessions.discard(session_key)
        self._emergency_compaction_overrides.pop(session_key, None)

    def _previous_router_dynamic_route(self, session_key: str) -> dict[str, Any] | None:
        route = self._router_dynamic_last_routes.pop(session_key, None)
        if route is None:
            return None
        self._router_dynamic_last_routes[session_key] = route
        return copy.deepcopy(route)

    @staticmethod
    def _read_global_memory_md(config: Any) -> str | None:
        """The consolidated global MEMORY.md text, or ``None`` when there is none.

        Pinned to the ``main`` agent's workspace because the profile is global
        (one operator), and resolved the *same* way Dream resolves the workspace
        it consolidates into — ``resolve_agent_workspace_dir("main", config)``,
        exactly what ``build_dream`` uses — so the projection reads the file
        Dream actually writes. Never raises: an absent or unreadable memory is
        an empty preference, not a failed turn. ``None`` (absent/unreadable) is
        kept distinct from ``""`` (present but empty) so the caller can tell
        "nothing of the operator's was read" from "read, no preference yet".
        """
        from opensquilla.agents.scope import resolve_agent_workspace_dir

        try:
            workspace = resolve_agent_workspace_dir("main", config)
            return (workspace / "MEMORY.md").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None

    @staticmethod
    def _resolve_user_profile(ranking_config: Any, config: Any = None) -> dict[str, Any]:
        """The resolved profile over the mock baseline — the only real read seam.

        Two sources feed it, and neither is an accumulator of its own:

        * ``history`` — which models the operator prefers or avoids — is a
          read-time *projection* over Dream-consolidated memory. A thumb was
          transcribed into a preference line, Dream promoted it into MEMORY.md,
          and :func:`project_history` recovers the model ids here.
        * ``permission``/``preference`` — ``deny_models`` and the tradeoff
          enums — come from the hand-edited file, the only surface for them.

        The mock supplies the shape and every default neither source carries.
        Only keys the baseline already defines are overlaid, so a hand-edited
        file can set ``deny_models`` but cannot introduce a key ranking never
        validated. Degrades to the baseline on any failure. This is deliberately
        NOT wired into ``ensemble.py``'s own fallback: that path serves
        benchmarks and the experiment runner, which must stay reproducible
        rather than read whatever the operator happens to have thumbed.
        """
        from opensquilla.provider.ranking_router import (
            mock_user_profile,
            validate_user_profile,
        )
        from opensquilla.squilla_router.self_learning.preference_projection import (
            PROFILE_SOURCE,
            project_history,
        )
        from opensquilla.squilla_router.self_learning.profile import (
            content_version,
            load_profile,
            profile_path,
        )

        base = mock_user_profile(ranking_config)
        try:
            # History: projected from Dream-consolidated global memory. Present
            # but empty memory yields an empty projection — an operator who has
            # expressed no preference, which is a real (empty) profile.
            memory_text = TurnRunner._read_global_memory_md(config)
            projected = project_history(memory_text or "")
            history = dict(base.get("history") or {})
            history.update({k: v for k, v in projected.items() if k in history})
            base["history"] = history

            # Permission/preference: the hand-edited file remains the only
            # surface. Absent is fine — the mock baseline already stands in.
            stored = load_profile()
            file_used = False
            if stored is not None:
                errors = validate_user_profile(stored, ranking_config)
                if errors:
                    # Refuse the whole file rather than route on the half of it
                    # that parsed. This file is the only way to say deny_models,
                    # so a typo that silently did nothing would be worse than one
                    # that is loudly ignored.
                    log.warning(
                        "router_dynamic.user_profile_invalid",
                        errors=errors,
                        path=str(profile_path()),
                    )
                    fallback = mock_user_profile(ranking_config)
                    fallback["profile_source"] = "fallback_mock"
                    return fallback
                file_used = True
                for section in ("permission", "preference"):
                    value = stored.get(section)
                    if not isinstance(value, Mapping):
                        continue
                    merged = dict(base.get(section) or {})
                    merged.update({k: v for k, v in value.items() if k in merged})
                    base[section] = merged

            if memory_text is None and not file_used:
                # Neither source read: what ranking is about to score against is
                # the bare mock, not the operator's profile. Say so, and stamp
                # no version — there is no profile content to version.
                base["profile_source"] = "fallback_mock"
                return base
            # Provenance is what happened here, not what any file claims. Both
            # fields are derived rather than read: the file is hand-editable, so
            # reading them would let it pin a source ranking never chose, and
            # would miss the one edit it exists for — ``deny_models`` has no TOML
            # key, and editing it never touches a write path that stamps a
            # version. Hash what was ranked with, after the overlay.
            base["profile_source"] = PROFILE_SOURCE
            base["profile_version"] = content_version(base)
        except Exception:  # noqa: BLE001 — a broken profile must not fail a turn
            log.warning("router_dynamic.user_profile_load_failed", exc_info=True)
            # Rebuild rather than return ``base``: the overlay above mutates it
            # section by section, so a raise partway through leaves a half-merged
            # profile that would ship as if it were the user's own.
            fallback = mock_user_profile(ranking_config)
            fallback["profile_source"] = "fallback_mock"
            return fallback
        return base

    def _router_dynamic_task_analyzer_provider(
        self,
        inherited_provider_config: Any,
        *,
        session_key: str,
        ranking_config: Mapping[str, Any],
        analyzer_route: Mapping[str, Any] | None = None,
        allow_canary_route: bool = False,
    ) -> Any | None:
        """Build the frozen task analyzer without reusing another credential."""

        from opensquilla.engine.selector_override import resolve_tier_provider_config
        from opensquilla.provider.ranking_router import (
            TaskAnalyzerCandidate,
            task_analyzer_policy,
        )
        from opensquilla.provider.registry import get_provider_spec
        from opensquilla.provider.selector import (
            ModelSelector,
            ProviderConfig,
            SelectorConfig,
        )

        analyzer_provider_id = "openrouter"
        analyzer_model_id = "unknown"
        try:
            analyzer_policy = task_analyzer_policy(ranking_config)
            route = (
                dict(analyzer_route)
                if analyzer_route is not None
                else {
                    "provider": analyzer_policy["provider"],
                    "model": analyzer_policy["model"],
                    "upstream_provider": analyzer_policy["upstream_provider"],
                }
            )
            validated_route = TaskAnalyzerCandidate(
                provider_id=str(route.get("provider") or ""),
                model_id=str(route.get("model") or ""),
                upstream_provider=str(route.get("upstream_provider") or ""),
            )
            analyzer_provider_id = validated_route.provider_id
            analyzer_model_id = validated_route.model_id
            analyzer_upstream_provider = validated_route.upstream_provider
            if not allow_canary_route:
                from opensquilla.provider.ranking_router import (
                    load_model_registry_snapshot,
                )

                authoritative_registry = load_model_registry_snapshot()
                registry_rows = authoritative_registry.get("models")
                if not isinstance(registry_rows, Sequence) or isinstance(
                    registry_rows,
                    (str, bytes),
                ):
                    raise ValueError("authoritative model registry has no model rows")
                for registry_row in registry_rows:
                    facts = (
                        registry_row.get("registry_facts")
                        if isinstance(registry_row, Mapping)
                        else None
                    )
                    if not isinstance(facts, Mapping):
                        continue
                    if (
                        str(facts.get("provider") or "").strip().casefold() == analyzer_provider_id
                        and str(facts.get("model_id") or "").strip().casefold() == analyzer_model_id
                        and str(facts.get("status") or "").strip().casefold() == "canary"
                    ):
                        log.warning(
                            "llm_ensemble.router_dynamic.task_analyzer_canary_blocked",
                            provider=analyzer_provider_id,
                            model=analyzer_model_id,
                        )
                        return None
            spec = get_provider_spec(analyzer_provider_id)
            turn_config = self._turn_config()
            inherited_provider = (
                str(getattr(inherited_provider_config, "provider", "") or "").strip().lower()
            )
            analyzer_config: ProviderConfig | None = None
            if inherited_provider == analyzer_provider_id:
                inherited_api_key = (
                    str(getattr(inherited_provider_config, "api_key", "") or "").strip()
                    or os.environ.get(spec.env_key, "").strip()
                )
                analyzer_config = replace(
                    inherited_provider_config,
                    model=analyzer_model_id,
                    api_key=inherited_api_key,
                    base_url=str(getattr(inherited_provider_config, "base_url", "") or "").strip()
                    or spec.default_base_url,
                    replay_provider_state=False,
                )
            else:
                primary = getattr(turn_config, "llm", None)
                primary_provider = str(getattr(primary, "provider", "") or "").strip().lower()
                if primary_provider == analyzer_provider_id:
                    api_key = str(getattr(primary, "api_key", "") or "").strip()
                    if not api_key:
                        env_name = str(getattr(primary, "api_key_env", "") or "").strip()
                        api_key = os.environ.get(env_name or spec.env_key, "").strip()
                    analyzer_config = ProviderConfig(
                        provider=analyzer_provider_id,
                        model=analyzer_model_id,
                        api_key=api_key,
                        base_url=str(getattr(primary, "base_url", "") or "").strip()
                        or spec.default_base_url,
                        proxy=str(getattr(primary, "proxy", "") or "").strip(),
                        provider_routing=dict(getattr(primary, "provider_routing", {}) or {}),
                        replay_provider_state=False,
                    )
                else:
                    analyzer_config = resolve_tier_provider_config(
                        turn_config,
                        analyzer_provider_id,
                        analyzer_model_id,
                        session_key=session_key,
                    )

            if analyzer_config is not None and (
                str(analyzer_config.provider or "").strip().casefold() != analyzer_provider_id
            ):
                raise ValueError("task analyzer credential resolver returned a different provider")
            if analyzer_config is not None:
                analyzer_routing = dict(analyzer_config.provider_routing)
                analyzer_routing[analyzer_model_id] = analyzer_upstream_provider
                analyzer_config = replace(
                    analyzer_config,
                    provider=analyzer_provider_id,
                    model=analyzer_model_id,
                    provider_routing=analyzer_routing,
                    replay_provider_state=False,
                    _provider_routing_strict_override=True,
                )

            if analyzer_config is None or (
                spec.requires_api_key() and not analyzer_config.api_key.strip()
            ):
                log.warning(
                    "llm_ensemble.router_dynamic.task_analyzer_provider_unavailable",
                    provider=analyzer_provider_id,
                    model=analyzer_model_id,
                    reason="credential_unavailable",
                )
                return None
            # Resolve the exact deployment assembled above. The convenience
            # ``build_provider`` factory only accepts legacy scalar fields and
            # would silently discard OpenRouter ``provider_routing`` as well as
            # the analyzer's no-private-state replay boundary.
            return ModelSelector(SelectorConfig(primary=analyzer_config)).resolve()
        except Exception as exc:  # noqa: BLE001 - task analysis has a local fallback
            log.warning(
                "llm_ensemble.router_dynamic.task_analyzer_provider_unavailable",
                provider=analyzer_provider_id,
                model=analyzer_model_id,
                reason=type(exc).__name__,
            )
            return None

    def _router_dynamic_cache_generation(self, session_key: str) -> int:
        generations = self._router_dynamic_cache_affinity_generation
        if generations is not None and session_key in generations:
            return generations[session_key]
        # Missing per-session state must not recreate generation zero after an
        # LRU eviction.  The process-local clock makes eviction conservative:
        # an unpinned selection can be invalidated spuriously, but a stale
        # selection can never regain an earlier generation through ABA.
        return self._router_dynamic_cache_affinity_generation_clock

    def _ensure_router_dynamic_cache_affinity_state(self) -> None:
        """Lazily allocate private state only after explicit feature enablement."""

        if self._router_dynamic_cache_affinity is None:
            self._router_dynamic_cache_affinity = OrderedDict()
        if self._router_dynamic_cache_affinity_sidecars is None:
            self._router_dynamic_cache_affinity_sidecars = {}
        if self._router_dynamic_cache_affinity_generation is None:
            self._router_dynamic_cache_affinity_generation = {}
        if self._router_dynamic_cache_affinity_epoch_by_key is None:
            self._router_dynamic_cache_affinity_epoch_by_key = {}

    def _prune_router_dynamic_cache_affinity_control_state(
        self,
        *,
        route_cache_max_entries: int | None = None,
        preserve_session_key: str | None = None,
    ) -> None:
        """Bound auxiliary maps while retaining every live dispatch fence."""

        if type(route_cache_max_entries) is int and route_cache_max_entries > 0:
            self._router_dynamic_cache_affinity_control_max_entries = route_cache_max_entries
        capacity = self._router_dynamic_cache_affinity_control_max_entries
        if capacity is None:
            return
        states = self._router_dynamic_cache_affinity
        sidecars = self._router_dynamic_cache_affinity_sidecars
        protected_sessions = {key.session_key for key in states or ()}
        protected_sessions.update(
            sidecar.context.session_key for sidecar in (sidecars or {}).values()
        )
        if preserve_session_key:
            protected_sessions.add(preserve_session_key)
        target_size = max(capacity, len(protected_sessions))
        for control_state in (
            self._router_dynamic_cache_affinity_generation,
            self._router_dynamic_cache_affinity_epoch_by_key,
        ):
            if control_state is None:
                continue
            while len(control_state) > target_size:
                evictable = next(
                    (key for key in control_state if key not in protected_sessions),
                    None,
                )
                if evictable is None:
                    break
                control_state.pop(evictable, None)

    def _ensure_router_dynamic_cache_compaction_listener(
        self,
        *,
        route_cache_max_entries: int,
    ) -> None:
        self._ensure_router_dynamic_cache_affinity_state()
        self._prune_router_dynamic_cache_affinity_control_state(
            route_cache_max_entries=route_cache_max_entries,
        )
        self._ensure_router_dynamic_cache_session_delete_listener()
        if self._router_dynamic_cache_affinity_compaction_remove is not None:
            return
        from opensquilla.engine.cache_break_monitor import add_compaction_listener

        runner_ref = weakref.ref(self)

        def _on_compaction(session_key: str, payload: dict[str, Any]) -> None:
            runner = runner_ref()
            if runner is None:
                return
            if str(payload.get("status") or "").strip().casefold() != "completed":
                return
            runner._invalidate_router_dynamic_cache_affinity(
                session_key=session_key,
                reason="compaction",
            )

        remove = add_compaction_listener(_on_compaction)
        self._router_dynamic_cache_affinity_compaction_remove = remove
        self._router_dynamic_cache_affinity_compaction_finalizer = weakref.finalize(
            self,
            remove,
        )

    def _ensure_router_dynamic_cache_session_delete_listener(self) -> None:
        """Bind private affinity state to every durable session-delete path."""

        if self._router_dynamic_cache_affinity_session_delete_remove is not None:
            return
        try:
            from opensquilla.gateway.session_services import get_session_storage

            storage = get_session_storage(self._session_manager)
            add_listener = getattr(storage, "add_session_delete_listener", None)
        except Exception:  # noqa: BLE001 - optional lifecycle seam fails closed
            return
        if not callable(add_listener):
            return

        runner_ref = weakref.ref(self)

        def _on_session_deleted(session_key: str) -> None:
            runner = runner_ref()
            if runner is None:
                return
            runner._invalidate_router_dynamic_cache_affinity(
                session_key=session_key,
                reason="session_deleted",
            )

        try:
            remove = add_listener(_on_session_deleted)
        except Exception:  # noqa: BLE001 - affinity remains fail-closed by epoch lookup
            return
        if not callable(remove):
            return
        self._router_dynamic_cache_affinity_session_delete_remove = remove
        self._router_dynamic_cache_affinity_session_delete_finalizer = weakref.finalize(
            self,
            remove,
        )

    def _invalidate_router_dynamic_cache_affinity(
        self,
        *,
        session_key: str | None = None,
        credential_namespace: object | None = None,
        reason: str,
    ) -> None:
        """Purge private affinity state and advance its dispatch generation."""

        delete_session_lifecycle = reason == "session_deleted" and session_key is not None
        del credential_namespace  # opaque guards require conservative scope purge
        states = self._router_dynamic_cache_affinity
        generations = self._router_dynamic_cache_affinity_generation
        if states is None or generations is None:
            return
        affected_sessions: set[str] = set()
        for key in list(states):
            if session_key is not None and key.session_key != session_key:
                continue
            # Cache-domain guards deliberately expose no credential material.
            # A credential-rotation callback therefore invalidates the whole
            # selected scope rather than introspecting or persisting a token.
            affected_sessions.add(key.session_key)
            states.pop(key, None)
        if session_key is not None:
            affected_sessions.add(session_key)
        if not affected_sessions:
            return
        next_generation = (
            max(
                [
                    self._router_dynamic_cache_affinity_generation_clock,
                    *generations.values(),
                ]
            )
            + 1
        )
        self._router_dynamic_cache_affinity_generation_clock = next_generation
        for affected in affected_sessions:
            generations.pop(affected, None)
            generations[affected] = next_generation
        if delete_session_lifecycle:
            sidecars = self._router_dynamic_cache_affinity_sidecars
            if sidecars is not None:
                for sidecar_key in list(sidecars):
                    if sidecar_key[0] == session_key:
                        sidecars.pop(sidecar_key, None)
            epoch_by_key = self._router_dynamic_cache_affinity_epoch_by_key
            if epoch_by_key is not None:
                epoch_by_key.pop(session_key, None)
        self._prune_router_dynamic_cache_affinity_control_state(
            preserve_session_key=session_key,
        )

    async def _resolve_router_dynamic_session_epoch(
        self,
        session_key: str,
    ) -> int | None:
        """Resolve reset fencing only on an enabled affinity path."""

        if self._session_manager is None:
            return None
        try:
            from opensquilla.gateway.session_services import (
                get_session_epoch,
                get_session_storage,
            )

            raw_epoch = get_session_epoch(self._session_manager, session_key)
            if raw_epoch is None:
                get_session = getattr(self._session_manager, "get_session", None)
                if callable(get_session):
                    node = await get_session(session_key)
                else:
                    storage = get_session_storage(self._session_manager)
                    node = await storage.get_session(session_key) if storage is not None else None
                if node is None:
                    return None
                raw_epoch = getattr(node, "epoch", None)
            if type(raw_epoch) is not int:
                return None
            epoch = raw_epoch
            if epoch < 0:
                return None
        except Exception:  # noqa: BLE001 - continuity is fail-closed
            return None

        epoch_by_key = self._router_dynamic_cache_affinity_epoch_by_key
        if epoch_by_key is None:
            return None
        previous_epoch = epoch_by_key.get(session_key)
        if previous_epoch is not None and previous_epoch != epoch:
            self._invalidate_router_dynamic_cache_affinity(
                session_key=session_key,
                reason="session_epoch_changed",
            )
        epoch_by_key.pop(session_key, None)
        epoch_by_key[session_key] = epoch
        self._prune_router_dynamic_cache_affinity_control_state(
            preserve_session_key=session_key,
        )
        return epoch

    def _router_dynamic_cache_continuity_snapshot(
        self,
        *,
        session_key: str,
        session_epoch: int,
        policy: _RouterDynamicCacheAffinityPolicy,
        now: float | None = None,
    ) -> tuple[bool, tuple[_RouterDynamicCacheAffinityReceipt, ...], int]:
        """Freeze unexpired receipts before Analyzer execution."""

        self._ensure_router_dynamic_cache_affinity_state()
        states = self._router_dynamic_cache_affinity
        generations = self._router_dynamic_cache_affinity_generation
        assert states is not None
        assert generations is not None
        generation = self._router_dynamic_cache_generation(session_key)
        generations.pop(session_key, None)
        generations[session_key] = generation
        while len(states) > policy.route_cache_max_entries:
            states.popitem(last=False)
        for key in list(states):
            if key.session_key == session_key and key.session_epoch != session_epoch:
                states.pop(key, None)
        state_key = _RouterDynamicCacheAffinityStateKey(
            session_key=session_key,
            session_epoch=session_epoch,
            topology=policy.topology,
        )
        state = states.get(state_key)
        if state is None:
            self._prune_router_dynamic_cache_affinity_control_state(
                route_cache_max_entries=policy.route_cache_max_entries,
                preserve_session_key=session_key,
            )
            return False, (), generation
        observed_at = time.monotonic() if now is None else now
        for receipt_key, receipt in list(state.receipts.items()):
            age = observed_at - receipt.observed_at_monotonic
            if (
                receipt.topology != policy.topology
                or (policy.topology == "single" and receipt.role != "single")
                or (
                    policy.topology == "multiple" and receipt.role not in {"proposer", "aggregator"}
                )
                or not math.isfinite(age)
                or age < 0.0
                or age > policy.ttl_seconds
            ):
                state.receipts.pop(receipt_key, None)
        if not state.receipts:
            states.pop(state_key, None)
            self._prune_router_dynamic_cache_affinity_control_state(
                route_cache_max_entries=policy.route_cache_max_entries,
                preserve_session_key=session_key,
            )
            return False, (), generation
        states.move_to_end(state_key)
        receipts = tuple(state.receipts.values())
        self._prune_router_dynamic_cache_affinity_control_state(
            route_cache_max_entries=policy.route_cache_max_entries,
            preserve_session_key=session_key,
        )
        return True, receipts, generation

    def _router_single_cache_continuity_snapshot(
        self,
        *,
        session_key: str,
        session_epoch: int,
        policy: _RouterDynamicCacheAffinityPolicy,
        now: float | None = None,
    ) -> tuple[bool, tuple[_RouterDynamicCacheAffinityReceipt, ...], int]:
        if policy.topology != "single":
            return False, (), self._router_dynamic_cache_generation(session_key)
        return self._router_dynamic_cache_continuity_snapshot(
            session_key=session_key,
            session_epoch=session_epoch,
            policy=policy,
            now=now,
        )

    @staticmethod
    def _router_dynamic_cache_sidecar_key(
        turn: object,
    ) -> tuple[str, str] | None:
        metadata = getattr(turn, "metadata", None)
        if not isinstance(metadata, Mapping):
            return None
        decision_id = str(
            metadata.get("router_single_decision_id") or metadata.get("ensemble_decision_id") or ""
        ).strip()
        session_key = str(getattr(turn, "session_key", "") or "")
        if not decision_id or not session_key:
            return None
        return session_key, decision_id

    def _register_router_dynamic_cache_sidecar(
        self,
        *,
        context: _RouterDynamicCacheAffinityCollectionContext,
        policy: _RouterDynamicCacheAffinityPolicy,
    ) -> tuple[str, str]:
        self._ensure_router_dynamic_cache_affinity_state()
        sidecars = self._router_dynamic_cache_affinity_sidecars
        generations = self._router_dynamic_cache_affinity_generation
        assert sidecars is not None
        assert generations is not None
        key = (context.session_key, context.decision_id)
        for existing in list(sidecars):
            if existing[0] == context.session_key:
                sidecars.pop(existing, None)
        sidecars[key] = _RouterDynamicCacheAffinityPendingSidecar(
            context=context,
            policy=policy,
            active_provider_instance_token=(context.provider_instance_token),
            active_provider_instance_generation=(context.provider_instance_generation),
        )
        current_generation = self._router_dynamic_cache_generation(context.session_key)
        if current_generation == context.selection_generation:
            generations.pop(context.session_key, None)
            generations[context.session_key] = current_generation
        self._prune_router_dynamic_cache_affinity_control_state(
            route_cache_max_entries=policy.route_cache_max_entries,
        )
        return key

    def _stage_router_dynamic_cache_affinity_batch(
        self,
        sidecar_key: tuple[str, str],
        batch: _RouterDynamicCacheAffinityReceiptBatch,
    ) -> bool:
        sidecars = self._router_dynamic_cache_affinity_sidecars
        if sidecars is None:
            return False
        sidecar = sidecars.get(sidecar_key)
        if sidecar is None:
            return False
        context = sidecar.context
        if (
            batch.turn_id != context.turn_id
            or batch.decision_id != context.decision_id
            or batch.topology != context.topology
            or not batch.provider_instance_token
            or type(batch.provider_instance_generation) is not int
            or batch.provider_instance_generation < 0
            or not batch.chat_call_id
            or type(batch.chat_call_sequence) is not int
            or batch.chat_call_sequence <= sidecar.latest_chat_sequence
        ):
            return False
        if batch.provider_instance_generation < sidecar.active_provider_instance_generation:
            return False
        if batch.provider_instance_generation > sidecar.active_provider_instance_generation:
            if context.topology != "multiple":
                return False
            sidecar.active_provider_instance_token = batch.provider_instance_token
            sidecar.active_provider_instance_generation = batch.provider_instance_generation
        elif batch.provider_instance_token != sidecar.active_provider_instance_token:
            return False
        sidecar.latest_chat_sequence = batch.chat_call_sequence
        sidecar.latest_batch = batch
        return True

    @staticmethod
    def _normalize_multiple_cache_affinity_batch(
        *,
        context: _RouterDynamicCacheAffinityCollectionContext,
        batch: object,
    ) -> _RouterDynamicCacheAffinityReceiptBatch | None:
        """Project one closed ensemble batch onto the neutral state contract."""

        if (
            context.topology != "multiple"
            or getattr(batch, "topology", None) != "multiple"
            or str(getattr(batch, "turn_id", "") or "") != context.turn_id
            or str(getattr(batch, "decision_id", "") or "") != context.decision_id
        ):
            return None
        provider_instance_token = str(getattr(batch, "provider_instance_token", "") or "").strip()
        chat_call_id = str(getattr(batch, "chat_call_id", "") or "").strip()
        provider_instance_generation = getattr(
            batch,
            "provider_instance_generation",
            None,
        )
        chat_call_sequence = getattr(batch, "chat_sequence", None)
        raw_receipts = getattr(batch, "receipts", None)
        if (
            not provider_instance_token
            or not chat_call_id
            or type(provider_instance_generation) is not int
            or provider_instance_generation < 0
            or type(chat_call_sequence) is not int
            or chat_call_sequence < 0
            or not isinstance(raw_receipts, Sequence)
            or isinstance(raw_receipts, (str, bytes, bytearray))
        ):
            return None
        receipts: list[_RouterDynamicCacheAffinityReceipt] = []
        for raw_receipt in raw_receipts:
            requested_provider = str(getattr(raw_receipt, "requested_provider", "") or "").strip()
            requested_model = str(getattr(raw_receipt, "requested_model", "") or "").strip()
            actual_provider = str(getattr(raw_receipt, "actual_provider", "") or "").strip()
            actual_model = str(getattr(raw_receipt, "actual_model", "") or "").strip()
            if (
                not requested_provider
                or not requested_model
                or requested_provider.casefold() != actual_provider.casefold()
                or not _router_dynamic_model_matches_frozen_alias(
                    requested_model,
                    actual_model,
                    getattr(raw_receipt, "actual_model_aliases", ()),
                )
            ):
                # The envelope still authoritatively closes the newest chat,
                # but an unknown serving alias is not continuity evidence.
                receipts.clear()
                break
            canonical_identity = f"{requested_provider}:{requested_model}"
            receipt = build_cache_affinity_receipt(
                physical_attempt_id=str(getattr(raw_receipt, "physical_attempt_id", "") or ""),
                role=str(getattr(raw_receipt, "role", "") or ""),
                topology="multiple",
                execution_slot=str(getattr(raw_receipt, "execution_slot", "") or ""),
                requested_identity=canonical_identity,
                actual_identity=canonical_identity,
                cache_domain_guard=getattr(
                    raw_receipt,
                    "cache_domain_guard",
                    None,
                ),
                cached_tokens=getattr(raw_receipt, "cached_tokens", None),
                cache_write_tokens=getattr(
                    raw_receipt,
                    "cache_write_tokens",
                    None,
                ),
                observed_at_monotonic=getattr(
                    raw_receipt,
                    "observed_at_monotonic",
                    None,
                ),
            )
            if receipt is None:
                # The envelope still proves this is the newest closed chat.
                # Publish an empty authoritative batch so malformed receipt
                # evidence cannot leave a previous chat's hit staged.
                receipts.clear()
                break
            receipts.append(receipt)
        return _RouterDynamicCacheAffinityReceiptBatch(
            turn_id=context.turn_id,
            decision_id=context.decision_id,
            provider_instance_token=provider_instance_token,
            provider_instance_generation=provider_instance_generation,
            chat_call_id=chat_call_id,
            chat_call_sequence=chat_call_sequence,
            runtime_generation=context.selection_generation,
            topology="multiple",
            receipts=tuple(receipts),
        )

    def _clear_router_dynamic_cache_affinity_state(
        self,
        *,
        session_key: str,
        session_epoch: int,
        topology: Literal["single", "multiple"],
    ) -> None:
        key = _RouterDynamicCacheAffinityStateKey(
            session_key=session_key,
            session_epoch=session_epoch,
            topology=topology,
        )
        states = self._router_dynamic_cache_affinity
        if states is None:
            return
        state = states.get(key)
        if state is None:
            return
        states.pop(key, None)

    def _clear_router_single_cache_affinity_state(
        self,
        *,
        session_key: str,
        session_epoch: int,
    ) -> None:
        self._clear_router_dynamic_cache_affinity_state(
            session_key=session_key,
            session_epoch=session_epoch,
            topology="single",
        )

    def _commit_pending_router_dynamic_cache_affinity(
        self,
        turn: TurnContext,
        done_event: DoneEvent | None,
    ) -> bool:
        sidecar_key = self._router_dynamic_cache_sidecar_key(turn)
        turn_metadata = getattr(turn, "metadata", None)
        sidecars = self._router_dynamic_cache_affinity_sidecars
        states = self._router_dynamic_cache_affinity
        if sidecar_key is None or sidecars is None or states is None:
            return False
        sidecar = sidecars.pop(sidecar_key, None)
        if sidecar is None:
            return False
        context = sidecar.context
        # A turn terminal is authoritative for its topology.  Clear the last
        # committed evidence before considering the new batch so failures,
        # cancellation, fallback, and successful turns without complete cache
        # evidence all produce S_cache=0.
        self._clear_router_dynamic_cache_affinity_state(
            session_key=context.session_key,
            session_epoch=context.session_epoch,
            topology=context.topology,
        )

        def reject_batch() -> bool:
            self._prune_router_dynamic_cache_affinity_control_state(
                route_cache_max_entries=sidecar.policy.route_cache_max_entries,
            )
            return False

        if done_event is None:
            return reject_batch()
        fallback_hops = (
            turn_metadata.get("router_fallback_hops")
            if isinstance(turn_metadata, Mapping)
            else None
        )
        if type(fallback_hops) is int and fallback_hops > 0:
            # A successful selector fallback proves the selected affinity
            # route did not serve the final chat. Discard every staged batch,
            # including a valid earlier tool-loop chat, because no receipt can
            # bind the eventual fallback deployment.
            return reject_batch()
        batch = sidecar.latest_batch
        if batch is None:
            return reject_batch()
        if batch.runtime_generation != self._router_dynamic_cache_generation(context.session_key):
            return reject_batch()

        valid_roles = {"single"} if context.topology == "single" else {"proposer", "aggregator"}
        receipts = tuple(
            receipt
            for receipt in batch.receipts
            if receipt.role in valid_roles and receipt.topology == context.topology
        )
        if not receipts:
            return reject_batch()
        state_key = _RouterDynamicCacheAffinityStateKey(
            session_key=context.session_key,
            session_epoch=context.session_epoch,
            topology=context.topology,
        )
        state = states.setdefault(
            state_key,
            _RouterDynamicCacheAffinitySessionState(),
        )
        seen_attempts: set[str] = set()
        for receipt in receipts:
            if not receipt.physical_attempt_id or receipt.physical_attempt_id in seen_attempts:
                continue
            seen_attempts.add(receipt.physical_attempt_id)
            state.receipts[
                (
                    receipt.role,
                    receipt.execution_slot,
                    receipt.requested_identity,
                )
            ] = receipt
        if not state.receipts:
            states.pop(state_key, None)
            return reject_batch()
        states.move_to_end(state_key)
        while len(states) > sidecar.policy.route_cache_max_entries:
            states.popitem(last=False)
        self._prune_router_dynamic_cache_affinity_control_state(
            route_cache_max_entries=sidecar.policy.route_cache_max_entries,
        )
        return True

    def _discard_pending_router_dynamic_cache_affinity(
        self,
        turn: object | None,
        *,
        session_key: str,
    ) -> None:
        sidecar_key = self._router_dynamic_cache_sidecar_key(turn) if turn is not None else None
        sidecars = self._router_dynamic_cache_affinity_sidecars
        if sidecars is None:
            return
        sidecar = sidecars.pop(sidecar_key, None) if sidecar_key is not None else None
        if sidecar is None:
            for key in list(sidecars):
                if key[0] == session_key:
                    sidecar = sidecars.pop(key)
                    break
        if sidecar is None:
            return
        context = sidecar.context
        self._clear_router_dynamic_cache_affinity_state(
            session_key=context.session_key,
            session_epoch=context.session_epoch,
            topology=context.topology,
        )
        self._prune_router_dynamic_cache_affinity_control_state(
            route_cache_max_entries=sidecar.policy.route_cache_max_entries,
        )

    def _remember_router_dynamic_route(
        self, session_key: str, selection_plan: Mapping[str, Any]
    ) -> None:
        from opensquilla.provider.ranking_router import (
            default_session_quality_feedback,
            router_dynamic_route_cache_max_entries,
        )

        session = selection_plan.get("session")
        session_map = session if isinstance(session, Mapping) else {}
        self._router_dynamic_last_routes.pop(session_key, None)
        remembered_route = {
            "selected_P": list(selection_plan.get("selected_P") or []),
            "selected_A": selection_plan.get("selected_A"),
            "strategy_mode": "B5_fuse",
            "quality_feedback": default_session_quality_feedback(),
            "escalation_level": int(session_map.get("escalation_level") or 0),
        }
        thinking_assignment = selection_plan.get("executed_thinking_assignment")
        if not isinstance(thinking_assignment, Mapping):
            thinking_assignment = selection_plan.get("thinking_assignment")
        if isinstance(thinking_assignment, Mapping):
            remembered_route["thinking_assignment"] = copy.deepcopy(dict(thinking_assignment))
        self._router_dynamic_last_routes[session_key] = remembered_route
        while len(self._router_dynamic_last_routes) > router_dynamic_route_cache_max_entries():
            oldest_session = next(iter(self._router_dynamic_last_routes))
            self._router_dynamic_last_routes.pop(oldest_session, None)

    def _commit_pending_router_dynamic_route(
        self,
        turn: TurnContext,
        done_event: DoneEvent | None,
    ) -> bool:
        """Remember a dynamic route only after its aggregator completed successfully."""

        pending = turn.metadata.pop("router_dynamic_pending_route_plan", None)
        if not isinstance(pending, Mapping) or done_event is None:
            return False
        ensemble_trace = getattr(done_event, "ensemble_trace", None)
        if not isinstance(ensemble_trace, Mapping):
            return False
        if ensemble_trace.get("fallback_used") is not False:
            return False
        effective = ensemble_trace.get("effective_selection_plan")
        selection_plan = pending
        if isinstance(effective, Mapping):
            decision_id = effective.get("decision_id")
            selected_proposers = effective.get("selected_P")
            selected_aggregator = effective.get("selected_A")
            pending_decision_id = pending.get("decision_id")
            pending_selected_proposers = pending.get("selected_P")
            pending_selected_aggregator = pending.get("selected_A")
            retry_routing = effective.get("retry_routing")

            same_decision = decision_id == pending_decision_id
            same_decision_same_roster = (
                same_decision
                and isinstance(selected_proposers, Sequence)
                and not isinstance(
                    selected_proposers,
                    (str, bytes, bytearray),
                )
                and isinstance(pending_selected_proposers, Sequence)
                and not isinstance(
                    pending_selected_proposers,
                    (str, bytes, bytearray),
                )
                and tuple(selected_proposers) == tuple(pending_selected_proposers)
                and selected_aggregator == pending_selected_aggregator
            )
            valid_route_lineage = same_decision_same_roster or (
                not same_decision
                and isinstance(pending_decision_id, str)
                and bool(pending_decision_id.strip())
                and effective.get("retry_parent_decision_id") == pending_decision_id
                and effective.get("task_analysis_reused") is True
                and isinstance(retry_routing, Mapping)
                and retry_routing.get("parent_decision_id") == pending_decision_id
            )
            if (
                effective.get("strategy") == "router_dynamic"
                and effective.get("selection_mode") == "router_dynamic"
                and isinstance(decision_id, str)
                and bool(decision_id.strip())
                and isinstance(selected_proposers, Sequence)
                and not isinstance(
                    selected_proposers,
                    (str, bytes, bytearray),
                )
                and bool(selected_proposers)
                and all(
                    _valid_router_dynamic_route_identity(identity)
                    for identity in selected_proposers
                )
                and len(set(selected_proposers)) == len(selected_proposers)
                and _valid_router_dynamic_route_identity(selected_aggregator)
                and valid_route_lineage
            ):
                selection_plan = effective
        try:
            self._remember_router_dynamic_route(turn.session_key, selection_plan)
        except Exception:  # noqa: BLE001 - continuity memory must not fail the turn
            log.warning(
                "llm_ensemble.router_dynamic.route_memory_failed",
                decision_id=turn.metadata.get("ensemble_decision_id"),
                session_key=turn.session_key,
                exc_info=True,
            )
            return False
        return True

    def refresh_memory_snapshot(self, agent_id: str) -> None:
        """Refresh frozen snapshots for all sessions of the given agent.

        Called by the on_memory_write callback when agent writes to
        MEMORY.md or daily notes via memory_save.
        """
        ws = self._resolve_memory_source_dir(agent_id)
        new_snap = MemorySnapshot(
            memory_md=self._load_memory_md(ws),
            daily_notes=self._load_daily_notes(ws),
        )
        for key in list(self._memory_snapshots):
            if key[0] == agent_id:
                self._memory_snapshots[key] = new_snap

    def _handle_memory_source_write(self, agent_id: str, path: str) -> None:
        """Refresh memory index/snapshots after a source Markdown file write."""
        sync_manager = (
            self._memory_sync_managers.get(agent_id) if self._memory_sync_managers else None
        )
        mark_dirty = getattr(sync_manager, "mark_dirty", None)
        if callable(mark_dirty):
            mark_dirty()
        self.refresh_memory_snapshot(agent_id)

    def _handle_bootstrap_source_write(self, agent_id: str, path: str) -> None:
        """Drop frozen bootstrap snapshots after a bootstrap workspace file write."""
        for key in list(self._bootstrap_snapshots):
            if key[0] == agent_id:
                del self._bootstrap_snapshots[key]

    def _with_runtime_write_callbacks(
        self, tool_context: ToolContext, agent_id: str
    ) -> ToolContext:
        """Attach runtime snapshot refresh callbacks without discarding caller hooks."""
        if not tool_context.memory_source_dir:
            try:
                tool_context = replace(
                    tool_context,
                    memory_source_dir=str(self._resolve_memory_source_dir(agent_id)),
                )
            except Exception:  # noqa: BLE001 - memory path should not block tool setup
                pass

        previous_memory_write = tool_context.on_memory_source_write
        if previous_memory_write is None:
            tool_context = replace(
                tool_context,
                on_memory_source_write=self._handle_memory_source_write,
            )
        else:

            def _on_memory_source_write(agent_id: str, path: str) -> None:
                previous_memory_write(agent_id, path)
                self._handle_memory_source_write(agent_id, path)

            tool_context = replace(
                tool_context,
                on_memory_source_write=_on_memory_source_write,
            )

        previous_bootstrap_write = tool_context.on_bootstrap_source_write
        if previous_bootstrap_write is None:
            return replace(
                tool_context,
                on_bootstrap_source_write=self._handle_bootstrap_source_write,
            )

        def _on_bootstrap_source_write(agent_id: str, path: str) -> None:
            previous_bootstrap_write(agent_id, path)
            self._handle_bootstrap_source_write(agent_id, path)

        return replace(
            tool_context,
            on_bootstrap_source_write=_on_bootstrap_source_write,
        )

    async def _with_artifact_context(
        self,
        tool_context: ToolContext,
        session_key: str,
    ) -> ToolContext:
        attachments_cfg = getattr(self._config, "attachments", None)
        media_root = self._attachment_media_root()
        session_id = await self._resolve_session_id_for_log(session_key)
        if not session_id:
            session_id = session_key.split(":")[-1] or session_key
        return replace(
            tool_context,
            session_key=session_key,
            artifact_media_root=str(media_root),
            artifact_session_id=session_id,
            tool_result_store_dir=str(media_root / "tool-results"),
            tool_result_store_session_id=session_id,
            workspace_file_writes=[],
            artifact_max_bytes=getattr(attachments_cfg, "artifact_max_bytes", None),
            artifact_disk_budget_bytes=getattr(
                attachments_cfg,
                "artifact_disk_budget_bytes",
                None,
            ),
        )

    async def _capture_turn_memory(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
    ) -> None:
        memory_cfg = getattr(self._config, "memory", None)
        if not self._turn_memory_capture_allowed(
            no_memory_capture=no_memory_capture,
            input_mode=input_mode,
            run_kind=run_kind,
            input_provenance=input_provenance,
            memory_config=memory_cfg,
        ):
            return
        if self._session_manager is None or not self._turn_capture_services:
            return
        capture_service = self._turn_capture_services.get(
            agent_id
        ) or self._turn_capture_services.get("main")
        if capture_service is None:
            return
        session = await self._session_manager.get_session(session_key)
        if session is None:
            return
        await capture_service.capture_turn(
            session_key=session_key,
            session_id=getattr(session, "session_id", ""),
            user_text=runtime_message,
            assistant_text=final_text,
            source=self._build_turn_call_source(
                tool_context,
                input_provenance,
                run_kind=run_kind,
            ),
            captured_at=datetime.now(tz=UTC),
            no_memory_capture=no_memory_capture,
        )

    @staticmethod
    def _capture_filter_matches(value: str | None, excluded_values: Any) -> bool:
        if not value:
            return False
        if isinstance(excluded_values, str):
            raw_patterns = [excluded_values]
        else:
            raw_patterns = list(excluded_values or [])
        normalized_value = _normalize_capture_kind(value)
        value_parts = {part for part in normalized_value.split("_") if part}
        for pattern in raw_patterns:
            if pattern is None:
                continue
            normalized_pattern = _normalize_capture_kind(str(pattern))
            if not normalized_pattern:
                continue
            if normalized_value == normalized_pattern or normalized_pattern in value_parts:
                return True
        return False

    @staticmethod
    def _input_provenance_kind(input_provenance: dict[str, Any] | None) -> str | None:
        if not isinstance(input_provenance, dict):
            return None
        kind = input_provenance.get("kind")
        return str(kind) if kind is not None and str(kind) else None

    @staticmethod
    def _normalize_input_provenance(
        input_provenance: dict[str, Any] | str | None,
    ) -> dict[str, Any] | None:
        if isinstance(input_provenance, dict):
            return dict(input_provenance)
        if input_provenance:
            return {"kind": str(input_provenance)}
        return None

    @classmethod
    def _turn_memory_capture_allowed(
        cls,
        *,
        no_memory_capture: bool,
        input_mode: str,
        run_kind: str | None,
        input_provenance: dict[str, Any] | None,
        memory_config: Any | None,
    ) -> bool:
        if no_memory_capture or input_mode != "user":
            return False
        if memory_config is None:
            return True
        if cls._capture_filter_matches(
            run_kind,
            getattr(memory_config, "capture_excluded_run_kinds", []),
        ):
            return False
        provenance_kind = cls._input_provenance_kind(input_provenance)
        if cls._capture_filter_matches(
            provenance_kind,
            getattr(memory_config, "capture_excluded_provenance_kinds", []),
        ):
            return False
        return True

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Return the per-session lock for *session_key* from the external provider.

        TurnRunner no longer owns an internal lock dict.  All per-session
        locks are managed by the provider supplied at construction
        (TaskRuntime._get_session_lock_for_turn for the gateway path, or the
        standalone provider for CLI paths).

        External callers (rpc_sessions.py, channel_dispatch.py) that call this
        directly receive the short write lock used for transcript/session state
        mutation. Gateway TaskRuntime uses a separate execution lock for the
        long-running turn lifecycle.
        """
        return self._session_lock_provider(session_key)

    def get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Public lock-provider seam for RPC/session services."""
        return self._get_session_lock(session_key)

    def set_session_lock_provider(self, provider: Callable[[str], asyncio.Lock]) -> None:
        """Replace the lock provider at the gateway composition root."""
        self._session_lock_provider = provider

    @contextlib.asynccontextmanager
    async def _session_write_context(self, session_key: str) -> AsyncIterator[None]:
        lock = self.get_session_lock(session_key)
        bypass_only = _SESSION_LOCK_BYPASS_ONLY.get(None)
        if bypass_only is not None and id(lock) in bypass_only:
            async with lock:
                yield
            return
        yield

    def _session_write_context_factory(
        self,
        session_key: str,
    ) -> Callable[[], contextlib.AbstractAsyncContextManager[None]]:
        return lambda: self._session_write_context(session_key)

    async def _append_session_message(self, session_key: str, **append_kwargs: Any) -> Any:
        if self._session_manager is None:
            return None
        async with self._session_write_context(session_key):
            return await self._session_manager.append_message(
                session_key,
                **append_kwargs,
            )

    async def run(
        self,
        message: str,
        session_key: str,
        tool_context: ToolContext,
        agent_id: str = "main",
        model: str | None = None,
        attachments: list[dict] | None = None,
        timeout: float | None = None,
        max_iterations: int | None = None,
        iteration_timeout: float | None = None,
        tool_timeout: float | None = None,
        request_timeout: float | None = None,
        max_provider_retries: int | None = None,
        length_capped_continuations: int | None = None,
        input_mode: str = "user",
        persist_input: bool = False,
        input_provenance: dict[str, Any] | str | None = None,
        history_has_persisted_user: bool = True,
        fresh_user_session: bool | None = None,
        session_intent: str | None = None,
        semantic_message: str | None = None,
        run_kind: str = "default",
        heartbeat_ack_max_chars: int = 300,
        bootstrap_context_mode: str | None = None,
        no_memory_capture: bool = False,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        router_control_replay_depth: int = 0,
        *,
        pending_input_provider: PendingInputProvider | None = None,
        bound_user_message_id: str | None = None,
        assistant_message_sink: Callable[[str | None, str], None] | None = None,
        trusted_route_metadata: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent turn with full orchestration.

        Acquires per-session lock, then:
        1. Resolve provider (cloned selector — no shared state mutation)
        2. Build tools + handler from registry (filtered by tool_context)
        3. Assemble identity system prompt
        4. Run pre-turn pipeline (model routing, squilla router, skills, prompt cache)
        5. Load session history
        6. Construct and run Agent
        7. Persist assistant response to transcript
        """
        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        normalized_input_provenance = self._normalize_input_provenance(input_provenance)
        trusted_route_metadata_snapshot = (
            dict(trusted_route_metadata) if isinstance(trusted_route_metadata, Mapping) else None
        )
        lock = self.get_session_lock(session_key)
        effective_tool_context = replace(
            tool_context,
            session_key=session_key,
            tool_run_budget_key=f"{session_key}:{uuid.uuid4().hex}",
            router_control_config=getattr(self._turn_config(), "squilla_router", None),
            router_control_hold_store=self._router_control_hold_store,
            router_control_replay_depth=router_control_replay_depth,
            router_control_turn_hold_applied=False,
        )
        # Re-entry detection: check whether this call chain already serializes
        # the turn lifecycle. On the gateway path TaskRuntime marks ownership
        # while holding its execution lock, so TurnRunner skips the legacy
        # coarse lock. lock.locked() is intentionally NOT used because it cannot
        # distinguish owners under concurrent turns.
        current_task = asyncio.current_task()
        owner_map = _SESSION_LOCK_OWNER.get(None)
        _caller_holds_lock = owner_map is not None and id(lock) in owner_map
        if _caller_holds_lock:
            # Same call chain already serializes this turn.
            try:
                async for event in self._run_turn(
                    message,
                    session_key,
                    agent_id,
                    model,
                    attachments or [],
                    effective_tool_context,
                    timeout=timeout,
                    max_iterations=max_iterations,
                    iteration_timeout=iteration_timeout,
                    tool_timeout=tool_timeout,
                    request_timeout=request_timeout,
                    max_provider_retries=max_provider_retries,
                    length_capped_continuations=length_capped_continuations,
                    input_mode=input_mode,
                    persist_input=persist_input,
                    input_provenance=normalized_input_provenance,
                    history_has_persisted_user=history_has_persisted_user,
                    fresh_user_session=fresh_user_session,
                    session_intent=session_intent,
                    semantic_message=semantic_message,
                    pending_input_provider=pending_input_provider,
                    run_kind=run_kind,
                    heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                    bootstrap_context_mode=bootstrap_context_mode,
                    no_memory_capture=no_memory_capture,
                    ingress_pipeline_steps=ingress_pipeline_steps,
                    router_control_replay_depth=router_control_replay_depth,
                    bound_user_message_id=bound_user_message_id,
                    assistant_message_sink=assistant_message_sink,
                    trusted_route_metadata=trusted_route_metadata_snapshot,
                ):
                    yield event
            finally:
                self.clear_compaction_turn_state(session_key)
        else:
            async with lock:
                # Record this Task as the lock owner in the ContextVar so that
                # any nested call to run() within the same Task can detect re-entry.
                _map: dict[int, asyncio.Task[Any]] = dict(owner_map or {})
                if current_task is not None:
                    _map[id(lock)] = current_task
                _token = _SESSION_LOCK_OWNER.set(_map)
                try:
                    async for event in self._run_turn(
                        message,
                        session_key,
                        agent_id,
                        model,
                        attachments or [],
                        effective_tool_context,
                        timeout=timeout,
                        max_iterations=max_iterations,
                        iteration_timeout=iteration_timeout,
                        tool_timeout=tool_timeout,
                        request_timeout=request_timeout,
                        max_provider_retries=max_provider_retries,
                        length_capped_continuations=length_capped_continuations,
                        input_mode=input_mode,
                        persist_input=persist_input,
                        input_provenance=normalized_input_provenance,
                        history_has_persisted_user=history_has_persisted_user,
                        fresh_user_session=fresh_user_session,
                        session_intent=session_intent,
                        semantic_message=semantic_message,
                        pending_input_provider=pending_input_provider,
                        run_kind=run_kind,
                        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                        bootstrap_context_mode=bootstrap_context_mode,
                        no_memory_capture=no_memory_capture,
                        ingress_pipeline_steps=ingress_pipeline_steps,
                        router_control_replay_depth=router_control_replay_depth,
                        bound_user_message_id=bound_user_message_id,
                        assistant_message_sink=assistant_message_sink,
                        trusted_route_metadata=trusted_route_metadata_snapshot,
                    ):
                        yield event
                finally:
                    self.clear_compaction_turn_state(session_key)
                    _SESSION_LOCK_OWNER.reset(_token)

    async def _run_turn(
        self,
        message: str,
        session_key: str,
        agent_id: str,
        model: str | None,
        attachments: list[dict],
        tool_context: ToolContext | None = None,
        timeout: float | None = None,
        max_iterations: int | None = None,
        iteration_timeout: float | None = None,
        tool_timeout: float | None = None,
        request_timeout: float | None = None,
        max_provider_retries: int | None = None,
        length_capped_continuations: int | None = None,
        input_mode: str = "user",
        persist_input: bool = False,
        input_provenance: dict[str, Any] | None = None,
        history_has_persisted_user: bool = True,
        fresh_user_session: bool | None = None,
        session_intent: str | None = None,
        semantic_message: str | None = None,
        run_kind: str = "default",
        heartbeat_ack_max_chars: int = 300,
        bootstrap_context_mode: str | None = None,
        no_memory_capture: bool = False,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        router_control_replay_depth: int = 0,
        *,
        pending_input_provider: PendingInputProvider | None = None,
        bound_user_message_id: str | None = None,
        assistant_message_sink: Callable[[str | None, str], None] | None = None,
        trusted_route_metadata: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        # Observability: bracket turn setup + stream loop with monotonic clock
        # so latency_ms reflects the full turn.
        turn_started_at = time.monotonic()
        turn_id = uuid.uuid4().hex
        resolved_model = ""
        final_prompt_str = ""
        turn_obj: Any | None = None
        tool_defs_for_log: list[Any] = []
        provider_for_log: Any | None = None
        turn_call_logger: TurnCallLogger | None = None
        trace_context = TraceContext.new(
            session_key=session_key,
            turn_id=turn_id,
            agent_id=agent_id,
        )
        session_id_for_log: str | None = None
        prompt_report_for_log: PromptReport | None = None
        # Declared up-front so the CancelledError handler below can always
        # access them, even if cancellation fires before the stream loop.
        final_text_parts: list[str] = []
        turn_segments: list[dict] = []
        turn_artifacts: list[dict[str, Any]] = []
        artifact_delivery_failures: list[str] = []
        # current_text_parts holds text streamed since the last tool boundary;
        # hoisted here (passed by reference into _StreamState) so the
        # CancelledError handler can flush a trailing text segment the same way
        # the normal-completion path does.
        current_text_parts: list[str] = []
        # Preserve provider usage for cancellation/exception settlement when a
        # later finalizer operation fails after Done was already observed.
        done_event: DoneEvent | None = None
        self._emit_turn_event(
            "turn_start",
            trace_context,
            session_key=session_key,
            agent_id=agent_id,
            turn_id=turn_id,
            run_kind=run_kind,
            input_mode=input_mode,
            seq=1,
            attrs={"input_mode": input_mode, "run_kind": run_kind},
            payload={
                "message_chars": len(message),
                "attachment_count": len(attachments),
            },
        )
        try:
            input_out = await self._input_stage.run(
                InputStageInput(
                    message=message,
                    semantic_message=semantic_message,
                    input_mode=input_mode,
                    persist_input=persist_input,
                    input_provenance=input_provenance,
                    session_key=session_key,
                    tool_context=tool_context,
                    session_append=self._session_manager,
                )
            )
            runtime_message = input_out.runtime_message
            semantic_input = input_out.semantic_input
            extra_prompt_context = input_out.extra_prompt_context
            normalization_metadata = input_out.normalization_metadata

            pt_outcome = await self._provider_and_tools_stage.run(
                ProviderAndToolsStageInput(
                    session_key=session_key,
                    agent_id=agent_id,
                    tool_context=tool_context,
                    run_kind=run_kind,
                    input_mode=input_mode,
                )
            )
            if pt_outcome.terminate:
                # Harness performs the legacy observability + persist +
                # yield sequence in the legacy ORDER (trace-emit, persist,
                # yield, return).
                provider_error_event = cast(ErrorEvent, pt_outcome.require_early_yield())
                log.error("turn_runner.no_provider", session_key=session_key)
                self._emit_turn_event(
                    "turn_error",
                    trace_context,
                    session_key=session_key,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    run_kind=run_kind,
                    input_mode=input_mode,
                    seq=2,
                    payload={
                        "error_type": "ProviderResolutionError",
                        "error_code": provider_error_event.code,
                        "error_chars": len(provider_error_event.message),
                    },
                )
                await self._persist_turn_error(session_key, provider_error_event)
                yield provider_error_event
                return
            pt_out = pt_outcome.require_output()
            provider = pt_out.provider
            cloned_selector = pt_out.cloned_selector
            tool_defs = pt_out.tool_defs
            tool_handler = pt_out.tool_handler
            tool_context = pt_out.effective_tool_context
            tool_metadata = pt_out.tool_metadata
            skill_catalog = pt_out.skill_catalog

            pipeline_usage_context: UsageExecutionContext | None = None
            turn_usage_scope: UsageAccountingScope | None = None
            if self._usage_event_sink is not None:
                pipeline_session_id = await self._resolve_session_id_for_log(session_key)
                pipeline_usage_context = UsageExecutionContext(
                    execution_id=turn_id,
                    agent_run_id=turn_id,
                    turn_id=turn_id,
                    session_id=pipeline_session_id,
                    session_epoch=self._usage_session_epoch_by_key.get(session_key, 0),
                    agent_id=agent_id,
                    run_kind=run_kind or "turn",
                )
                turn_usage_scope = UsageAccountingScope(
                    sink=self._usage_event_sink,
                    context=pipeline_usage_context,
                )

            with bind_usage_accounting_scope(turn_usage_scope):
                turn_absolute_deadline = time.monotonic() + (
                    self._resolve_agent_request_timeout(
                        session_key,
                        request_timeout,
                    )
                )
                pa_outcome = await self._prompt_assembler_stage.run(
                    PromptAssemblerStageInput(
                        runtime_message=runtime_message,
                        semantic_input=semantic_input,
                        extra_prompt_context=extra_prompt_context,
                        provider=provider,
                        cloned_selector=cloned_selector,
                        tool_defs=tool_defs,
                        effective_tool_context=tool_context,
                        tool_metadata=tool_metadata,
                        session_key=session_key,
                        agent_id=agent_id,
                        turn_id=turn_id,
                        attachments=attachments,
                        bootstrap_context_mode=bootstrap_context_mode,
                        model=model,
                        history_has_persisted_user=history_has_persisted_user,
                        persist_input=persist_input,
                        bound_user_message_id=bound_user_message_id,
                        fresh_user_session=(
                            fresh_user_session
                            if fresh_user_session is not None
                            else input_mode == "user"
                            and run_kind == "default"
                            and not history_has_persisted_user
                        ),
                        ingress_pipeline_steps=ingress_pipeline_steps,
                        normalization_metadata=normalization_metadata,
                        input_provenance=input_provenance,
                        skill_catalog=skill_catalog,
                        usage_execution_context=pipeline_usage_context,
                        turn_absolute_deadline=turn_absolute_deadline,
                        trusted_route_metadata=trusted_route_metadata,
                    )
                )
            pa_out = pa_outcome.require_output()
            provider = pa_out.provider
            turn = pa_out.turn
            turn_obj = turn
            tool_defs_for_log = turn.tool_defs
            provider_for_log = provider
            effective_runtime_message = pa_out.effective_runtime_message
            final_prompt = pa_out.final_prompt
            final_prompt_str = final_prompt
            cache_breakpoints = pa_out.cache_breakpoints
            request_context_prompt = pa_out.request_context_prompt
            resolved_model = pa_out.resolved_model
            provider_name = pa_out.provider_name
            session_id_for_log = pa_out.session_id_for_log
            prompt_report_for_log = pa_out.prompt_report
            selector_model = pa_out.selector_model
            cache_reroute_plan = getattr(
                provider,
                "_router_dynamic_cache_reroute_plan",
                None,
            )
            if not isinstance(
                cache_reroute_plan,
                _RouterDynamicCacheReroutePlan,
            ):
                cache_reroute_plan = None
            trace_context = replace(
                trace_context,
                session_id=pa_out.trace_context_session_id,
            )
            if is_turn_call_log_enabled(self._diagnostics_state):
                turn_call_logger = TurnCallLogger(
                    trace_id=trace_context.trace_id,
                    turn_id=turn_id,
                    session_key=session_key,
                    session_id=session_id_for_log,
                    session_intent=session_intent,
                    agent_id=agent_id,
                    provider=provider_name,
                    model=resolved_model,
                    source=self._build_turn_call_source(
                        tool_context,
                        input_provenance,
                        run_kind=run_kind,
                    ),
                )
                if cache_reroute_plan is None:
                    turn_call_logger.write(
                        "prompt_report",
                        asdict(prompt_report_for_log),
                    )
                    turn_call_logger.write(
                        "turn_start",
                        {
                            "input_mode": input_mode,
                            "message": effective_runtime_message,
                            "attachment_count": len(attachments),
                            "tool_names": [getattr(td, "name", "") for td in turn.tool_defs],
                        },
                    )
            log.debug(
                "turn_runner.model_resolved",
                explicit_model=model,
                pipeline_model=turn.model,
                selector_model=selector_model,
                resolved=resolved_model,
                squilla_router_tier=pa_out.squilla_router_tier,
            )
            if tool_context is not None:
                tool_context.router_control_config = getattr(
                    self._turn_config(), "squilla_router", None
                )
                tool_context.router_control_hold_store = self._router_control_hold_store
                tool_context.router_control_replay_depth = router_control_replay_depth
                tool_context.router_control_turn_hold_applied = bool(
                    turn.metadata.get("router_control_hold_applied")
                )
            router_event = build_router_decision_event(turn)
            if (
                router_event is not None
                and turn.metadata.get("_router_single_provider_finalized") is True
            ):
                frozen_event_catalog = turn.metadata.get("_router_single_frozen_catalog")
                if isinstance(frozen_event_catalog, Mapping):
                    frozen_window = frozen_event_catalog.get("context_window")
                    if isinstance(frozen_window, int) and not isinstance(
                        frozen_window,
                        bool,
                    ):
                        router_event = replace(router_event, context_window=frozen_window)
            if router_event is not None and cache_reroute_plan is None:
                yield router_event
            active_provider_id = getattr(cloned_selector, "active_provider_id", "") or provider_name
            runtime_timeout_override = self._web_chat_runtime_timeout_override(
                session_key,
                explicit=timeout,
                tool_context=tool_context,
                input_mode=input_mode,
                turn_metadata=turn.metadata,
            )
            frozen_catalog_token: contextvars.Token[dict[str, Any] | None] | None = None
            if turn.metadata.get("_router_single_provider_finalized") is True:
                frozen_catalog = getattr(
                    provider,
                    "router_single_frozen_catalog",
                    None,
                )
                if not isinstance(frozen_catalog, Mapping):
                    raise RuntimeError("router_single frozen catalog is unavailable")
                frozen_catalog_token = _ROUTER_SINGLE_FROZEN_CATALOG.set(dict(frozen_catalog))
            try:
                ab_outcome = await self._agent_bootstrap_stage.run(
                    AgentBootstrapStageInput(
                        provider=provider,
                        cloned_selector=cloned_selector,
                        turn=turn,
                        final_prompt=final_prompt,
                        cache_breakpoints=cache_breakpoints,
                        request_context_prompt=request_context_prompt,
                        resolved_model=resolved_model,
                        session_id_for_log=session_id_for_log,
                        tool_handler=tool_handler,
                        turn_call_logger=turn_call_logger,
                        tool_context=tool_context,
                        session_key=session_key,
                        agent_id=agent_id,
                        timeout=runtime_timeout_override,
                        max_iterations=max_iterations,
                        iteration_timeout=iteration_timeout,
                        tool_timeout=tool_timeout,
                        request_timeout=request_timeout,
                        max_provider_retries=max_provider_retries,
                        length_capped_continuations=length_capped_continuations,
                        active_provider_id=active_provider_id,
                        turn_id=turn_id,
                        run_kind=run_kind,
                        session_epoch=self._usage_session_epoch_by_key.get(session_key, 0),
                    )
                )
            finally:
                if frozen_catalog_token is not None:
                    _ROUTER_SINGLE_FROZEN_CATALOG.reset(frozen_catalog_token)
            ab_out = ab_outcome.require_output()
            agent = ab_out.agent
            agent_config = ab_out.agent_config
            # These locals are read by the test_agent_bootstrap_stage_snapshot
            # frame-walking probe. Do not remove.
            effective_runtime_timeout = ab_out.effective_runtime_timeout  # noqa: F841
            effective_max_iterations = ab_out.effective_max_iterations  # noqa: F841
            effective_max_iterations_source = ab_out.effective_max_iterations_source  # noqa: F841
            effective_iteration_timeout = ab_out.effective_iteration_timeout  # noqa: F841
            effective_tool_timeout = ab_out.effective_tool_timeout  # noqa: F841
            effective_agent_request_timeout = ab_out.effective_request_timeout  # noqa: F841
            effective_max_provider_retries = ab_out.effective_max_provider_retries  # noqa: F841
            model_caps = ab_out.model_capabilities  # noqa: F841
            private_memory_allowed = ab_out.private_memory_allowed
            sync_manager = ab_out.sync_manager
            if turn_call_logger is not None and cache_reroute_plan is None:
                turn_call_logger.write(
                    "agent_runtime_budget",
                    {
                        "max_iterations": effective_max_iterations,
                        "max_iterations_source": effective_max_iterations_source,
                    },
                )

            post_compaction_agent_resolver: Callable[[], Awaitable[Agent]] | None = None
            if cache_reroute_plan is not None:

                async def _resolve_post_compaction_agent() -> Agent:
                    nonlocal active_provider_id
                    nonlocal agent
                    nonlocal agent_config
                    nonlocal effective_agent_request_timeout
                    nonlocal effective_iteration_timeout
                    nonlocal effective_max_iterations
                    nonlocal effective_max_iterations_source
                    nonlocal effective_max_provider_retries
                    nonlocal effective_runtime_timeout
                    nonlocal effective_tool_timeout
                    nonlocal model_caps
                    nonlocal private_memory_allowed
                    nonlocal prompt_report_for_log
                    nonlocal provider
                    nonlocal provider_for_log
                    nonlocal provider_name
                    nonlocal resolved_model
                    nonlocal sync_manager
                    nonlocal turn_call_logger

                    rerouted = False
                    if (
                        self._router_dynamic_cache_generation(cache_reroute_plan.session_key)
                        != cache_reroute_plan.selection_generation
                    ):
                        reroute_result = cache_reroute_plan.reroute_without_affinity()
                        provider = reroute_result.provider
                        provider_for_log = provider
                        resolved_model = reroute_result.resolved_model
                        provider_name = reroute_result.provider_name
                        active_provider_id = reroute_result.active_provider_id
                        turn.metadata["resolved_model"] = resolved_model
                        turn.metadata["alias_resolution_chain"] = [resolved_model]
                        turn.metadata["provider_after_rewrite"] = provider_name
                        prompt_report_for_log = replace(
                            prompt_report_for_log,
                            resolved_model=resolved_model,
                            alias_resolution_chain=[resolved_model],
                            provider_after_rewrite=provider_name,
                        )
                        rerouted = True

                    if cache_reroute_plan.finalize_observability is not None:
                        cache_reroute_plan.finalize_observability()

                    if is_turn_call_log_enabled(self._diagnostics_state):
                        if rerouted:
                            turn_call_logger = TurnCallLogger(
                                trace_id=trace_context.trace_id,
                                turn_id=turn_id,
                                session_key=session_key,
                                session_id=session_id_for_log,
                                session_intent=session_intent,
                                agent_id=agent_id,
                                provider=provider_name,
                                model=resolved_model,
                                source=self._build_turn_call_source(
                                    tool_context,
                                    input_provenance,
                                    run_kind=run_kind,
                                ),
                            )
                        assert turn_call_logger is not None
                        turn_call_logger.write(
                            "prompt_report",
                            asdict(prompt_report_for_log),
                        )
                        turn_call_logger.write(
                            "turn_start",
                            {
                                "input_mode": input_mode,
                                "message": effective_runtime_message,
                                "attachment_count": len(attachments),
                                "tool_names": [getattr(td, "name", "") for td in turn.tool_defs],
                            },
                        )

                    if not rerouted:
                        if turn_call_logger is not None:
                            turn_call_logger.write(
                                "agent_runtime_budget",
                                {
                                    "max_iterations": effective_max_iterations,
                                    "max_iterations_source": (effective_max_iterations_source),
                                },
                            )
                        return agent

                    final_catalog_token: contextvars.Token[dict[str, Any] | None] | None = None
                    if turn.metadata.get("_router_single_provider_finalized") is True:
                        final_catalog = getattr(
                            provider,
                            "router_single_frozen_catalog",
                            None,
                        )
                        if not isinstance(final_catalog, Mapping):
                            raise RuntimeError("router_single frozen catalog is unavailable")
                        final_catalog_token = _ROUTER_SINGLE_FROZEN_CATALOG.set(dict(final_catalog))
                    try:
                        final_ab_outcome = await self._agent_bootstrap_stage.run(
                            AgentBootstrapStageInput(
                                provider=provider,
                                cloned_selector=cloned_selector,
                                turn=turn,
                                final_prompt=final_prompt,
                                cache_breakpoints=cache_breakpoints,
                                request_context_prompt=request_context_prompt,
                                resolved_model=resolved_model,
                                session_id_for_log=session_id_for_log,
                                tool_handler=tool_handler,
                                turn_call_logger=turn_call_logger,
                                tool_context=tool_context,
                                session_key=session_key,
                                agent_id=agent_id,
                                timeout=runtime_timeout_override,
                                max_iterations=max_iterations,
                                iteration_timeout=iteration_timeout,
                                tool_timeout=tool_timeout,
                                request_timeout=request_timeout,
                                max_provider_retries=max_provider_retries,
                                length_capped_continuations=(length_capped_continuations),
                                active_provider_id=active_provider_id,
                                turn_id=turn_id,
                                run_kind=run_kind,
                                session_epoch=(
                                    self._usage_session_epoch_by_key.get(
                                        session_key,
                                        0,
                                    )
                                ),
                            )
                        )
                    finally:
                        if final_catalog_token is not None:
                            _ROUTER_SINGLE_FROZEN_CATALOG.reset(final_catalog_token)
                    final_ab_out = final_ab_outcome.require_output()
                    agent = final_ab_out.agent
                    agent_config = final_ab_out.agent_config
                    effective_runtime_timeout = final_ab_out.effective_runtime_timeout
                    effective_max_iterations = final_ab_out.effective_max_iterations
                    effective_max_iterations_source = final_ab_out.effective_max_iterations_source
                    effective_iteration_timeout = final_ab_out.effective_iteration_timeout
                    effective_tool_timeout = final_ab_out.effective_tool_timeout
                    effective_agent_request_timeout = final_ab_out.effective_request_timeout
                    effective_max_provider_retries = final_ab_out.effective_max_provider_retries
                    model_caps = final_ab_out.model_capabilities
                    private_memory_allowed = final_ab_out.private_memory_allowed
                    sync_manager = final_ab_out.sync_manager
                    if turn_call_logger is not None:
                        turn_call_logger.write(
                            "agent_runtime_budget",
                            {
                                "max_iterations": effective_max_iterations,
                                "max_iterations_source": (effective_max_iterations_source),
                            },
                        )
                    return agent

                post_compaction_agent_resolver = _resolve_post_compaction_agent

            # 6. Compaction (t3 + preflight) + history load + request-context
            # prepend. CompactionAndHistoryStage owns the four-call sequence
            # (t3_upgrade → preflight → load_history → prepend_request_context_prompt).
            compaction_model = resolved_model
            compaction_context_window_tokens = agent_config.context_window_tokens
            if model:
                compaction_model = model
                if self._model_catalog is not None:
                    # Same precedence as the harness catalog adapter: a
                    # per-model [models.*] override beats the global
                    # llm.context_window_tokens value, which beats the catalog.
                    llm_cfg = getattr(self._config, "llm", None) if self._config else None
                    window, _window_source = resolve_effective_context_window(
                        self._model_catalog,
                        model,
                        provider=active_provider_id,
                        global_override=getattr(llm_cfg, "context_window_tokens", 0) or 0,
                    )
                    compaction_context_window_tokens = window
            with bind_usage_accounting_scope(turn_usage_scope):
                ch_outcome = await self._compaction_and_history_stage.run(
                    CompactionAndHistoryStageInput(
                        agent=agent,
                        context_window_tokens=agent_config.context_window_tokens,
                        provider=provider,
                        resolved_model=resolved_model,
                        compaction_context_window_tokens=compaction_context_window_tokens,
                        compaction_provider=provider,
                        compaction_model=compaction_model,
                        turn=turn,
                        session_key=session_key,
                        agent_id=agent_id,
                        history_has_persisted_user=history_has_persisted_user,
                        bound_user_message_id=bound_user_message_id,
                        post_compaction_agent_resolver=(post_compaction_agent_resolver),
                    )
                )
            ch_out = ch_outcome.require_output()
            agent.config.request_context_prompt = ch_out.final_request_context_prompt

            # 8. Build extra messages for attachments + turn_input rebind.
            # AttachmentStage owns the slice.
            attachment_materialization_session_id = None
            if attachments:
                attachment_materialization_session_id = await self._resolve_session_id_for_log(
                    session_key
                )
                if attachment_materialization_session_id is None:
                    attachment_materialization_session_id = session_key
            att_outcome = await self._attachment_stage.run(
                AttachmentStageInput(
                    effective_runtime_message=effective_runtime_message,
                    attachments=attachments,
                    workspace_dir=agent_config.workspace_dir,
                    session_id=attachment_materialization_session_id,
                )
            )
            att_out = att_outcome.require_output()
            extra_msgs = att_out.extra_messages

            # 9. Stream events (final_text_parts/turn_segments are declared
            # up-front above so the CancelledError handler can read them).
            # StreamConsumerStage owns the slice. The four pre-stream
            # accumulators (final_text_parts, turn_segments, turn_artifacts,
            # artifact_delivery_failures) stay declared in this scope and
            # are PASSED BY REFERENCE into _StreamState so the
            # CancelledError handler below still sees them.
            error_message: str | None = None
            pending_error_event: ErrorEvent | None = None
            turn_input = att_out.turn_input

            stream_state = _StreamState(
                current_text_parts=current_text_parts,
                final_text_parts=final_text_parts,
                turn_segments=turn_segments,
                turn_artifacts=turn_artifacts,
                artifact_delivery_failures=artifact_delivery_failures,
            )
            stream_inp = StreamConsumerStageInput(
                agent=agent,
                agent_id=agent_id,
                sync_manager=sync_manager,
                private_memory_allowed=private_memory_allowed,
                turn=turn,
                tool_defs=tool_defs,
                turn_input=turn_input,
                extra_messages=extra_msgs,
                semantic_input=semantic_input,
                effective_runtime_message=effective_runtime_message,
                input_provenance=input_provenance,
                session_key=session_key,
                run_kind=run_kind,
                heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                bootstrap_context_mode=bootstrap_context_mode,
                router_cfg=getattr(self._turn_config(), "squilla_router", None),
                session_manager_present=self._session_manager is not None,
                state=stream_state,
                tool_context=tool_context,
                pending_input_provider=pending_input_provider,
            )
            router_control_replay_event: RouterControlReplayEvent | None = None
            deferred_router_event_pending = cache_reroute_plan is not None
            with bind_usage_accounting_scope(turn_usage_scope):
                async for event in self._stream_consumer_stage.run(stream_inp):
                    if isinstance(event, RouterControlReplayEvent):
                        router_control_replay_event = event
                        yield event
                        break
                    if deferred_router_event_pending and getattr(event, "kind", "") not in {
                        "heartbeat",
                        "ensemble_progress",
                    }:
                        deferred_router_event_pending = False
                        generation_guard_failed = (
                            isinstance(event, ErrorEvent)
                            and event.code == "router_dynamic_cache_generation_changed"
                        )
                        if not generation_guard_failed:
                            router_event = build_router_decision_event(turn)
                            if (
                                router_event is not None
                                and turn.metadata.get("_router_single_provider_finalized") is True
                            ):
                                frozen_event_catalog = turn.metadata.get(
                                    "_router_single_frozen_catalog"
                                )
                                if isinstance(
                                    frozen_event_catalog,
                                    Mapping,
                                ):
                                    frozen_window = frozen_event_catalog.get("context_window")
                                    if isinstance(
                                        frozen_window,
                                        int,
                                    ) and not isinstance(
                                        frozen_window,
                                        bool,
                                    ):
                                        router_event = replace(
                                            router_event,
                                            context_window=frozen_window,
                                        )
                            if router_event is not None:
                                yield router_event
                    yield event
            if router_control_replay_event is not None:
                async for replayed_event in self._run_turn(
                    message,
                    session_key,
                    agent_id,
                    model,
                    attachments,
                    tool_context,
                    timeout=timeout,
                    max_iterations=max_iterations,
                    iteration_timeout=iteration_timeout,
                    tool_timeout=tool_timeout,
                    request_timeout=request_timeout,
                    max_provider_retries=max_provider_retries,
                    length_capped_continuations=length_capped_continuations,
                    input_mode=input_mode,
                    persist_input=False,
                    input_provenance=input_provenance,
                    history_has_persisted_user=True,
                    fresh_user_session=False,
                    session_intent=session_intent,
                    semantic_message=semantic_message,
                    pending_input_provider=pending_input_provider,
                    bound_user_message_id=bound_user_message_id,
                    run_kind=run_kind,
                    heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                    bootstrap_context_mode=bootstrap_context_mode,
                    no_memory_capture=no_memory_capture,
                    ingress_pipeline_steps=ingress_pipeline_steps,
                    router_control_replay_depth=router_control_replay_depth + 1,
                    assistant_message_sink=assistant_message_sink,
                ):
                    yield replayed_event
                return
            # Read terminal state off the shared _StreamState. The
            # four pass-by-reference lists were mutated in place, so
            # this preserves the harness's read-after-stream
            # contract; only the four owned fields need explicit
            # writeback.
            current_text_parts = stream_state.current_text_parts
            error_message = stream_state.error_message
            pending_error_event = stream_state.pending_error_event
            done_event = stream_state.done_event
            # Post-stage edge owned by the harness: flush remaining
            # text segment. The stage's post-stream notify already
            # fired (it is the last action of the stage body).
            if current_text_parts:
                turn_segments.append({"type": "text", "text": "".join(current_text_parts)})
                current_text_parts.clear()

            terminal_error_code = str(getattr(pending_error_event, "code", "") or "") or None
            if terminal_error_code is None and error_message:
                terminal_error_code = "stream_failed"
            terminal_execution_status = (
                "failed" if pending_error_event is not None or bool(error_message) else "succeeded"
            )
            assistant_turn_context_patch = self._fixed_four_tier_v2_response_binding_context(
                turn,
                execution_status=terminal_execution_status,
                error_code=terminal_error_code,
            )

            # 10. Persist assistant response (filter sentinel tokens).
            # TurnFinalizerStage owns the slice. The four side effects
            # fire in legacy order: heartbeat normalize -> transcript
            # append -> memory capture (try/except) -> error persist ->
            # session totals rollup (try/except).
            fin_outcome = await self._turn_finalizer_stage.run(
                TurnFinalizerStageInput(
                    final_text_parts=final_text_parts,
                    turn_segments=turn_segments,
                    turn_artifacts=turn_artifacts,
                    error_message=error_message,
                    pending_error_event=pending_error_event,
                    done_event=done_event,
                    runtime_message=runtime_message,
                    input_mode=input_mode,
                    input_provenance=input_provenance,
                    resolved_model=resolved_model,
                    agent_id=agent_id,
                    session_key=session_key,
                    tool_context=tool_context,
                    run_kind=run_kind,
                    heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                    no_memory_capture=no_memory_capture,
                    assistant_turn_context_patch=assistant_turn_context_patch,
                )
            )
            fin_out = fin_outcome.require_output()
            final_text = fin_out.final_text
            turn_segments = fin_out.turn_segments
            await self._settle_fixed_four_tier_v2_route(
                turn,
                execution_status=terminal_execution_status,
                response_id=fin_out.assistant_message_id,
                error_code=terminal_error_code,
                done_event=done_event,
                required=True,
            )
            if (
                fin_out.transcript_appended
                and fin_out.assistant_message_content is not None
                and assistant_message_sink is not None
            ):
                try:
                    assistant_message_sink(
                        fin_out.assistant_message_id,
                        fin_out.assistant_message_content,
                    )
                except Exception:  # noqa: BLE001 - observer must not fail the turn
                    log.warning(
                        "turn_runner.assistant_message_sink_failed",
                        session_key=session_key,
                        exc_info=True,
                    )

            if turn_call_logger is not None:
                turn_call_logger.write(
                    "turn_end",
                    {
                        "final_text": final_text,
                        "segments": turn_segments,
                        "error": error_message,
                    },
                )
            if trace_context is not None:
                self._emit_turn_event(
                    "turn_end",
                    trace_context,
                    session_key=session_key,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    run_kind=run_kind,
                    input_mode=input_mode,
                    seq=2,
                    attrs={"provider": provider_name, "model": resolved_model},
                    payload={
                        "final_text_chars": len(final_text),
                        "segment_count": len(turn_segments),
                        "artifact_count": len(turn_artifacts),
                        "error": bool(error_message),
                        "tool_projection_applied": bool(
                            turn.metadata.get("tool_projection_applied", False)
                        ),
                        "tool_projection_calls": int(
                            turn.metadata.get("tool_projection_calls", 0) or 0
                        ),
                        "tool_projection_tokens_saved": int(
                            turn.metadata.get("tool_projection_tokens_saved", 0) or 0
                        ),
                        "tool_result_store_writes": int(
                            turn.metadata.get("tool_result_store_writes", 0) or 0
                        ),
                        "tool_result_store_skips": int(
                            turn.metadata.get("tool_result_store_skips", 0) or 0
                        ),
                    },
                )

            # 11. Observability: best-effort DecisionEntry for this turn.
            #     Must never break turn execution — wrap in try/except.
            turn.metadata.update(
                self._collect_session_flush_metadata(agent_id, session_key=session_key)
            )
            prompt_report_for_decision = build_prompt_report(
                turn_id=turn_id,
                session_key=session_key,
                session_id=session_id_for_log,
                agent_id=agent_id,
                system_prompt=final_prompt_str,
                tool_defs=turn.tool_defs,
                metadata=turn.metadata,
                tool_profile=turn.metadata.get("tool_profile"),
            )
            self._emit_decision_entry(
                turn_id=turn_id,
                session_key=session_key,
                session_id=session_id_for_log,
                message=message,
                final_prompt=final_prompt_str,
                tool_defs=tool_defs_for_log,
                turn_obj=turn_obj,
                provider=provider_for_log,
                resolved_model=resolved_model,
                turn_started_at=turn_started_at,
                prompt_report=prompt_report_for_decision,
                session_intent=session_intent,
                done_event=done_event,
                trace_id=trace_context.trace_id if trace_context is not None else None,
                skills_invoked=collect_invoked_skills(turn_segments),
            )
            self._emit_router_train_sample(
                agent_id=agent_id,
                session_key=session_key,
                turn_obj=turn_obj,
                message=message,
            )
            if pending_error_event is None:
                self._commit_pending_router_dynamic_cache_affinity(turn, done_event)
                self._commit_pending_router_dynamic_route(turn, done_event)
            else:
                self._discard_pending_router_dynamic_cache_affinity(
                    turn,
                    session_key=session_key,
                )
                turn.metadata.pop("router_dynamic_pending_route_plan", None)
            if pending_error_event is not None:
                yield pending_error_event

        except asyncio.CancelledError:
            self._discard_pending_router_dynamic_cache_affinity(
                turn_obj,
                session_key=session_key,
            )
            # Bug 2 partial-persistence: preserve whatever assistant text has
            # already streamed back so a cancelled turn does not leave the
            # transcript with an orphan user message. Marker `[interrupted]`
            # lets future turns (and users reading history) recognise the
            # response is incomplete.
            # Flush trailing text streamed since the last tool boundary into
            # turn_segments, mirroring the normal-completion path — otherwise a
            # tool-using turn cancelled mid-answer persists segments with no
            # text and the UI (which renders reloaded turns from the segment
            # timeline) drops the visible partial answer.
            trailing = "".join(current_text_parts)
            if trailing:
                turn_segments.append({"type": "text", "text": trailing})
                current_text_parts.clear()
            partial_text = "".join(final_text_parts).rstrip()
            cancelled_response_id: str | None = None
            cancelled_binding = self._fixed_four_tier_v2_response_binding_context(
                turn_obj,
                execution_status="cancelled",
                error_code="cancelled",
            )
            if (
                partial_text
                or turn_segments
                or turn_artifacts
                # Fixed-v2 commits its task/input anchor before dispatch. Even a
                # zero-output cancellation therefore needs a terminal assistant
                # row; deleting the user input would strand durable task state on
                # an anchor that no longer exists.
                or cancelled_binding is not None
            ) and self._session_manager is not None:
                try:
                    body = _cancelled_partial_response_text(partial_text, turn_artifacts)
                    if turn_artifacts:
                        body = json.dumps(
                            {"text": body, "artifacts": turn_artifacts},
                            ensure_ascii=False,
                        )
                    if cancelled_binding is None:
                        cancelled_append = await _finish_required_cancel_cleanup(
                            self._append_session_message(
                                session_key,
                                role="assistant",
                                content=body,
                                tool_calls=turn_segments if turn_segments else None,
                            )
                        )
                    else:
                        from opensquilla.session.turn_context import (
                            current_turn_context,
                            turn_context_scope,
                        )

                        cancelled_context = {
                            **(current_turn_context() or {}),
                            **cancelled_binding,
                        }
                        with turn_context_scope(cancelled_context):
                            cancelled_append = await _finish_required_cancel_cleanup(
                                self._append_session_message(
                                    session_key,
                                    role="assistant",
                                    content=body,
                                    tool_calls=(turn_segments if turn_segments else None),
                                )
                            )
                    cancelled_response_id = (
                        str(getattr(cancelled_append, "message_id", "") or "") or None
                    )
                    log.info(
                        "turn_runner.cancelled_partial_persisted",
                        session_key=session_key,
                        text_chars=len(partial_text),
                        segment_count=len(turn_segments),
                    )
                except Exception:  # pragma: no cover — defensive: don't swallow the cancel
                    log.warning(
                        "turn_runner.cancelled_persist_failed",
                        session_key=session_key,
                        exc_info=True,
                    )
            elif bound_user_message_id and self._session_manager is not None:
                # Legacy zero-output cancel: no assistant text/segments/artifacts
                # ever streamed, so the only trace is the ingress-persisted user
                # prompt. Fixed-v2 is excluded above because its committed state
                # owns that input as a durable task boundary.
                await _finish_required_cancel_cleanup(
                    self._rollback_cancelled_prompt(session_key, bound_user_message_id)
                )
            await _finish_required_cancel_cleanup(
                self._settle_fixed_four_tier_v2_route(
                    turn_obj,
                    execution_status="cancelled",
                    response_id=cancelled_response_id,
                    error_code="cancelled",
                    done_event=done_event,
                )
            )
            if turn_call_logger is not None:
                try:
                    turn_call_logger.write(
                        "turn_cancelled",
                        {"partial_text_chars": len(partial_text)},
                    )
                except Exception:
                    pass
            if trace_context is not None:
                self._emit_turn_event(
                    "turn_cancelled",
                    trace_context,
                    session_key=session_key,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    run_kind=run_kind,
                    input_mode=input_mode,
                    seq=2,
                    payload={"partial_text_chars": len(partial_text)},
                )
            raise

        except Exception as exc:
            self._discard_pending_router_dynamic_cache_affinity(
                turn_obj,
                session_key=session_key,
            )
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
            if isinstance(exc, UsageAccountingUnavailableError):
                event_code = UsageAccountingUnavailableError.code
                error_code = event_code
                error_message = str(exc) or (
                    "Usage accounting is temporarily unavailable; retry the turn."
                )
            else:
                event_code = (
                    error_code
                    if error_code in {"provider_request_too_large", "provider_output_truncated"}
                    else "agent_error"
                )
            log.error(
                "turn_runner.failed",
                session_key=session_key,
                error=str(exc),
                exc_info=True,
            )
            fallback_hops = 0
            if turn_obj is not None:
                try:
                    fallback_hops = int(
                        (getattr(turn_obj, "metadata", None) or {}).get("router_fallback_hops", 0)
                    )
                except (TypeError, ValueError):
                    fallback_hops = 0
            error_id = await self._record_turn_error(
                session_key=session_key,
                turn_id=turn_id,
                session_id=session_id_for_log,
                surface=input_mode or "unknown",
                error_class=error_code or type(exc).__name__,
                message=error_message,
                exc=exc,
                provider=(
                    type(provider_for_log).__name__ if provider_for_log is not None else None
                ),
                model=resolved_model or None,
                fallback_hops=fallback_hops,
            )
            if self._session_manager is not None:
                if event_code == "provider_output_truncated":
                    transcript_message = append_error_ref(
                        build_terminal_reply(
                            {
                                "status": "failed",
                                "terminal_reason": "output_truncated",
                                "error_class": event_code,
                                "error_message": error_message,
                            }
                        ),
                        error_id,
                    )
                else:
                    transcript_message = f"Error: {append_error_ref(error_message, error_id)}"
                await self._append_session_message(
                    session_key, role="system", content=transcript_message
                )
            await self._settle_fixed_four_tier_v2_route(
                turn_obj,
                execution_status="failed",
                error_code=error_code or type(exc).__name__,
                done_event=done_event,
            )
            if turn_call_logger is not None:
                turn_call_logger.write(
                    "turn_error",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            if trace_context is not None:
                self._emit_turn_event(
                    "turn_error",
                    trace_context,
                    session_key=session_key,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    run_kind=run_kind,
                    input_mode=input_mode,
                    seq=2,
                    payload={
                        "error_type": type(exc).__name__,
                        "error_chars": len(str(exc)),
                    },
                )
            yield ErrorEvent(message=error_message, code=event_code, error_id=error_id or "")

    @staticmethod
    def _write_trace_event(
        kind: str,
        context: TraceContext,
        *,
        seq: int | None = None,
        attrs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            write_trace_event(
                TraceEvent(
                    kind=kind,
                    context=context,
                    privacy="operational",
                    seq=seq,
                    attrs=attrs or {},
                    payload=payload or {},
                )
            )
        except Exception as exc:  # pragma: no cover - observability must not break turns
            log.debug("trace_event.write_failed", kind=kind, error=str(exc))

    def _emit_turn_event(
        self,
        kind: str,
        context: TraceContext | None,
        *,
        session_key: str,
        agent_id: str,
        turn_id: str | None = None,
        run_kind: str | None = None,
        input_mode: str | None = None,
        seq: int | None = None,
        attrs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fan a turn event out through the registered ``TurnHook`` chain.

        ``OPENSQUILLA_HOOKS=legacy`` is honored as an escape hatch and routes
        through the static :meth:`_write_trace_event` directly so any
        unforeseen production drift can be confined to the hook fan-out
        without rolling back the call sites.
        """

        if context is None:
            return
        if _hooks_mode_from_env() == "legacy":
            self._write_trace_event(
                kind,
                context,
                seq=seq,
                attrs=attrs,
                payload=payload,
            )
            return
        hook_ctx = TurnHookContext(
            session_key=session_key,
            agent_id=agent_id,
            turn_id=turn_id,
            run_kind=run_kind,
            input_mode=input_mode,
            trace_context=context,
        )
        event = TurnEvent(
            kind=kind,
            seq=seq,
            attrs=dict(attrs or {}),
            payload=dict(payload or {}),
        )
        for hook in self._turn_hooks:
            try:
                hook.on_event(hook_ctx, event)
            except Exception as exc:  # noqa: BLE001 - hooks must not break turns
                log.warning(
                    "turn_hook.on_event_failed",
                    hook=getattr(hook, "name", type(hook).__name__),
                    kind=kind,
                    error=str(exc),
                )

    @staticmethod
    def _build_turn_call_source(
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        *,
        run_kind: str | None = None,
    ) -> dict[str, Any]:
        """Build stable source metadata for raw call-log filtering."""

        source: dict[str, Any] = {}
        if tool_context is not None:
            source.update(
                {
                    "caller_kind": str(tool_context.caller_kind),
                    "channel_kind": tool_context.channel_kind,
                    "channel_id": tool_context.channel_id,
                    "sender_id": tool_context.sender_id,
                    "source_kind": tool_context.source_kind,
                    "source_name": tool_context.source_name,
                }
            )
        if run_kind:
            source["run_kind"] = run_kind
        if input_provenance:
            source["input_provenance"] = input_provenance
            provenance_kind = TurnRunner._input_provenance_kind(input_provenance)
            if provenance_kind:
                source["input_provenance_kind"] = provenance_kind
        return source

    async def _resolve_session_id_for_log(self, session_key: str) -> str | None:
        """Best-effort lookup of the transcript identity for observability."""

        if self._session_manager is None:
            return None
        try:
            if hasattr(self._session_manager, "get_session"):
                node = await self._session_manager.get_session(session_key)
            else:
                from opensquilla.gateway.session_services import get_session_storage

                storage = get_session_storage(self._session_manager)
                node = await storage.get_session(session_key) if storage is not None else None
        except Exception:
            return None
        session_id = getattr(node, "session_id", None)
        if isinstance(session_id, str) and session_id:
            try:
                session_epoch = max(0, int(getattr(node, "epoch", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                session_epoch = 0
            self._usage_session_epoch_by_key[session_key] = session_epoch
        return session_id if isinstance(session_id, str) and session_id else None

    def _resolve_provider(self) -> tuple[Any | None, Any | None]:
        """Clone the selector and resolve provider (no shared state mutation)."""
        if self._provider_selector is None:
            return None, None
        # A gateway can boot with a selector that has no usable primary yet
        # (no API key configured); treat it like "no provider" so the turn
        # fails with the same clean no_provider error instead of raising.
        # getattr default True keeps duck-typed test selectors working.
        if not getattr(self._provider_selector, "is_configured", True):
            return None, None
        cloned = self._provider_selector.clone()
        return cloned.resolve(), cloned

    def _handle_runtime_warning(self, event: WarningEvent) -> WarningEvent:
        return event

    async def _record_turn_error(
        self,
        *,
        session_key: str,
        turn_id: str | None,
        session_id: str | None,
        surface: str,
        error_class: str | None,
        message: str,
        exc: BaseException | None,
        provider: str | None,
        model: str | None,
        fallback_hops: int,
    ) -> str | None:
        """Best-effort durable error record; returns the error_id or None.

        Never raises: a persistence failure must not mask the turn error
        being recorded.
        """
        if self._turn_error_writer is None:
            return None
        try:
            from opensquilla.persistence.turn_error_writer import new_error_id

            error_id = new_error_id()
            traceback_text = None
            if exc is not None:
                import traceback as _traceback

                traceback_text = "".join(
                    _traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
            record = {
                "error_id": error_id,
                "turn_id": turn_id,
                "session_key": session_key,
                "session_id": session_id,
                "surface": surface,
                "error_class": error_class,
                "message": message,
                "traceback": traceback_text,
                "provider": provider,
                "model": model,
                "fallback_hops": fallback_hops,
            }
            # TurnErrorWriter is deliberately synchronous and may wait for its
            # SQLite busy timeout. Keep that wait off the shared turn loop while
            # preserving its existing best-effort return contract.
            recorded = await asyncio.to_thread(
                self._turn_error_writer.record_error,
                record,
            )
            return error_id if recorded else None
        except Exception as record_exc:  # noqa: BLE001 - must not mask the turn error
            log.warning(
                "turn_runner.error_record_failed",
                session_key=session_key,
                error=str(record_exc),
            )
            return None

    async def _persist_turn_error(
        self,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None:
        """Best-effort durable transcript record for terminal turn errors."""
        if self._session_manager is None or event is None:
            return
        error_code, message = sanitize_agent_error(
            {
                "status": "failed",
                "terminal_reason": event.code,
                "error_class": event.code,
                "error_message": event.message,
            },
            fallback_error_class=event.code,
            fallback_error_message=event.message or "Unknown error",
        )
        event_code = (
            error_code
            if error_code in {"provider_request_too_large", "provider_output_truncated"}
            else event.code
        )
        # When the event already carries an error_id from the catch-all, no
        # second turn_errors row is written — getattr short-circuits.
        error_id = getattr(event, "error_id", "")
        if not error_id:
            error_id = await self._record_turn_error(
                session_key=session_key,
                turn_id=None,
                session_id=None,
                surface="unknown",
                error_class=event_code,
                message=message,
                exc=None,
                provider=None,
                model=None,
                fallback_hops=0,
            )
        outcome_details = turn_outcome_details(
            outcome_from_error(
                code=event_code,
                message=message,
                error_class=event_code,
            )
        )
        if event_code == "provider_output_truncated":
            transcript_message = append_error_ref(
                build_terminal_reply(
                    {
                        "status": "failed",
                        "terminal_reason": "output_truncated",
                        "error_class": event_code,
                        "error_message": message,
                    }
                ),
                error_id,
            )
        else:
            transcript_message = f"Error: {append_error_ref(message, error_id)}"
        try:
            if event_code == "current_turn_context_exhausted":
                compact = getattr(self._session_manager, "compact", None)
                if callable(compact):
                    budget = int(
                        getattr(self._config, "context_budget_tokens", None)
                        or getattr(self._config, "context_window_tokens", None)
                        or 100_000
                    )
                    try:
                        maybe_summary = compact(session_key, budget)
                        if inspect.isawaitable(maybe_summary):
                            await maybe_summary
                    except Exception as exc:  # noqa: BLE001 - error append must still run
                        log.warning(
                            "turn_runner.error_compaction_failed",
                            session_key=session_key,
                            code=event_code,
                            error=str(exc),
                        )
            await self._append_session_message(
                session_key,
                role="system",
                content=transcript_message,
            )
            log.info(
                "turn_runner.error_persisted",
                session_key=session_key,
                code=event_code,
                **outcome_details,
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not mask the original error
            log.warning(
                "turn_runner.error_persist_failed",
                session_key=session_key,
                code=event_code,
                **outcome_details,
                error=str(exc),
            )

    @staticmethod
    def _non_bool_number(value: Any) -> TypeGuard[int | float]:
        return not isinstance(value, bool) and isinstance(value, int | float)

    @staticmethod
    def _non_bool_int(value: Any) -> TypeGuard[int]:
        return not isinstance(value, bool) and isinstance(value, int)

    def _resolve_agent_runtime_timeout(self, session_key: str) -> float:
        """Resolve whole-turn runtime timeout.

        ``0`` is intentional and disables the runtime budget. The old
        ``llm_timeout_seconds`` setting remains a legacy runtime alias.
        """

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    for attr in ("agent_runtime_timeout_seconds", "llm_timeout_seconds"):
                        value = getattr(session_cfg, attr, None)
                        if self._non_bool_number(value) and value >= 0:
                            return float(value)
            except Exception:  # noqa: BLE001
                pass

        env_timeout = os.environ.get("OPENSQUILLA_TURN_TIMEOUT")
        if env_timeout is not None and env_timeout.strip():
            raw = env_timeout.strip()
            try:
                value = float(raw)
            except ValueError:
                log.warning("turn_runner.invalid_runtime_timeout", raw=raw)
            else:
                if value >= 0:
                    return value
                log.warning("turn_runner.negative_runtime_timeout", value=value)

        for attr in ("agent_runtime_timeout_seconds", "llm_timeout_seconds"):
            value = getattr(self._config, attr, None)
            if self._non_bool_number(value) and value >= 0:
                return float(value)

        return _DEFAULT_AGENT_RUNTIME_TIMEOUT_SECONDS

    def _web_chat_runtime_timeout_override(
        self,
        session_key: str,
        *,
        explicit: float | None,
        tool_context: ToolContext | None,
        input_mode: str,
        turn_metadata: Mapping[str, Any] | None,
    ) -> float | None:
        """Cap ordinary interactive Web turns without constraining long jobs."""

        if explicit is not None:
            return float(explicit)
        cap = getattr(self._config, "web_chat_runtime_timeout_seconds", 0.0)
        if not self._non_bool_number(cap) or cap <= 0:
            return None
        if tool_context is None or tool_context.caller_kind is not CallerKind.WEB:
            return None
        if tool_context.interaction_mode is not InteractionMode.INTERACTIVE:
            return None
        if input_mode != "user":
            return None

        metadata = turn_metadata or {}
        if tool_context.coding_mode or bool(metadata.get("coding_mode")):
            return None
        if any(metadata.get(key) is not None for key in _WEB_CHAT_META_EXEMPT_KEYS):
            return None

        base_timeout = self._resolve_agent_runtime_timeout(session_key)
        if base_timeout == 0:
            return 0.0
        effective_timeout = min(base_timeout, float(cap))
        log.debug(
            "turn_runner.web_chat_runtime_timeout",
            session_key=session_key,
            base_timeout_seconds=base_timeout,
            cap_seconds=float(cap),
            effective_timeout_seconds=effective_timeout,
        )
        return effective_timeout

    def _resolve_agent_max_iterations(
        self,
        session_key: str,
        explicit: int | None = None,
    ) -> int:
        """Resolve model/tool loop budget for this turn."""

        if explicit is not None:
            if self._non_bool_int(explicit) and explicit >= 0:
                self._last_agent_max_iterations_source = "explicit argument"
                return int(explicit)
            raise ValueError("max_iterations must be an integer >= 0")

        sm = self._session_manager
        session_value = None
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    session_value = getattr(session_cfg, "agent_max_iterations", None)
                    if session_value is not None and not (
                        self._non_bool_int(session_value) and session_value >= 0
                    ):
                        log.warning(
                            "turn_runner.invalid_agent_max_iterations",
                            source="session",
                            value=session_value,
                        )
            except Exception:  # noqa: BLE001
                pass

        env_value = os.environ.get("OPENSQUILLA_AGENT_MAX_ITERATIONS")
        if env_value is not None and env_value.strip():
            raw = env_value.strip()
            try:
                parsed_env = int(raw)
            except ValueError:
                log.warning("turn_runner.invalid_agent_max_iterations", source="env", raw=raw)
            else:
                if parsed_env < 0:
                    log.warning(
                        "turn_runner.invalid_agent_max_iterations",
                        source="env",
                        value=parsed_env,
                    )

        config_value = getattr(self._config, "agent_max_iterations", None)
        if config_value is not None and not (
            self._non_bool_int(config_value) and config_value >= 0
        ):
            log.warning(
                "turn_runner.invalid_agent_max_iterations",
                source="config",
                value=config_value,
            )

        policy = resolve_turn_policy(
            session_key=session_key,
            explicit_max_iterations=explicit,
            session_manager=self._session_manager,
            gateway_config=self._config,
            env=os.environ,
        )
        self._last_agent_max_iterations_source = policy.max_iterations_source
        return policy.max_iterations

    def _resolve_agent_iteration_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Per-iteration timeout, with a coding-mode floor.

        A coding-mode turn delegates to code-task and then blocks in a single
        long ``process(action="wait")`` (code-task can run ~90 min). The
        per-iteration watchdog must not clamp that wait, so floor the timeout
        at 5400s while coding mode is on.
        """
        value = self._resolve_agent_iteration_timeout_base(session_key, explicit)
        skills_cfg = getattr(self._config, "skills", None)
        if bool(getattr(skills_cfg, "coding_mode", False)) and value < 5400.0:
            return 5400.0
        return value

    def _resolve_agent_iteration_timeout_base(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Resolve per-iteration timeout for this turn.

        Precedence: explicit arg > session config > env > gateway config > default.
        """

        if explicit is not None:
            if self._non_bool_number(explicit) and explicit >= 0:
                return float(explicit)
            raise ValueError("iteration_timeout must be a non-negative number")

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    value = getattr(session_cfg, "agent_iteration_timeout_seconds", None)
                    if self._non_bool_number(value) and value >= 0:
                        return float(value)
                    if value is not None:
                        log.warning(
                            "turn_runner.invalid_agent_iteration_timeout",
                            source="session",
                            value=value,
                        )
            except Exception:  # noqa: BLE001
                pass

        env_value = os.environ.get("OPENSQUILLA_AGENT_ITERATION_TIMEOUT")
        if env_value is not None and env_value.strip():
            raw = env_value.strip()
            try:
                value = float(raw)
            except ValueError:
                log.warning("turn_runner.invalid_agent_iteration_timeout", source="env", raw=raw)
            else:
                if value >= 0:
                    return value
                log.warning(
                    "turn_runner.invalid_agent_iteration_timeout", source="env", value=value
                )

        value = getattr(self._config, "agent_iteration_timeout_seconds", None)
        if self._non_bool_number(value) and value >= 0:
            return float(value)
        if value is not None:
            log.warning(
                "turn_runner.invalid_agent_iteration_timeout",
                source="config",
                value=value,
            )

        return AgentConfig().iteration_timeout

    def _resolve_agent_tool_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Resolve per-tool execution timeout for this turn."""

        if explicit is not None:
            if self._non_bool_number(explicit) and explicit >= 0:
                return float(explicit)
            raise ValueError("tool_timeout must be a non-negative number")

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    value = getattr(session_cfg, "agent_tool_timeout_seconds", None)
                    if self._non_bool_number(value) and value >= 0:
                        return float(value)
                    if value is not None:
                        log.warning(
                            "turn_runner.invalid_agent_tool_timeout",
                            source="session",
                            value=value,
                        )
            except Exception:  # noqa: BLE001
                pass

        env_value = os.environ.get("OPENSQUILLA_AGENT_TOOL_TIMEOUT")
        if env_value is not None and env_value.strip():
            raw = env_value.strip()
            try:
                value = float(raw)
            except ValueError:
                log.warning("turn_runner.invalid_agent_tool_timeout", source="env", raw=raw)
            else:
                if value >= 0:
                    return value
                log.warning("turn_runner.invalid_agent_tool_timeout", source="env", value=value)

        value = getattr(self._config, "agent_tool_timeout_seconds", None)
        if self._non_bool_number(value) and value >= 0:
            return float(value)
        if value is not None:
            log.warning(
                "turn_runner.invalid_agent_tool_timeout",
                source="config",
                value=value,
            )

        return AgentConfig().tool_timeout

    def _resolve_agent_request_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Resolve single LLM request timeout for this turn (agent-runtime path)."""

        if explicit is not None:
            if self._non_bool_number(explicit) and explicit > 0:
                return float(explicit)
            raise ValueError("request_timeout must be a positive number")

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    value = getattr(session_cfg, "agent_request_timeout_seconds", None)
                    if self._non_bool_number(value) and value > 0:
                        return float(value)
                    if value is not None:
                        log.warning(
                            "turn_runner.invalid_agent_request_timeout",
                            source="session",
                            value=value,
                        )
            except Exception:  # noqa: BLE001
                pass

        env_value = os.environ.get("OPENSQUILLA_AGENT_REQUEST_TIMEOUT")
        if env_value is not None and env_value.strip():
            raw = env_value.strip()
            try:
                value = float(raw)
            except ValueError:
                log.warning("turn_runner.invalid_agent_request_timeout", source="env", raw=raw)
            else:
                if value > 0:
                    return value
                log.warning("turn_runner.invalid_agent_request_timeout", source="env", value=value)

        value = getattr(self._config, "agent_request_timeout_seconds", None)
        if self._non_bool_number(value) and value > 0:
            return float(value)
        if value is not None:
            log.warning(
                "turn_runner.invalid_agent_request_timeout",
                source="config",
                value=value,
            )

        return self._resolve_llm_timeout(session_key)

    def _resolve_agent_max_provider_retries(
        self,
        session_key: str,
        explicit: int | None = None,
    ) -> int:
        """Resolve max provider retries for this turn."""

        if explicit is not None:
            if self._non_bool_int(explicit) and explicit >= 0:
                return int(explicit)
            raise ValueError("max_provider_retries must be an integer >= 0")

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    value = getattr(session_cfg, "agent_max_provider_retries", None)
                    if self._non_bool_int(value) and value >= 0:
                        return int(value)
                    if value is not None:
                        log.warning(
                            "turn_runner.invalid_agent_max_provider_retries",
                            source="session",
                            value=value,
                        )
            except Exception:  # noqa: BLE001
                pass

        env_value = os.environ.get("OPENSQUILLA_AGENT_MAX_PROVIDER_RETRIES")
        if env_value is not None and env_value.strip():
            raw = env_value.strip()
            try:
                value = int(raw)
            except ValueError:
                log.warning("turn_runner.invalid_agent_max_provider_retries", source="env", raw=raw)
            else:
                if value >= 0:
                    return value
                log.warning(
                    "turn_runner.invalid_agent_max_provider_retries", source="env", value=value
                )

        value = getattr(self._config, "agent_max_provider_retries", None)
        if self._non_bool_int(value) and value >= 0:
            return int(value)
        if value is not None:
            log.warning(
                "turn_runner.invalid_agent_max_provider_retries",
                source="config",
                value=value,
            )

        return AgentConfig().max_provider_retries

    def _resolve_turn_thinking(self, turn: Any) -> bool | ThinkingLevel:
        """Resolve explicit config thinking before squilla-router suggestions."""

        metadata = getattr(turn, "metadata", {}) or {}
        managed_native_level = metadata.get("_router_single_managed_provider_thinking_level")
        if managed_native_level is not None:
            if isinstance(managed_native_level, ThinkingLevel):
                return managed_native_level
            native_raw = str(managed_native_level).strip().lower()
            if native_raw == "x-high":
                native_raw = ThinkingLevel.XHIGH.value
            try:
                return ThinkingLevel(native_raw)
            except ValueError as exc:
                raise ValueError(
                    "router_single managed provider thinking level is invalid"
                ) from exc

        llm_cfg = getattr(self._config, "llm", None) if self._config else None
        explicit = getattr(llm_cfg, "thinking", None)
        parsed = self._parse_thinking_level(
            explicit,
            source="config",
        )
        if parsed is not None:
            return parsed
        if explicit is not None and str(explicit).strip():
            return False

        if not metadata.get("thinking_requested"):
            return False

        parsed = self._parse_thinking_level(
            metadata.get("thinking_level", "medium"),
            source="squilla_router",
        )
        return parsed if parsed is not None else False

    def _router_dynamic_outer_thinking_projection(
        self,
        turn: Any,
    ) -> dict[str, bool | str | int]:
        """Freeze the exact outer Agent ChatConfig thinking domain."""

        setting = self._resolve_turn_thinking(turn)
        thinking_enabled, thinking_budget = AgentConfig(thinking=setting).resolve_thinking(
            prompt=str(getattr(turn, "semantic_message", "") or "")
        )
        if not thinking_enabled:
            effective_level = "off"
        elif isinstance(setting, ThinkingLevel):
            effective_level = setting.value
        else:
            effective_level = "enabled"
        return {
            "thinking_enabled": thinking_enabled,
            "effective_thinking_level": effective_level,
            "thinking_budget_tokens": thinking_budget,
        }

    @staticmethod
    def _parse_thinking_level(value: Any, *, source: str) -> bool | ThinkingLevel | None:
        if value is None:
            return None
        if isinstance(value, ThinkingLevel):
            return value
        if isinstance(value, bool):
            return value

        raw = str(value).strip().lower()
        if not raw:
            return None
        normalized = _THINKING_ALIASES.get(raw.replace("_", "-"), raw)
        try:
            return ThinkingLevel(normalized)
        except ValueError:
            log.warning("turn_runner.invalid_thinking_level", source=source, value=value)
            return None

    def _resolve_llm_timeout(self, session_key: str) -> float:
        """Resolve single provider-request timeout for this turn."""

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    per_session = getattr(session_cfg, "llm_request_timeout_seconds", None)
                    if isinstance(per_session, int | float) and per_session > 0:
                        return float(per_session)
            except Exception:  # noqa: BLE001
                pass

        gw_timeout = getattr(self._config, "llm_request_timeout_seconds", None)
        if isinstance(gw_timeout, int | float) and gw_timeout > 0:
            return float(gw_timeout)
        return _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS

    def _resolve_skill_catalog(self) -> Any | None:
        """Refresh and return the immutable catalog pinned to this turn.

        Legacy/custom loaders that only expose ``load_all`` remain supported;
        in that case ``_build_tools`` and the pipeline keep their compatibility
        fallback instead of manufacturing a mutable pseudo-snapshot.
        """
        loader = self._skill_loader
        if loader is None:
            return None
        refresh = getattr(loader, "refresh_if_changed", None)
        snapshot = getattr(loader, "snapshot", None)
        if not callable(refresh) or not callable(snapshot):
            return None
        try:
            refresh(reason="turn")
        except Exception as exc:  # noqa: BLE001 - preserve last-known-good catalog
            log.warning("skills.catalog.turn_refresh_failed", error=str(exc))
        try:
            return snapshot()
        except Exception as exc:  # noqa: BLE001 - legacy fail-open behavior
            log.warning("skills.catalog.turn_snapshot_failed", error=str(exc))
            return None

    def _build_tools(
        self,
        ctx: ToolContext | None = None,
        metadata: dict[str, Any] | None = None,
        skill_catalog: Any | None = None,
    ) -> tuple[list, ToolHandler | None]:
        """Build tool definitions and handler from registry, filtered by ToolContext."""
        if self._tool_registry is None:
            return [], None
        from opensquilla.skills.meta.enabled import (
            is_meta_auto_trigger_enabled,
            is_meta_skill_enabled,
        )
        from opensquilla.tools.dispatch import build_tool_handler
        from opensquilla.tools.policy import apply_tool_policy_from_config
        from opensquilla.tools.registry import filter_by_profile, resolve_profile

        loaded_skills: list[Any] = []
        if skill_catalog is not None:
            loaded_skills = list(getattr(skill_catalog, "skills", ()))
        elif self._skill_loader is not None:
            try:
                loaded_skills = list(self._skill_loader.load_all())
            except Exception:
                loaded_skills = []
        meta_skill_enabled = is_meta_skill_enabled(self._config)
        meta_auto_trigger = is_meta_auto_trigger_enabled(self._config)
        has_invokable_meta_skill = any(
            getattr(skill, "kind", "skill") == "meta"
            and not getattr(skill, "disable_model_invocation", False)
            for skill in loaded_skills
        )
        if ctx is not None:
            if meta_skill_enabled and meta_auto_trigger and has_invokable_meta_skill:
                if ctx.surfaced_tools is None:
                    ctx.surfaced_tools = set()
                ctx.surfaced_tools.add("meta_invoke")
            else:
                ctx.denied_tools.add("meta_invoke")
        if metadata is not None:
            metadata["meta_skill_enabled"] = meta_skill_enabled
            if skill_catalog is not None:
                metadata["skill_catalog_generation"] = int(getattr(skill_catalog, "generation", 0))

        if ctx is not None:
            caller_ctx = ctx
            ctx = apply_tool_policy_from_config(
                ctx,
                available_tools=self._tool_registry.list_names(),
                config=self._turn_config(),
            )
            if ctx.tool_policy:
                from opensquilla.tools.policy import apply_tool_policy_layer

                ctx = apply_tool_policy_layer(
                    ctx,
                    ctx.tool_policy,
                    available_tools=self._tool_registry.list_names(),
                    hard_denied=None,
                )
            ctx = self._apply_runtime_capability_denies(ctx)
            from opensquilla.tools.policy_config import coding_mode_denied_tools

            skills_cfg = getattr(self._config, "skills", None)
            coding_mode = bool(getattr(skills_cfg, "coding_mode", False))
            ctx.denied_tools.update(coding_mode_denied_tools(coding_mode))
            ctx.coding_mode = coding_mode
            if ctx is not caller_ctx:
                caller_ctx.allowed_tools = (
                    set(ctx.allowed_tools) if ctx.allowed_tools is not None else None
                )
                caller_ctx.denied_tools.clear()
                caller_ctx.denied_tools.update(ctx.denied_tools)
                caller_ctx.workspace_write_deny_globs[:] = ctx.workspace_write_deny_globs
                caller_ctx.coding_mode = ctx.coding_mode
            log.debug(
                "tool_policy.policy_pre",
                allowed_tool_count=len(self._tool_registry.to_tool_definitions(ctx)),
                denied_count=len(ctx.denied_tools),
                profile=resolve_profile(ctx).value,
            )
        log.info(
            "tool_context_created",
            caller_kind=ctx.caller_kind if ctx else "none",
            denied_count=len(ctx.denied_tools) if ctx else 0,
        )
        tool_defs = self._tool_registry.to_tool_definitions(ctx)
        profile = resolve_profile(ctx)
        tool_defs = filter_by_profile(tool_defs, profile, ctx)
        # layered intentionally — policy first, profile second.
        log.debug(
            "tool_policy.profile_post",
            allowed_tool_count=len(tool_defs),
            denied_count=len(ctx.denied_tools) if ctx else 0,
            profile=profile.value,
        )
        if metadata is not None:
            metadata["tool_profile"] = profile.value
        known_skill_names = {
            skill.name
            for skill in loaded_skills
            if not getattr(skill, "disable_model_invocation", False)
            and (meta_skill_enabled or getattr(skill, "kind", "skill") != "meta")
        }
        tool_handler = build_tool_handler(
            self._tool_registry,
            ctx,
            known_skill_names=known_skill_names,
        )
        return tool_defs, tool_handler

    def _filter_tool_defs_by_capability(self, tool_defs: list) -> list:
        """Compatibility shim; runtime capability filtering is resolved in ToolContext."""
        return tool_defs

    def _apply_runtime_capability_denies(self, ctx: ToolContext) -> ToolContext:
        from opensquilla.tools.policy import (
            ToolSurfaceCapabilities,
            detect_runtime_tool_surface_capabilities,
            resolve_runtime_tool_surface,
        )

        detected = detect_runtime_tool_surface_capabilities(
            channel_backing=(
                ctx.caller_kind in {CallerKind.CHANNEL, CallerKind.WEB} and bool(ctx.channel_id)
            )
        )
        capabilities = ToolSurfaceCapabilities(
            session_manager=getattr(self, "_session_manager", None) is not None,
            task_runtime=detected.task_runtime,
            scheduler=detected.scheduler,
            gateway_config=getattr(self, "_config", None) is not None,
            channel_backing=detected.channel_backing,
            image_generation=detected.image_generation,
        )
        return resolve_runtime_tool_surface(ctx, capabilities=capabilities)

    @staticmethod
    def _extra_context_for_tool_context(ctx: ToolContext | None) -> dict[str, str]:
        if ctx is None:
            return {}
        extra: dict[str, str] = {}
        run_mode = getattr(ctx, "run_mode", None)
        if run_mode:
            try:
                normalized_run_mode = normalize_run_mode(run_mode)
            except ValueError:
                normalized_run_mode = None
            if normalized_run_mode is not None:
                lines = [f"Run mode: {display_name(normalized_run_mode)}"]
                if normalized_run_mode is RunMode.TRUSTED:
                    lines.extend(
                        [
                            "Default execution target: sandbox",
                            (
                                "Host filesystem: broadly readable; writes stay within "
                                "declared writable roots by default."
                            ),
                            (
                                "Host escalation: explicit host-affecting actions can run "
                                "on the host when policy allows."
                            ),
                            (
                                "Elevation: when a tool returns elevation_required, retry "
                                "that exact action with sandbox_permissions="
                                "require_escalated and a precise justification only when "
                                "the user request warrants it. Never elevate a generic "
                                "runtime or command failure."
                            ),
                            (
                                "Review: elevation is independently authorized once for "
                                "the exact arguments; explain denials before seeking a new "
                                "explicit user instruction."
                            ),
                            (
                                "Sandbox: enabled by default; do not treat it as a "
                                "prohibition on requested host work."
                            ),
                            (
                                "Do not refuse a user-requested installation merely because "
                                "the default path starts sandboxed; use available shell, "
                                "package, or download tools and let the runtime enforce policy."
                            ),
                        ]
                    )
                else:
                    sandbox_line = (
                        "Sandbox: disabled for tool execution"
                        if normalized_run_mode is RunMode.FULL
                        else "Sandbox: enabled for tool execution"
                    )
                    lines.extend(
                        [
                            f"Execution target: {execution_target(normalized_run_mode)}",
                            sandbox_line,
                        ]
                    )
                extra["Execution Context"] = "\n".join(lines)
        if ctx.caller_kind is CallerKind.SUBAGENT:
            extra["Subagent Task Protocol"] = _SUBAGENT_TASK_PROTOCOL
        return extra

    @staticmethod
    def _merge_extra_prompt_context(
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None:
        if not extra:
            return base
        if base is None:
            return dict(extra)
        merged = dict(base)
        merged.update(extra)
        return merged

    @staticmethod
    def _render_volatile_block(
        daily_notes: dict[str, str] | None,
        workspace_files: dict[str, str] | None,
        extra_context: dict[str, str] | None,
        prompt_mode: str = "full",
        wrap_untrusted_workspace: bool = True,
    ) -> str:
        """Render per-turn / per-day volatile content as the dynamic suffix.

        Replaces three previously-cacheable blocks once carried by
        the prior ``identity/templates/system_prompt.j2`` template:

        1. ``## Recent Notes`` (daily_notes) — gated on prompt_mode != minimal.
        2. ``## Workspace Files (injected)`` — gated on prompt_mode != minimal,
           with SOUL.md / IDENTITY.md filtered out (parsed elsewhere into
           AgentProfile.identity).
        3. ``## <key>`` blocks for each ``extra_context`` entry (no gating).

        Each section's bytes match what the prior Jinja render produced for
        the same inputs.
        Sections are joined directly with no separator — adjacent ``\\n\\n``
        terminators in each section already provide the visual break, the
        same way the prior template rendered them inline. The final result
        is right-stripped of newlines so it slots cleanly into the dynamic
        suffix (``base + "\\n\\n" + suffix`` is reassembled downstream).
        """
        sections: list[str] = []

        # 1. ## Recent Notes (daily_notes), suppressed in minimal mode.
        if daily_notes and prompt_mode != "minimal":
            buf = "## Recent Notes\n\n"
            for filename, content in daily_notes.items():
                buf += f"### {filename}\n\n{content}\n\n"
            sections.append(buf)

        # 2. ## Workspace Files (injected), suppressed in minimal mode.
        # SOUL.md / IDENTITY.md are filtered (parsed elsewhere into
        # AgentProfile.identity); if every entry is filtered out, no header
        # is emitted at all so the volatile suffix doesn't carry a stranded
        # bare heading whose tuple-return would later trip downstream
        # consumers (empty-suffix invariant).
        if workspace_files and prompt_mode != "minimal":
            visible = {
                filename: content
                for filename, content in workspace_files.items()
                if filename not in ("SOUL.md", "IDENTITY.md")
            }
            if visible:
                buf = "## Workspace Files (injected)\n\n"
                # Filenames are masked as ``### Workspace Context N`` so the
                # template surface mirrors pilot's filename-non-exposure
                # convention (commit 93dfb8a). BOOTSTRAP.md is the exception:
                # it gets a named heading so the model recognizes it as a
                # one-shot setup ritual and removes the file on completion
                # (see identity/templates/bootstrap/BOOTSTRAP.md).
                context_index = 0
                for filename, content in visible.items():
                    if filename == "BOOTSTRAP.md":
                        buf += f"### One-Shot Workspace Bootstrap\n\n{content}\n\n"
                        continue
                    context_index += 1
                    rendered_content = (
                        injection_guard.wrap_untrusted(content, source=f"workspace:{filename}")
                        if wrap_untrusted_workspace
                        else content
                    )
                    buf += f"### Workspace Context {context_index}\n\n{rendered_content}\n\n"
                sections.append(buf)

        # 3. extra_context — emitted as ## <key> blocks regardless of mode.
        if extra_context:
            buf = ""
            for key, value in extra_context.items():
                buf += f"## {key}\n\n{value}\n\n"
            if buf:
                sections.append(buf)

        if not sections:
            return ""
        return "".join(sections).rstrip("\n")

    def _assemble_prompt(
        self,
        agent_id: str,
        tool_defs: list,
        session_key: str | None = None,
        semantic_message: str | None = None,
        extra_context: dict[str, str] | None = None,
        prompt_metadata: dict[str, Any] | None = None,
        bootstrap_context_mode: str | None = None,
        fresh_user_session: bool = False,
    ) -> str | tuple[str, str]:
        """Assemble identity system prompt via Jinja2 template.

        Uses frozen snapshot when available (keyed by agent_id + session_key),
        falls back to live disk reads for backwards compatibility.

        Returns ``str`` for the prompt-cache-stable case; returns
        ``(base, dynamic_context)`` only when daily notes, workspace files, or
        tool-context blocks need to stay outside the cacheable prefix.
        """
        from opensquilla.identity.parser import parse_agents, parse_identity, parse_soul
        from opensquilla.identity.prompt import assemble_system_prompt
        from opensquilla.identity.types import AgentIdentity, AgentProfile
        from opensquilla.identity.workspace import (
            filter_workspace_filenames_for_session,
            filter_workspace_files_for_session,
            load_workspace_files_budgeted_with_report,
        )

        configured_agent_name = getattr(self._config, "agent_name", None) if self._config else None
        agent_name = (
            configured_agent_name.strip()
            if isinstance(configured_agent_name, str) and configured_agent_name.strip()
            else None
        )
        bootstrap_workspace_dir = self._resolve_bootstrap_workspace_dir(agent_id)
        bootstrap_context_key = bootstrap_context_mode or "full"
        bootstrap_snap_key = (agent_id, session_key, bootstrap_context_key) if session_key else None
        bootstrap_snap = (
            self._bootstrap_snapshots.get(bootstrap_snap_key)
            if bootstrap_snap_key is not None
            else None
        )
        if bootstrap_snap is not None:
            workspace_files = dict(bootstrap_snap.workspace_files)
            visible_bootstrap_report = list(bootstrap_snap.report)
        else:
            safety_cfg = getattr(self._config, "safety", None) if self._config else None
            bootstrap_filenames = (
                ("HEARTBEAT.md",)
                if bootstrap_context_mode == "heartbeat_light"
                else filter_workspace_filenames_for_session(None, session_key)
            )
            if bootstrap_context_mode == "unattended":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name != "BOOTSTRAP.md"
                )
            elif bootstrap_context_mode == "stateless":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name == "TOOLS.md"
                )
            elif bootstrap_context_mode == "stateless_keep_project_rules":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name in {"AGENTS.md", "TOOLS.md"}
                )
            loaded_workspace_files, bootstrap_report = load_workspace_files_budgeted_with_report(
                str(bootstrap_workspace_dir),
                per_file_max_chars=self._resolve_bootstrap_max_chars(),
                total_max_chars=self._resolve_bootstrap_total_max_chars(),
                filenames=bootstrap_filenames,
                injection_scan_mode=getattr(safety_cfg, "injection_scan_mode", "report"),
            )
            workspace_files = filter_workspace_files_for_session(
                loaded_workspace_files,
                session_key,
            )
            subagents_cfg = getattr(self._config, "subagents", None) if self._config else None
            if (
                session_key
                and is_subagent_key(session_key)
                and getattr(subagents_cfg, "prompt_compact", False)
            ):
                workspace_files = {
                    name: content
                    for name, content in workspace_files.items()
                    if name in {"AGENTS.md", "TOOLS.md"}
                }
            visible_bootstrap_report = [
                report for report in bootstrap_report if report.filename in workspace_files
            ]
            if bootstrap_snap_key is not None:
                self._bootstrap_snapshots[bootstrap_snap_key] = BootstrapSnapshot(
                    workspace_files=dict(workspace_files),
                    report=list(visible_bootstrap_report),
                )
        memory_source_dir = self._resolve_memory_source_dir(agent_id)
        stateless_prompt = bootstrap_context_mode in {
            "stateless",
            "stateless_keep_project_rules",
        }
        private_memory_allowed = (
            False if stateless_prompt else allows_private_memory_prompt_injection(session_key)
        )

        # Use frozen snapshot if available, otherwise read from disk
        snap_key = (agent_id, session_key) if session_key else None
        snap = self._memory_snapshots.get(snap_key) if snap_key else None
        if not private_memory_allowed:
            memory_text = None
            daily = {}
        elif snap is not None:
            memory_text = snap.memory_md
            daily = snap.daily_notes
        else:
            daily = self._load_daily_notes(memory_source_dir)
            memory_text = self._load_memory_md(memory_source_dir)
        daily_notes_count_before_omit = len(daily)
        daily_notes_omitted = daily_notes_count_before_omit > 0
        if daily_notes_omitted:
            daily = {}
        if prompt_metadata is not None:
            prompt_metadata["daily_notes_omitted"] = daily_notes_omitted
            prompt_metadata["daily_notes_count_before_omit"] = daily_notes_count_before_omit
            if daily_notes_omitted:
                prompt_metadata["daily_notes_policy_reason"] = "auto_injection_disabled"
            if fresh_user_session:
                prompt_metadata["daily_notes_fresh_session_omitted"] = True
            prompt_metadata["memory_md_present"] = memory_text is not None
            prompt_metadata["injected_workspace_files_count"] = len(workspace_files)
            prompt_metadata["bootstrap_files"] = visible_bootstrap_report
            if not private_memory_allowed:
                prompt_metadata["memory_prompt_injection_skipped"] = (
                    "stateless" if stateless_prompt else "session-scope"
                )
            retrieval_metadata = self._effective_memory_retrieval_metadata(agent_id)
            prompt_metadata["retrieval_mode"] = retrieval_metadata.get("retrieval_mode")
            prompt_metadata["embedding_requested_provider"] = retrieval_metadata.get(
                "embedding_requested_provider"
            )
            prompt_metadata["embedding_effective_provider"] = retrieval_metadata.get(
                "embedding_effective_provider"
            )
            prompt_metadata["embedding_model"] = retrieval_metadata.get("embedding_model")
            prompt_metadata["memory_retrieval_vector_weight"] = retrieval_metadata.get(
                "vector_weight"
            )
            prompt_metadata["memory_retrieval_text_weight"] = retrieval_metadata.get("text_weight")
            prompt_metadata["memory_mode_fingerprint"] = retrieval_metadata

        soul_doc = parse_soul(workspace_files["SOUL.md"]) if "SOUL.md" in workspace_files else None
        identity_fields = (
            parse_identity(workspace_files["IDENTITY.md"])
            if "IDENTITY.md" in workspace_files
            else None
        )
        agents_doc = (
            parse_agents(workspace_files["AGENTS.md"]) if "AGENTS.md" in workspace_files else None
        )
        if agent_name is None and identity_fields is not None:
            agent_name = identity_fields.name
        prompt_mode = _resolve_identity_prompt_mode(self._config)
        patch_evidence_protocol = _resolve_patch_evidence_protocol(self._config)
        finalize_evidence_gate = _resolve_finalize_evidence_gate(self._config)
        legacy_prompt_style = _resolve_legacy_prompt_style(self._config)

        agent_profile = AgentProfile(
            agent_id=agent_id,
            identity=AgentIdentity(
                name=agent_name,
                emoji=identity_fields.emoji if identity_fields else None,
                theme=identity_fields.theme if identity_fields else None,
                avatar=identity_fields.avatar if identity_fields else None,
                soul=soul_doc,
                identity_fields=identity_fields,
            ),
            agents_doc=agents_doc,
            workspace_files=workspace_files,
            prompt_mode=prompt_mode,
            patch_evidence_protocol=patch_evidence_protocol,
            finalize_evidence_gate=finalize_evidence_gate,
            legacy_prompt_style=legacy_prompt_style,
        )
        os_name = os.uname().sysname if hasattr(os, "uname") else platform.system()
        runtime_info = {
            "os": os_name,
            "shell": os.environ.get("SHELL", ""),
            "workspace_dir": str(bootstrap_workspace_dir),
        }
        base_prompt = assemble_system_prompt(
            agent_profile,
            tools=[td.name for td in tool_defs] if tool_defs else None,
            memory=memory_text,
            runtime_info=runtime_info,
            docs_path=self._resolve_docs_path(),
            heartbeat_prompt=getattr(self._config, "heartbeat_prompt", None),
        )
        # daily_notes, workspace_files, and extra_context are per-turn /
        # per-day volatile content. Keeping them in the cacheable base
        # invalidates the prompt-cache prefix every time any of them
        # changes (every day for daily_notes, every workspace edit for
        # workspace_files, every tool_context shift for extra_context).
        # Render them into the dynamic suffix instead so the base hash
        # stays stable across those rotations.
        dynamic_blocks: list[str] = []
        volatile_block = self._render_volatile_block(
            daily_notes=daily,
            workspace_files=workspace_files,
            extra_context=extra_context,
            prompt_mode=prompt_mode,
            wrap_untrusted_workspace=getattr(
                getattr(self._config, "safety", None),
                "wrap_untrusted_workspace",
                True,
            ),
        )
        if volatile_block:
            dynamic_blocks.append(volatile_block)
        if tool_defs and any(getattr(td, "name", "") == "router_control" for td in tool_defs):
            router_block = render_router_control_prompt_block(
                getattr(self._turn_config(), "squilla_router", None)
            )
            if router_block:
                dynamic_blocks.append(f"## Router Control\n\n{router_block}")

        if dynamic_blocks:
            return base_prompt, "\n\n".join(dynamic_blocks)
        return base_prompt

    @staticmethod
    def _resolve_docs_path() -> str | None:
        return None

    def _resolve_memory_source_dir(self, agent_id: str):
        from opensquilla.agents.scope import resolve_agent_memory_source_dir

        source = getattr(getattr(self._config, "memory", None), "source", "state")
        return resolve_agent_memory_source_dir(agent_id, self._config, source=source)

    def _effective_memory_retrieval_metadata(self, agent_id: str) -> dict[str, str]:
        retrievers = self._memory_retrievers or {}
        for key in (agent_id, "main"):
            retriever = retrievers.get(key)
            metadata_fn = getattr(retriever, "effective_retrieval_metadata", None)
            if callable(metadata_fn):
                try:
                    metadata = metadata_fn()
                except Exception:
                    continue
                if isinstance(metadata, dict):
                    return {str(k): str(v) for k, v in metadata.items()}

        memory_cfg = getattr(self._config, "memory", None)
        configured_mode = str(getattr(memory_cfg, "retrieval_mode", "hybrid"))
        effective_mode = "fts_only" if configured_mode == "fts_only" else configured_mode
        return {
            "configured_retrieval_mode": configured_mode,
            "retrieval_mode": effective_mode,
            "embedding_requested_provider": "",
            "embedding_effective_provider": "",
            "embedding_model": "",
            "vector_weight": str(getattr(memory_cfg, "vector_weight", "")),
            "text_weight": str(getattr(memory_cfg, "text_weight", "")),
        }

    def _resolve_bootstrap_workspace_dir(self, agent_id: str):
        from opensquilla.agents.scope import resolve_agent_workspace_dir

        return resolve_agent_workspace_dir(agent_id, self._config)

    def _resolve_bootstrap_max_chars(self) -> int:
        value = getattr(self._config, "bootstrap_max_chars", None) if self._config else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return int(value)
        return 20_000

    def _resolve_bootstrap_total_max_chars(self) -> int:
        value = getattr(self._config, "bootstrap_total_max_chars", None) if self._config else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return int(value)
        return 50_000

    def _load_memory_md(self, workspace_dir: Any, max_chars: int | None = None) -> str | None:
        """Load MEMORY.md from agent workspace for system prompt injection."""
        from pathlib import Path

        if max_chars is None:
            max_chars = getattr(getattr(self._config, "memory", None), "inject_limit", 4000)
        root = Path(workspace_dir)
        memory_file = root / "MEMORY.md"
        if not memory_file.is_file():
            memory_file = root / "memory.md"
        if not memory_file.is_file():
            return None
        try:
            content = memory_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not content:
            return None
        if len(content) > max_chars:
            return content[:max_chars] + "\n..."
        return content

    def _make_meta_llm_chat(
        self,
        provider: Any,
        session_key: str,
        usage_execution_context: UsageExecutionContext | None = None,
    ) -> Any:
        """Construct the (system_prompt, user_message) -> str callable that
        meta_resolution's awaiting branch invokes for ``nl_extract: true``.

        Returns None when the provider isn't available — the awaiting
        branch silently falls back to the deterministic parser's errors,
        which is exactly the behavior we want for non-LLM unit tests.
        """
        if provider is None:
            return None
        # Lazy import keeps the runtime cold-start independent of meta.
        from opensquilla.engine.types import AgentConfig
        from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider

        # ``make_llm_chat_from_provider`` only reads ``model_id`` /
        # ``metadata`` off base_config (via getattr). ``self._config`` is
        # the GatewayConfig (different shape — no .model_id), so build a
        # minimal AgentConfig() rather than passing the wrong type.
        return make_llm_chat_from_provider(
            provider=provider,
            base_config=AgentConfig(),
            usage_tracker=getattr(self, "_usage_tracker", None),
            session_key=session_key,
            usage_event_sink=self._usage_event_sink,
            usage_execution_context=usage_execution_context,
        )

    def _resolve_vision_followup_gate_model(self) -> str | None:
        router_cfg = getattr(self._turn_config(), "squilla_router", None)
        if router_cfg is None:
            return None
        configured_model = str(getattr(router_cfg, "vision_followup_gate_model", "") or "").strip()
        if configured_model:
            return configured_model
        tier_name = str(getattr(router_cfg, "vision_followup_gate_tier", "c0") or "").strip()
        if not tier_name:
            return None
        tiers = getattr(router_cfg, "tiers", {})
        if not isinstance(tiers, Mapping):
            return None
        tier = tiers.get(tier_name)
        if not isinstance(tier, Mapping):
            return None
        model = tier.get("model")
        if not isinstance(model, str):
            return None
        model = model.strip()
        return model or None

    def _make_vision_followup_gate_chat(
        self,
        cloned_selector: Any,
        usage_execution_context: UsageExecutionContext | None = None,
    ) -> tuple[Any | None, str | None]:
        gate_model = self._resolve_vision_followup_gate_model()
        if not gate_model or cloned_selector is None:
            return None, gate_model
        if not hasattr(cloned_selector, "clone"):
            return None, gate_model
        try:
            gate_selector = cloned_selector.clone()
            gate_selector.override_model(gate_model)
            gate_provider = gate_selector.resolve()
        except Exception:
            return None, gate_model

        async def _chat(
            messages: list[Any],
            tools: Any = None,
            config: Any = None,
        ) -> AsyncIterator[Any]:
            scope: UsageAccountingScope | None = None
            if self._usage_event_sink is not None:
                execution_id = uuid.uuid4().hex
                parent = usage_execution_context
                scope = UsageAccountingScope(
                    sink=self._usage_event_sink,
                    context=UsageExecutionContext(
                        execution_id=execution_id,
                        agent_run_id=execution_id,
                        turn_id=execution_id,
                        parent_turn_id=(
                            parent.turn_id or parent.execution_id if parent is not None else None
                        ),
                        session_id=parent.session_id if parent is not None else None,
                        session_epoch=parent.session_epoch if parent is not None else 0,
                        agent_id=parent.agent_id if parent is not None else "",
                        run_kind="vision_followup_gate",
                    ),
                )
            provider_id = str(
                getattr(gate_selector, "active_provider_id", "")
                or getattr(gate_provider, "provider_name", "")
                or ""
            )
            with bind_usage_accounting_scope(scope):
                stream = (
                    gate_provider.chat(messages, tools=tools, config=config)
                    if scope is not None and provider_accounts_physical_usage(gate_provider)
                    else account_provider_stream(
                        lambda: gate_provider.chat(
                            messages,
                            tools=tools,
                            config=config,
                        ),
                        provider=provider_id,
                        model=gate_model,
                    )
                )
                async for event in stream:
                    yield event

        return _chat, gate_model

    def _load_daily_notes(self, workspace_dir: Any) -> dict[str, str]:
        from opensquilla.identity.workspace import load_daily_notes

        memory_cfg = getattr(self._config, "memory", None)
        return load_daily_notes(
            str(workspace_dir),
            per_note_max_chars=getattr(memory_cfg, "daily_note_max_chars", 4000),
            total_max_chars=getattr(memory_cfg, "daily_notes_total_max_chars", 8000),
        )

    async def _resolve_fixed_four_tier_v2_provider(
        self,
        *,
        turn: Any,
        provider: Any,
        cloned_selector: Any,
        turn_config: Any,
        ensemble_cfg: Any,
        turn_absolute_deadline: float | None,
        usage_execution_context: UsageExecutionContext | None = None,
        bound_user_message_id: str | None = None,
    ) -> Any:
        """Resolve the isolated four_tier_mapping v2 decision to one provider."""

        from opensquilla.engine.routing.fixed_four_tier_v2 import (
            MODE,
            SCHEMA_VERSION,
            FixedFourTierRoutingError,
            FixedFourTierTaskState,
            RoutingRequest,
            Tier,
            normalize_attachment_modalities,
        )
        from opensquilla.engine.routing.health import get_provider_health_ledger
        from opensquilla.engine.selector_override import resolve_tier_provider_config
        from opensquilla.provider.model_catalog import resolve_effective_context_window
        from opensquilla.provider.selector import ModelSelector, SelectorConfig
        from opensquilla.session.models import (
            FixedFourTierDecisionRecord,
            FixedFourTierRequestClaim,
            FixedFourTierState,
        )
        from opensquilla.session.storage import FixedFourTierStateConflictError

        session_manager = self._session_manager
        required_persistence_methods = (
            "get_session",
            "get_transcript",
            "get_fixed_four_tier_state",
            "reconcile_stale_fixed_four_tier_request",
            "claim_fixed_four_tier_request",
            "settle_fixed_four_tier_request_claim",
            "get_fixed_four_tier_decision_by_request",
            "get_fixed_four_tier_decision_by_route",
            "get_fixed_four_tier_decision_by_input_message",
            "stage_fixed_four_tier_decision",
            "commit_fixed_four_tier_decision",
            "settle_fixed_four_tier_decision",
        )
        if session_manager is None or any(
            not callable(getattr(session_manager, name, None))
            for name in required_persistence_methods
        ):
            raise FixedFourTierRoutingError(
                "four_tier_mapping requires durable session routing storage",
                reason="routing_persistence_unavailable",
            )
        session_node = await session_manager.get_session(turn.session_key)
        durable_session_id = str(getattr(session_node, "session_id", "") or "")
        session_epoch = int(getattr(session_node, "epoch", 0) or 0)
        if not durable_session_id:
            raise FixedFourTierRoutingError(
                "four_tier_mapping session identity is unavailable",
                reason="session_identity_unavailable",
            )
        if not str(bound_user_message_id or "").strip():
            raise FixedFourTierRoutingError(
                "four_tier_mapping requires a durable input message anchor",
                reason="input_message_anchor_unavailable",
            )

        mode_cfg = getattr(ensemble_cfg, "four_tier_mapping", None)
        tiers = getattr(mode_cfg, "tiers", None)
        if not isinstance(tiers, Mapping):
            raise FixedFourTierRoutingError(
                "four_tier_mapping tier mapping is unavailable",
                reason="tier_mapping_unavailable",
            )
        classifier_cfg = getattr(mode_cfg, "classifier", None)
        classifier_backend = str(getattr(classifier_cfg, "backend", "") or "")
        if classifier_backend not in {"random_mock", "registered_model"}:
            raise FixedFourTierRoutingError(
                "four_tier_mapping classifier configuration is unavailable",
                reason="classifier_configuration_unavailable",
            )
        if classifier_backend == "registered_model" and not callable(
            getattr(session_manager, "list_recent_fixed_four_tier_decisions", None)
        ):
            raise FixedFourTierRoutingError(
                "registered four_tier_mapping requires bounded route history storage",
                reason="route_history_storage_unavailable",
            )

        metadata = turn.metadata
        history_value = metadata.get("router_history_user_texts")
        user_history = (
            tuple(str(value) for value in history_value if isinstance(value, str))
            if isinstance(history_value, Sequence) and not isinstance(history_value, (str, bytes))
            else ()
        )
        control_event = metadata.get("fixed_four_tier_v2_control_event")
        execution_id = str(
            getattr(usage_execution_context, "turn_id", "")
            or getattr(usage_execution_context, "execution_id", "")
            or uuid.uuid4().hex
        ).strip()
        request_id = str(
            metadata.get("fixed_four_tier_v2_request_id")
            or metadata.get("client_request_id")
            or bound_user_message_id
        ).strip()
        claimed_at_ms = time.time_ns() // 1_000_000
        if turn_absolute_deadline is None:
            lease_duration_ms = 5 * 60 * 1_000
        else:
            lease_duration_ms = max(
                60_000,
                int(max(0.0, turn_absolute_deadline - time.monotonic()) * 1_000) + 60_000,
            )
        lease_expires_at_ms = claimed_at_ms + lease_duration_ms
        existing_claim = await session_manager.reconcile_stale_fixed_four_tier_request(
            session_id=durable_session_id,
            request_id=request_id,
            now_ms=claimed_at_ms,
        )
        if existing_claim is not None:
            claim_status = str(getattr(existing_claim, "status", "") or "")
            raise FixedFourTierRoutingError(
                "four_tier_mapping request already has a durable execution claim",
                reason=(
                    "duplicate_request_in_progress"
                    if claim_status in {"claimed", "materialized"}
                    else "duplicate_request_replay"
                ),
            )
        existing_request_decision = await session_manager.get_fixed_four_tier_decision_by_request(
            session_id=durable_session_id,
            request_id=request_id,
        )
        if existing_request_decision is not None:
            if int(existing_request_decision.schema_version) != 1:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping decision schema is incompatible",
                    reason="decision_schema_incompatible",
                )
            # Gateway ingress normally deduplicates before reaching the
            # runner.  This guard is the durable last line of defence for a
            # crash/replay or a second worker: never classify, advance state,
            # or call a model twice for the same accepted request.
            raise FixedFourTierRoutingError(
                "four_tier_mapping request was already classified",
                reason="duplicate_request_replay",
            )
        persisted_state = await session_manager.get_fixed_four_tier_state(durable_session_id)
        if persisted_state is not None and int(persisted_state.schema_version) != 1:
            raise FixedFourTierRoutingError(
                "four_tier_mapping state schema is incompatible",
                reason="state_schema_incompatible",
            )
        task_state = (
            FixedFourTierTaskState(
                task_id=str(persisted_state.task_id),
                tier=cast(Tier, str(persisted_state.tier)),
                turn_count=int(persisted_state.task_turn_count),
                version=int(persisted_state.version),
                task_start_input_message_id=(
                    str(persisted_state.task_start_input_message_id)
                    if persisted_state.task_start_input_message_id is not None
                    else None
                ),
            )
            if persisted_state is not None
            else None
        )
        expected_state_version = task_state.version if task_state is not None else None
        redo_parent_route_id: str | None = None
        parent_decision: Any | None = None
        redo_child_task_start_message_id: str | None = None
        feature_session_key = turn.session_key
        feature_session_id = durable_session_id
        feature_session_epoch = session_epoch
        feature_route_before_ms = claimed_at_ms
        redo_feature_transcript: list[Any] | None = None
        feature_task_start_message_id = (
            task_state.task_start_input_message_id if task_state is not None else None
        )
        feature_current_message_id = bound_user_message_id
        provenance = metadata.get("input_provenance")
        provenance_mapping = provenance if isinstance(provenance, Mapping) else {}
        redo_parent_session_id = str(
            metadata.get("fixed_four_tier_v2_redo_parent_session_id") or ""
        ).strip()
        redo_of_message_id = str(
            metadata.get("fixed_four_tier_v2_redo_of_message_id") or ""
        ).strip()
        trusted_child_task_start_message_id = str(
            metadata.get("fixed_four_tier_v2_redo_child_task_start_input_message_id") or ""
        ).strip()
        untrusted_redo_provenance = bool(
            provenance_mapping.get("action") == "redo"
            or provenance_mapping.get("control_event") == "redo"
            or provenance_mapping.get("fixed_four_tier_v2_redo_parent_session_id")
            or provenance_mapping.get("fixed_four_tier_v2_redo_of_message_id")
        )
        trusted_redo = bool(
            control_event == "redo"
            and redo_parent_session_id
            and redo_of_message_id
            and trusted_child_task_start_message_id
        )
        if untrusted_redo_provenance and not trusted_redo:
            raise FixedFourTierRoutingError(
                "four_tier_mapping regenerate provenance is not server-authoritative",
                reason="redo_provenance_untrusted",
            )
        if (
            redo_parent_session_id
            or redo_of_message_id
            or trusted_child_task_start_message_id
            or control_event == "redo"
        ) and not trusted_redo:
            raise FixedFourTierRoutingError(
                "four_tier_mapping regenerate controls are incomplete",
                reason="redo_control_marker_unavailable",
            )
        if redo_parent_session_id and redo_of_message_id:
            parent_decision = await session_manager.get_fixed_four_tier_decision_by_input_message(
                session_id=redo_parent_session_id,
                input_message_id=redo_of_message_id,
            )
            if parent_decision is not None:
                parent_task_turn_index = int(parent_decision.task_turn_index)
                source_task_start_message_id = (
                    parent_decision.task_start_input_message_id or parent_decision.input_message_id
                )
                if not str(source_task_start_message_id or "").strip():
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate source has no task boundary",
                        reason="redo_parent_task_boundary_unavailable",
                    )
                if parent_task_turn_index == 0:
                    redo_child_task_start_message_id = str(bound_user_message_id)
                    if trusted_child_task_start_message_id != redo_child_task_start_message_id:
                        raise FixedFourTierRoutingError(
                            "four_tier_mapping first-turn regenerate boundary is invalid",
                            reason="redo_child_task_boundary_unavailable",
                        )
                else:
                    child_transcript = list(await session_manager.get_transcript(turn.session_key))
                    boundary_matches = [
                        index
                        for index, entry in enumerate(child_transcript)
                        if getattr(entry, "message_id", None) == trusted_child_task_start_message_id
                    ]
                    bound_matches = [
                        index
                        for index, entry in enumerate(child_transcript)
                        if getattr(entry, "message_id", None) == bound_user_message_id
                    ]
                    if (
                        len(boundary_matches) == 1
                        and len(bound_matches) == 1
                        and boundary_matches[0] < bound_matches[0]
                    ):
                        redo_child_task_start_message_id = trusted_child_task_start_message_id
                if not str(redo_child_task_start_message_id or "").strip():
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate child has no exact task boundary",
                        reason="redo_child_task_boundary_unavailable",
                    )
                task_state = FixedFourTierTaskState(
                    task_id=parent_decision.task_id,
                    tier=cast(Tier, parent_decision.final_tier),
                    # The stored index is the pre-route task count. Reusing it
                    # makes the regenerated turn replace, rather than append
                    # after, the original answer in the semantic task.
                    turn_count=parent_task_turn_index,
                    version=0,
                    # Classification is bound to the parent source transcript.
                    # The returned state is translated to its exact child row
                    # immediately after classification, before persistence.
                    task_start_input_message_id=str(source_task_start_message_id),
                )
                expected_state_version = None
                redo_parent_route_id = parent_decision.route_id
                feature_session_key = parent_decision.session_key
                feature_session_id = parent_decision.session_id
                feature_session_epoch = int(parent_decision.session_epoch)
                feature_route_before_ms = int(parent_decision.decided_at_ms)
                feature_task_start_message_id = source_task_start_message_id
                feature_current_message_id = parent_decision.input_message_id
                get_canonical_transcript = getattr(
                    session_manager,
                    "get_canonical_transcript_by_session_id",
                    None,
                )
                is_canonical_complete = getattr(
                    session_manager,
                    "is_canonical_transcript_complete_by_session_id",
                    None,
                )
                if not callable(get_canonical_transcript) or not callable(is_canonical_complete):
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate source archive is unavailable",
                        reason="redo_parent_canonical_transcript_unavailable",
                    )
                try:
                    canonical_complete = await is_canonical_complete(redo_parent_session_id)
                    redo_feature_transcript = list(
                        await get_canonical_transcript(redo_parent_session_id)
                    )
                except FixedFourTierRoutingError:
                    raise
                except Exception as exc:
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate source archive could not be read",
                        reason="redo_parent_canonical_transcript_unavailable",
                    ) from exc
                if canonical_complete is not True:
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate source archive is incomplete",
                        reason="redo_parent_canonical_transcript_incomplete",
                    )
            else:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping regenerate source has no committed route",
                    reason="redo_parent_route_unavailable",
                )

        if task_state is not None and not str(feature_task_start_message_id or "").strip():
            raise FixedFourTierRoutingError(
                "four_tier_mapping active task has no durable input anchor",
                reason="task_input_anchor_unavailable",
            )
        feature_transcript = (
            redo_feature_transcript
            if redo_feature_transcript is not None
            else list(await session_manager.get_transcript(feature_session_key))
        )
        feature_start_index = 0
        if feature_task_start_message_id:
            feature_start_index_value = next(
                (
                    index
                    for index, entry in enumerate(feature_transcript)
                    if getattr(entry, "message_id", None) == feature_task_start_message_id
                ),
                None,
            )
            if feature_start_index_value is None:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping task feature boundary is unavailable",
                    reason="task_feature_boundary_unavailable",
                )
            feature_start_index = feature_start_index_value
        feature_end_index_value = next(
            (
                index
                for index, entry in enumerate(feature_transcript)
                if getattr(entry, "message_id", None) == feature_current_message_id
            ),
            None,
        )
        if feature_end_index_value is None:
            raise FixedFourTierRoutingError(
                "four_tier_mapping current feature boundary is unavailable",
                reason="current_feature_boundary_unavailable",
            )
        feature_end_index = feature_end_index_value
        if feature_end_index < feature_start_index:
            raise FixedFourTierRoutingError(
                "four_tier_mapping feature boundaries are reversed",
                reason="invalid_feature_boundary_order",
            )
        # The trained Router contract retains the latest cross-task history.
        # Task boundaries still govern state/context reuse, but must not hide
        # route-before user turns from the classifiers.
        prior_feature_entries = feature_transcript[:feature_end_index]
        user_history = tuple(
            text
            for entry in prior_feature_entries
            if getattr(entry, "role", None) == "user"
            and (
                text := _fixed_route_visible_transcript_text(
                    "user",
                    getattr(entry, "content", ""),
                )
            )
        )
        task_anchor = ""
        if feature_task_start_message_id:
            task_anchor_entry = feature_transcript[feature_start_index]
            task_anchor = _fixed_route_visible_transcript_text(
                "user",
                getattr(task_anchor_entry, "content", ""),
            )
        previous_route = parent_decision
        if previous_route is None and persisted_state is not None and persisted_state.last_route_id:
            previous_route = await session_manager.get_fixed_four_tier_decision_by_route(
                str(persisted_state.last_route_id)
            )
        previous_assistant_entry = None
        previous_response_id = str(getattr(previous_route, "response_id", "") or "").strip()
        if previous_response_id:
            candidate_entry = next(
                (
                    (index, entry)
                    for index, entry in enumerate(feature_transcript)
                    if getattr(entry, "message_id", None) == previous_response_id
                    and getattr(entry, "role", None) == "assistant"
                ),
                None,
            )
            if candidate_entry is not None:
                response_index, response_entry = candidate_entry
                if parent_decision is not None or (
                    feature_start_index <= response_index < feature_end_index
                ):
                    previous_assistant_entry = response_entry
        # A session can enable this routing mode after earlier turns already
        # exist. In that case there is no fixed-route response binding yet,
        # but the last route-before assistant row is still observable input.
        # Do not use this fallback for a bound route whose response is absent:
        # combining an older answer with a newer route outcome would be false.
        if (
            classifier_backend == "registered_model"
            and previous_assistant_entry is None
            and previous_route is None
        ):
            previous_assistant_entry = next(
                (
                    entry
                    for entry in reversed(prior_feature_entries)
                    if getattr(entry, "role", None) == "assistant"
                ),
                None,
            )
        previous_assistant_text: str | None
        if previous_assistant_entry is not None:
            previous_assistant_text = _fixed_route_visible_transcript_text(
                "assistant",
                getattr(previous_assistant_entry, "content", ""),
            )
            if not previous_assistant_text:
                previous_assistant_text = None
        else:
            previous_assistant_text = None
        previous_usage = None
        if previous_assistant_entry is not None and isinstance(
            getattr(previous_assistant_entry, "turn_usage", None),
            Mapping,
        ):
            previous_usage = dict(previous_assistant_entry.turn_usage)
        if previous_route is not None:
            previous_route_trace = dict(previous_route.route_trace or {})
            previous_usage = {
                **(previous_usage or {}),
                "route_id": previous_route.route_id,
                "execution_status": previous_route.execution_status,
                "error_code": previous_route.error_code,
                "response_id": previous_route.response_id,
                "attempt_ids": list(previous_route_trace.get("attempt_ids") or []),
                "retry_count": max(
                    0,
                    len(previous_route_trace.get("attempt_ids") or []) - 1,
                ),
            }
        previous_outcome = _fixed_route_previous_outcome(
            route=previous_route,
            assistant_entry=previous_assistant_entry,
            assistant_text=previous_assistant_text,
        )

        list_recent_routes = getattr(
            session_manager,
            "list_recent_fixed_four_tier_decisions",
            None,
        )
        recent_routes = []
        if classifier_backend == "registered_model" and callable(list_recent_routes):
            history_before_ms = feature_route_before_ms
            history_limit = 5
            if parent_decision is not None:
                # The parent itself occupies the fifth slot below. Querying
                # through its millisecond would let unordered same-tick peers
                # crowd genuinely older routes out before we can filter them.
                history_before_ms = feature_route_before_ms - 1
                history_limit = 4
            if history_before_ms >= 0:
                recent_routes = list(
                    await list_recent_routes(
                        session_id=feature_session_id,
                        session_epoch=feature_session_epoch,
                        since_ms=max(0, feature_route_before_ms - 30 * 60 * 1_000),
                        before_ms=history_before_ms,
                        limit=history_limit,
                    )
                )
            if parent_decision is not None:
                parent_route_id = str(getattr(parent_decision, "route_id", "") or "")
                parent_decided_at_ms = int(parent_decision.decided_at_ms)
                recent_routes = [
                    route
                    for route in recent_routes
                    if str(getattr(route, "route_id", "") or "") != parent_route_id
                    and int(route.decided_at_ms) < parent_decided_at_ms
                ]
                # The redo boundary itself is route-before evidence for the
                # replacement request. Same-millisecond peers have no causal
                # order, so retain only strictly older records and append the
                # already authenticated parent explicitly.
                recent_routes.append(parent_decision)
                recent_routes = recent_routes[-5:]
        route_history: list[dict[str, Any]] = []
        for historical_route in recent_routes:
            history_entry: dict[str, Any] = {
                "tier_id": str(getattr(historical_route, "final_tier", "") or "").upper()
            }
            tier_audit = getattr(historical_route, "tier", None)
            probabilities = (
                tier_audit.get("probabilities") if isinstance(tier_audit, Mapping) else None
            )
            if isinstance(probabilities, Mapping):
                normalized_probabilities: dict[str, float] = {}
                for raw_tier, raw_probability in probabilities.items():
                    try:
                        probability = float(raw_probability)
                    except (TypeError, ValueError):
                        normalized_probabilities = {}
                        break
                    tier_name = str(raw_tier).upper()
                    if tier_name not in {"C0", "C1", "C2", "C3"} or not math.isfinite(probability):
                        normalized_probabilities = {}
                        break
                    normalized_probabilities[tier_name] = probability
                if set(normalized_probabilities) == {"C0", "C1", "C2", "C3"}:
                    ordered_probabilities = sorted(normalized_probabilities.values(), reverse=True)
                    history_entry["difficulty"] = sum(
                        index * normalized_probabilities[f"C{index}"] for index in range(4)
                    )
                    history_entry["margin"] = ordered_probabilities[0] - ordered_probabilities[1]
            route_history.append(history_entry)

        context: dict[str, Any] = {
            "turn_index": len(route_history),
            "context_tokens_est": (
                len(str(turn.semantic_message or turn.message or ""))
                + sum(len(value) for value in user_history)
                + len(previous_assistant_text or "")
            )
            // 4,
        }
        surface_kind = str(getattr(turn, "surface_kind", "") or "").strip()
        channel_kind = str(metadata.get("channel_kind") or "").strip()
        if surface_kind and surface_kind != "unknown":
            context["entrypoint"] = surface_kind
        if channel_kind:
            context["platform"] = channel_kind
        available_tools = sorted(
            {
                name
                for tool in (turn.tool_defs or [])
                if (
                    name := str(
                        tool.get("name") if isinstance(tool, Mapping) else getattr(tool, "name", "")
                    ).strip()
                )
            }
        )
        tool_state = {"available_tools": available_tools} if available_tools else {}
        normalized_modalities = normalize_attachment_modalities(turn.attachments or [])
        router_attachments: list[dict[str, Any]] = []
        if normalized_modalities is not None:
            for attachment, modality in zip(turn.attachments or [], normalized_modalities):
                safe_attachment: dict[str, Any] = {"type": modality}
                if isinstance(attachment, Mapping):
                    for key in (
                        "mime_type",
                        "media_type",
                        "parse_status",
                        "status",
                        "summary",
                        "truncated",
                        "token_count",
                    ):
                        value = attachment.get(key)
                        if isinstance(value, (str, int, float, bool)) or value is None:
                            safe_attachment[key] = value
                router_attachments.append(safe_attachment)
        route_request = RoutingRequest(
            session_id=durable_session_id,
            request_id=request_id,
            message=str(turn.semantic_message or turn.message or ""),
            # Redo classification reads the immutable parent source slice;
            # audit its source input id, while the decision record and claim
            # below remain bound to the new child input/execution.
            input_message_id=(
                feature_current_message_id if parent_decision is not None else bound_user_message_id
            ),
            task_anchor=task_anchor,
            user_history=user_history,
            previous_assistant_text=previous_assistant_text,
            previous_assistant_usage=previous_usage,
            previous_outcome=cast(Any, previous_outcome),
            route_history=tuple(route_history),
            context=context,
            tool_state=tool_state,
            attachments=tuple(router_attachments),
            attachment_count=len(turn.attachments or []),
            attachment_modalities=normalized_modalities,
            control_event=str(control_event) if control_event is not None else None,
        )
        claim_id = uuid.uuid4().hex
        request_claim = FixedFourTierRequestClaim(
            claim_id=claim_id,
            session_id=durable_session_id,
            session_key=turn.session_key,
            session_epoch=session_epoch,
            request_id=request_id,
            execution_id=execution_id,
            input_message_id=str(bound_user_message_id),
            claimed_at_ms=claimed_at_ms,
            updated_at_ms=claimed_at_ms,
            lease_expires_at_ms=lease_expires_at_ms,
        )

        async def _settle_claim_terminal(
            execution_status: str,
            error_code: str,
        ) -> None:
            await session_manager.settle_fixed_four_tier_request_claim(
                claim_id=claim_id,
                execution_status=execution_status,
                error_code=error_code,
            )

        try:
            acquired, existing_claim = await session_manager.claim_fixed_four_tier_request(
                request_claim
            )
        except asyncio.CancelledError:
            await _finish_required_cancel_cleanup(
                _settle_claim_terminal(
                    "cancelled",
                    "cancelled_during_request_claim",
                )
            )
            raise
        except FixedFourTierStateConflictError as exc:
            raise FixedFourTierRoutingError(
                "four_tier_mapping request claim raced with another execution",
                reason="duplicate_request_in_progress",
            ) from exc
        if not acquired:
            existing_status = str(getattr(existing_claim, "status", "") or "")
            raise FixedFourTierRoutingError(
                "four_tier_mapping request already has a durable execution claim",
                reason=(
                    "duplicate_request_in_progress"
                    if existing_status in {"claimed", "materialized"}
                    else "duplicate_request_replay"
                ),
            )

        def _load_and_decide_fixed_route() -> tuple[Any, Any]:
            # Loading and native inference are blocking.  The shared lock keeps
            # a hot config replacement from closing this runner mid-request.
            with self._fixed_four_tier_v2_router_lock:
                router = self._fixed_four_tier_v2_router_for_config(ensemble_cfg)
                return cast(tuple[Any, Any], router.decide(route_request, task_state))

        try:
            decision, next_task_state = await self._run_fixed_four_tier_v2_job(
                _load_and_decide_fixed_route
            )
            if parent_decision is not None:
                if not str(redo_child_task_start_message_id or "").strip():
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping regenerate child has no exact task boundary",
                        reason="redo_child_task_boundary_unavailable",
                    )
                next_task_state = replace(
                    next_task_state,
                    task_start_input_message_id=redo_child_task_start_message_id,
                )
        except BaseException as exc:
            terminal_status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                await _finish_required_cancel_cleanup(
                    _settle_claim_terminal(terminal_status, reason)
                )
            else:
                await _settle_claim_terminal(terminal_status, reason)
            raise

        def _build_staged_fixed_route() -> tuple[
            str,
            str,
            str,
            str,
            dict[str, Any],
            FixedFourTierDecisionRecord,
        ]:
            """Build every fallible post-claim object before persistence."""

            tier_cfg = tiers.get(decision.final_tier)
            target_provider = str(getattr(tier_cfg, "provider", "") or "").strip().casefold()
            target_model = str(getattr(tier_cfg, "model", "") or "").strip()
            reasoning = str(getattr(tier_cfg, "reasoning", "") or "").strip().casefold()
            deployment_version = str(getattr(tier_cfg, "deployment_version", "") or "").strip()
            route_trace = decision.trace(
                provider=target_provider or None,
                model=target_model or None,
            )
            route_trace.update(
                {
                    "session_id": durable_session_id,
                    "session_epoch": session_epoch,
                    "claim_id": claim_id,
                    "execution_id": execution_id,
                    "session_key_hash": hashlib.sha256(
                        canonicalize_session_key(turn.session_key).encode("utf-8")
                    ).hexdigest(),
                    "input_message_id": bound_user_message_id,
                    "redo_parent_route_id": redo_parent_route_id,
                    "task_start_input_message_id": (next_task_state.task_start_input_message_id),
                    "state_version_before": expected_state_version,
                    "attempt_id": None,
                    "attempt_ids": [],
                    "response_id": None,
                    "execution_status": "pending",
                    "state_committed": False,
                    "reasoning": reasoning or None,
                    "deployment_version": deployment_version or None,
                    "deployment_version_attested": False,
                    "execution_lease": {
                        "claimed_at_ms": claimed_at_ms,
                        "lease_expires_at_ms": lease_expires_at_ms,
                        "status": "materialized",
                    },
                    "preflight": {
                        "status": "pending",
                        "deployment_resolved": False,
                        "health_admission": "deferred_to_dispatch",
                    },
                    "dispatch": {
                        "health_admission": "deferred",
                        "physical_request_started": False,
                        "physical_request_count": 0,
                    },
                }
            )
            staged_record = FixedFourTierDecisionRecord(
                route_id=decision.route_id,
                session_id=durable_session_id,
                session_key=turn.session_key,
                session_epoch=session_epoch,
                claim_id=claim_id,
                request_id=request_id,
                execution_id=execution_id,
                input_message_id=bound_user_message_id,
                task_id=decision.task_id,
                redo_parent_route_id=redo_parent_route_id,
                decided_at_ms=decision.decided_at_ms,
                updated_at_ms=decision.decided_at_ms,
                intent=decision.intent.trace(),
                tier=decision.tier.trace(),
                previous_tier=decision.previous_tier,
                final_tier=decision.final_tier,
                task_turn_index=decision.task_turn_index,
                task_start_input_message_id=(next_task_state.task_start_input_message_id),
                context_action=decision.context_action,
                state_version_before=expected_state_version,
                selected_provider=target_provider or None,
                selected_model=target_model or None,
                reasoning=reasoning or None,
                deployment_version=deployment_version or None,
                config_version=SCHEMA_VERSION,
                route_trace=route_trace,
            )
            return (
                target_provider,
                target_model,
                reasoning,
                deployment_version,
                route_trace,
                staged_record,
            )

        try:
            (
                target_provider,
                target_model,
                reasoning,
                deployment_version,
                route_trace,
                staged_record,
            ) = _build_staged_fixed_route()
        except BaseException as exc:
            terminal_status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                await _finish_required_cancel_cleanup(
                    _settle_claim_terminal(terminal_status, reason)
                )
            else:
                await _settle_claim_terminal(terminal_status, reason)
            raise
        metadata.update(
            {
                "fixed_four_tier_v2_decision_id": decision.route_id,
                "fixed_four_tier_v2_decision": route_trace,
                "fixed_four_tier_v2_context_action": decision.context_action,
                "fixed_four_tier_v2_history_turns_to_keep": (decision.history_turns_to_keep),
                "fixed_four_tier_v2_task_start_input_message_id": (
                    next_task_state.task_start_input_message_id
                ),
                "fixed_four_tier_v2_task_id": decision.task_id,
                "fixed_four_tier_v2_selected_provider": target_provider or None,
                "fixed_four_tier_v2_selected_model": target_model or None,
                "router_single_decision_id": decision.route_id,
                "router_decision_id": decision.route_id,
            }
        )

        async def _settle_resolver_terminal(
            *,
            execution_status: str,
            error_code: str,
            preflight_status: str = "failed",
            detect_committed_state: bool = False,
        ) -> None:
            if detect_committed_state:
                try:
                    live_state = await session_manager.get_fixed_four_tier_state(durable_session_id)
                except Exception:
                    live_state = None
                route_trace["state_committed"] = bool(
                    live_state is not None
                    and str(getattr(live_state, "last_route_id", "") or "") == decision.route_id
                )
            preflight_value = route_trace.get("preflight")
            preflight_trace = (
                copy.deepcopy(dict(preflight_value)) if isinstance(preflight_value, Mapping) else {}
            )
            preflight_trace["status"] = preflight_status
            if preflight_status == "failed":
                preflight_trace["error_code"] = error_code
            route_trace["preflight"] = preflight_trace
            route_trace["execution_status"] = execution_status
            route_trace["error_code"] = error_code
            metadata["fixed_four_tier_v2_decision"] = route_trace
            try:
                await session_manager.settle_fixed_four_tier_decision(
                    route_id=decision.route_id,
                    execution_status=execution_status,
                    preflight_status=preflight_status,
                    error_code=error_code,
                    route_trace=route_trace,
                )
            finally:
                await _settle_claim_terminal(execution_status, error_code)

        try:
            await session_manager.stage_fixed_four_tier_decision(staged_record)
        except asyncio.CancelledError:
            await _finish_required_cancel_cleanup(
                _settle_resolver_terminal(
                    execution_status="cancelled",
                    error_code="cancelled_during_decision_persistence",
                )
            )
            raise
        except Exception as exc:
            try:
                await _settle_resolver_terminal(
                    execution_status="failed",
                    error_code="decision_persistence_failed",
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.persistence_settlement_failed",
                    route_id=decision.route_id,
                )
            raise FixedFourTierRoutingError(
                "four_tier_mapping could not persist its classification",
                reason="decision_persistence_failed",
            ) from exc

        try:
            if not target_provider or not target_model or reasoning not in {"thinking", "max"}:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected tier is incomplete",
                    reason="selected_tier_invalid",
                )
            if provider is None or cloned_selector is None:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping requires a configured provider selector",
                    reason="provider_selector_unavailable",
                )
            current_provider_config = getattr(cloned_selector, "current_config", None)
            if (
                current_provider_config is None
                or not str(getattr(current_provider_config, "provider", "") or "").strip()
                or not str(getattr(current_provider_config, "model", "") or "").strip()
            ):
                raise FixedFourTierRoutingError(
                    "four_tier_mapping provider selector has no complete current config",
                    reason="provider_selector_unavailable",
                )
            if self._model_catalog is None:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping requires an authoritative model catalog",
                    reason="model_catalog_unavailable",
                )

            current_provider = (
                str(getattr(current_provider_config, "provider", "") or "").strip().casefold()
            )
            if current_provider == target_provider:
                selected_config = replace(
                    current_provider_config,
                    model=target_model,
                    provider_routing=dict(
                        getattr(current_provider_config, "provider_routing", {}) or {}
                    ),
                    replay_provider_state=False,
                )
                metadata["routed_provider_resolution"] = {
                    "provider": target_provider,
                    "model": target_model,
                    "ready": True,
                    "reason": "inherited_provider",
                    "credential_source": "inherited_provider",
                    "endpoint_source": "inherited_provider",
                }
            else:
                selected_config = resolve_tier_provider_config(
                    turn_config,
                    target_provider,
                    target_model,
                    session_key=turn.session_key,
                    turn_metadata=metadata,
                )
            if selected_config is None:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected deployment is unavailable",
                    reason="selected_deployment_unavailable",
                )

            llm_cfg = getattr(turn_config, "llm", None)
            user_max_tokens = int(getattr(llm_cfg, "max_tokens", 0) or 0)
            user_context_window = int(getattr(llm_cfg, "context_window_tokens", 0) or 0)
            max_tokens = self._model_catalog.resolve_max_tokens(
                target_model,
                user_override=user_max_tokens,
                provider=target_provider,
            )
            context_window, context_window_source = resolve_effective_context_window(
                self._model_catalog,
                target_model,
                provider=target_provider,
                global_override=user_context_window,
            )
            capabilities = self._model_catalog.get_capabilities(
                target_model,
                provider_name=target_provider,
                base_url=str(getattr(selected_config, "base_url", "") or ""),
            )
            if (
                getattr(capabilities, "supports_reasoning", False) is not True
                and target_provider == "openrouter"
                and target_model
                in {
                    "deepseek/deepseek-v4-flash",
                    "deepseek/deepseek-v4-pro",
                }
            ):
                capabilities = replace(
                    capabilities,
                    supports_reasoning=True,
                    reasoning_format="openrouter",
                )
            if getattr(capabilities, "supports_reasoning", False) is not True:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected model lacks required reasoning support",
                    reason="selected_model_reasoning_unavailable",
                )
            has_image = any(
                str(
                    attachment.get("mime")
                    or attachment.get("mime_type")
                    or attachment.get("type")
                    or ""
                )
                .strip()
                .casefold()
                .startswith(("image/", "image"))
                for attachment in (turn.attachments or [])
                if isinstance(attachment, Mapping)
            )
            if has_image and getattr(capabilities, "supports_vision", False) is not True:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected model cannot process image input",
                    reason="selected_model_vision_unavailable",
                )
            if turn.tool_defs and getattr(capabilities, "supports_tools", False) is not True:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected model cannot execute tools",
                    reason="selected_model_tools_unavailable",
                )

            current_input_tokens = metadata.get("material_estimated_tokens")
            if not isinstance(current_input_tokens, int) or isinstance(current_input_tokens, bool):
                current_input_tokens = max(1, (len(turn.semantic_message) + 3) // 4)
            from opensquilla.session.compaction import (
                estimate_entry_model_replay_tokens,
            )

            transcript = list(await session_manager.get_transcript(turn.session_key))
            task_start_message_id = next_task_state.task_start_input_message_id
            scoped_transcript: list[Any] = []
            if decision.history_turns_to_keep > 0:
                boundary_index = next(
                    (
                        index
                        for index, entry in enumerate(transcript)
                        if getattr(entry, "message_id", None) == task_start_message_id
                    ),
                    None,
                )
                if boundary_index is None:
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping task history boundary is unavailable",
                        reason="task_history_boundary_unavailable",
                    )
                bound_index = next(
                    (
                        index
                        for index, entry in enumerate(transcript)
                        if getattr(entry, "message_id", None) == bound_user_message_id
                    ),
                    None,
                )
                if bound_index is None or bound_index < boundary_index:
                    raise FixedFourTierRoutingError(
                        "four_tier_mapping current input boundary is unavailable",
                        reason="current_input_boundary_unavailable",
                    )
                # Exclude the current persisted row and every later queued row;
                # the current semantic message is counted separately below.
                scoped_transcript = transcript[boundary_index:bound_index]
            history_tokens = sum(
                estimate_entry_model_replay_tokens(entry) for entry in scoped_transcript
            )
            system_tokens = max(
                1,
                (
                    len(
                        json.dumps(
                            turn.system_prompt,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        )
                    )
                    + 3
                )
                // 4,
            )
            tool_schema_tokens = (
                max(
                    1,
                    (
                        len(
                            json.dumps(
                                [
                                    (
                                        tool.model_dump(mode="json")
                                        if callable(getattr(tool, "model_dump", None))
                                        else vars(tool)
                                    )
                                    for tool in (turn.tool_defs or [])
                                ],
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )
                        )
                        + 3
                    )
                    // 4,
                )
                if turn.tool_defs
                else 0
            )
            estimated_input_tokens = (
                current_input_tokens + history_tokens + system_tokens + tool_schema_tokens
            )
            estimated_total_tokens = estimated_input_tokens + max_tokens
            if estimated_total_tokens >= context_window:
                raise FixedFourTierRoutingError(
                    "four_tier_mapping selected model context window is too small",
                    reason="context_length_exceeded",
                )

            direct_selector = ModelSelector(SelectorConfig(primary=selected_config, fallbacks=[]))
            guarded_provider = _RouterSingleDirectProvider(
                direct_selector.resolve(),
                selected_config,
                health_ledger=get_provider_health_ledger(),
                absolute_deadline=turn_absolute_deadline,
                frozen_catalog={
                    "provider": target_provider,
                    "model": target_model,
                    "max_tokens": max_tokens,
                    "context_window": context_window,
                    "capabilities": capabilities,
                },
                enforces_routed_thinking_policy=True,
                turn_metadata=metadata,
                deployment_version=deployment_version,
            )
            direct_provider = _SelectorFallbackProvider(
                guarded_provider,
                direct_selector,
                turn_metadata=metadata,
            )
        except asyncio.CancelledError:
            await _finish_required_cancel_cleanup(
                _settle_resolver_terminal(
                    execution_status="cancelled",
                    error_code="cancelled_during_preflight",
                )
            )
            raise
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__).strip()
            try:
                await _settle_resolver_terminal(
                    execution_status="failed",
                    error_code=reason,
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.preflight_settlement_failed",
                    route_id=decision.route_id,
                )
            raise

        try:
            route_trace["preflight"] = {
                "status": "passed",
                "deployment_resolved": True,
                "health_admission": "deferred_to_dispatch",
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_history_tokens": history_tokens,
                "estimated_system_tokens": system_tokens,
                "estimated_tool_schema_tokens": tool_schema_tokens,
                "reserved_output_tokens": max_tokens,
                "estimated_total_tokens": estimated_total_tokens,
                "context_window_tokens": context_window,
                "context_window_source": context_window_source,
            }
            durable_next_state = FixedFourTierState(
                session_id=durable_session_id,
                session_key=turn.session_key,
                session_epoch=session_epoch,
                version=next_task_state.version,
                task_id=next_task_state.task_id,
                tier=next_task_state.tier,
                task_turn_count=next_task_state.turn_count,
                task_start_input_message_id=(next_task_state.task_start_input_message_id),
                last_request_id=request_id,
                last_route_id=decision.route_id,
                updated_at_ms=time.time_ns() // 1_000_000,
            )
            route_trace["state_committed"] = True
            route_trace["state_version_after"] = durable_next_state.version
            await session_manager.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=durable_next_state,
                expected_version=expected_state_version,
                route_trace=route_trace,
                updated_at_ms=durable_next_state.updated_at_ms,
            )
        except asyncio.CancelledError:
            await _finish_required_cancel_cleanup(
                _settle_resolver_terminal(
                    execution_status="cancelled",
                    error_code="cancelled_during_state_commit",
                    preflight_status="passed",
                    detect_committed_state=True,
                )
            )
            raise
        except FixedFourTierStateConflictError as exc:
            route_trace["state_committed"] = False
            try:
                await _settle_resolver_terminal(
                    execution_status="failed",
                    preflight_status="failed",
                    error_code="task_state_conflict",
                    detect_committed_state=True,
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.conflict_settlement_failed",
                    route_id=decision.route_id,
                )
            raise FixedFourTierRoutingError(
                "four_tier_mapping task state changed during routing",
                reason="task_state_conflict",
            ) from exc
        except Exception as exc:
            try:
                await _settle_resolver_terminal(
                    execution_status="failed",
                    error_code="state_commit_failed",
                    preflight_status="passed",
                    detect_committed_state=True,
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.commit_settlement_failed",
                    route_id=decision.route_id,
                )
            raise FixedFourTierRoutingError(
                "four_tier_mapping could not commit task state",
                reason="state_commit_failed",
            ) from exc

        baseline_model = str(turn.model or "")
        try:
            cloned_selector.override_provider_config(selected_config)
            turn.model = target_model
        except Exception as exc:
            try:
                await _settle_resolver_terminal(
                    execution_status="failed",
                    error_code="provider_activation_failed",
                    preflight_status="passed",
                    detect_committed_state=True,
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.activation_settlement_failed",
                    route_id=decision.route_id,
                )
            raise FixedFourTierRoutingError(
                "four_tier_mapping could not activate its selected provider",
                reason="provider_activation_failed",
            ) from exc
        thinking_level = "high" if reasoning == "thinking" else "max"
        metadata.update(
            {
                "_router_single_provider_finalized": True,
                "_router_single_frozen_catalog": {
                    "provider": target_provider,
                    "model": target_model,
                    "max_tokens": max_tokens,
                    "context_window": context_window,
                },
                "_router_single_managed_provider_thinking_level": thinking_level,
                "_fixed_four_tier_v2_provider_finalized": True,
                "fixed_four_tier_v2_decision_id": decision.route_id,
                "fixed_four_tier_v2_decision": route_trace,
                "fixed_four_tier_v2_context_action": decision.context_action,
                "fixed_four_tier_v2_history_turns_to_keep": (decision.history_turns_to_keep),
                "fixed_four_tier_v2_task_start_input_message_id": (
                    next_task_state.task_start_input_message_id
                ),
                "fixed_four_tier_v2_task_id": decision.task_id,
                "fixed_four_tier_v2_selected_provider": target_provider,
                "fixed_four_tier_v2_selected_model": target_model,
                "router_single_decision_id": decision.route_id,
                "router_decision_id": decision.route_id,
                "baseline_model": baseline_model,
                "routed_tier": decision.final_tier,
                "routed_model": target_model,
                "routed_provider": target_provider,
                "routing_applied": True,
                "routing_confidence": (
                    decision.tier.confidence
                    if decision.tier.confidence is not None
                    else decision.intent.confidence
                ),
                "routing_source": MODE,
                "rollout_phase": "full",
                "applied_model": target_model,
                "router_fallback_chain": [],
                "route_max_history_turns": decision.history_turns_to_keep,
                "thinking_requested": True,
                "thinking_level": thinking_level,
                "requested_provider": target_provider,
                "requested_model": target_model,
                "routed_provider_applied": target_provider,
                "resolved_model": target_model,
                "alias_resolution_chain": [target_model],
                "provider_after_rewrite": target_provider,
            }
        )
        # Initial pipeline metadata describes the pre-route provider; neither
        # it nor this resolver may claim physical execution before chat starts.
        metadata.pop("executed_provider", None)
        metadata.pop("executed_model", None)
        return direct_provider

    @staticmethod
    def _fixed_four_tier_v2_response_binding_context(
        turn: Any | None,
        *,
        execution_status: str,
        error_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Build the content-free binding persisted with a four_tier_mapping response."""

        if turn is None:
            return None
        metadata = getattr(turn, "metadata", None)
        if not isinstance(metadata, Mapping):
            return None
        trace = metadata.get("fixed_four_tier_v2_decision")
        if not isinstance(trace, Mapping):
            return None
        execution_id = str(trace.get("execution_id") or "").strip()
        route_id = str(metadata.get("fixed_four_tier_v2_decision_id") or "").strip()
        request_id = str(trace.get("request_id") or "").strip()
        if not execution_id or not route_id or not request_id:
            raise RuntimeError("four_tier_mapping response binding identity is incomplete")
        return {
            "schema": "fixed_four_tier_v2_response_binding_v1",
            "execution_id": execution_id,
            "route_id": route_id,
            "request_id": request_id,
            "execution_status": execution_status,
            "error_code": error_code,
        }

    async def _settle_fixed_four_tier_v2_route(
        self,
        turn: Any | None,
        *,
        execution_status: str,
        response_id: str | None = None,
        error_code: str | None = None,
        done_event: Any | None = None,
        required: bool = False,
    ) -> bool:
        """Settle an already committed route, retrying transient write gaps."""

        if turn is None or self._session_manager is None:
            return True
        metadata = getattr(turn, "metadata", None)
        if not isinstance(metadata, dict):
            return True
        route_id = str(metadata.get("fixed_four_tier_v2_decision_id") or "").strip()
        trace_value = metadata.get("fixed_four_tier_v2_decision")
        if not route_id or not isinstance(trace_value, Mapping):
            return True
        settle = getattr(
            self._session_manager,
            "settle_fixed_four_tier_decision",
            None,
        )
        usage_ids = getattr(
            self._session_manager,
            "get_usage_event_ids_for_turn",
            None,
        )
        if not callable(settle):
            if required:
                raise RuntimeError("four_tier_mapping terminal settlement is unavailable")
            return False

        route_trace = copy.deepcopy(dict(trace_value))
        session_id = str(route_trace.get("session_id") or "").strip()
        execution_id = str(route_trace.get("execution_id") or "").strip()
        attempt_ids: list[str] = []
        if session_id and execution_id and callable(usage_ids):
            try:
                attempt_ids = await usage_ids(
                    session_id=session_id,
                    turn_id=execution_id,
                )
            except Exception:
                log.exception(
                    "fixed_four_tier_v2.attempt_link_failed",
                    route_id=route_id,
                )
        route_trace["attempt_id"] = attempt_ids[0] if attempt_ids else None
        route_trace["attempt_ids"] = attempt_ids
        route_trace["execution_status"] = execution_status
        lease_value = route_trace.get("execution_lease")
        execution_lease = (
            copy.deepcopy(dict(lease_value)) if isinstance(lease_value, Mapping) else {}
        )
        execution_lease["status"] = execution_status
        execution_lease["terminal_at_ms"] = time.time_ns() // 1_000_000
        route_trace["execution_lease"] = execution_lease
        if response_id is not None:
            route_trace["response_id"] = response_id
        if error_code is not None:
            route_trace["error_code"] = error_code
        dispatch_value = route_trace.get("dispatch")
        dispatch = dict(dispatch_value) if isinstance(dispatch_value, Mapping) else {}
        physical_started = dispatch.get("physical_request_started") is True
        if done_event is not None:
            physical_started = True
        if physical_started:
            executed_provider = str(
                getattr(done_event, "provider", "")
                or metadata.get("executed_provider")
                or dispatch.get("executed_provider")
                or ""
            ).strip()
            executed_model = str(
                getattr(done_event, "model", "")
                or metadata.get("executed_model")
                or dispatch.get("executed_model")
                or ""
            ).strip()
            route_trace["executed_provider"] = executed_provider or None
            route_trace["executed_model"] = executed_model or None
        else:
            route_trace["executed_provider"] = None
            route_trace["executed_model"] = None
        if done_event is not None:
            # Provider adapters expose ``input_tokens`` as the total input
            # envelope, including cache reads/writes.  Keep those raw counters
            # intact and add the mutually-exclusive billing buckets required
            # for route audit/reconciliation.
            raw_input_tokens = int(getattr(done_event, "input_tokens", 0) or 0)
            raw_output_tokens = int(getattr(done_event, "output_tokens", 0) or 0)
            raw_reasoning_tokens = int(getattr(done_event, "reasoning_tokens", 0) or 0)
            raw_cache_read_tokens = int(getattr(done_event, "cached_tokens", 0) or 0)
            raw_cache_write_tokens = int(getattr(done_event, "cache_write_tokens", 0) or 0)
            input_tokens = max(0, raw_input_tokens)
            output_tokens = max(0, raw_output_tokens)
            reasoning_tokens = max(0, raw_reasoning_tokens)
            cache_read_tokens = min(
                input_tokens,
                max(0, raw_cache_read_tokens),
            )
            cache_write_tokens = min(
                input_tokens - cache_read_tokens,
                max(0, raw_cache_write_tokens),
            )
            normal_input_tokens = input_tokens - cache_read_tokens - cache_write_tokens
            raw_input_buckets_reconcile = (
                raw_input_tokens >= 0
                and raw_cache_read_tokens >= 0
                and raw_cache_write_tokens >= 0
                and raw_cache_read_tokens + raw_cache_write_tokens <= raw_input_tokens
            )
            route_trace["provider_usage"] = {
                # These top-level counters preserve the provider adapter's raw
                # aggregate.  Do not silently repair inconsistent receipts.
                "input_tokens": raw_input_tokens,
                "output_tokens": raw_output_tokens,
                "reasoning_tokens": raw_reasoning_tokens,
                "cache_read_tokens": raw_cache_read_tokens,
                "cache_write_tokens": raw_cache_write_tokens,
                "cost_usd": float(getattr(done_event, "cost_usd", 0.0) or 0.0),
                "billed_cost_usd": float(getattr(done_event, "billed_cost", 0.0) or 0.0),
                "cost_source": str(getattr(done_event, "cost_source", "none") or "none"),
                "provider": str(getattr(done_event, "provider", "") or "") or None,
                "model": str(getattr(done_event, "model", "") or "") or None,
                "requested_provider": str(getattr(done_event, "requested_provider", "") or "")
                or None,
                "requested_model": str(getattr(done_event, "requested_model", "") or "") or None,
                "normalized_billing_buckets": {
                    "normal_input_tokens": normal_input_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "output_tokens": output_tokens,
                    # Reasoning is an output detail unless the physical
                    # provider receipt explicitly reports separate billing.
                    "reasoning_tokens_detail": reasoning_tokens,
                    "input_tokens_total": input_tokens,
                    "input_buckets_reconcile": raw_input_buckets_reconcile,
                    "normalization_anomaly": not raw_input_buckets_reconcile,
                },
            }
            usage_ledger = getattr(done_event, "model_usage_ledger", None)
            if isinstance(usage_ledger, list) and usage_ledger:
                route_trace["provider_usage"]["physical_ledger"] = copy.deepcopy(
                    [row for row in usage_ledger if isinstance(row, Mapping)]
                )
            provider_native_usage = getattr(done_event, "provider_usage", None)
            if isinstance(provider_native_usage, Mapping):
                route_trace["provider_usage"]["provider_native_usage"] = (
                    _fixed_route_provider_native_usage(provider_native_usage)
                )
        metadata["fixed_four_tier_v2_decision"] = route_trace
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                updated = await settle(
                    route_id=route_id,
                    execution_status=execution_status,
                    response_id=response_id,
                    error_code=error_code,
                    route_trace=route_trace,
                )
                if updated is True:
                    metadata.pop(
                        "fixed_four_tier_v2_terminal_settlement_pending",
                        None,
                    )
                    return True
                last_error = RuntimeError("four_tier_mapping terminal decision was not available")
            except Exception as exc:  # noqa: BLE001 - bounded durable retry
                last_error = exc
            if attempt < 2:
                await asyncio.sleep(0)

        metadata["fixed_four_tier_v2_terminal_settlement_pending"] = {
            "route_id": route_id,
            "execution_status": execution_status,
            "response_id": response_id,
            "error_code": error_code,
            "recorded_at_ms": time.time_ns() // 1_000_000,
        }
        log.error(
            "fixed_four_tier_v2.terminal_settlement_failed",
            route_id=route_id,
            execution_status=execution_status,
            error_type=type(last_error).__name__ if last_error is not None else "unknown",
        )
        if required:
            raise RuntimeError(
                "four_tier_mapping terminal audit persistence failed"
            ) from last_error
        return False

    async def _resolve_router_single_provider(
        self,
        *,
        turn: Any,
        provider: Any,
        cloned_selector: Any,
        turn_config: Any,
        ensemble_cfg: Any,
        turn_absolute_deadline: float | None,
        usage_execution_context: UsageExecutionContext | None = None,
    ) -> Any:
        """Resolve router_dynamic Top-1 onto an ordinary provider."""

        from opensquilla.engine.routing.health import get_provider_health_ledger
        from opensquilla.engine.selector_override import acquire_profile_credential
        from opensquilla.provider.ensemble import resolve_router_single_route
        from opensquilla.provider.ranking_router import (
            RANKING_CONFIG_SCHEMA_VERSION,
            DynamicRankingError,
            TaskAnalyzerCandidate,
            _prepare_effective_ranking_config,
            analyze_task_with_fallback_chain,
            analyze_task_with_provider,
            build_single_model_request_context,
            ranking_config_snapshot,
            task_analyzer_chain_policy,
            task_analyzer_policy,
        )
        from opensquilla.provider.selector import ModelSelector, SelectorConfig

        if provider is None or cloned_selector is None:
            raise DynamicRankingError(
                "router_single requires a configured provider selector",
                reason="router_single_provider_selector_unavailable",
            )
        current_provider_config = getattr(cloned_selector, "current_config", None)
        if (
            current_provider_config is None
            or not str(getattr(current_provider_config, "provider", "") or "").strip()
            or not str(getattr(current_provider_config, "model", "") or "").strip()
        ):
            raise DynamicRankingError(
                "router_single provider selector has no complete current config",
                reason="router_single_provider_selector_unavailable",
            )
        if self._model_catalog is None:
            raise DynamicRankingError(
                "router_single requires an authoritative model catalog",
                reason="router_single_model_catalog_unavailable",
            )

        prepared_ranking_config = getattr(
            ensemble_cfg,
            "prepared_ranking_config",
            None,
        )
        frozen_resolution_snapshot = getattr(
            ensemble_cfg,
            "ranking_config_resolution_snapshot",
            None,
        )
        if callable(prepared_ranking_config):
            ranking_config = prepared_ranking_config()
            if not isinstance(ranking_config, Mapping):
                raise DynamicRankingError("prepared router_single ranking config is unavailable")
            thinking_policy = ranking_config.get("thinking_assignment")
            thinking_assignment_enabled = bool(
                ranking_config.get("schema_version") == RANKING_CONFIG_SCHEMA_VERSION
                and isinstance(thinking_policy, Mapping)
                and thinking_policy.get("enabled") is True
            )
        elif callable(frozen_resolution_snapshot):
            frozen_resolution = frozen_resolution_snapshot()
            ranking_config = frozen_resolution.get("effective_config")
            if not isinstance(ranking_config, Mapping):
                raise DynamicRankingError("frozen router_single ranking config is unavailable")
            thinking_assignment_enabled = (
                frozen_resolution.get("thinking_assignment_enabled") is True
            )
        else:
            thinking_assignment_enabled = bool(
                getattr(
                    ensemble_cfg,
                    "ranking_thinking_assignment_enabled",
                    False,
                )
            )
            ranking_config = ranking_config_snapshot(
                thinking_assignment_enabled=thinking_assignment_enabled,
                override=(getattr(ensemble_cfg, "ranking_config_override", None) or None),
            )
        ranking_config = _prepare_effective_ranking_config(
            ranking_config,
            thinking_assignment_enabled=thinking_assignment_enabled,
        )
        cache_affinity_policy: _RouterDynamicCacheAffinityPolicy | None = None
        cache_affinity_session_epoch: int | None = None
        cache_continuity_available = False
        cache_affinity_receipts: tuple[_RouterDynamicCacheAffinityReceipt, ...] = ()
        cache_affinity_generation = 0
        cache_affinity_now_monotonic: float | None = None
        cache_affinity_outer_thinking_projection: dict[str, bool | str | int] | None = None
        cache_affinity_turn_id = ""
        decision_id = str(turn.metadata.get("router_decision_id") or uuid.uuid4().hex)
        ranking_session = ranking_config.get("session")
        if isinstance(ranking_session, Mapping) and isinstance(
            ranking_session.get("kv_cache_affinity"),
            Mapping,
        ):
            cache_affinity_policy = _router_dynamic_cache_affinity_policy(
                ranking_config,
                topology="single",
            )
        if cache_affinity_policy is not None:
            cache_affinity_outer_thinking_projection = (
                self._router_dynamic_outer_thinking_projection(turn)
            )
            self._ensure_router_dynamic_cache_compaction_listener(
                route_cache_max_entries=(cache_affinity_policy.route_cache_max_entries),
            )
            cache_affinity_session_epoch = await self._resolve_router_dynamic_session_epoch(
                turn.session_key
            )
            if cache_affinity_session_epoch is None:
                self._invalidate_router_dynamic_cache_affinity(
                    session_key=turn.session_key,
                    reason="session_epoch_unavailable",
                )
            else:
                cache_affinity_now_monotonic = time.monotonic()
                (
                    cache_continuity_available,
                    cache_affinity_receipts,
                    cache_affinity_generation,
                ) = self._router_single_cache_continuity_snapshot(
                    session_key=turn.session_key,
                    session_epoch=cache_affinity_session_epoch,
                    policy=cache_affinity_policy,
                    now=cache_affinity_now_monotonic,
                )
                # Register an empty terminal sidecar before any Analyzer or
                # ranking work. If that work fails, the turn-level exception
                # cleanup can still invalidate the previous successful
                # affinity evidence for this topology.
                cache_affinity_turn_id = str(
                    getattr(usage_execution_context, "turn_id", "")
                    or getattr(usage_execution_context, "execution_id", "")
                    or uuid.uuid4().hex
                )
                turn.metadata["router_single_decision_id"] = decision_id
                self._register_router_dynamic_cache_sidecar(
                    context=_RouterDynamicCacheAffinityCollectionContext(
                        turn_id=cache_affinity_turn_id,
                        decision_id=decision_id,
                        provider_instance_token=uuid.uuid4().hex,
                        provider_instance_generation=0,
                        session_key=turn.session_key,
                        session_epoch=cache_affinity_session_epoch,
                        selection_generation=cache_affinity_generation,
                        topology="single",
                    ),
                    policy=cache_affinity_policy,
                )
        analyzer_policy = task_analyzer_policy(ranking_config)
        analyzer_chain = task_analyzer_chain_policy(ranking_config)
        analyzer_provider_id = str(analyzer_policy["provider"])
        analyzer_model_id = str(analyzer_policy["model"])
        routing_extra = turn.metadata.get("routing_extra")
        routing_extra_map = routing_extra if isinstance(routing_extra, Mapping) else {}
        routed_tier = str(
            turn.metadata.get("routed_tier")
            or routing_extra_map.get("final_tier")
            or routing_extra_map.get("base_tier")
            or "c1"
        )
        try:
            routing_confidence = float(turn.metadata.get("routing_confidence") or 0.0)
        except (TypeError, ValueError):
            routing_confidence = 0.0
        configured_output_tokens = int(
            getattr(getattr(turn_config, "llm", None), "max_tokens", 0) or 0
        )
        if configured_output_tokens <= 0:
            context_policy = ranking_config.get("context")
            context_policy_map = context_policy if isinstance(context_policy, Mapping) else {}
            output_policy = context_policy_map.get("output_budget")
            output_policy_map = output_policy if isinstance(output_policy, Mapping) else {}
            configured_output_tokens = output_policy_map.get("default_tokens")
            if (
                isinstance(configured_output_tokens, bool)
                or not isinstance(configured_output_tokens, int)
                or configured_output_tokens <= 0
            ):
                raise DynamicRankingError(
                    "router_single analyzer output budget is unavailable",
                    reason="router_single_output_budget_unavailable",
                )
        request_context = build_single_model_request_context(
            message=turn.semantic_message,
            turn_metadata=turn.metadata,
            attachments=turn.attachments,
            output_tokens=configured_output_tokens,
            ranking_config=ranking_config,
        )
        user_profile_enabled = bool(
            getattr(
                ensemble_cfg,
                "ranking_user_profile_enabled",
                False,
            )
        )
        user_profile = (
            self._resolve_user_profile(ranking_config, turn_config)
            if user_profile_enabled
            else None
        )
        analyzer_admission_controller = None
        analyzer_admission_deadline = None
        admission_config = getattr(ensemble_cfg, "admission", None)
        if (
            str(getattr(ensemble_cfg, "latency_class", "normal") or "normal") != "experiment"
            and admission_config is not None
            and bool(getattr(admission_config, "enabled", True))
        ):
            from opensquilla.provider.admission import (
                get_shared_provider_admission_controller,
                provider_admission_settings_from_config,
            )

            analyzer_admission_controller = get_shared_provider_admission_controller(
                provider_admission_settings_from_config(admission_config)
            )
            analyzer_admission_timeout = float(
                analyzer_chain["total_timeout_seconds"]
                if analyzer_chain["configured"]
                else analyzer_policy["timeout_seconds"]
            )
            analyzer_admission_deadline = time.monotonic() + analyzer_admission_timeout
            if turn_absolute_deadline is not None:
                analyzer_admission_deadline = min(
                    analyzer_admission_deadline,
                    turn_absolute_deadline,
                )
        allow_canary_analyzer_route = bool(
            str(getattr(ensemble_cfg, "latency_class", "normal") or "normal").strip().casefold()
            == "experiment"
        )
        if analyzer_chain["configured"]:
            analyzer_candidates = [
                TaskAnalyzerCandidate(
                    provider=self._router_dynamic_task_analyzer_provider(
                        current_provider_config,
                        session_key=turn.session_key,
                        ranking_config=ranking_config,
                        analyzer_route=route,
                        allow_canary_route=allow_canary_analyzer_route,
                    ),
                    provider_id=str(route["provider"]),
                    model_id=str(route["model"]),
                    upstream_provider=str(route["upstream_provider"]),
                )
                for route in analyzer_chain["routes"]
            ]
            analyzer_cache_kwargs: dict[str, Any] = {}
            if _accepts_keyword_arg(
                analyze_task_with_fallback_chain,
                "cache_continuity_available",
            ):
                analyzer_cache_kwargs["cache_continuity_available"] = cache_continuity_available
            task_analysis = await analyze_task_with_fallback_chain(
                candidates=analyzer_candidates,
                message=turn.semantic_message,
                user_profile_enabled=user_profile is not None,
                request_context=request_context,
                routed_tier=routed_tier,
                routing_confidence=routing_confidence,
                usage_tracker=self._usage_tracker,
                session_key=turn.session_key,
                ranking_config=ranking_config,
                decision_id=decision_id,
                absolute_deadline=turn_absolute_deadline,
                admission_controller=analyzer_admission_controller,
                admission_deadline=analyzer_admission_deadline,
                **analyzer_cache_kwargs,
            )
        else:
            analyzer_provider = self._router_dynamic_task_analyzer_provider(
                current_provider_config,
                session_key=turn.session_key,
                ranking_config=ranking_config,
                allow_canary_route=allow_canary_analyzer_route,
            )
            analyzer_cache_kwargs = {}
            if _accepts_keyword_arg(
                analyze_task_with_provider,
                "cache_continuity_available",
            ):
                analyzer_cache_kwargs["cache_continuity_available"] = cache_continuity_available
            task_analysis = await analyze_task_with_provider(
                provider=analyzer_provider,
                message=turn.semantic_message,
                user_profile_enabled=user_profile is not None,
                request_context=request_context,
                routed_tier=routed_tier,
                routing_confidence=routing_confidence,
                usage_tracker=self._usage_tracker,
                session_key=turn.session_key,
                analyzer_provider_id=analyzer_provider_id,
                analyzer_model_id=analyzer_model_id,
                ranking_config=ranking_config,
                decision_id=decision_id,
                admission_controller=analyzer_admission_controller,
                admission_deadline=analyzer_admission_deadline,
                _absolute_deadline=turn_absolute_deadline,
                **analyzer_cache_kwargs,
            )

        # Analyzer latency must not extend receipt lifetime. Intent continuity
        # uses the pre-Analyzer snapshot, while ranking re-evaluates TTL/decay
        # against a fresh monotonic instant immediately before mapping.
        if cache_affinity_session_epoch is not None:
            cache_affinity_now_monotonic = time.monotonic()
        provider_health_ledger = get_provider_health_ledger()
        ranking_inputs: dict[str, Any] = {
            "decision_id": decision_id,
            "task_analysis": task_analysis,
            "user_profile": user_profile,
            "request_context": request_context,
            "ranking_config": ranking_config,
        }
        if cache_affinity_policy is not None:
            ranking_inputs.update(
                {
                    "cache_continuity_available": cache_continuity_available,
                    "cache_affinity_receipts": cache_affinity_receipts,
                    "cache_affinity_policy": cache_affinity_policy.source,
                    "cache_affinity_session_epoch": cache_affinity_session_epoch,
                    "cache_affinity_now_monotonic": cache_affinity_now_monotonic,
                    "cache_affinity_price_quote_resolver": (
                        _router_dynamic_cache_price_quote_resolver
                    ),
                    "cache_affinity_outer_thinking_projection": (
                        cache_affinity_outer_thinking_projection
                    ),
                }
            )
        route = resolve_router_single_route(
            config=turn_config,
            inherited_provider_config=current_provider_config,
            turn_metadata=turn.metadata,
            ranking_inputs=ranking_inputs,
            requires_tools=bool(turn.tool_defs),
            credential_pool_acquirer=acquire_profile_credential,
            session_key=turn.session_key,
            provider_health_ledger=provider_health_ledger,
            model_catalog=self._model_catalog,
        )

        affinity_enabled = bool(cache_affinity_turn_id)

        def _materialize_route(
            selected_route: Any,
            *,
            selection_generation: int,
        ) -> Any:
            selected_config = selected_route.provider_config
            direct_selector = ModelSelector(SelectorConfig(primary=selected_config, fallbacks=[]))
            affinity_context: _RouterDynamicCacheAffinityCollectionContext | None = None
            affinity_sink: Callable[[_RouterDynamicCacheAffinityReceiptBatch], None] | None = None
            affinity_generation_getter: Callable[[], int] | None = None
            if affinity_enabled:
                assert cache_affinity_policy is not None
                assert cache_affinity_session_epoch is not None
                provider_instance_token = uuid.uuid4().hex
                affinity_context = _RouterDynamicCacheAffinityCollectionContext(
                    turn_id=cache_affinity_turn_id,
                    decision_id=decision_id,
                    provider_instance_token=provider_instance_token,
                    provider_instance_generation=0,
                    session_key=turn.session_key,
                    session_epoch=cache_affinity_session_epoch,
                    selection_generation=selection_generation,
                    topology="single",
                )
                affinity_sidecar_key = self._register_router_dynamic_cache_sidecar(
                    context=affinity_context,
                    policy=cache_affinity_policy,
                )

                def _stage_affinity(
                    batch: _RouterDynamicCacheAffinityReceiptBatch,
                    *,
                    _key: tuple[str, str] = affinity_sidecar_key,
                ) -> None:
                    self._stage_router_dynamic_cache_affinity_batch(_key, batch)

                affinity_sink = _stage_affinity

                def _current_affinity_generation() -> int:
                    return self._router_dynamic_cache_generation(turn.session_key)

                affinity_generation_getter = _current_affinity_generation
            resolved_provider = _RouterSingleDirectProvider(
                direct_selector.resolve(),
                selected_config,
                health_ledger=provider_health_ledger,
                absolute_deadline=turn_absolute_deadline,
                frozen_catalog={
                    "provider": selected_config.provider,
                    "model": selected_config.model,
                    "max_tokens": selected_route.direct_output_tokens,
                    "context_window": selected_route.context_window_tokens,
                    "capabilities": selected_route.model_capabilities,
                },
                enforces_routed_thinking_policy=bool(selected_route.thinking_policy_version),
                cache_affinity_context=affinity_context,
                cache_affinity_receipt_sink=affinity_sink,
                cache_affinity_generation_getter=affinity_generation_getter,
                cache_affinity_actual_model_aliases=getattr(
                    selected_route,
                    "actual_model_aliases",
                    (),
                ),
                cache_affinity_credential_namespace_token=getattr(
                    selected_route,
                    "credential_namespace_token",
                    None,
                ),
            )
            resolved_provider = _SelectorFallbackProvider(
                resolved_provider,
                direct_selector,
                turn_metadata=turn.metadata,
                cache_affinity_credential_failure_callback=(
                    (
                        lambda: self._invalidate_router_dynamic_cache_affinity(
                            session_key=turn.session_key,
                            reason="credential_failure",
                        )
                    )
                    if affinity_context is not None
                    else None
                ),
            )
            cloned_selector.override_provider_config(selected_config)
            turn.model = selected_config.model
            turn.metadata.update(
                {
                    "_router_single_provider_finalized": True,
                    "router_single_decision_id": decision_id,
                    "router_single_task_profile": task_analysis.profile,
                    "router_single_task_analyzer": task_analysis.trace(ranking_config),
                    "router_single_request_context_hash": request_context.get("snapshot_hash"),
                    "router_single_decision": selected_route.trace,
                    "router_single_selected_provider": selected_config.provider,
                    "router_single_selected_model": selected_config.model,
                    "executed_provider": selected_config.provider,
                    "executed_model": selected_config.model,
                    "routed_provider_applied": selected_config.provider,
                    "resolved_model": selected_config.model,
                    "alias_resolution_chain": [selected_config.model],
                    "provider_after_rewrite": selected_config.provider,
                    "_router_single_frozen_catalog": {
                        "provider": selected_config.provider,
                        "model": selected_config.model,
                        "max_tokens": selected_route.direct_output_tokens,
                        "context_window": selected_route.context_window_tokens,
                    },
                }
            )
            # Align RouterDecisionEvent and savings telemetry with the direct
            # model that will actually execute.
            resolved_provider._realign_routed_model_after_fallback()
            if selected_route.thinking_policy_version:
                if not str(selected_route.thinking or "").strip():
                    raise DynamicRankingError(
                        "router_single managed thinking is missing provider-native level",
                        reason="thinking_level_unavailable",
                    )
                turn.metadata["thinking_requested"] = True
                turn.metadata["thinking_level"] = selected_route.thinking
                turn.metadata["_router_single_managed_provider_thinking_level"] = (
                    selected_route.thinking
                )
            return resolved_provider

        direct_provider = _materialize_route(
            route,
            selection_generation=cache_affinity_generation,
        )
        if affinity_enabled:
            no_affinity_inputs = dict(ranking_inputs)
            for affinity_key in (
                "cache_affinity_policy",
                "cache_affinity_receipts",
                "cache_affinity_session_epoch",
                "cache_affinity_now_monotonic",
                "cache_affinity_price_quote_resolver",
                "cache_affinity_outer_thinking_projection",
            ):
                no_affinity_inputs.pop(affinity_key, None)
            no_affinity_inputs["cache_continuity_available"] = False
            # Compaction invalidates every pre-compaction affinity score, but
            # the freshly selected deployment must still be able to publish
            # evidence for this physical post-compaction request.  The
            # collection-only seam performs no receipt lookup/ranking work and
            # carries only the freshly resolved opaque credential token into
            # final materialization.
            no_affinity_inputs["cache_affinity_collection_enabled"] = True

            def _reroute_without_affinity() -> _RouterDynamicCacheRerouteResult:
                rerouted = resolve_router_single_route(
                    config=turn_config,
                    inherited_provider_config=current_provider_config,
                    turn_metadata=turn.metadata,
                    ranking_inputs=no_affinity_inputs,
                    requires_tools=bool(turn.tool_defs),
                    credential_pool_acquirer=acquire_profile_credential,
                    session_key=turn.session_key,
                    provider_health_ledger=provider_health_ledger,
                    model_catalog=self._model_catalog,
                )
                final_provider = _materialize_route(
                    rerouted,
                    selection_generation=(self._router_dynamic_cache_generation(turn.session_key)),
                )
                final_config = rerouted.provider_config
                return _RouterDynamicCacheRerouteResult(
                    provider=final_provider,
                    resolved_model=str(final_config.model or ""),
                    provider_name=str(final_config.provider or ""),
                    active_provider_id=str(final_config.provider or ""),
                )

            direct_provider._router_dynamic_cache_reroute_plan = _RouterDynamicCacheReroutePlan(
                session_key=turn.session_key,
                selection_generation=cache_affinity_generation,
                reroute_without_affinity=_reroute_without_affinity,
            )
        return direct_provider

    async def _run_pipeline(
        self,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list,
        base_prompt: str | tuple[str, str],
        attachments: list[dict],
        semantic_message: str | None = None,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict[str, Any] | None = None,
        history_user_texts: list[str] | None = None,
        history_has_recent_image: bool = False,
        history_image_turn_count: int = 0,
        vision_sticky_remaining: int = 0,
        turns_since_last_image: int | None = None,
        last_image_turn_text: str | None = None,
        vision_candidate_turns: int = 0,
        flags_text_override: str | None = None,
        tool_context: ToolContext | None = None,
        normalization_metadata: dict[str, Any] | None = None,
        input_provenance: dict[str, Any] | None = None,
        skill_catalog: Any | None = None,
        usage_execution_context: UsageExecutionContext | None = None,
        turn_absolute_deadline: float | None = None,
        explicit_model: str | None = None,
        bound_user_message_id: str | None = None,
        trusted_route_metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Run the pre-turn pipeline and re-resolve provider if model changed.

        Pre-seeds ``turn.metadata['pipeline_steps']`` with any
        ``ingress_pipeline_steps`` recorded by the turn-ingress helper
        (under DecisionLog ownership). The engine pipeline's
        ``setdefault`` then appends step records to the same list, so
        ``DecisionEntry`` ends up with ingress records first followed by
        engine pipeline records.
        """
        from opensquilla.engine.pipeline import TurnContext, run_pipeline
        from opensquilla.engine.steps import (
            apply_prompt_cache,
            apply_squilla_router,
            apply_vision_followup_gate,
            enforce_coding_mode,
            filter_skills,
            inject_platform_hint,
            inject_subagent_grounding,
            meta_command_launch,
            meta_resolution,
            observe_reasoning_hint,
            resolve_model,
        )
        from opensquilla.engine.steps.squilla_router import (
            commit_deferred_router_history,
        )

        router_cfg = getattr(self._turn_config(), "squilla_router", None)
        router_timeout = float(getattr(router_cfg, "routing_timeout_seconds", 5.0) or 5.0)
        initial_ensemble_cfg = getattr(self._turn_config(), "llm_ensemble", None)
        fixed_four_tier_v2_active = bool(
            getattr(initial_ensemble_cfg, "enabled", False) is True
            and str(getattr(initial_ensemble_cfg, "mode", "") or "").strip().casefold() == "single"
            and str(getattr(initial_ensemble_cfg, "selection_mode", "") or "").strip().casefold()
            == "four_tier_mapping"
        )
        if not fixed_four_tier_v2_active:
            # A newly accepted non-fixed configuration is a reliable point at
            # which to retire any model cached by the previous mode. The close
            # is queued behind in-flight native work on the same private worker.
            await self._release_fixed_four_tier_v2_routers()

        def _copy_router_turn(turn: TurnContext) -> TurnContext:
            metadata: dict[str, Any] = {}
            for key, value in turn.metadata.items():
                try:
                    metadata[key] = copy.deepcopy(value)
                except Exception:
                    metadata[key] = value
            pipeline_steps = metadata.get("pipeline_steps")
            if isinstance(pipeline_steps, list):
                metadata["pipeline_steps"] = list(pipeline_steps)
            metadata["_defer_squilla_router_history"] = True
            return replace(
                turn,
                tool_defs=list(turn.tool_defs),
                attachments=list(turn.attachments),
                metadata=metadata,
            )

        async def _bounded_apply_squilla_router(turn: TurnContext) -> TurnContext:
            turn_ensemble_cfg = getattr(turn.config, "llm_ensemble", None)
            turn_selection_mode = (
                str(getattr(turn_ensemble_cfg, "selection_mode", "") or "").strip().casefold()
            )
            if (
                getattr(turn_ensemble_cfg, "enabled", False) is True
                and str(getattr(turn_ensemble_cfg, "mode", "") or "").strip().casefold() == "single"
                and turn_selection_mode == "four_tier_mapping"
            ):
                turn.metadata["fixed_four_tier_v2_legacy_router_skipped"] = True
                return turn

            def _run_router_step_sync() -> TurnContext:
                return asyncio.run(apply_squilla_router(_copy_router_turn(turn)))

            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="opensquilla-router-timeout",
            )
            future = loop.run_in_executor(executor, _run_router_step_sync)
            try:
                routed = await asyncio.wait_for(
                    future,
                    timeout=router_timeout,
                )
                return commit_deferred_router_history(routed)
            except TimeoutError as exc:
                future.cancel()
                raise TimeoutError(f"squilla router timed out after {router_timeout:g}s") from exc
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        _bounded_apply_squilla_router.__name__ = "apply_squilla_router"

        async def _apply_vision_gate_for_mode(turn: TurnContext) -> TurnContext:
            if fixed_four_tier_v2_active:
                turn.metadata["fixed_four_tier_v2_vision_gate_skipped"] = True
                return turn
            return await apply_vision_followup_gate(turn)

        _apply_vision_gate_for_mode.__name__ = "apply_vision_followup_gate"

        async def _apply_prompt_cache_for_mode(turn: TurnContext) -> TurnContext:
            if fixed_four_tier_v2_active:
                # The final provider/model does not exist until the fixed resolver
                # runs below. Applying a cache policy here would bind the prompt to
                # the pre-route selector identity and leak legacy routing behavior.
                turn.metadata["fixed_four_tier_v2_prompt_cache_skipped"] = True
                return turn
            return await apply_prompt_cache(turn)

        _apply_prompt_cache_for_mode.__name__ = "apply_prompt_cache"
        if fixed_four_tier_v2_active:
            gate_chat, gate_model = None, None
        else:
            gate_chat, gate_model = self._make_vision_followup_gate_chat(
                cloned_selector,
                usage_execution_context,
            )
        agent_skill_loader = self._skill_loader
        if skill_catalog is not None and self._skill_loader is not None:
            from opensquilla.skills.loader import PinnedSkillLoader

            agent_skill_loader = PinnedSkillLoader(skill_catalog, self._skill_loader)
        initial_metadata: dict[str, Any] = {
            # Agent-side skill_view coercion, meta execution, and child
            # orchestrators must resolve against the same generation used for
            # prompt/tool selection. The pinned loader view preserves configured
            # roots while keeping every catalog read free of filesystem probes.
            "skill_loader": agent_skill_loader,
            "meta_run_writer": getattr(self, "_meta_run_writer", None),
            # PR9+: meta_resolution's awaiting branch calls this first when
            # the SKILL.md has ``nl_extract: true``. None keeps clarify reply
            # parsing on the deterministic compatibility path.
            # The fixed route's contract permits exactly one selected model
            # deployment.  Meta-resolution must stay deterministic in this
            # mode rather than issuing an extra LLM request on the pre-route
            # provider.
            "meta_llm_chat": (
                None
                if fixed_four_tier_v2_active
                else self._make_meta_llm_chat(
                    provider,
                    session_key,
                    usage_execution_context,
                )
            ),
            # TaskRuntime owns a retry-stable ingress execution identity.  The
            # inner TurnRunner UUID remains separate for usage-attempt linkage.
            "fixed_four_tier_v2_request_id": (
                str(getattr(tool_context, "task_id", "") or "").strip() or None
            ),
            "router_control_hold_store": self._router_control_hold_store,
            # Surface the resolved per-agent workspace so the meta_invoke
            # handler in Agent._run_one_streaming (agent.py ~L4724) can
            # find it without falling through to default_workspace_dir().
            # Prefer tool_context.workspace_dir (already resolved with
            # the gateway config in rpc_sessions / channel_dispatch /
            # scheduler); fall back to resolving from agent_id on the
            # tool_context, then to an empty string. When this key was
            # absent the meta_invoke handler dropped to
            # default_workspace_dir() and exec_command sandbox blocked
            # paths under ``/root/`` instead of the gateway workspace.
            "bootstrap_workspace_dir": (
                getattr(tool_context, "workspace_dir", None)
                or (
                    str(
                        self._resolve_bootstrap_workspace_dir(
                            getattr(tool_context, "agent_id", "main") or "main"
                        )
                    )
                    if tool_context is not None
                    else ""
                )
            ),
        }
        if fixed_four_tier_v2_active and isinstance(trusted_route_metadata, Mapping):
            for key in (
                "fixed_four_tier_v2_control_event",
                "fixed_four_tier_v2_redo_parent_session_id",
                "fixed_four_tier_v2_redo_of_message_id",
                "fixed_four_tier_v2_redo_child_task_start_input_message_id",
            ):
                value = trusted_route_metadata.get(key)
                if isinstance(value, str) and value.strip():
                    initial_metadata[key] = value.strip()
        if skill_catalog is not None:
            initial_metadata["skill_catalog_generation"] = int(
                getattr(skill_catalog, "generation", 0)
            )
        initial_provider_config = getattr(cloned_selector, "current_config", None)
        if initial_provider_config is not None and not fixed_four_tier_v2_active:
            initial_metadata["executed_provider"] = str(
                getattr(initial_provider_config, "provider", "") or ""
            )
            initial_metadata["executed_model"] = str(
                getattr(initial_provider_config, "model", "") or ""
            )
        if gate_chat is not None:
            initial_metadata["router_vision_followup_gate_chat"] = gate_chat
        if gate_model:
            initial_metadata["router_vision_followup_gate_model"] = gate_model
        if normalization_metadata is not None:
            initial_metadata["input_normalization"] = dict(normalization_metadata)
            material_tokens = normalization_metadata.get("material_estimated_tokens")
            if type(material_tokens) is int and material_tokens > 0:
                initial_metadata["material_estimated_tokens"] = material_tokens
        if input_provenance:
            if isinstance(input_provenance, dict):
                normalized_provenance = dict(input_provenance)
            else:
                normalized_provenance = {"kind": str(input_provenance)}
            initial_metadata["input_provenance"] = normalized_provenance
            provenance_kind = self._input_provenance_kind(normalized_provenance)
            if provenance_kind:
                initial_metadata["input_provenance_kind"] = provenance_kind
        if ingress_pipeline_steps:
            initial_metadata["pipeline_steps"] = list(ingress_pipeline_steps)
        if prev_assistant_text:
            initial_metadata["router_prev_assistant_text"] = prev_assistant_text
        if prev_assistant_usage:
            initial_metadata["router_prev_assistant_usage"] = dict(prev_assistant_usage)
        if history_user_texts:
            initial_metadata["router_history_user_texts"] = list(history_user_texts)
        if history_has_recent_image:
            initial_metadata["router_history_has_recent_image"] = True
            initial_metadata["router_history_image_turn_count"] = max(
                int(history_image_turn_count),
                1,
            )
        if vision_sticky_remaining > 0:
            initial_metadata["router_vision_sticky_remaining"] = int(vision_sticky_remaining)
        if turns_since_last_image is not None:
            initial_metadata["router_turns_since_last_image"] = int(turns_since_last_image)
        if last_image_turn_text:
            initial_metadata["router_last_image_turn_text"] = last_image_turn_text
        if vision_candidate_turns > 0:
            initial_metadata["router_vision_candidate_turns"] = int(vision_candidate_turns)
        if flags_text_override:
            initial_metadata["router_flags_text_override"] = flags_text_override
        if tool_context is not None:
            initial_metadata["channel_kind"] = tool_context.channel_kind
            initial_metadata["channel_id"] = tool_context.channel_id

        # Budget gate (opt-in): seed the session's already-accumulated spend so
        # the router step can read it. Gated on an active limit, so the default
        # path pays no extra session read. Reads existing session cost totals;
        # it never recomputes cost math.
        budget_cfg = getattr(router_cfg, "budget", None)
        if (
            budget_cfg is not None
            and str(getattr(budget_cfg, "action", "warn") or "warn").strip().lower() != "off"
            and getattr(budget_cfg, "limit_usd", None)
            and self._session_manager is not None
        ):
            try:
                budget_session = await self._session_manager.get_session(session_key)
            except Exception:  # noqa: BLE001 - budget seeding must never break a turn
                budget_session = None
            if budget_session is not None:
                initial_metadata["session_billed_cost_usd"] = float(
                    getattr(budget_session, "billed_cost_usd", 0.0) or 0.0
                )
                initial_metadata["session_total_cost_usd"] = float(
                    getattr(budget_session, "total_cost_usd", 0.0) or 0.0
                )
                initial_metadata["session_estimated_cost_usd"] = float(
                    getattr(budget_session, "estimated_cost_usd", 0.0) or 0.0
                )
                initial_metadata["session_cost_source"] = str(
                    getattr(budget_session, "cost_source", "") or ""
                )

        turn = TurnContext(
            message=message,
            session_key=session_key,
            config=self._turn_config(),
            provider=provider,
            model="",
            tool_defs=tool_defs,
            system_prompt=base_prompt,
            attachments=attachments,
            metadata=initial_metadata,
            raw_message=semantic_message,
            skill_catalog=skill_catalog,
        )
        turn = await run_pipeline(
            turn,
            [
                resolve_model,
                _apply_vision_gate_for_mode,
                _bounded_apply_squilla_router,
                observe_reasoning_hint,
                meta_resolution,
                enforce_coding_mode,
                meta_command_launch,
                filter_skills,
                inject_subagent_grounding,
                inject_platform_hint,
                _apply_prompt_cache_for_mode,
            ],
        )

        # Apply routed model back to cloned selector (local, not shared)
        if turn.model and cloned_selector is not None and not fixed_four_tier_v2_active:
            from opensquilla.engine.selector_override import (
                apply_model_override,
                cross_provider_tier_config,
            )

            provider = apply_model_override(
                cloned_selector,
                turn.model,
                turn_metadata=turn.metadata,
                realign_routed_model=False,
                tier_provider_config=cross_provider_tier_config(
                    self._turn_config(),
                    turn.metadata,
                    turn.model,
                    active_provider_id=getattr(cloned_selector, "active_provider_id", ""),
                    session_key=turn.session_key,
                ),
            )

        turn_config = self._turn_config()
        ensemble_cfg = getattr(turn_config, "llm_ensemble", None)
        router_single_mode = bool(
            getattr(ensemble_cfg, "enabled", False)
            and str(getattr(ensemble_cfg, "mode", "multiple") or "multiple") == "single"
        )
        if router_single_mode:
            single_selection_mode = (
                str(getattr(ensemble_cfg, "selection_mode", "") or "").strip().casefold()
            )
            explicit_model_id = str(explicit_model or "").strip()
            if explicit_model_id and single_selection_mode != "four_tier_mapping":
                # Single-only early override preserves PromptAssembler's
                # shared model precedence for the pre-existing dynamic mode.
                turn.model = explicit_model_id
                return turn, provider
            if single_selection_mode == "four_tier_mapping":
                provider = await self._resolve_fixed_four_tier_v2_provider(
                    turn=turn,
                    provider=provider,
                    cloned_selector=cloned_selector,
                    turn_config=turn_config,
                    ensemble_cfg=ensemble_cfg,
                    turn_absolute_deadline=turn_absolute_deadline,
                    usage_execution_context=usage_execution_context,
                    bound_user_message_id=bound_user_message_id,
                )
            else:
                provider = await self._resolve_router_single_provider(
                    turn=turn,
                    provider=provider,
                    cloned_selector=cloned_selector,
                    turn_config=turn_config,
                    ensemble_cfg=ensemble_cfg,
                    turn_absolute_deadline=turn_absolute_deadline,
                    usage_execution_context=usage_execution_context,
                )
            return turn, provider

        if provider is not None and getattr(ensemble_cfg, "enabled", False):
            from opensquilla.engine.selector_override import (
                acquire_profile_credential,
                report_profile_credential_failure,
            )
            from opensquilla.provider.ensemble import (
                CUSTOM_B5_SELECTION_MODE,
                TREE_BASELINE_SELECTION_MODE,
                TreeBaselineSelectionError,
                build_ensemble_provider_from_config,
                static_b5_credential_available,
                static_b5_profile,
            )
            from opensquilla.provider.ensemble_observability import (
                log_ensemble_decision_failed,
                log_ensemble_decision_skipped,
                log_ensemble_decision_started,
                log_ensemble_decision_steps,
            )

            current_provider_config = (
                getattr(cloned_selector, "current_config", None)
                if cloned_selector is not None
                else None
            )
            selection_mode = str(getattr(ensemble_cfg, "selection_mode", "") or "")
            static_profile = static_b5_profile(selection_mode)
            internal_selection_profile = selection_mode
            if static_profile is not None:
                internal_selection_profile = static_profile.profile_name
            elif selection_mode == CUSTOM_B5_SELECTION_MODE:
                internal_selection_profile = "custom_b5"
            provider_health_ledger = None
            canary_rollout_ledger = None
            if selection_mode == "router_dynamic":
                from opensquilla.engine.routing.health import (
                    get_provider_health_ledger,
                )

                provider_health_ledger = get_provider_health_ledger()
                canary_rollout_ledger = self._persistent_canary_rollout_ledger(turn_config)
            dynamic_cleanup_errors: tuple[type[Exception], ...] = ()
            dynamic_selection_errors: tuple[type[Exception], ...] = ()
            if selection_mode == "router_dynamic":
                from opensquilla.provider.ranking_router import (
                    TaskAnalyzerStreamCleanupError,
                )

                # Dynamic selection spans analysis, ranking, credential
                # resolution, and member materialization. Any ordinary
                # exception in that optional wrapper must fail open to the
                # already resolved single provider. Cancellation and other
                # BaseException subclasses intentionally still propagate.
                # Unproven analyzer cleanup is also explicitly fail-closed so
                # the turn cannot start a second, overlapping billed request.
                dynamic_cleanup_errors = (TaskAnalyzerStreamCleanupError,)
                dynamic_selection_errors = (Exception,)
            # The shared deployment resolver marks an unexecutable member
            # unavailable before any network call. Keep the ensemble wrapper so
            # custom lineups can retain quorum semantics when only one provider
            # is unavailable; only a structurally empty lineup is rejected here.
            custom_has_proposer = (
                any(
                    getattr(candidate, "enabled", True) is not False
                    and str(getattr(candidate, "provider", "") or "").strip()
                    and str(getattr(candidate, "model", "") or "").strip()
                    and str(getattr(candidate, "role", "") or "").strip().lower() != "aggregator"
                    for candidate in (getattr(ensemble_cfg, "candidates", None) or [])
                )
                if selection_mode == CUSTOM_B5_SELECTION_MODE
                else True
            )
            ensemble_decision_id = str(turn.metadata.get("router_decision_id") or uuid.uuid4().hex)
            turn.metadata["ensemble_decision_id"] = ensemble_decision_id
            ranking_user_profile_application_enabled = (
                bool(
                    getattr(
                        ensemble_cfg,
                        "ranking_user_profile_enabled",
                        False,
                    )
                )
                if selection_mode == "router_dynamic"
                else None
            )
            log_ensemble_decision_started(
                decision_id=ensemble_decision_id,
                selection_mode=selection_mode,
                turn_metadata=turn.metadata,
                user_profile_enabled=ranking_user_profile_application_enabled,
            )
            if current_provider_config is None:
                log_ensemble_decision_skipped(
                    decision_id=ensemble_decision_id,
                    selection_mode=selection_mode,
                    reason="missing_provider_selector_current_config",
                )
                log.warning(
                    "llm_ensemble.wrap_skipped",
                    reason="missing_provider_selector_current_config",
                    decision_id=ensemble_decision_id,
                )
            elif not getattr(current_provider_config, "provider", None) or not getattr(
                current_provider_config,
                "model",
                None,
            ):
                log_ensemble_decision_skipped(
                    decision_id=ensemble_decision_id,
                    selection_mode=selection_mode,
                    reason="incomplete_provider_selector_current_config",
                )
                log.warning(
                    "llm_ensemble.wrap_skipped",
                    reason="incomplete_provider_selector_current_config",
                    decision_id=ensemble_decision_id,
                )
            elif static_profile is not None and not (
                static_b5_credential_available(
                    self._turn_config(),
                    current_provider_config,
                    selection_mode,
                )
            ):
                # Every member of a static profile shares one provider
                # credential; without it no member can ever succeed, and
                # wrapping would run a degraded quorum-unavailable fallback
                # round (with its heartbeats, labels, and fallback budget) on
                # every turn instead of the user's plain single-model
                # provider. Keep the wrap off, matching the config-side
                # static_b5_ensemble_active() gate.
                log_ensemble_decision_skipped(
                    decision_id=ensemble_decision_id,
                    selection_mode=selection_mode,
                    reason=f"{internal_selection_profile}_no_credential",
                )
                log.warning(
                    "llm_ensemble.wrap_skipped",
                    reason=f"{internal_selection_profile}_no_credential",
                    decision_id=ensemble_decision_id,
                )
                turn.metadata["ensemble_wrap_skipped_reason"] = (
                    f"{internal_selection_profile}_no_credential"
                )
            elif not custom_has_proposer:
                log_ensemble_decision_skipped(
                    decision_id=ensemble_decision_id,
                    selection_mode=selection_mode,
                    reason=f"{internal_selection_profile}_not_ready:no_proposers",
                )
                log.warning(
                    "llm_ensemble.wrap_skipped",
                    reason=f"{internal_selection_profile}_not_ready:no_proposers",
                    decision_id=ensemble_decision_id,
                )
                turn.metadata["ensemble_wrap_skipped_reason"] = (
                    f"{internal_selection_profile}_not_ready:no_proposers"
                )
            else:
                turn.metadata["ensemble_enabled"] = True
                turn.metadata["routed_model_before_ensemble"] = turn.model or getattr(
                    current_provider_config, "model", ""
                )
                # The mode belongs to this turn's immutable ranking root. Keep
                # it stable through error classification even if a concurrent
                # config refreeze publishes a different mode.
                thinking_assignment_enabled = False
                try:
                    ranking_inputs: dict[str, Any] | None = None
                    multiple_cache_policy: _RouterDynamicCacheAffinityPolicy | None = None
                    multiple_cache_session_epoch: int | None = None
                    multiple_cache_receipts: tuple[
                        _RouterDynamicCacheAffinityReceipt,
                        ...,
                    ] = ()
                    multiple_cache_continuity = False
                    multiple_cache_generation = 0
                    multiple_cache_now: float | None = None
                    multiple_cache_outer_thinking_projection: dict[str, bool | str | int] | None = (
                        None
                    )
                    multiple_affinity_callback: Callable[[object], None] | None = None
                    multiple_credential_failure_reporter = report_profile_credential_failure
                    multiple_affinity_turn_id = ""
                    multiple_affinity_provider_token = ""
                    multiple_context: _RouterDynamicCacheAffinityCollectionContext | None = None
                    multiple_sidecar_key: tuple[str, str] | None = None
                    if selection_mode == "router_dynamic":
                        from opensquilla.provider.ranking_router import (
                            RANKING_CONFIG_SCHEMA_VERSION,
                            DynamicRankingError,
                            TaskAnalyzerCandidate,
                            _prepare_effective_ranking_config,
                            analyze_task_with_fallback_chain,
                            analyze_task_with_provider,
                            build_request_context,
                            dynamic_output_token_budgets,
                            ranking_config_snapshot,
                            task_analyzer_chain_policy,
                            task_analyzer_policy,
                        )

                        prepared_ranking_config = getattr(
                            ensemble_cfg,
                            "prepared_ranking_config",
                            None,
                        )
                        frozen_resolution_snapshot = None
                        if callable(prepared_ranking_config):
                            ranking_config = prepared_ranking_config()
                            if not isinstance(ranking_config, Mapping):
                                raise DynamicRankingError(
                                    "prepared router_dynamic ranking config is unavailable"
                                )
                            # Derive the mode from the same immutable root. A
                            # concurrent refreeze may update the config object,
                            # but an in-flight turn must keep its old pair.
                            thinking_policy = ranking_config.get("thinking_assignment")
                            thinking_assignment_enabled = bool(
                                ranking_config.get("schema_version")
                                == RANKING_CONFIG_SCHEMA_VERSION
                                and isinstance(thinking_policy, Mapping)
                                and thinking_policy.get("enabled") is True
                            )
                        else:
                            frozen_resolution_snapshot = getattr(
                                ensemble_cfg,
                                "ranking_config_resolution_snapshot",
                                None,
                            )
                        if not callable(prepared_ranking_config) and callable(
                            frozen_resolution_snapshot
                        ):
                            frozen_resolution = frozen_resolution_snapshot()
                            ranking_config = frozen_resolution.get("effective_config")
                            if not isinstance(ranking_config, Mapping):
                                raise DynamicRankingError(
                                    "frozen router_dynamic ranking config is unavailable"
                                )
                            thinking_assignment_enabled = (
                                frozen_resolution.get("thinking_assignment_enabled") is True
                            )
                        elif not callable(prepared_ranking_config):
                            thinking_assignment_enabled = bool(
                                getattr(
                                    ensemble_cfg,
                                    "ranking_thinking_assignment_enabled",
                                    False,
                                )
                            )
                            ranking_config = ranking_config_snapshot(
                                thinking_assignment_enabled=(thinking_assignment_enabled),
                                override=(
                                    getattr(
                                        ensemble_cfg,
                                        "ranking_config_override",
                                        None,
                                    )
                                    or None
                                ),
                            )
                        ranking_config = _prepare_effective_ranking_config(
                            ranking_config,
                            thinking_assignment_enabled=(thinking_assignment_enabled),
                        )
                        ranking_session = ranking_config.get("session")
                        if (
                            not str(explicit_model or "").strip()
                            and isinstance(ranking_session, Mapping)
                            and isinstance(
                                ranking_session.get("kv_cache_affinity"),
                                Mapping,
                            )
                        ):
                            multiple_cache_policy = _router_dynamic_cache_affinity_policy(
                                ranking_config,
                                topology="multiple",
                            )
                        if multiple_cache_policy is not None:
                            multiple_cache_outer_thinking_projection = (
                                self._router_dynamic_outer_thinking_projection(turn)
                            )
                            self._ensure_router_dynamic_cache_compaction_listener(
                                route_cache_max_entries=(
                                    multiple_cache_policy.route_cache_max_entries
                                ),
                            )
                            multiple_cache_session_epoch = (
                                await self._resolve_router_dynamic_session_epoch(turn.session_key)
                            )
                            if multiple_cache_session_epoch is None:
                                self._invalidate_router_dynamic_cache_affinity(
                                    session_key=turn.session_key,
                                    reason="session_epoch_unavailable",
                                )
                            else:
                                multiple_cache_now = time.monotonic()
                                (
                                    multiple_cache_continuity,
                                    multiple_cache_receipts,
                                    multiple_cache_generation,
                                ) = self._router_dynamic_cache_continuity_snapshot(
                                    session_key=turn.session_key,
                                    session_epoch=multiple_cache_session_epoch,
                                    policy=multiple_cache_policy,
                                    now=multiple_cache_now,
                                )
                                # Install an empty sidecar before Analyzer and
                                # ranking. Any failure before provider
                                # materialization then clears the prior
                                # topology evidence instead of silently
                                # retaining a stale successful receipt.
                                multiple_affinity_turn_id = str(
                                    getattr(usage_execution_context, "turn_id", "")
                                    or getattr(
                                        usage_execution_context,
                                        "execution_id",
                                        "",
                                    )
                                    or uuid.uuid4().hex
                                )
                                multiple_affinity_provider_token = uuid.uuid4().hex
                                multiple_context = _RouterDynamicCacheAffinityCollectionContext(
                                    turn_id=multiple_affinity_turn_id,
                                    decision_id=ensemble_decision_id,
                                    provider_instance_token=(multiple_affinity_provider_token),
                                    provider_instance_generation=0,
                                    session_key=turn.session_key,
                                    session_epoch=multiple_cache_session_epoch,
                                    selection_generation=multiple_cache_generation,
                                    topology="multiple",
                                )
                                multiple_sidecar_key = self._register_router_dynamic_cache_sidecar(
                                    context=multiple_context,
                                    policy=multiple_cache_policy,
                                )
                        analyzer_policy = task_analyzer_policy(ranking_config)
                        analyzer_chain = task_analyzer_chain_policy(ranking_config)
                        analyzer_provider_id = str(analyzer_policy["provider"])
                        analyzer_model_id = str(analyzer_policy["model"])
                        routing_extra = turn.metadata.get("routing_extra")
                        routing_extra_map = (
                            routing_extra if isinstance(routing_extra, Mapping) else {}
                        )
                        routed_tier = str(
                            turn.metadata.get("routed_tier")
                            or routing_extra_map.get("final_tier")
                            or routing_extra_map.get("base_tier")
                            or "c1"
                        )
                        try:
                            routing_confidence = float(
                                turn.metadata.get("routing_confidence") or 0.0
                            )
                        except (TypeError, ValueError):
                            routing_confidence = 0.0
                        configured_output_tokens = int(
                            getattr(getattr(turn_config, "llm", None), "max_tokens", 0) or 0
                        )
                        candidate_max_chars = int(
                            getattr(ensemble_cfg, "candidate_max_chars", 24_000) or 0
                        )
                        if candidate_max_chars <= 0:
                            raise DynamicRankingError(
                                "router_dynamic requires candidate_max_chars > 0 "
                                "to prove aggregator context feasibility"
                            )
                        (
                            candidate_output_tokens,
                            aggregator_output_tokens,
                        ) = dynamic_output_token_budgets(
                            configured_output_tokens=configured_output_tokens,
                            candidate_max_chars=candidate_max_chars,
                            ranking_config=ranking_config,
                        )
                        if "router_dynamic_last_route" not in turn.metadata:
                            previous_route = self._previous_router_dynamic_route(turn.session_key)
                            if previous_route is not None:
                                turn.metadata["router_dynamic_last_route"] = previous_route
                        request_context = build_request_context(
                            message=turn.semantic_message,
                            turn_metadata=turn.metadata,
                            attachments=turn.attachments,
                            candidate_output_tokens=candidate_output_tokens,
                            aggregator_output_tokens=aggregator_output_tokens,
                            ranking_config=ranking_config,
                        )
                        user_profile = (
                            self._resolve_user_profile(ranking_config, self._config)
                            if ranking_user_profile_application_enabled
                            else None
                        )
                        analyzer_admission_controller = None
                        analyzer_admission_deadline = None
                        admission_config = getattr(
                            ensemble_cfg,
                            "admission",
                            None,
                        )
                        if (
                            str(
                                getattr(
                                    ensemble_cfg,
                                    "latency_class",
                                    "normal",
                                )
                                or "normal"
                            )
                            != "experiment"
                            and admission_config is not None
                            and bool(
                                getattr(
                                    admission_config,
                                    "enabled",
                                    True,
                                )
                            )
                        ):
                            from opensquilla.provider.admission import (
                                get_shared_provider_admission_controller,
                                provider_admission_settings_from_config,
                            )

                            analyzer_admission_controller = (
                                get_shared_provider_admission_controller(
                                    provider_admission_settings_from_config(admission_config)
                                )
                            )
                            analyzer_admission_timeout = float(
                                analyzer_chain["total_timeout_seconds"]
                                if analyzer_chain["configured"]
                                else analyzer_policy["timeout_seconds"]
                            )
                            analyzer_admission_deadline = (
                                time.monotonic() + analyzer_admission_timeout
                            )
                            if turn_absolute_deadline is not None:
                                analyzer_admission_deadline = min(
                                    analyzer_admission_deadline,
                                    turn_absolute_deadline,
                                )
                        allow_canary_analyzer_route = bool(
                            str(
                                getattr(
                                    ensemble_cfg,
                                    "latency_class",
                                    "normal",
                                )
                                or "normal"
                            )
                            .strip()
                            .casefold()
                            == "experiment"
                        )
                        if analyzer_chain["configured"]:
                            analyzer_candidates = [
                                TaskAnalyzerCandidate(
                                    provider=self._router_dynamic_task_analyzer_provider(
                                        current_provider_config,
                                        session_key=turn.session_key,
                                        ranking_config=ranking_config,
                                        analyzer_route=route,
                                        allow_canary_route=(allow_canary_analyzer_route),
                                    ),
                                    provider_id=str(route["provider"]),
                                    model_id=str(route["model"]),
                                    upstream_provider=str(route["upstream_provider"]),
                                )
                                for route in analyzer_chain["routes"]
                            ]
                            task_analysis = await analyze_task_with_fallback_chain(
                                candidates=analyzer_candidates,
                                message=turn.semantic_message,
                                user_profile_enabled=user_profile is not None,
                                request_context=request_context,
                                routed_tier=routed_tier,
                                routing_confidence=routing_confidence,
                                usage_tracker=self._usage_tracker,
                                session_key=turn.session_key,
                                ranking_config=ranking_config,
                                decision_id=ensemble_decision_id,
                                absolute_deadline=turn_absolute_deadline,
                                admission_controller=(analyzer_admission_controller),
                                admission_deadline=analyzer_admission_deadline,
                                cache_continuity_available=(multiple_cache_continuity),
                            )
                        else:
                            analyzer_provider = self._router_dynamic_task_analyzer_provider(
                                current_provider_config,
                                session_key=turn.session_key,
                                ranking_config=ranking_config,
                                allow_canary_route=(allow_canary_analyzer_route),
                            )
                            task_analysis = await analyze_task_with_provider(
                                provider=analyzer_provider,
                                message=turn.semantic_message,
                                user_profile_enabled=user_profile is not None,
                                request_context=request_context,
                                routed_tier=routed_tier,
                                routing_confidence=routing_confidence,
                                usage_tracker=self._usage_tracker,
                                session_key=turn.session_key,
                                analyzer_provider_id=analyzer_provider_id,
                                analyzer_model_id=analyzer_model_id,
                                ranking_config=ranking_config,
                                decision_id=ensemble_decision_id,
                                admission_controller=(analyzer_admission_controller),
                                admission_deadline=analyzer_admission_deadline,
                                _absolute_deadline=turn_absolute_deadline,
                                cache_continuity_available=(multiple_cache_continuity),
                            )
                        # Keep the Analyzer's frozen intent input, but do not
                        # let its latency extend affinity TTL or decay at
                        # candidate-mapping time.
                        if multiple_cache_session_epoch is not None:
                            multiple_cache_now = time.monotonic()
                        ranking_inputs = {
                            "decision_id": ensemble_decision_id,
                            "task_analysis": task_analysis,
                            "user_profile": user_profile,
                            "request_context": request_context,
                            "ranking_config": ranking_config,
                        }
                        if multiple_cache_policy is not None:
                            ranking_inputs.update(
                                {
                                    "cache_continuity_available": (multiple_cache_continuity),
                                    "cache_affinity_policy": (multiple_cache_policy.source),
                                    "cache_affinity_receipts": (multiple_cache_receipts),
                                    "cache_affinity_session_epoch": (multiple_cache_session_epoch),
                                    "cache_affinity_now_monotonic": (multiple_cache_now),
                                    "cache_affinity_price_quote_resolver": (
                                        _router_dynamic_cache_price_quote_resolver
                                    ),
                                    "cache_affinity_outer_thinking_projection": (
                                        multiple_cache_outer_thinking_projection
                                    ),
                                }
                            )
                        turn.metadata["router_dynamic_task_profile"] = task_analysis.profile
                        turn.metadata["router_dynamic_task_analyzer"] = task_analysis.trace(
                            ranking_config
                        )
                        turn.metadata["router_dynamic_request_context_hash"] = request_context.get(
                            "snapshot_hash"
                        )
                        turn.metadata["router_dynamic_user_profile"] = {
                            "enabled": ranking_user_profile_application_enabled,
                            "source": (
                                str(user_profile.get("profile_source") or "")
                                if user_profile is not None
                                else ""
                            ),
                            "version": (
                                str(user_profile.get("profile_version") or "")
                                if user_profile is not None
                                else ""
                            ),
                        }

                    if (
                        multiple_cache_policy is not None
                        and multiple_cache_session_epoch is not None
                    ):
                        assert multiple_context is not None
                        assert multiple_sidecar_key is not None

                        def _stage_multiple_affinity(batch: object) -> None:
                            normalized = self._normalize_multiple_cache_affinity_batch(
                                context=multiple_context,
                                batch=batch,
                            )
                            if normalized is not None:
                                self._stage_router_dynamic_cache_affinity_batch(
                                    multiple_sidecar_key,
                                    normalized,
                                )

                        multiple_affinity_callback = _stage_multiple_affinity

                        def _report_multiple_credential_failure(
                            provider_id: str,
                            credential_session_key: str,
                            failure_kind: object,
                            retry_after_s: float | None = None,
                        ) -> None:
                            report_profile_credential_failure(
                                provider_id,
                                credential_session_key,
                                failure_kind,
                                retry_after_s,
                            )
                            if failure_kind in {
                                ProviderFailureKind.RATE_LIMITED,
                                ProviderFailureKind.INSUFFICIENT_CREDITS,
                                ProviderFailureKind.AUTH_INVALID,
                            }:
                                self._invalidate_router_dynamic_cache_affinity(
                                    session_key=turn.session_key,
                                    reason="credential_failure",
                                )

                        multiple_credential_failure_reporter = _report_multiple_credential_failure

                    multiple_base_fallback_provider = provider
                    ensemble_provider = build_ensemble_provider_from_config(
                        config=turn_config,
                        inherited_provider_config=current_provider_config,
                        fallback_provider=multiple_base_fallback_provider,
                        turn_metadata=turn.metadata,
                        ranking_inputs=ranking_inputs,
                        _enable_member_request_budget_rebinding=True,
                        _model_catalog=self._model_catalog,
                        _context_overflow_threshold=(AgentConfig().context_overflow_threshold),
                        _credential_pool_acquirer=acquire_profile_credential,
                        _credential_pool_failure_reporter=(multiple_credential_failure_reporter),
                        _session_key=turn.session_key,
                        _fallback_selector=cloned_selector,
                        _provider_health_ledger=provider_health_ledger,
                        _canary_rollout_ledger=canary_rollout_ledger,
                        _absolute_deadline=turn_absolute_deadline,
                        _cache_affinity_receipt_callback=(multiple_affinity_callback),
                        _cache_affinity_turn_id=multiple_affinity_turn_id,
                        _cache_affinity_provider_instance_token=(multiple_affinity_provider_token),
                        _cache_affinity_session_epoch=(
                            multiple_cache_session_epoch
                            if multiple_affinity_callback is not None
                            else None
                        ),
                    )
                    if multiple_affinity_callback is not None:

                        def _initial_multiple_generation_is_current() -> bool:
                            return (
                                self._router_dynamic_cache_generation(turn.session_key)
                                == multiple_cache_generation
                            )

                        ensemble_provider._router_dynamic_cache_dispatch_generation_guard = (
                            _initial_multiple_generation_is_current
                        )
                except dynamic_cleanup_errors as exc:
                    log_ensemble_decision_failed(
                        decision_id=ensemble_decision_id,
                        selection_mode=selection_mode,
                        reason="ensemble_selection_error",
                        error=exc,
                    )
                    raise
                except dynamic_selection_errors as exc:
                    thinking_fail_closed = thinking_assignment_enabled and (
                        getattr(exc, "reason", "") == "thinking_level_unavailable"
                        or "thinking" in str(exc).casefold()
                    )
                    log_ensemble_decision_failed(
                        decision_id=ensemble_decision_id,
                        selection_mode=selection_mode,
                        reason=(
                            "router_dynamic_thinking_assignment_unavailable"
                            if thinking_fail_closed
                            else "router_dynamic_ranking_unavailable"
                        ),
                        error=exc,
                    )
                    if thinking_fail_closed:
                        turn.metadata["router_dynamic_ranking_error"] = str(exc)
                        turn.metadata["router_dynamic_thinking_assignment_error"] = str(exc)
                        raise
                    log.warning(
                        "llm_ensemble.wrap_skipped",
                        reason="router_dynamic_ranking_unavailable",
                        error=str(exc),
                        decision_id=ensemble_decision_id,
                    )
                    turn.metadata.pop("ensemble_enabled", None)
                    turn.metadata["ensemble_wrap_skipped_reason"] = (
                        "router_dynamic_ranking_unavailable"
                    )
                    turn.metadata["router_dynamic_ranking_error"] = str(exc)
                except TreeBaselineSelectionError as exc:
                    log_ensemble_decision_failed(
                        decision_id=ensemble_decision_id,
                        selection_mode=selection_mode,
                        reason="router_tree_baseline_unavailable",
                        error=exc,
                    )
                    log.warning(
                        "llm_ensemble.wrap_skipped",
                        reason="router_tree_baseline_unavailable",
                        error=str(exc),
                        decision_id=ensemble_decision_id,
                    )
                    turn.metadata.pop("ensemble_enabled", None)
                    turn.metadata["ensemble_wrap_skipped_reason"] = (
                        "router_tree_baseline_unavailable"
                    )
                    turn.metadata["router_tree_baseline_error"] = str(exc)
                except Exception as exc:
                    log_ensemble_decision_failed(
                        decision_id=ensemble_decision_id,
                        selection_mode=selection_mode,
                        reason="ensemble_selection_error",
                        error=exc,
                    )
                    raise
                else:
                    provider = ensemble_provider
                    plan = ensemble_provider.selection_plan
                    plan["decision_id"] = ensemble_decision_id
                    if selection_mode == "router_dynamic":
                        turn.metadata["router_dynamic_pending_route_plan"] = plan
                        router_dynamic_decision = _router_dynamic_decision_projection(plan)
                        if router_dynamic_decision is None:
                            raise ValueError(
                                "router_dynamic selection plan cannot be "
                                "projected into runtime audit metadata"
                            )
                        turn.metadata["router_dynamic_decision"] = router_dynamic_decision
                        if (
                            multiple_cache_policy is not None
                            and multiple_cache_session_epoch is not None
                            and isinstance(ranking_inputs, Mapping)
                        ):
                            no_affinity_inputs = dict(ranking_inputs)
                            for affinity_key in (
                                "cache_affinity_policy",
                                "cache_affinity_receipts",
                                "cache_affinity_session_epoch",
                                "cache_affinity_now_monotonic",
                                "cache_affinity_price_quote_resolver",
                                "cache_affinity_outer_thinking_projection",
                            ):
                                no_affinity_inputs.pop(affinity_key, None)
                            no_affinity_inputs["cache_continuity_available"] = False
                            no_affinity_inputs["cache_affinity_collection_enabled"] = True
                            final_provider_holder = {
                                "provider": ensemble_provider,
                            }

                            def _reroute_multiple_without_affinity() -> (
                                _RouterDynamicCacheRerouteResult
                            ):
                                final_generation = self._router_dynamic_cache_generation(
                                    turn.session_key
                                )
                                final_provider_token = uuid.uuid4().hex
                                final_context = _RouterDynamicCacheAffinityCollectionContext(
                                    turn_id=multiple_affinity_turn_id,
                                    decision_id=ensemble_decision_id,
                                    provider_instance_token=(final_provider_token),
                                    provider_instance_generation=0,
                                    session_key=turn.session_key,
                                    session_epoch=(multiple_cache_session_epoch),
                                    selection_generation=final_generation,
                                    topology="multiple",
                                )
                                final_sidecar_key = self._register_router_dynamic_cache_sidecar(
                                    context=final_context,
                                    policy=multiple_cache_policy,
                                )

                                def _stage_final_multiple_affinity(
                                    batch: object,
                                ) -> None:
                                    normalized = self._normalize_multiple_cache_affinity_batch(
                                        context=final_context,
                                        batch=batch,
                                    )
                                    if normalized is not None:
                                        self._stage_router_dynamic_cache_affinity_batch(
                                            final_sidecar_key,
                                            normalized,
                                        )

                                final_provider = build_ensemble_provider_from_config(
                                    config=turn_config,
                                    inherited_provider_config=(current_provider_config),
                                    fallback_provider=(multiple_base_fallback_provider),
                                    turn_metadata=turn.metadata,
                                    ranking_inputs=no_affinity_inputs,
                                    _enable_member_request_budget_rebinding=True,
                                    _model_catalog=self._model_catalog,
                                    _context_overflow_threshold=(
                                        AgentConfig().context_overflow_threshold
                                    ),
                                    _credential_pool_acquirer=(acquire_profile_credential),
                                    _credential_pool_failure_reporter=(
                                        multiple_credential_failure_reporter
                                    ),
                                    _session_key=turn.session_key,
                                    _fallback_selector=cloned_selector,
                                    _provider_health_ledger=(provider_health_ledger),
                                    _canary_rollout_ledger=(canary_rollout_ledger),
                                    _absolute_deadline=turn_absolute_deadline,
                                    _cache_affinity_receipt_callback=(
                                        _stage_final_multiple_affinity
                                    ),
                                    _cache_affinity_turn_id=(multiple_affinity_turn_id),
                                    _cache_affinity_provider_instance_token=(final_provider_token),
                                    _cache_affinity_session_epoch=(multiple_cache_session_epoch),
                                )

                                def _final_multiple_generation_is_current() -> bool:
                                    return (
                                        self._router_dynamic_cache_generation(turn.session_key)
                                        == final_generation
                                    )

                                final_provider._router_dynamic_cache_dispatch_generation_guard = (
                                    _final_multiple_generation_is_current
                                )
                                final_plan = final_provider.selection_plan
                                final_plan["decision_id"] = ensemble_decision_id
                                final_projection = _router_dynamic_decision_projection(final_plan)
                                if final_projection is None:
                                    raise ValueError(
                                        "router_dynamic reroute plan cannot be "
                                        "projected into runtime audit metadata"
                                    )
                                turn.metadata["router_dynamic_pending_route_plan"] = final_plan
                                turn.metadata["router_dynamic_decision"] = final_projection
                                final_provider_holder["provider"] = final_provider
                                final_runtime_provider = _SelectorFallbackProvider(
                                    final_provider,
                                    cloned_selector,
                                    turn.metadata,
                                )
                                final_config = getattr(
                                    cloned_selector,
                                    "current_config",
                                    None,
                                )
                                final_provider_id = str(
                                    getattr(
                                        cloned_selector,
                                        "active_provider_id",
                                        "",
                                    )
                                    or getattr(
                                        final_config,
                                        "provider",
                                        "",
                                    )
                                    or ""
                                )
                                final_model = str(
                                    getattr(turn, "model", "")
                                    or getattr(final_config, "model", "")
                                    or ""
                                )
                                return _RouterDynamicCacheRerouteResult(
                                    provider=final_runtime_provider,
                                    resolved_model=final_model,
                                    provider_name=final_provider_id,
                                    active_provider_id=final_provider_id,
                                )

                            def _finalize_multiple_observability() -> None:
                                final_provider = final_provider_holder["provider"]
                                log_ensemble_decision_steps(
                                    decision_id=ensemble_decision_id,
                                    selection_mode=selection_mode,
                                    profile_name=final_provider.profile_name,
                                    selection_plan=(final_provider.selection_plan),
                                )

                            ensemble_provider._router_dynamic_cache_reroute_plan = (
                                _RouterDynamicCacheReroutePlan(
                                    session_key=turn.session_key,
                                    selection_generation=(multiple_cache_generation),
                                    reroute_without_affinity=(_reroute_multiple_without_affinity),
                                    finalize_observability=(_finalize_multiple_observability),
                                )
                            )
                        else:
                            log_ensemble_decision_steps(
                                decision_id=ensemble_decision_id,
                                selection_mode=selection_mode,
                                profile_name=ensemble_provider.profile_name,
                                selection_plan=plan,
                            )
                    elif selection_mode == TREE_BASELINE_SELECTION_MODE:
                        log_ensemble_decision_steps(
                            decision_id=ensemble_decision_id,
                            selection_mode=selection_mode,
                            profile_name=ensemble_provider.profile_name,
                            selection_plan=plan,
                        )
                        turn.metadata["router_tree_baseline_decision"] = {
                            "decision_id": ensemble_decision_id,
                            "algorithm_version": plan.get("algorithm_version"),
                            "config_version": plan.get("config_version"),
                            "config_hash": plan.get("config_hash"),
                            "router_source": plan.get("router_source"),
                            "routed_tier": plan.get("routed_tier"),
                            "selected_P": list(plan.get("selected_P") or []),
                            "selected_A": plan.get("selected_A"),
                        }
                    else:
                        log_ensemble_decision_steps(
                            decision_id=ensemble_decision_id,
                            selection_mode=selection_mode,
                            profile_name=ensemble_provider.profile_name,
                            selection_plan=plan,
                        )

        return turn, provider

    async def _router_previous_assistant_context(
        self,
        session_key: str,
        *,
        exclude_last_user: bool = False,
        bound_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Return transcript context for the V4 router, excluding the current user turn."""
        if self._session_manager is None:
            return {}
        get_transcript = getattr(self._session_manager, "get_transcript", None)
        if not callable(get_transcript):
            return {}
        try:
            transcript = get_transcript(session_key)
            if inspect.isawaitable(transcript):
                transcript = await transcript
        except Exception:  # noqa: BLE001 - router context must never block a turn
            log.debug("turn_runner.router_context_failed", session_key=session_key)
            return {}
        entries = list(transcript or [])
        # When the turn is bound to a specific user message id (queued-sends
        # path), exclude the bound current prompt AND every later user entry
        # (still-queued future prompts persisted at ingress), mirroring
        # _load_history's id-bound slice. The positional exclude_last_user
        # fallback only handles the simple no-queue case and misclassifies the
        # current/queued prompts as history under queued sends.
        bound_index: int | None = None
        if bound_user_message_id is not None:
            for idx, entry in enumerate(entries):
                if getattr(entry, "message_id", None) == bound_user_message_id:
                    bound_index = idx
                    break
        user_texts: list[str] = []
        user_contents: list[str] = []
        for index, entry in enumerate(entries):
            if getattr(entry, "role", None) != "user":
                continue
            if bound_index is not None and index >= bound_index:
                # The bound current prompt and any later (queued) user entry.
                continue
            if bound_index is None and exclude_last_user and index == len(entries) - 1:
                continue
            content = getattr(entry, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            user_contents.append(content)
            unpacked = self._maybe_unpack_attachments(content)
            text = unpacked.strip() if isinstance(unpacked, str) else content.strip()
            if len(text) > _ROUTER_HISTORY_USER_MAX_CHARS:
                text = text[-_ROUTER_HISTORY_USER_MAX_CHARS:]
            user_texts.append(text)

        context: dict[str, Any] = {}
        if user_texts:
            context["history_user_texts"] = user_texts[-_ROUTER_HISTORY_USER_MAX_TURNS:]
        router_cfg = getattr(self._turn_config(), "squilla_router", None)
        lookback = int(
            getattr(
                router_cfg,
                "vision_history_lookback_turns",
                8,
            )
            or 0
        )
        candidate_turns = int(
            getattr(
                router_cfg,
                "vision_history_candidate_turns",
                lookback,
            )
            or 0
        )
        if lookback > 0 or candidate_turns > 0:
            recent_limit = max(lookback, candidate_turns)
            recent_user_contents = user_contents[-recent_limit:]
            image_positions = [
                index
                for index, content in enumerate(recent_user_contents)
                if self._attachment_envelope_has_image(content)
            ]
            image_turn_count = len(image_positions)
            if image_turn_count:
                context["history_has_recent_image"] = True
                context["history_image_turn_count"] = image_turn_count
                turns_since_last_image = len(recent_user_contents) - image_positions[-1] - 1
                context["turns_since_last_image"] = turns_since_last_image
                context["vision_candidate_turns"] = candidate_turns
                absolute_image_index = (
                    len(user_contents) - len(recent_user_contents) + image_positions[-1]
                )
                if 0 <= absolute_image_index < len(user_texts):
                    context["last_image_turn_text"] = user_texts[absolute_image_index]
                sticky_turns = int(
                    getattr(
                        router_cfg,
                        "vision_sticky_followup_turns",
                        2,
                    )
                    or 0
                )
                if sticky_turns > 0 and turns_since_last_image < sticky_turns:
                    context["vision_sticky_remaining"] = sticky_turns - turns_since_last_image

        for entry in reversed(entries):
            if getattr(entry, "role", None) != "assistant":
                continue
            content = getattr(entry, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            text = content.strip()
            if len(text) > _ROUTER_PREV_ASSISTANT_MAX_CHARS:
                text = text[-_ROUTER_PREV_ASSISTANT_MAX_CHARS:]
            context["prev_assistant_text"] = text
            token_count = getattr(entry, "token_count", None)
            if (
                isinstance(token_count, int)
                and not isinstance(token_count, bool)
                and token_count > 0
            ):
                context["prev_assistant_usage"] = {"output_tokens": token_count}
            return context
        return context

    def _resolve_prompt_config(self, turn: Any) -> tuple[str, list | None, str | None]:
        """Resolve final system prompt and cache breakpoints from pipeline output."""
        final_prompt = turn.system_prompt
        cache_breakpoints = None
        request_context_prompt = None

        if turn.metadata.get("cache_enabled") and isinstance(final_prompt, tuple):
            base, dynamic = final_prompt
            cache_breakpoints = [{"text": base, "cache": "true"}]
            final_prompt = base
            request_context_prompt = dynamic
        elif turn.metadata.get("cache_enabled") and isinstance(final_prompt, str):
            base = turn.metadata.get("cache_base_prompt") or final_prompt
            if isinstance(base, str) and base:
                cache_breakpoints = [{"text": base, "cache": "true"}]
        elif isinstance(final_prompt, tuple):
            final_prompt = "\n\n".join(final_prompt)

        return final_prompt, cache_breakpoints, request_context_prompt

    def _collect_session_flush_metadata(
        self,
        agent_id: str,
        *,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        """Collect last SessionFlush extraction attribution for decision logs."""

        svc = self._session_flush_service
        get_stats = getattr(svc, "last_extraction_stats", None)
        if not callable(get_stats):
            return {}
        try:
            try:
                stats = get_stats(agent_id, session_key) if session_key is not None else get_stats()
            except TypeError:
                stats = get_stats()
        except Exception:
            return {}
        if not isinstance(stats, dict) or not stats:
            return {}
        stat_agent = stats.get("agent_id")
        if stat_agent and str(stat_agent) != agent_id:
            return {}
        stat_session_key = stats.get("session_key")
        if session_key and stat_session_key and str(stat_session_key) != session_key:
            return {}
        fallback_reason = str(stats.get("fallback_reason") or "")
        return {
            "session_flush_extraction_model": str(stats.get("extraction_model") or ""),
            "session_flush_fallback_used": bool(fallback_reason),
            "session_flush_fallback_reason": fallback_reason,
        }

    async def _record_checkpoint_before_compaction(
        self,
        session_key: str,
        transcript: Sequence[Any],
        *,
        turn_id: str,
        source: str,
    ) -> bool:
        if self._session_manager is None:
            return False
        method = getattr(type(self._session_manager), "record_memory_checkpoint", None)
        if method is None:
            method = getattr(
                getattr(self._session_manager, "__dict__", {}),
                "get",
                lambda *_: None,
            )("record_memory_checkpoint")
        if not callable(method):
            return False
        async with self._session_write_context(session_key):
            receipt = await self._session_manager.record_memory_checkpoint(
                session_key,
                list(transcript),
                turn_id=turn_id,
                source=source,
            )
        return durable_receipt_allows_destructive_compaction(receipt)

    def _emit_decision_entry(
        self,
        *,
        turn_id: str,
        session_key: str,
        session_id: str | None = None,
        message: str,
        final_prompt: str,
        tool_defs: list[Any],
        turn_obj: Any | None,
        provider: Any | None,
        resolved_model: str,
        turn_started_at: float,
        prompt_report: PromptReport | None = None,
        session_intent: str | None = None,
        done_event: DoneEvent | None = None,
        trace_id: str | None = None,
        skills_invoked: list[str] | None = None,
    ) -> None:
        """Write one DecisionEntry for this turn (best-effort, never raises).

        Pipeline steps are read off ``turn_obj.metadata['pipeline_steps']``
        (populated by :func:`pipeline.run_pipeline`). Token counts are pulled
        from ``usage_tracker`` when available; otherwise default to 0.
        """

        try:
            # Flush the staged router decision record (V017 router_decisions)
            # with executed facts: executed_kind/ensemble_profile/fallback_hops
            # are only knowable now that the provider ran. Best-effort — the
            # hook never raises and no-ops when nothing was staged. The SQLite
            # insert is scheduled onto a worker thread (fire-and-forget) so a
            # contended WAL commit can never stall the event loop.
            if turn_obj is not None:
                from opensquilla.engine.steps.router_decision_record import (
                    schedule_router_decision_flush,
                )

                schedule_router_decision_flush(
                    turn_obj.metadata,
                    ensemble_trace=(
                        getattr(done_event, "ensemble_trace", None)
                        if done_event is not None
                        else None
                    ),
                )

            tool_names = [getattr(td, "name", "") for td in tool_defs]
            prompt_hash, system_prompt_hash, tool_list_hash = compute_hashes(
                message, final_prompt, [n for n in tool_names if n]
            )

            pipeline_steps: list[PipelineStepRecord] = []
            if turn_obj is not None:
                pipeline_steps = list(turn_obj.metadata.get("pipeline_steps", []))

            # Per-turn token counts come from the final DoneEvent (which carries
            # cumulative input_tokens / output_tokens for the whole turn). The
            # legacy code looked up `usage_tracker.last_input_tokens`, but
            # UsageTracker exposes only per-session aggregates and never had
            # `last_input_tokens` / `last_output_tokens` attributes — the
            # getattr defaults silently produced zero on every turn. See
            # engine/usage.py for the actual UsageTracker surface.
            if done_event is not None:
                tokens_input = int(done_event.input_tokens or 0)
                tokens_output = int(done_event.output_tokens or 0)
            else:
                tokens_input = 0
                tokens_output = 0

            latency_ms = int((time.monotonic() - turn_started_at) * 1000)
            ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            tool_choice = "auto" if tool_defs else "none"
            provider_name = type(provider).__name__ if provider is not None else ""

            # Populate SavingsTelemetry
            savings_telemetry = SavingsTelemetry()
            if turn_obj is not None:
                metadata = turn_obj.metadata
                router_cfg = getattr(self._turn_config(), "squilla_router", None)
                squilla_router_tiers = getattr(router_cfg, "tiers", {})

                # Squilla router
                savings_telemetry.routed_model = metadata.get("routed_model")
                savings_telemetry.baseline_model = metadata.get("baseline_model")
                savings_telemetry.routing_confidence = metadata.get("routing_confidence")
                savings_telemetry.routing_savings_pct = metadata.get("savings_pct")

                _max_p = float(metadata.get("savings_max_price_per_m") or 0.0)
                _rte_p = float(metadata.get("savings_routed_price_per_m") or 0.0)
                if done_event is not None:
                    savings_telemetry.routing_savings_usd_estimated_vs_baseline = (
                        _compute_route_input_savings_usd(
                            _max_p,
                            _rte_p,
                            done_event.input_tokens,
                        )
                    )

                # Tool-result projection (values will be set in agent.py)
                savings_telemetry.tool_projection_applied = metadata.get(
                    "tool_projection_applied",
                    False,
                )
                savings_telemetry.tool_projection_calls = metadata.get("tool_projection_calls", 0)
                savings_telemetry.tool_projection_tokens_before = metadata.get(
                    "tool_projection_tokens_before",
                    0,
                )
                savings_telemetry.tool_projection_tokens_after = metadata.get(
                    "tool_projection_tokens_after",
                    0,
                )
                savings_telemetry.tool_projection_tokens_saved = metadata.get(
                    "tool_projection_tokens_saved",
                    0,
                )
                savings_telemetry.tool_result_store_writes = metadata.get(
                    "tool_result_store_writes",
                    0,
                )
                savings_telemetry.tool_result_store_skips = metadata.get(
                    "tool_result_store_skips",
                    0,
                )

                # Thinking mode
                savings_telemetry.thinking_mode = metadata.get("thinking_mode")

                # Short-reply prompt enforcement
                savings_telemetry.short_reply_active = metadata.get("prompt_policy") == "P0"
                if savings_telemetry.short_reply_active and done_event is not None:
                    estimated_output_savings_pct = getattr(
                        router_cfg,
                        "estimated_output_savings_pct",
                        0.03,
                    )
                    output_side_tokens = _non_negative_int(
                        done_event.output_tokens
                    ) + _non_negative_int(done_event.reasoning_tokens)
                    restored_output_tokens = _restored_output_side_tokens(
                        output_side_tokens,
                        metadata,
                        estimated_output_savings_pct,
                    )
                    savings_telemetry.short_reply_savings_tokens_estimated = round(
                        max(0.0, restored_output_tokens - output_side_tokens)
                    )
                    baseline = _select_savings_baseline_model(
                        squilla_router_tiers,
                        _non_negative_int(done_event.input_tokens)
                        + _non_negative_int(
                            metadata.get("tool_projection_tokens_saved"),
                        ),
                        restored_output_tokens,
                    )
                    if baseline.price.output_per_m > 0:
                        savings_telemetry.short_reply_savings_usd_estimated_vs_baseline = round(
                            (savings_telemetry.short_reply_savings_tokens_estimated / 1_000_000)
                            * baseline.price.output_per_m,
                            6,
                        )

                # Cache Hit — fires when EITHER OpenSquilla's prompt-cache split
                # infra reports a hit OR the upstream provider returns
                # `cached_tokens > 0` (OpenRouter prompt-cache passthrough).
                # Without the OR, provider-side cache hits were silently
                # losing the active flag while still recording tokens_saved.
                provider_cache_hit = done_event is not None and (done_event.cached_tokens or 0) > 0
                opensquilla_cache_hit = metadata.get("cache_mode") == "hit"
                event_cache_hit = bool(getattr(done_event, "cache_hit_active", False))
                savings_telemetry.cache_hit_active = (
                    event_cache_hit or provider_cache_hit or opensquilla_cache_hit
                )
                if done_event is not None:
                    savings_telemetry.cache_hit_tokens_saved = done_event.cached_tokens
                    if savings_telemetry.cache_hit_tokens_saved > 0 and _max_p > 0:
                        savings_telemetry.cache_hit_usd_estimated_vs_baseline = round(
                            (savings_telemetry.cache_hit_tokens_saved / 1_000_000) * _max_p, 6
                        )

                savings_telemetry.billed_cost_usd = (
                    done_event.billed_cost if done_event is not None else None
                )
                savings_telemetry.cost_usd = done_event.cost_usd if done_event is not None else None
                savings_telemetry.cost_source = (
                    normalize_event_cost_source(
                        done_event.cost_source,
                        input_tokens=done_event.input_tokens,
                        output_tokens=done_event.output_tokens,
                        cache_read_tokens=done_event.cached_tokens,
                        cache_write_tokens=done_event.cache_write_tokens,
                        cost_usd=done_event.cost_usd,
                        billed_cost_usd=done_event.billed_cost,
                    )
                    if done_event is not None
                    else None
                )

                # Total savings is the comprehensive per-turn estimate used by
                # the popup. It intentionally excludes billed-cost and cache-hit
                # effects so it remains a token/price estimate.
                if done_event is not None:
                    savings_telemetry.total_savings_pct = done_event.total_savings_pct
                    savings_telemetry.total_savings_usd = done_event.total_savings_usd

            entry = DecisionEntry(
                turn_id=turn_id,
                session_key=session_key,
                session_id=session_id,
                session_intent=session_intent,
                intent_summary=build_intent_summary(message),
                trace_id=trace_id or turn_id,
                decision_id=(
                    turn_obj.metadata.get("router_decision_id") if turn_obj is not None else None
                ),
                tool_profile=prompt_report.tool_profile if prompt_report else None,
                prompt_hash=prompt_hash,
                system_prompt_hash=system_prompt_hash,
                tool_list_hash=tool_list_hash,
                tool_choice=tool_choice,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=resolved_model,
                provider=provider_name,
                latency_ms=latency_ms,
                ts=ts,
                skills_invoked=skills_invoked if skills_invoked is not None else [],
                pipeline_steps=pipeline_steps,
                savings=savings_telemetry,
                system_chars=prompt_report.system_chars if prompt_report else 0,
                tool_count=prompt_report.tool_count if prompt_report else 0,
                tools_schema_chars=prompt_report.tools_schema_chars if prompt_report else 0,
                skill_count=prompt_report.skill_count if prompt_report else 0,
                skills_prompt_chars=prompt_report.skills_prompt_chars if prompt_report else 0,
                memory_md_present=prompt_report.memory_md_present if prompt_report else False,
                daily_notes_omitted=(prompt_report.daily_notes_omitted if prompt_report else False),
                daily_notes_count_before_omit=(
                    prompt_report.daily_notes_count_before_omit if prompt_report else 0
                ),
                daily_notes_policy_reason=(
                    prompt_report.daily_notes_policy_reason if prompt_report else None
                ),
                injected_workspace_files_count=(
                    prompt_report.injected_workspace_files_count if prompt_report else 0
                ),
                bootstrap_files=prompt_report.bootstrap_files if prompt_report else [],
                memory_mode_fingerprint=(
                    prompt_report.memory_mode_fingerprint if prompt_report else {}
                ),
                retrieval_mode=prompt_report.retrieval_mode if prompt_report else None,
                cache_mode=prompt_report.cache_mode if prompt_report else None,
                cache_base_hash=prompt_report.cache_base_hash if prompt_report else None,
                cache_dynamic_hash=(prompt_report.cache_dynamic_hash if prompt_report else None),
                cache_read_input_tokens=(
                    int(done_event.cached_tokens or 0) if done_event is not None else 0
                ),
                cache_creation_input_tokens=(
                    int(done_event.cache_write_tokens or 0) if done_event is not None else 0
                ),
                resolved_model=(prompt_report.resolved_model if prompt_report else None)
                or resolved_model,
                alias_resolution_chain=(
                    prompt_report.alias_resolution_chain
                    if prompt_report and prompt_report.alias_resolution_chain
                    else ([resolved_model] if resolved_model else [])
                ),
                provider_after_rewrite=(
                    prompt_report.provider_after_rewrite if prompt_report else None
                )
                or provider_name,
                cache_legacy_hash=prompt_report.cache_legacy_hash if prompt_report else None,
                cache_shadow_final_hash=(
                    prompt_report.cache_shadow_final_hash if prompt_report else None
                ),
                cache_key_collision=(prompt_report.cache_key_collision if prompt_report else False),
                reasoning_hint_resolved=(
                    prompt_report.reasoning_hint_resolved if prompt_report else None
                ),
                cache_base_chars=prompt_report.cache_base_chars if prompt_report else 0,
                cache_dynamic_chars=prompt_report.cache_dynamic_chars if prompt_report else 0,
                runtime_context_hash=(
                    done_event.runtime_context_hash if done_event is not None else None
                ),
                runtime_context_chars=(
                    done_event.runtime_context_chars if done_event is not None else 0
                ),
                session_flush_extraction_model=(
                    prompt_report.session_flush_extraction_model if prompt_report else None
                ),
                session_flush_fallback_used=(
                    prompt_report.session_flush_fallback_used if prompt_report else False
                ),
                session_flush_fallback_reason=(
                    prompt_report.session_flush_fallback_reason if prompt_report else None
                ),
                image_route_reason=(
                    turn_obj.metadata.get("image_route_reason") if turn_obj is not None else None
                ),
                vision_followup_gate_decision=(
                    turn_obj.metadata.get("router_vision_followup_gate_decision")
                    if turn_obj is not None
                    else None
                ),
                vision_followup_gate_confidence=(
                    turn_obj.metadata.get("router_vision_followup_gate_confidence")
                    if turn_obj is not None
                    else None
                ),
                vision_followup_gate_reason=(
                    build_vision_followup_gate_reason_code(
                        decision=turn_obj.metadata.get("router_vision_followup_gate_decision"),
                        source=turn_obj.metadata.get("router_vision_followup_gate_source"),
                        reason=turn_obj.metadata.get("router_vision_followup_gate_reason"),
                        fallback=turn_obj.metadata.get("router_vision_followup_fallback"),
                    )
                    if turn_obj is not None
                    else None
                ),
                vision_followup_gate_source=(
                    turn_obj.metadata.get("router_vision_followup_gate_source")
                    if turn_obj is not None
                    else None
                ),
                vision_followup_gate_model=(
                    turn_obj.metadata.get("router_vision_followup_gate_model")
                    if turn_obj is not None
                    else None
                ),
                vision_followup_needs_image=(
                    turn_obj.metadata.get("router_vision_followup_needs_image")
                    if turn_obj is not None
                    else None
                ),
                vision_followup_fallback=(
                    turn_obj.metadata.get("router_vision_followup_fallback")
                    if turn_obj is not None
                    else None
                ),
            )
            write_decision_entry(entry)
        except Exception as exc:  # pragma: no cover — observability must not break turns
            log.warning("decision_log.write_failed", error=str(exc))

    def _emit_router_train_sample(
        self,
        *,
        agent_id: str,
        session_key: str,
        turn_obj: Any | None,
        message: str,
    ) -> None:
        """Append one self-learning sample for this turn (best-effort).

        Opt-in (``squilla_router.self_learning.{enabled,capture_enabled}``) and
        kill-switched. Writes the float16 feature vectors the model produced plus
        the routing decision; never raw prompt text (unless the audit sidecar is
        explicitly enabled). Must never break turn execution.
        """

        try:
            if turn_obj is None:
                return
            router_cfg = getattr(self._turn_config(), "squilla_router", None)
            sl = getattr(router_cfg, "self_learning", None)
            if sl is None or not getattr(sl, "enabled", False):
                return
            if not getattr(sl, "capture_enabled", True):
                return

            from opensquilla.squilla_router.self_learning import (
                self_learning_disabled_by_env,
                write_sample,
            )
            from opensquilla.squilla_router.self_learning.capture import build_train_sample

            if self_learning_disabled_by_env():
                return

            sample = build_train_sample(
                session_key=session_key,
                metadata=turn_obj.metadata,
                store_audit_summary=bool(getattr(sl, "store_audit_summary", False)),
                message=message,
            )
            if sample is None:
                return
            write_sample(sample, agent_id)
        except Exception as exc:  # pragma: no cover — capture must not break turns
            log.warning("router_self_learning.capture_failed", error=str(exc))

    async def _maybe_compact_on_t3_upgrade(
        self,
        session_key: str,
        turn: TurnContext,
        context_window_tokens: int,
        *,
        compaction_provider: Any | None = None,
        compaction_model: str | None = None,
    ) -> str:
        """Flush memory and compact transcript when the router upgrades into t3.

        Returns a status string so the caller can distinguish non-applicable
        routes, flush failures that may still fall back to generic preflight,
        and compact failures that should trip the circuit without retrying.
        """
        router_cfg = getattr(self._turn_config(), "squilla_router", None)
        upgrade_compaction_enabled = getattr(
            router_cfg,
            "upgrade_to_c3_compaction_enabled",
            getattr(router_cfg, "upgrade_to_t3_compaction_enabled", False),
        )
        if not upgrade_compaction_enabled:
            return _T3_NOT_APPLICABLE

        routed_tier = normalize_text_tier(turn.metadata.get("routed_tier"))
        if routed_tier != HIGHEST_TEXT_TIER:
            return _T3_NOT_APPLICABLE

        if not turn.metadata.get("routing_applied", False):
            return _T3_NOT_APPLICABLE

        routing_extra = turn.metadata.get("routing_extra", {})
        previous = normalize_text_tier(routing_extra.get("previous_tier"))
        if previous is None:
            final = normalize_text_tier(routing_extra.get("final_tier"))
            base = normalize_text_tier(routing_extra.get("base_tier"))
            if final == HIGHEST_TEXT_TIER and tier_index(base) in {0, 1, 2}:
                previous = base
            else:
                return _T3_NOT_APPLICABLE

        if tier_index(previous) not in {0, 1, 2}:
            return _T3_NOT_APPLICABLE

        if session_key.startswith(("cron:", "subagent:")):
            return _T3_NOT_APPLICABLE

        if self._session_manager is None:
            return _T3_NOT_APPLICABLE

        if self.has_compacted_this_turn(session_key):
            log.info(
                "t3_upgrade_compaction.skipped",
                session_key=session_key,
                reason="already_compacted_this_turn",
            )
            return _T3_HANDLED
        if self.has_attempted_compaction_this_turn(session_key):
            log.info(
                "t3_upgrade_compaction.skipped",
                session_key=session_key,
                reason="already_attempted_this_turn",
            )
            return _T3_HANDLED

        try:
            transcript = await self._session_manager.get_transcript(session_key)
        except KeyError:
            return _T3_HANDLED
        if not transcript:
            return _T3_HANDLED

        compaction_config = None
        configured_compaction = getattr(getattr(self, "_config", None), "compaction", None)
        if compaction_provider is not None or compaction_model or configured_compaction is not None:
            from opensquilla.session.compaction import build_compaction_config_from_provider

            compaction_config = build_compaction_config_from_provider(
                compaction_provider,
                model_override=compaction_model,
                compaction_config=configured_compaction,
            )

        from opensquilla.session.compaction import (
            CompactionConfig,
            estimate_entry_model_replay_tokens,
        )

        # Measure what the model actually replays (full tool_calls JSON), the
        # same estimator preflight uses. The summarized estimator undercounts
        # tool-heavy transcripts, so a within-budget "handled" verdict computed
        # from it would suppress the correct-estimator preflight fallback.
        total_tokens = sum(estimate_entry_model_replay_tokens(e) for e in transcript)
        safety_margin = float(
            getattr(compaction_config or CompactionConfig(), "safety_margin", 1.2) or 1.2
        )
        if total_tokens * safety_margin <= context_window_tokens:
            log.info(
                "t3_upgrade_compaction.skipped",
                session_key=session_key,
                reason="within_budget",
                total_tokens=total_tokens,
                context_window_tokens=context_window_tokens,
                safety_margin=safety_margin,
            )
            return _T3_HANDLED
        if self._compaction_circuit_open(session_key):
            self.mark_compaction_attempted_this_turn(session_key)
            await self._record_emergency_ephemeral_compaction(
                session_key,
                transcript,
                context_window_tokens,
                compaction_id=new_compaction_id(),
                phase="t3_upgrade",
                reason="durable_compaction_circuit_open",
            )
            return _T3_HANDLED

        log.info(
            "t3_upgrade_compaction.triggered",
            session_key=session_key,
            previous_tier=previous,
            final_tier=HIGHEST_TEXT_TIER,
            context_window_tokens=context_window_tokens,
        )
        self.mark_compaction_attempted_this_turn(session_key)
        compaction_id = new_compaction_id()
        notify_compaction(
            session_key,
            source="automatic",
            phase="t3_upgrade",
            status="started",
            previous_tier=previous,
            context_window_tokens=context_window_tokens,
            **compaction_effect_payload(status="started"),
            **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
        )

        checkpoint_saved = await self._record_checkpoint_before_compaction(
            session_key,
            transcript,
            turn_id=compaction_id,
            source="t3_upgrade_compaction",
        )
        flush_receipt = None
        flush_receipt_status = "not_required"
        requires_safe_receipt = self._pre_compaction_flush_requires_safe_receipt()
        if self._pre_compaction_flush_enabled():
            flush_receipt = await self._await_pre_compaction_flush_grace(
                transcript,
                session_key,
                event_prefix="t3_upgrade_compaction",
                wait_for_receipt=requires_safe_receipt,
                turn_id=compaction_id,
                checkpoint_exists=checkpoint_saved,
            )
            flush_receipt_status = flush_receipt_status_for_compaction(
                flush_receipt,
                self._config,
            )
            memory_status = compaction_memory_status(
                flush_receipt,
                deterministic_receipt_safe=checkpoint_saved and not requires_safe_receipt,
                required=self._pre_compaction_flush_enabled(),
            )
            if requires_safe_receipt and not memory_status.allows_destructive_compaction:
                log.warning(
                    "t3_upgrade_compaction.skipped",
                    session_key=session_key,
                    reason="unsafe_flush_receipt",
                )
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="t3_upgrade",
                    status="skipped",
                    reason="unsafe_flush_receipt",
                    context_window_tokens=context_window_tokens,
                    flush_receipt_status=flush_receipt_status,
                    memory_safety_status=memory_status.safety_status,
                    semantic_memory_status=memory_status.semantic_status,
                    **compaction_effect_payload(
                        status="skipped",
                        reason="unsafe_flush_receipt",
                    ),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                return _T3_HANDLED

        try:
            from opensquilla.session.compaction import call_compact_with_optional_config

            compaction_result = None
            compact_with_result = getattr(type(self._session_manager), "compact_with_result", None)
            if callable(compact_with_result):
                compact_method = self._session_manager.compact_with_result
                compact_kwargs: dict[str, Any] = {}
                if _accepts_keyword_arg(compact_method, "compaction_id"):
                    compact_kwargs["compaction_id"] = compaction_id
                if _accepts_keyword_arg(compact_method, "trigger_reason"):
                    compact_kwargs["trigger_reason"] = "t3_upgrade"
                if _accepts_keyword_arg(compact_method, "flush_receipt_status"):
                    compact_kwargs["flush_receipt_status"] = flush_receipt_status
                if _accepts_keyword_arg(compact_method, "mutation_context"):
                    compact_kwargs["mutation_context"] = self._session_write_context_factory(
                        session_key
                    )
                compaction_result = await self._session_manager.compact_with_result(
                    session_key,
                    context_window_tokens,
                    compaction_config,
                    **compact_kwargs,
                )
                result = getattr(compaction_result, "summary", "") or ""
            else:
                result = await call_compact_with_optional_config(
                    self._session_manager.compact,
                    session_key,
                    context_window_tokens,
                    compaction_config,
                )
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
                        phase="t3_upgrade",
                        status="observed",
                        context_window_tokens=context_window_tokens,
                        flush_receipt_status=flush_receipt_status,
                        **compaction_effect_payload(status="observed"),
                        **observed_payload,
                    )
            if result:
                self.mark_compacted_this_turn(session_key)
                self._record_compaction_success(session_key)
                completed_payload = {"summary_len": len(result)}
                if compaction_result is not None:
                    completed_payload.update(compaction_result_payload(compaction_result))
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="t3_upgrade",
                    status="completed",
                    context_window_tokens=context_window_tokens,
                    flush_receipt_status=flush_receipt_status,
                    **compaction_effect_payload(status="completed"),
                    **completed_payload,
                    **compaction_lifecycle_payload(compaction_id, COMPACTION_PERSISTED_EVENT),
                )
            else:
                skip_reason = str(
                    getattr(compaction_result, "skip_reason", None) or "empty_summary"
                )
                if skip_reason != "stale_preimage":
                    emergency_applied = await self._record_emergency_ephemeral_compaction(
                        session_key,
                        transcript,
                        context_window_tokens,
                        compaction_id=compaction_id,
                        phase="t3_upgrade",
                        reason=skip_reason,
                    )
                    if emergency_applied:
                        return _T3_HANDLED
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="t3_upgrade",
                    status="skipped",
                    reason=skip_reason,
                    context_window_tokens=context_window_tokens,
                    flush_receipt_status=flush_receipt_status,
                    **compaction_effect_payload(
                        status="skipped",
                        reason=skip_reason,
                    ),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
            log.info(
                "t3_upgrade_compaction.compact_done",
                session_key=session_key,
                summary_produced=bool(result),
                summary_length=len(result) if result else 0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "t3_upgrade_compaction.compact_failed",
                session_key=session_key,
                error=str(exc),
            )
            self._record_compaction_failure(session_key)
            emergency_applied = await self._record_emergency_ephemeral_compaction(
                session_key,
                transcript,
                context_window_tokens,
                compaction_id=compaction_id,
                phase="t3_upgrade",
                reason="compact_failed",
            )
            if emergency_applied:
                return _T3_COMPACT_FAILED
            notify_compaction(
                session_key,
                source="automatic",
                phase="t3_upgrade",
                status="failed",
                message=str(exc),
                context_window_tokens=context_window_tokens,
                flush_receipt_status=flush_receipt_status,
                **compaction_effect_payload(status="failed"),
                **compaction_lifecycle_payload(
                    compaction_id,
                    COMPACTION_TRIGGERED_EVENT,
                ),
            )
            return _T3_COMPACT_FAILED

        return _T3_HANDLED

    async def _maybe_preflight_compact(
        self,
        session_key: str,
        context_window_tokens: int,
        *,
        compaction_provider: Any | None = None,
        compaction_model: str | None = None,
    ) -> None:
        """Compact proactively if session history exceeds token budget.

        Called before _load_history(). Uses SessionManager.compact() directly
        because no Agent state exists yet — the DB is the sole source of truth.
        Safe to re-compact from DB at this point (no double-compaction risk).
        """
        if self._session_manager is None:
            return
        # Skip ephemeral sessions
        if session_key.startswith(("cron:", "subagent:")):
            return
        if self.has_compacted_this_turn(session_key):
            log.info(
                "preflight_compaction.skipped",
                session_key=session_key,
                reason="already_compacted_this_turn",
            )
            return
        if self.has_attempted_compaction_this_turn(session_key):
            log.info(
                "preflight_compaction.skipped",
                session_key=session_key,
                reason="already_attempted_this_turn",
            )
            return
        try:
            transcript = await self._session_manager.get_transcript(session_key)
        except KeyError:
            return  # session doesn't exist yet
        if not transcript:
            return

        from opensquilla.session.compaction import estimate_entry_model_replay_tokens

        total_tokens = sum(estimate_entry_model_replay_tokens(e) for e in transcript)
        ratio = self._preflight_compact_ratio()
        threshold = int(context_window_tokens * ratio)
        if total_tokens <= threshold:
            return
        if self._compaction_circuit_open(session_key):
            self.mark_compaction_attempted_this_turn(session_key)
            await self._record_emergency_ephemeral_compaction(
                session_key,
                transcript,
                context_window_tokens,
                compaction_id=new_compaction_id(),
                phase="preflight",
                reason="durable_compaction_circuit_open",
            )
            return

        log.info(
            "preflight_compaction.triggered",
            session_key=session_key,
            total_tokens=total_tokens,
            threshold=threshold,
            ratio=ratio,
        )
        self.mark_compaction_attempted_this_turn(session_key)
        compaction_id = new_compaction_id()
        notify_compaction(
            session_key,
            source="automatic",
            phase="preflight",
            status="started",
            tokens_before=total_tokens,
            context_window_tokens=context_window_tokens,
            **compaction_effect_payload(status="started"),
            **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
        )
        checkpoint_saved = await self._record_checkpoint_before_compaction(
            session_key,
            transcript,
            turn_id=compaction_id,
            source="preflight_compaction",
        )
        flush_receipt = None
        flush_receipt_status = "not_required"
        requires_safe_receipt = self._pre_compaction_flush_requires_safe_receipt()
        if self._pre_compaction_flush_enabled():
            flush_receipt = await self._await_pre_compaction_flush_grace(
                transcript,
                session_key,
                event_prefix="preflight_compaction",
                wait_for_receipt=requires_safe_receipt,
                turn_id=compaction_id,
                checkpoint_exists=checkpoint_saved,
            )
            flush_receipt_status = flush_receipt_status_for_compaction(
                flush_receipt,
                self._config,
            )
            memory_status = compaction_memory_status(
                flush_receipt,
                deterministic_receipt_safe=checkpoint_saved and not requires_safe_receipt,
                required=self._pre_compaction_flush_enabled(),
            )
            if requires_safe_receipt and not memory_status.allows_destructive_compaction:
                log.warning(
                    "preflight_compaction.skipped",
                    session_key=session_key,
                    reason="unsafe_flush_receipt",
                )
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="preflight",
                    status="skipped",
                    reason="unsafe_flush_receipt",
                    tokens_before=total_tokens,
                    context_window_tokens=context_window_tokens,
                    flush_receipt_status=flush_receipt_status,
                    memory_safety_status=memory_status.safety_status,
                    semantic_memory_status=memory_status.semantic_status,
                    **compaction_effect_payload(
                        status="skipped",
                        reason="unsafe_flush_receipt",
                    ),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                return
        skip_reason = "empty_summary"
        compaction_config = None
        configured_compaction = getattr(getattr(self, "_config", None), "compaction", None)
        if compaction_provider is not None or compaction_model or configured_compaction is not None:
            from opensquilla.session.compaction import build_compaction_config_from_provider

            compaction_config = build_compaction_config_from_provider(
                compaction_provider,
                model_override=compaction_model,
                compaction_config=configured_compaction,
            )
        from opensquilla.session.compaction import call_compact_with_optional_config

        try:
            compaction_result = None
            compact_with_result = getattr(type(self._session_manager), "compact_with_result", None)
            if callable(compact_with_result):
                compact_method = self._session_manager.compact_with_result
                compact_kwargs: dict[str, Any] = {}
                if _accepts_keyword_arg(compact_method, "compaction_id"):
                    compact_kwargs["compaction_id"] = compaction_id
                if _accepts_keyword_arg(compact_method, "trigger_reason"):
                    compact_kwargs["trigger_reason"] = "preflight"
                if _accepts_keyword_arg(compact_method, "flush_receipt_status"):
                    compact_kwargs["flush_receipt_status"] = flush_receipt_status
                if _accepts_keyword_arg(compact_method, "mutation_context"):
                    compact_kwargs["mutation_context"] = self._session_write_context_factory(
                        session_key
                    )
                compaction_result = await self._session_manager.compact_with_result(
                    session_key,
                    context_window_tokens,
                    compaction_config,
                    **compact_kwargs,
                )
                result = getattr(compaction_result, "summary", "") or ""
            else:
                result = await call_compact_with_optional_config(
                    self._session_manager.compact,
                    session_key,
                    context_window_tokens,
                    compaction_config,
                )
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
                    observed_payload.update(
                        compaction_result_payload(
                            compaction_result,
                            tokens_before=total_tokens,
                        )
                    )
                    notify_compaction(
                        session_key,
                        source="automatic",
                        phase="preflight",
                        status="observed",
                        context_window_tokens=context_window_tokens,
                        flush_receipt_status=flush_receipt_status,
                        **compaction_effect_payload(status="observed"),
                        **observed_payload,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "preflight_compaction.compact_failed",
                session_key=session_key,
                error=str(exc),
            )
            self._record_compaction_failure(session_key)
            emergency_applied = await self._record_emergency_ephemeral_compaction(
                session_key,
                transcript,
                context_window_tokens,
                compaction_id=compaction_id,
                phase="preflight",
                reason="compact_failed",
            )
            if emergency_applied:
                return
            notify_compaction(
                session_key,
                source="automatic",
                phase="preflight",
                status="failed",
                message=str(exc),
                tokens_before=total_tokens,
                context_window_tokens=context_window_tokens,
                flush_receipt_status=flush_receipt_status,
                **compaction_effect_payload(status="failed"),
                **compaction_lifecycle_payload(
                    compaction_id,
                    COMPACTION_TRIGGERED_EVENT,
                ),
            )
            return
        if not result:
            skip_reason = str(getattr(compaction_result, "skip_reason", None) or "empty_summary")
            if skip_reason == "stale_preimage":
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase="preflight",
                    status="skipped",
                    reason=skip_reason,
                    tokens_before=total_tokens,
                    context_window_tokens=context_window_tokens,
                    flush_receipt_status=flush_receipt_status,
                    **compaction_effect_payload(
                        status="skipped",
                        reason=skip_reason,
                    ),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                return
            emergency_applied = await self._record_emergency_ephemeral_compaction(
                session_key,
                transcript,
                context_window_tokens,
                compaction_id=compaction_id,
                phase="preflight",
                reason=skip_reason,
            )
            if emergency_applied:
                return
        if result:
            self.mark_compacted_this_turn(session_key)
            self._record_compaction_success(session_key)
            completed_payload = {"tokens_before": total_tokens}
            if compaction_result is not None:
                completed_payload.update(
                    compaction_result_payload(
                        compaction_result,
                        tokens_before=total_tokens,
                    )
                )
            notify_compaction(
                session_key,
                source="automatic",
                phase="preflight",
                status="completed",
                context_window_tokens=context_window_tokens,
                flush_receipt_status=flush_receipt_status,
                **compaction_effect_payload(status="completed"),
                **completed_payload,
                **compaction_lifecycle_payload(compaction_id, COMPACTION_PERSISTED_EVENT),
            )
        else:
            notify_compaction(
                session_key,
                source="automatic",
                phase="preflight",
                status="skipped",
                reason=skip_reason,
                tokens_before=total_tokens,
                context_window_tokens=context_window_tokens,
                flush_receipt_status=flush_receipt_status,
                **compaction_effect_payload(
                    status="skipped",
                    reason=skip_reason,
                ),
                **compaction_lifecycle_payload(
                    compaction_id,
                    COMPACTION_TRIGGERED_EVENT,
                ),
            )

    def _pre_compaction_flush_enabled(self) -> bool:
        return flush_trigger_enabled(self._config, "pre_compaction")

    def _pre_compaction_flush_requires_safe_receipt(self) -> bool:
        return pre_compaction_flush_requires_safe_receipt(self._config)

    def _pre_compaction_flush_timeout_seconds(self) -> float:
        memory_cfg = getattr(self._config, "memory", None)
        raw_timeout = getattr(memory_cfg, "flush_timeout_seconds", 15.0)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return 15.0
        return max(timeout, 0.0)

    def _pre_compaction_flush_background_timeout_seconds(self) -> float:
        memory_cfg = getattr(self._config, "memory", None)
        raw_timeout = getattr(memory_cfg, "flush_background_timeout_seconds", 120.0)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return 120.0
        return max(timeout, 0.0)

    async def _await_pre_compaction_flush_grace(
        self,
        transcript: list[Any],
        session_key: str,
        *,
        event_prefix: str,
        wait_for_receipt: bool | None = None,
        turn_id: str | None = None,
        checkpoint_exists: bool | None = None,
    ) -> Any | None:
        if self._session_flush_service is None:
            log.warning(
                f"{event_prefix}.flush_unavailable",
                session_key=session_key,
                error="flush_service_unavailable",
            )
            return None

        should_wait = (
            self._pre_compaction_flush_requires_safe_receipt()
            if wait_for_receipt is None
            else bool(wait_for_receipt)
        )
        background_timeout = self._pre_compaction_flush_background_timeout_seconds()
        task = self._active_pre_compaction_flush_tasks.get(session_key)
        if task is not None:
            if task.done():
                try:
                    receipt = task.result()
                except asyncio.CancelledError:
                    log.debug(f"{event_prefix}.flush_cancelled", session_key=session_key)
                    return None
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        f"{event_prefix}.flush_failed",
                        session_key=session_key,
                        error=str(exc),
                    )
                    return None
                self._consume_pre_compaction_flush_task(session_key, task, event_prefix)
                return receipt
            log.debug(
                f"{event_prefix}.flush_skipped",
                session_key=session_key,
                reason="already_running",
                waiting=should_wait,
            )
            if not should_wait:
                return None

        else:
            from opensquilla.session.keys import parse_agent_id

            task = asyncio.create_task(
                self._session_flush_service.execute(
                    transcript,
                    session_key,
                    agent_id=parse_agent_id(session_key),
                    message_window=0,
                    segment_mode="auto",
                    timeout=background_timeout,
                    raw_capture_policy="required",
                    turn_id=turn_id,
                    checkpoint_exists=checkpoint_exists,
                )
            )
            self._active_pre_compaction_flush_tasks[session_key] = task
            task.add_done_callback(
                lambda completed: self._consume_pre_compaction_flush_task(
                    session_key,
                    completed,
                    event_prefix,
                    background=True,
                    compaction_id=turn_id,
                )
            )
            if not should_wait:
                log.info(
                    f"{event_prefix}.flush_background_started",
                    session_key=session_key,
                    background_timeout_seconds=background_timeout,
                )
                return None

        grace_timeout = self._pre_compaction_flush_timeout_seconds()
        flush_t0 = time.monotonic()
        try:
            receipt = await asyncio.wait_for(asyncio.shield(task), timeout=grace_timeout)
        except TimeoutError:
            log.warning(
                f"{event_prefix}.flush_timed_out",
                session_key=session_key,
                timeout_seconds=grace_timeout,
                background_timeout_seconds=background_timeout,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._active_pre_compaction_flush_tasks.get(session_key) is task:
                self._active_pre_compaction_flush_tasks.pop(session_key, None)
            log.warning(
                f"{event_prefix}.flush_failed",
                session_key=session_key,
                error=str(exc),
            )
            return None

        if self._active_pre_compaction_flush_tasks.get(session_key) is task:
            self._active_pre_compaction_flush_tasks.pop(session_key, None)
        self._log_pre_compaction_flush_receipt(
            event_prefix,
            session_key,
            receipt,
            duration_ms=int((time.monotonic() - flush_t0) * 1000),
            background=False,
        )
        return receipt

    def _consume_pre_compaction_flush_task(
        self,
        session_key: str,
        task: asyncio.Task,
        event_prefix: str,
        *,
        background: bool = False,
        compaction_id: str | None = None,
    ) -> None:
        if self._active_pre_compaction_flush_tasks.get(session_key) is not task:
            return
        self._active_pre_compaction_flush_tasks.pop(session_key, None)
        try:
            receipt = task.result()
        except asyncio.CancelledError:
            log.debug(f"{event_prefix}.flush_cancelled", session_key=session_key)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"{event_prefix}.flush_failed",
                session_key=session_key,
                error=str(exc),
                background=background,
            )
            if background and compaction_id:
                self._schedule_pre_compaction_flush_status_update(
                    session_key,
                    compaction_id,
                    "failed_retryable",
                    event_prefix,
                )
        else:
            self._log_pre_compaction_flush_receipt(
                event_prefix,
                session_key,
                receipt,
                duration_ms=getattr(receipt, "duration_ms", 0),
                background=background,
            )
            if background and compaction_id:
                self._schedule_pre_compaction_flush_status_update(
                    session_key,
                    compaction_id,
                    flush_receipt_status_for_compaction(receipt, self._config),
                    event_prefix,
                )

    def _schedule_pre_compaction_flush_status_update(
        self,
        session_key: str,
        compaction_id: str,
        status: str,
        event_prefix: str,
    ) -> None:
        if self._session_manager is None:
            return
        mark_status = getattr(self._session_manager, "mark_compaction_flush_receipt_status", None)
        if not callable(mark_status):
            return
        asyncio.create_task(
            mark_compaction_flush_status_with_retry(
                mark_status,
                session_key=session_key,
                compaction_id=compaction_id,
                status=status,
                log=log,
                failed_event=f"{event_prefix}.flush_status_update_failed",
                updated_event=f"{event_prefix}.flush_status_updated",
                skipped_event=f"{event_prefix}.flush_status_update_skipped",
            )
        )

    def _log_pre_compaction_flush_receipt(
        self,
        event_prefix: str,
        session_key: str,
        receipt: Any,
        *,
        duration_ms: int,
        background: bool,
    ) -> None:
        result_status = getattr(receipt, "result_status", None)
        if flush_receipt_is_successful_flush(receipt):
            log.info(
                f"{event_prefix}.flush_done",
                session_key=session_key,
                mode=getattr(receipt, "mode", "unknown"),
                result_status=result_status,
                message_count=getattr(receipt, "message_count", 0),
                duration_ms=duration_ms,
                background=background,
            )
            return

        log.warning(
            f"{event_prefix}.flush_degraded",
            session_key=session_key,
            error=getattr(receipt, "error", None) or "degraded_flush_receipt",
            mode=getattr(receipt, "mode", "unknown"),
            result_status=result_status,
            integrity_status=getattr(receipt, "integrity_status", None),
            indexed_chunk_count=getattr(receipt, "indexed_chunk_count", None),
            output_coverage_status=getattr(receipt, "output_coverage_status", None),
            invalid_candidate_count=getattr(receipt, "invalid_candidate_count", None),
            candidate_missing_ids=getattr(receipt, "candidate_missing_ids", None),
            obligation_status=getattr(receipt, "obligation_status", None),
            obligation_missing_ids=getattr(receipt, "obligation_missing_ids", None),
            background=background,
        )

    @staticmethod
    def _receipt_value(receipt: Any, name: str, default: Any) -> Any:
        if isinstance(receipt, Mapping):
            return receipt.get(name, default)
        return getattr(receipt, name, default)

    @staticmethod
    def _receipt_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _flush_receipt_allows_destructive_compaction(self, receipt: Any) -> bool:
        return flush_receipt_allows_destructive_compaction(receipt)

    def _compaction_circuit_open(self, session_key: str) -> bool:
        state = getattr(self, "_compaction_failures", {}).get(session_key)
        if state is None or state.count < _COMPACTION_FAILURE_LIMIT:
            return False
        opened_at = state.opened_at if state.opened_at is not None else time.monotonic()
        cooldown_elapsed = time.monotonic() - opened_at
        if cooldown_elapsed >= _COMPACTION_CIRCUIT_COOLDOWN_SECONDS:
            log.info(
                "compaction_circuit.half_open",
                session_key=session_key,
                consecutive_failures=state.count,
                cooldown_elapsed_s=round(cooldown_elapsed, 1),
            )
            return False
        log.warning(
            "compaction_circuit.open",
            session_key=session_key,
            consecutive_failures=state.count,
            cooldown_remaining_s=round(
                _COMPACTION_CIRCUIT_COOLDOWN_SECONDS - cooldown_elapsed,
                1,
            ),
        )
        return True

    def _record_compaction_failure(self, session_key: str) -> None:
        if not hasattr(self, "_compaction_failures"):
            self._compaction_failures = {}
        state = self._compaction_failures.setdefault(session_key, _CompactionFailureState())
        state.count += 1
        state.opened_at = time.monotonic() if state.count >= _COMPACTION_FAILURE_LIMIT else None

    def _record_compaction_success(self, session_key: str) -> None:
        if not hasattr(self, "_compaction_failures"):
            self._compaction_failures = {}
        self._compaction_failures.pop(session_key, None)

    @staticmethod
    def _entry_for_emergency_compaction(entry: Any) -> dict[str, Any]:
        return {
            "role": getattr(entry, "role", "user"),
            "content": getattr(entry, "content", "") or "",
            "token_count": getattr(entry, "token_count", None),
            "tool_calls": getattr(entry, "tool_calls", None),
            "tool_call_id": getattr(entry, "tool_call_id", None),
            "reasoning_content": getattr(entry, "reasoning_content", None),
            "turn_usage": getattr(entry, "turn_usage", None),
        }

    @staticmethod
    def _emergency_replay_entry(raw: Mapping[str, Any]) -> Any:
        return SimpleNamespace(
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            token_count=raw.get("token_count"),
            tool_calls=raw.get("tool_calls"),
            tool_call_id=raw.get("tool_call_id"),
            reasoning_content=raw.get("reasoning_content"),
            turn_usage=raw.get("turn_usage"),
        )

    async def _record_emergency_ephemeral_compaction(
        self,
        session_key: str,
        transcript: Sequence[Any],
        context_window_tokens: int,
        *,
        compaction_id: str,
        phase: str,
        reason: str,
    ) -> bool:
        if not transcript:
            return False
        try:
            from opensquilla.session.compaction import (
                CompactionConfig,
                CompactionRequest,
                compact_context,
            )

            raw_entries = [self._entry_for_emergency_compaction(entry) for entry in transcript]
            session_id = str(getattr(transcript[0], "session_id", "") or session_key)
            result = await compact_context(
                CompactionRequest(
                    session_id=session_id,
                    entries=raw_entries,
                    context_window_tokens=context_window_tokens,
                    config=CompactionConfig(model=None, api_key=""),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "compaction.emergency_ephemeral_failed",
                session_key=session_key,
                phase=phase,
                error=str(exc),
            )
            return False

        if not result.summary or result.removed_count <= 0:
            return False
        kept_entries = [self._emergency_replay_entry(raw) for raw in result.kept_entries]
        if not kept_entries or len(kept_entries) >= len(transcript):
            return False
        summary = f"Emergency request-scoped compaction\nReason: {reason}\n\n{result.summary}"
        self._emergency_compaction_overrides[session_key] = _EmergencyCompactionOverride(
            summary=summary,
            kept_entries=kept_entries,
            reason=reason,
            compaction_id=compaction_id,
        )
        self.mark_compacted_this_turn(session_key)
        notify_compaction(
            session_key,
            source="automatic",
            phase=phase,
            status="emergency_ephemeral",
            reason=reason,
            removed_count=result.removed_count,
            kept_count=len(kept_entries),
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            flush_receipt_status="emergency_ephemeral",
            **compaction_effect_payload(
                status="emergency_ephemeral",
                reason=reason,
            ),
            **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
        )
        return True

    def _preflight_compact_ratio(self) -> float:
        raw_ratio = getattr(self._config, "preflight_compact_ratio", None)
        if raw_ratio is None:
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        if ratio <= 0.0 or ratio > 1.0:
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        return ratio

    async def _rollback_cancelled_prompt(
        self,
        session_key: str,
        message_id: str,
    ) -> bool:
        """Keep the ingress-persisted user prompt for a zero-output cancel.

        WebUI Stop can happen before any assistant output exists. The user still
        needs the submitted question to remain visible and reloadable from
        history, so cancellation no longer rolls back this transcript row.
        """
        log.info(
            "turn_runner.cancelled_prompt_retained",
            session_key=session_key,
            message_id=message_id,
        )
        return False

    async def _load_history(
        self,
        agent: Agent,
        session_key: str,
        *,
        trim_last_user: bool = True,
        bound_user_message_id: str | None = None,
        suppress_compaction_context: bool = False,
        history_start_message_id: str | None = None,
    ) -> str | None:
        """Load existing transcript as agent history.

        ``bound_user_message_id`` binds this turn's history to the specific
        persisted user message it answers, rather than to transcript position.
        When sends are queued, ingress persists later prompts before earlier
        turns finish, so the transcript can hold the bound message mid-stream
        with unanswered future prompts after it. A positional "drop the last
        user entry" then duplicates the current prompt and leaks those future
        prompts into context. Slicing by id drops the bound entry (the caller
        re-appends it) plus any later user entry while keeping the intervening
        assistant replies. When the id is absent or not found, fall back to the
        positional trim. ``suppress_compaction_context`` is the four_tier_mapping
        exact-task path and therefore fails closed instead of using either
        positional fallback or request-scoped emergency compaction.
        """
        if self._session_manager is None:
            return None

        transcript = await self._session_manager.get_transcript(session_key)

        from opensquilla.engine.history import reconstruct_messages_from_entry
        from opensquilla.provider import Message

        history: list[Message] = []
        summary_markers: list[str] = []
        subagent_terminal_notices: list[str] = []
        emergency_override = getattr(self, "_emergency_compaction_overrides", {}).pop(
            session_key,
            None,
        )
        if emergency_override is not None and not suppress_compaction_context:
            transcript = list(emergency_override.kept_entries)
            summary_markers.append(emergency_override.summary)

        if history_start_message_id:
            boundary_index = next(
                (
                    index
                    for index, entry in enumerate(transcript)
                    if getattr(entry, "message_id", None) == history_start_message_id
                ),
                None,
            )
            if boundary_index is None:
                if suppress_compaction_context:
                    from opensquilla.engine.routing.fixed_four_tier_v2 import (
                        FixedFourTierRoutingError,
                    )

                    raise FixedFourTierRoutingError(
                        "four_tier_mapping history task boundary is unavailable",
                        reason="task_history_boundary_unavailable",
                    )
                log.warning(
                    "load_history.task_boundary_missing",
                    session_key=session_key,
                    history_start_message_id=history_start_message_id,
                )
                transcript = []
            else:
                transcript = transcript[boundary_index:]

        # Resolve the id-bound slice (see method docstring). Only active when we
        # would otherwise trim positionally.
        bound_index: int | None = None
        bound_skip_indexes: set[int] = set()
        if trim_last_user and bound_user_message_id:
            for idx, candidate in enumerate(transcript):
                if getattr(candidate, "message_id", None) == bound_user_message_id:
                    bound_index = idx
                    break
            if bound_index is not None:
                bound_skip_indexes = {
                    idx
                    for idx, candidate in enumerate(transcript)
                    if idx >= bound_index and getattr(candidate, "role", None) == "user"
                }
            else:
                if suppress_compaction_context:
                    from opensquilla.engine.routing.fixed_four_tier_v2 import (
                        FixedFourTierRoutingError,
                    )

                    raise FixedFourTierRoutingError(
                        "four_tier_mapping current history boundary is unavailable",
                        reason="current_history_boundary_unavailable",
                    )
                # The bound message is not in the (possibly compacted) transcript;
                # fall back to positional trim but surface it — a persistent
                # occurrence means queued binding is silently degrading.
                log.warning(
                    "load_history.bound_message_missing",
                    session_key=session_key,
                    bound_user_message_id=bound_user_message_id,
                    transcript_len=len(transcript),
                )
        bound_slice_applied = bool(bound_skip_indexes)
        model_caps = getattr(getattr(agent, "config", None), "model_capabilities", None)
        preserve_image_history = bool(
            getattr(getattr(agent, "config", None), "preserve_historical_images", False)
            and getattr(model_caps, "supports_vision", False)
        )
        workspace_dir = getattr(getattr(agent, "config", None), "workspace_dir", None)
        materialize_historical_attachments = bool(
            getattr(
                getattr(agent, "config", None),
                "materialize_historical_attachments",
                True,
            )
            and workspace_dir
        )
        lookback = int(
            getattr(
                getattr(self._turn_config(), "squilla_router", None),
                "vision_history_lookback_turns",
                3,
            )
            or 0
        )
        image_replay_entry_indexes: set[int] = set()
        image_replay_session_id: str | None = None
        if preserve_image_history and lookback > 0:
            current_user_entry_index = bound_index
            if current_user_entry_index is None:
                current_user_entry_index = (
                    len(transcript) - 1
                    if trim_last_user
                    and transcript
                    and getattr(transcript[-1], "role", None) == "user"
                    else None
                )
            user_entry_indexes = [
                index
                for index, entry in enumerate(transcript)
                if getattr(entry, "role", None) == "user"
                and index != current_user_entry_index
                and index not in bound_skip_indexes
                and isinstance(getattr(entry, "content", None), str)
                and bool(str(getattr(entry, "content", "")).strip())
            ]
            image_replay_entry_indexes = set(user_entry_indexes[-lookback:])
            image_replay_session_id = await self._resolve_session_id_for_log(session_key)
            if image_replay_session_id is None:
                image_replay_session_id = session_key
        attachment_replay_session_id = image_replay_session_id
        if attachment_replay_session_id is None and materialize_historical_attachments:
            attachment_replay_session_id = await self._resolve_session_id_for_log(session_key)
            if attachment_replay_session_id is None:
                attachment_replay_session_id = session_key
        last_entry_was_user = False
        history_materializer: AttachmentWorkspaceMaterializer | None = None
        if materialize_historical_attachments and workspace_dir and attachment_replay_session_id:
            # One instance per history load so first-materialization replays
            # pay for a single workspace-tree budget scan, not one per entry.
            history_materializer = AttachmentWorkspaceMaterializer(
                media_root=self._attachment_media_root(),
                workspace_dir=workspace_dir,
                materializable_mimes=None,
                disk_budget_bytes=workspace_attachment_budget_from_config(self._config),
            )
        for entry_index, entry in enumerate(transcript):
            if entry_index in bound_skip_indexes:
                # The bound current prompt (re-appended by the caller) and any
                # later still-queued user prompt are excluded from history.
                last_entry_was_user = False
                continue
            if (
                entry.role == "system"
                and entry.content
                and entry.content.startswith(_CONTEXT_SUMMARY_MARKER)
            ):
                summary_markers.append(_strip_context_summary_marker(entry.content))
                continue
            subagent_notice = _subagent_terminal_history_notice(entry)
            if subagent_notice is not None:
                subagent_terminal_notices.append(subagent_notice)
                last_entry_was_user = False
                continue
            if entry.role not in ("user", "assistant"):
                continue
            raw_content = entry.content or ""
            # User messages may carry attachment envelopes; assistant messages
            # may carry artifact metadata. Both become text-only safe markers
            # for model-context replay.
            if raw_content and entry.role == "user":
                content: Any = self._maybe_unpack_attachments(
                    raw_content,
                    preserve_image_attachments=entry_index in image_replay_entry_indexes,
                    materialize_historical_attachments=materialize_historical_attachments,
                    media_root=self._attachment_media_root(),
                    session_id=attachment_replay_session_id,
                    workspace_dir=workspace_dir,
                    historical_materializer=history_materializer,
                )
            elif raw_content and entry.role == "assistant":
                content = self._maybe_unpack_assistant_artifacts(raw_content)
            else:
                content = raw_content
            history.extend(
                reconstruct_messages_from_entry(
                    entry.role,
                    content,
                    entry.tool_calls,
                    getattr(entry, "reasoning_content", None),
                )
            )
            last_entry_was_user = entry.role == "user"
        # Strip the caller-appended user turn only when the transcript really
        # ended on a user entry; an assistant entry that reconstructs into
        # assistant + user(tool_result) must keep its tool_result tail. When the
        # id-bound slice already excluded the current prompt, skip the positional
        # pop entirely.
        if (
            not bound_slice_applied
            and trim_last_user
            and last_entry_was_user
            and history
            and history[-1].role == "user"
        ):
            history.pop()
        history.extend(
            Message(role="assistant", content=notice)
            for notice in dict.fromkeys(subagent_terminal_notices)
        )
        context_states = (
            [] if suppress_compaction_context else await self._load_context_states(session_key)
        )
        provider = getattr(agent, "provider", None)
        provider_context = build_provider_compaction_context(
            context_states=context_states,
            provider_kind=str(getattr(provider, "provider_name", "")),
        )
        if provider_context.messages:
            history = provider_context.messages + history
        if history:
            agent.set_history(history)
        if suppress_compaction_context:
            return None
        return await self._compaction_summary_context(
            session_key,
            summary_markers,
            context_states=context_states,
            skip_covered_through_ids=provider_context.covered_through_ids,
        )

    async def _load_context_states(self, session_key: str) -> list[Any]:
        context_states: list[Any] = []
        get_context_states = getattr(self._session_manager, "get_context_states", None)
        if callable(get_context_states):
            try:
                context_states = await get_context_states(session_key)
            except KeyError:
                context_states = []
            except Exception as exc:  # pragma: no cover - context state is best-effort
                log.warning(
                    "compaction_context_state.load_failed",
                    session_key=session_key,
                    error=str(exc),
                )
                context_states = []
        return context_states

    async def _compaction_summary_context(
        self,
        session_key: str,
        legacy_summary_markers: list[str],
        *,
        context_states: list[Any] | None = None,
        skip_covered_through_ids: set[int] | None = None,
    ) -> str | None:
        """Return durable compaction summaries as request-scoped context."""
        summaries: list[Any] = []
        get_summaries = getattr(self._session_manager, "get_summaries", None)
        if callable(get_summaries):
            try:
                summaries = await get_summaries(session_key)
            except KeyError:
                summaries = []
            except Exception as exc:  # pragma: no cover - summary context is best-effort
                log.warning(
                    "compaction_summary_context.load_failed",
                    session_key=session_key,
                    error=str(exc),
                )
                summaries = []
        loaded_context_states = (
            await self._load_context_states(session_key)
            if context_states is None
            else context_states
        )
        context_records = build_compaction_context_records(
            context_states=loaded_context_states,
            summaries=summaries,
            legacy_summary_markers=legacy_summary_markers,
            skip_covered_through_ids=skip_covered_through_ids,
        )
        context_items = [record.text for record in context_records]
        if context_items:
            replayed_compaction_ids = list(
                dict.fromkeys(
                    record.compaction_id
                    for record in context_records
                    if record.compaction_id is not None
                )
            )
            replay_compaction_id = (
                replayed_compaction_ids[0] if replayed_compaction_ids else new_compaction_id()
            )
            notify_compaction(
                session_key,
                source="automatic",
                phase="summary_replay",
                status="replayed",
                summary_count=len(context_items),
                summary_len=sum(len(text) for text in context_items),
                context_state_count=len(loaded_context_states),
                replayed_compaction_ids=replayed_compaction_ids,
                **compaction_lifecycle_payload(
                    replay_compaction_id,
                    COMPACTION_REPLAYED_EVENT,
                ),
            )
        return _format_compaction_summary_context(context_items)

    @staticmethod
    def _attachment_envelope_has_image(content: str) -> bool:
        if not content or not content.lstrip().startswith("{"):
            return False
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(parsed, dict):
            return False
        atts = parsed.get("attachments") or []
        if not isinstance(atts, list):
            return False
        for att in atts:
            if not isinstance(att, dict):
                continue
            media_type = att.get("type") or att.get("mime") or att.get("media_type")
            if not (isinstance(media_type, str) and media_type.startswith("image/")):
                continue
            if media_type not in _ALLOWED_ENGINE_MEDIA_TYPES:
                continue
            if isinstance(att.get("data"), str) and att.get("data"):
                return True
            if isinstance(att.get("sha256_ref"), str) and att.get("sha256_ref"):
                return True
        return False

    @staticmethod
    def _maybe_unpack_attachments(
        content: str,
        *,
        preserve_image_attachments: bool = False,
        materialize_historical_attachments: bool = False,
        media_root: Path | None = None,
        session_id: str | None = None,
        workspace_dir: str | Path | None = None,
        workspace_attachment_budget_bytes: int | None = None,
        historical_materializer: AttachmentWorkspaceMaterializer | None = None,
    ) -> Any:
        """Reduce persisted attachment envelopes to text-only history.

        User messages with attachments are persisted as a JSON envelope
        ``{"text": "...", "attachments": [{"type": "image/png", "data": "<b64>"}...]}``
        in ``transcript_entries.content`` (see rpc_sessions._persist_user_message).
        Historical images are text markers by default so text routes do not
        replay old image blocks to providers that cannot consume them. When the
        caller has already selected a vision model, a bounded recent window can
        be hydrated back into image blocks.

        Returns the original string for non-envelope content so non-attachment
        history (assistant text, tool results) is unaffected. On any parse error,
        missing key, or invalid attachment entry, fall back to the original string
        to keep history loading crash-proof.
        """
        if not content or not content.lstrip().startswith("{"):
            return content
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content
        if not isinstance(parsed, dict) or "text" not in parsed:
            return content
        text = parsed.get("text")
        if not isinstance(text, str):
            return content
        atts = parsed.get("attachments") or []
        if not isinstance(atts, list) or not atts:
            return text

        omitted: list[str] = []
        replay_blocks: list[Any] = []
        preserved_image = False
        if not materialize_historical_attachments:
            historical_materializer = None
        elif historical_materializer is None and session_id and workspace_dir:
            # Fallback for direct callers: the history loader passes one
            # shared instance per load so the whole transcript shares a
            # single budget scan instead of re-walking the tree per entry.
            historical_materializer = AttachmentWorkspaceMaterializer(
                media_root=media_root or Path("."),
                workspace_dir=workspace_dir,
                materializable_mimes=None,
                disk_budget_bytes=workspace_attachment_budget_bytes,
            )
        if preserve_image_attachments and text:
            from opensquilla.provider.types import ContentBlockText

            replay_blocks.append(ContentBlockText(text=text))
        for att in atts:
            if not isinstance(att, dict):
                continue
            media_type = att.get("type") or att.get("mime") or att.get("media_type")
            if not isinstance(media_type, str):
                continue
            # Persisted attachment envelope: ``sha256_ref`` indicates the bytes live on
            # disk under media/transcripts/<session>/<sha>. Text routes keep
            # a marker; vision routes may replay a bounded recent image window.
            data = att.get("data")
            sha_ref = att.get("sha256_ref")
            missing_reason = att.get("missing_reason")
            if not (
                (isinstance(data, str) and data)
                or (isinstance(sha_ref, str) and sha_ref)
                or (isinstance(missing_reason, str) and missing_reason)
            ):
                continue
            name = att.get("name")
            fallback = "image" if media_type.startswith("image/") else "attachment"
            label = name if isinstance(name, str) and name.strip() else fallback
            if preserve_image_attachments and media_type in _IMAGE_ATTACHMENT_MIMES:
                from opensquilla.provider.types import ContentBlockImage

                if isinstance(data, str) and data:
                    try:
                        base64.b64decode(data, validate=True)
                    except (binascii.Error, ValueError):
                        omitted.append(f"[attachment unavailable: {label} ({media_type})]")
                    else:
                        replay_blocks.append(ContentBlockImage(media_type=media_type, data=data))
                        preserved_image = True
                    continue
                if isinstance(sha_ref, str) and sha_ref and media_root and session_id:
                    raw_size = att.get("size")
                    size = raw_size if isinstance(raw_size, int) else -1
                    ref = make_attachment_ref(
                        sha256=sha_ref,
                        name=label,
                        mime=media_type,
                        size=size,
                        session_id=session_id,
                        source="transcript",
                    )
                    try:
                        raw_bytes = read_attachment_ref_bytes(ref, media_root=media_root)
                    except (FileNotFoundError, ValueError) as exc:
                        omitted.append(f"[attachment unavailable: {label}: {exc}]")
                    else:
                        replay_blocks.append(
                            ContentBlockImage(
                                media_type=media_type,
                                data=base64.b64encode(raw_bytes).decode("ascii"),
                            )
                        )
                        preserved_image = True
                    continue
            if (
                historical_materializer is not None
                and session_id
                and _is_materializable_attachment_mime(media_type)
            ):
                materializer = historical_materializer
                result = None
                if isinstance(sha_ref, str) and sha_ref and media_root is not None:
                    raw_size = att.get("size")
                    size = raw_size if isinstance(raw_size, int) else -1
                    ref = make_attachment_ref(
                        sha256=sha_ref,
                        name=label,
                        mime=media_type,
                        size=size,
                        session_id=session_id,
                        source="transcript",
                    )
                    result = materializer.materialize(ref, session_id=session_id)
                elif isinstance(data, str) and data:
                    try:
                        payload = base64.b64decode(data, validate=True)
                    except (binascii.Error, ValueError):
                        omitted.append(
                            "[historical attachment unavailable: "
                            f"{label} ({media_type}): attachment data is not valid base64]"
                        )
                        continue
                    result = materializer.materialize_bytes(
                        payload,
                        name=label,
                        mime=media_type,
                        session_id=session_id,
                    )
                if result is not None:
                    prefix = (
                        "historical attachment available"
                        if result.available
                        else "historical attachment unavailable"
                    )
                    omitted.append(render_attachment_material_marker(result, prefix=prefix))
                    continue
            omitted.append(f"[historical attachment omitted: {label} ({media_type})]")
        if preserved_image:
            if omitted:
                from opensquilla.provider.types import ContentBlockText

                replay_blocks.extend(ContentBlockText(text=marker) for marker in omitted)
            return replay_blocks
        if not omitted:
            return text
        return "\n".join([text, *omitted]).strip()

    @staticmethod
    def _maybe_unpack_assistant_artifacts(content: str) -> str:
        if not content or not content.lstrip().startswith("{"):
            return content
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content
        if not isinstance(parsed, dict) or "artifacts" not in parsed:
            return content
        text = parsed.get("text")
        artifacts = parsed.get("artifacts")
        if not isinstance(text, str) or not isinstance(artifacts, list):
            return content
        markers = [
            artifact_marker(artifact) for artifact in artifacts if isinstance(artifact, dict)
        ]
        if not markers:
            return text
        return "\n".join([text, *markers]).strip()

    @staticmethod
    def _attachment_media_root_from_config(config: Any | None) -> Path:
        return media_root_from_config(config)

    def _attachment_media_root(self) -> Path:
        return self._attachment_media_root_from_config(self._config)

    @staticmethod
    def _build_attachment_messages(
        message: str,
        attachments: list[dict],
        *,
        media_root: Path | None = None,
        workspace_dir: str | Path | None = None,
        session_id: str | None = None,
        workspace_attachment_budget_bytes: int | None = None,
    ) -> list | None:
        """Build a multimodal user message that carries the attachments.

        The engine sees one normalised attachment shape. Provider
        conversion is deliberately narrow:

          * ``image/*``           -> ``ContentBlockImage``
          * ``application/pdf``   -> local text extraction, then ``ContentBlockText``
          * text-family / json    -> ``ContentBlockText`` wrapped in an
                                     ``<file name="…" mime="…">…</file>``
                                     envelope with escaped filename and content
                                     boundaries.
        """

        if not attachments:
            return None
        if len(attachments) > _MAX_ATTACHMENT_COUNT:
            raise ValueError(f"attachments supports at most {_MAX_ATTACHMENT_COUNT} items")

        from opensquilla.provider.types import (
            ContentBlockImage,
            ContentBlockText,
            Message,
        )

        prompt_block = ContentBlockText(text=message)
        attachment_blocks: list[Any] = []
        turn_materializer: AttachmentWorkspaceMaterializer | None = None
        if workspace_dir:
            # One instance per turn so the attachment batch shares a single
            # budget scan instead of re-walking the tree per attachment.
            turn_materializer = AttachmentWorkspaceMaterializer(
                media_root=media_root or Path("."),
                workspace_dir=workspace_dir,
                materializable_mimes=None,
                disk_budget_bytes=workspace_attachment_budget_bytes,
            )
        for index, att in enumerate(attachments, start=1):
            att_type = att.get("type")
            media_type: str | None = att_type if isinstance(att_type, str) else None
            if media_type is None or media_type not in _ALLOWED_ENGINE_MEDIA_TYPES:
                mime = att.get("mime") or att.get("media_type")
                if isinstance(mime, str) and mime in _ALLOWED_ENGINE_MEDIA_TYPES:
                    media_type = mime
            if media_type is None or media_type not in _ALLOWED_ENGINE_MEDIA_TYPES:
                # Not a rendered family. Normalization resolves parameterized
                # rendered claims ("text/plain; charset=utf-8"); anything else
                # is an opaque attachment carried under its normalized label.
                normalized = _normalize_attachment_mime(
                    media_type or att.get("mime") or att.get("media_type")
                )
                if normalized in _ALLOWED_ENGINE_MEDIA_TYPES:
                    media_type = normalized
                else:
                    media_type = normalized or _OPAQUE_MIME
            if is_attachment_ref(att):
                missing_ref_marker = ""
                if media_root is None:
                    raise ValueError(f"attachments[{index}] media_root is required")
                try:
                    raw_bytes = read_attachment_ref_bytes(att, media_root=media_root)
                except FileNotFoundError:
                    raw_bytes = b""
                    missing_ref_marker = "[attachment unavailable: material file is missing]"
                except ValueError as exc:
                    raw_bytes = b""
                    missing_ref_marker = f"[attachment unavailable: {exc}]"
                data = base64.b64encode(raw_bytes).decode("ascii") if raw_bytes else ""
            else:
                missing_ref_marker = ""
                data_raw = att.get("data")
                if not isinstance(data_raw, str) or not data_raw:
                    raise ValueError(f"attachments[{index}].data is required")
                data = data_raw
                try:
                    raw_bytes = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError(f"attachments[{index}].data must be valid base64") from exc
            max_bytes = _attachment_size_limit_for_mime(
                media_type,
                staged=(att.get("_was_staged") is True and _can_stage_attachment_mime(media_type)),
            )
            if len(raw_bytes) > max_bytes:
                raise ValueError(f"attachments[{index}] exceeds the {max_bytes} byte limit")

            name_raw = att.get("name")
            filename = _sanitize_attachment_filename(name_raw)
            material_marker = ""
            if turn_materializer is not None and _is_materializable_attachment_mime(media_type):
                materializer = turn_materializer
                if is_attachment_ref(att):
                    result = materializer.materialize(att, session_id=session_id)
                else:
                    result = materializer.materialize_bytes(
                        raw_bytes,
                        name=filename,
                        mime=media_type,
                        session_id=session_id,
                    )
                prefix = "attachment available" if result.available else "attachment unavailable"
                material_marker = render_attachment_material_marker(result, prefix=prefix)
            if missing_ref_marker:
                missing_text = (
                    "\n\n".join([missing_ref_marker, material_marker])
                    if material_marker
                    else missing_ref_marker
                )
                wrapped = _render_file_context_block(filename, media_type, missing_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
                continue

            if media_type in _IMAGE_ATTACHMENT_MIMES:
                attachment_blocks.append(ContentBlockImage(media_type=media_type, data=data))
            elif media_type == "application/pdf":
                try:
                    extracted_pdf_text = _extract_pdf_attachment_text(raw_bytes, filename)
                except ValueError as exc:
                    extracted_pdf_text = (
                        f"[attachment unavailable: PDF text could not be extracted: {exc}]"
                    )
                if material_marker:
                    extracted_pdf_text = "\n\n".join(
                        [
                            extracted_pdf_text,
                            material_marker,
                            (
                                "[attachment note: use the workspace path for PDF "
                                "layout, images, colors, or edits; extracted text is "
                                "only a preview.]"
                            ),
                        ]
                    )
                wrapped = _render_file_context_block(filename, media_type, extracted_pdf_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            elif media_type in _OFFICE_ATTACHMENT_MIMES:
                try:
                    extracted_office_text = _extract_office_attachment_text(
                        raw_bytes, filename, media_type
                    )
                except ValueError as exc:
                    extracted_office_text = (
                        f"[attachment unavailable: document text could not be extracted: {exc}]"
                    )
                if material_marker:
                    extracted_office_text = "\n\n".join([extracted_office_text, material_marker])
                wrapped = _render_file_context_block(filename, media_type, extracted_office_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            elif media_type in _EMAIL_ATTACHMENT_MIMES:
                try:
                    extracted_email_text = _extract_email_attachment_text(
                        raw_bytes, filename, media_type
                    )
                except ValueError as exc:
                    extracted_email_text = (
                        f"[attachment unavailable: email could not be extracted: {exc}]"
                    )
                if material_marker:
                    extracted_email_text = "\n\n".join([extracted_email_text, material_marker])
                wrapped = _render_file_context_block(filename, media_type, extracted_email_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            elif media_type in _ENGINE_TEXT_FAMILY_MIMES:
                if is_attachment_ref(att) and att.get("_provider_inline_policy") == "preview_only":
                    decoded_text = _render_preview_only_attachment_text(
                        att,
                        filename=filename,
                        mime=media_type,
                        raw_bytes=raw_bytes,
                        media_root=media_root,
                    )
                else:
                    try:
                        decoded_text = _truncate_attachment_text(
                            raw_bytes.decode("utf-8"),
                            limit=_TEXT_ATTACHMENT_TEXT_LIMIT,
                        )
                    except UnicodeDecodeError:
                        decoded_text = (
                            "[attachment unavailable: declared text content is not valid UTF-8]"
                        )
                if material_marker:
                    decoded_text = "\n\n".join([decoded_text, material_marker])
                wrapped = _render_file_context_block(filename, media_type, decoded_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            else:
                # Opaque attachment: the raw bytes never reach the provider.
                # The model gets an escaped metadata envelope plus the
                # workspace marker so it can act on the file with tools.
                sha = att.get("sha256") or att.get("sha256_ref")
                details = f"[opaque attachment: {media_type}, {len(raw_bytes)} bytes"
                if isinstance(sha, str) and sha:
                    details += f", sha256 {sha}"
                details += (
                    "; content is not inlined. Inspect or convert the workspace "
                    "copy with filesystem, shell, or code tools.]"
                )
                if material_marker:
                    details = "\n\n".join([details, material_marker])
                wrapped = _render_file_context_block(filename, media_type, details)
                attachment_blocks.append(ContentBlockText(text=wrapped))

        return [
            Message(
                role="user",
                content=[prompt_block] + attachment_blocks,  # type: ignore[arg-type]
            )
        ]
