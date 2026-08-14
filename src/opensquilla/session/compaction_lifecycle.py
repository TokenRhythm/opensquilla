"""Shared compaction lifecycle helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import uuid4

FlushCompactionDecision = Literal[
    "safe_destructive",
    "degraded_forensic",
    "emergency_ephemeral",
    "disabled",
]
FlushCompactionSafetyMode = Literal["protect", "best_effort", "block", "off"]
FlushTrigger = Literal["session_reset", "manual", "idle", "pre_compaction"]
CompactionSafetyStatus = Literal["safe", "degraded_archive", "unsafe", "not_required"]
SemanticMemoryStatus = Literal["healthy", "pending", "degraded", "failed", "not_required"]
CompactionDurability = Literal["durable", "request_scoped", "none"]
ALL_FLUSH_TRIGGERS: Final[tuple[FlushTrigger, ...]] = (
    "session_reset",
    "manual",
    "idle",
    "pre_compaction",
)
DEFAULT_FLUSH_TRIGGERS: Final[tuple[FlushTrigger, ...]] = (
    "session_reset",
    "manual",
    "idle",
)

SAFE_FLUSH_OUTPUT_COVERAGE_STATUSES: Final[frozenset[str]] = frozenset({"ok"})
SAFE_FLUSH_OBLIGATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok", "backfilled"}
)
COMPACTION_TRIGGERED_EVENT: Final[str] = "compaction.triggered"
COMPACTION_CHUNK_SUMMARIZED_EVENT: Final[str] = "compaction.chunk_summarized"
COMPACTION_SUMMARY_VERIFIED_EVENT: Final[str] = "compaction.summary_verified"
COMPACTION_PERSISTED_EVENT: Final[str] = "compaction.persisted"
COMPACTION_REPLAYED_EVENT: Final[str] = "compaction.replayed"
COMPACTION_COVERAGE_UNKNOWN: Final[str] = "unknown"
BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "already_attempted_this_turn",
        "already_compacted_this_turn",
        "no_entries",
        "stale_preimage",
        "structured_content_noop",
        "within_budget",
        "within_compaction_budget",
    }
)
NOOP_FLUSH_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"ok_noop_no_memory"})
ARCHIVE_ONLY_FLUSH_RESULT_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok_archive_only"}
)
ARCHIVED_DEGRADED_FLUSH_RESULT_STATUSES: Final[frozenset[str]] = frozenset(
    {"parse_failed_archived", "provider_failed_archived", "apply_failed_archived"}
)
FAILED_FLUSH_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"archive_failed"})
_FLUSH_TRIGGER_ALIASES: Final[dict[str, FlushTrigger]] = {
    "reset": "session_reset",
    "session-reset": "session_reset",
    "session_reset": "session_reset",
    "sessions_reset": "session_reset",
    "manual": "manual",
    "idle": "idle",
    "pre-compaction": "pre_compaction",
    "pre_compaction": "pre_compaction",
    "compaction": "pre_compaction",
    "context-compaction": "pre_compaction",
    "context_compaction": "pre_compaction",
    "inline-overflow": "pre_compaction",
    "inline_overflow": "pre_compaction",
}


@dataclass(frozen=True)
class CompactionMemoryStatus:
    safety_status: CompactionSafetyStatus
    semantic_status: SemanticMemoryStatus
    allows_destructive_compaction: bool


@dataclass(frozen=True)
class CompactionLifecycleResult:
    compacted: bool
    refused: bool
    reason: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    remaining_budget_tokens: int | None = None
    removed_count: int = 0
    kept_count: int = 0
    summary_len: int = 0
    summary_source: str = "unknown"
    flush_receipt: Any = None


class CompactionTimeoutError(TimeoutError):
    """A compaction operation exhausted its shared absolute deadline."""

    def __init__(self, phase: str, timeout_seconds: float | None = None) -> None:
        self.phase = str(phase or "unknown")
        self.timeout_seconds = timeout_seconds
        detail = (
            f" after {timeout_seconds:g}s"
            if timeout_seconds is not None and timeout_seconds > 0
            else ""
        )
        super().__init__(f"Compaction timed out during {self.phase}{detail}")


def new_compaction_id() -> str:
    """Return an opaque id used to correlate one compaction attempt's events."""

    return f"cmp_{uuid4().hex}"


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _flush_memory_config(config: Any) -> Any:
    memory_cfg = getattr(config, "memory", None)
    return memory_cfg if memory_cfg is not None else config


def normalize_flush_triggers(value: Any) -> frozenset[FlushTrigger]:
    """Normalize flush trigger config into canonical trigger names."""

    if value is None:
        return frozenset(DEFAULT_FLUSH_TRIGGERS)
    if isinstance(value, str):
        items: list[Any] = [item for item in value.replace(";", ",").split(",")]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]

    normalized: set[FlushTrigger] = set()
    for item in items:
        raw = str(item or "").strip().lower().replace("_", "-")
        if not raw:
            continue
        if raw == "all":
            return frozenset(ALL_FLUSH_TRIGGERS)
        trigger = _FLUSH_TRIGGER_ALIASES.get(raw)
        if trigger is not None:
            normalized.add(trigger)
    return frozenset(normalized)


def normalize_flush_triggers_strict(value: Any) -> tuple[FlushTrigger, ...]:
    """Normalize trigger config and reject unknown trigger names."""

    if value is None:
        return DEFAULT_FLUSH_TRIGGERS
    if isinstance(value, str):
        items: list[Any] = [item for item in value.replace(";", ",").split(",")]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]

    normalized = normalize_flush_triggers(items)
    invalid: list[str] = []
    for item in items:
        raw = str(item or "").strip()
        if not raw:
            continue
        if raw.strip().lower().replace("_", "-") == "all":
            continue
        if not normalize_flush_triggers([raw]):
            invalid.append(raw)
    if invalid:
        raise ValueError(f"unknown flush trigger(s): {', '.join(invalid)}")
    return tuple(trigger for trigger in ALL_FLUSH_TRIGGERS if trigger in normalized)


def flush_trigger_enabled(config: Any, trigger: FlushTrigger) -> bool:
    """Return whether a specific flush trigger is enabled for a config."""

    from opensquilla.memory.flush_config import is_session_flush_enabled

    if not is_session_flush_enabled():
        return False
    memory_cfg = _flush_memory_config(config)
    if not _coerce_bool(getattr(memory_cfg, "flush_enabled", False)):
        return False
    if trigger == "pre_compaction" and _coerce_bool(
        getattr(memory_cfg, "flush_pre_compaction", False)
    ):
        return True
    return trigger in normalize_flush_triggers(getattr(memory_cfg, "flush_triggers", None))


def compaction_event_chain(event: str) -> list[str]:
    """Return the lifecycle events completed by the given telemetry event."""

    if event == COMPACTION_REPLAYED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
            COMPACTION_PERSISTED_EVENT,
            COMPACTION_REPLAYED_EVENT,
        ]
    if event == COMPACTION_PERSISTED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
            COMPACTION_PERSISTED_EVENT,
        ]
    if event == COMPACTION_SUMMARY_VERIFIED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
        ]
    if event == COMPACTION_CHUNK_SUMMARIZED_EVENT:
        return [COMPACTION_TRIGGERED_EVENT, COMPACTION_CHUNK_SUMMARIZED_EVENT]
    return [COMPACTION_TRIGGERED_EVENT]


def compaction_lifecycle_payload(compaction_id: str, event: str) -> dict[str, Any]:
    payload = {
        "compaction_id": compaction_id,
        "event": event,
        "event_chain": compaction_event_chain(event),
    }
    if event not in {COMPACTION_PERSISTED_EVENT, COMPACTION_REPLAYED_EVENT}:
        payload["coverage_status"] = COMPACTION_COVERAGE_UNKNOWN
    return payload


def compaction_effect_payload(
    *,
    status: str,
    source: str = "automatic",
    reason: str | None = None,
    skip_reason: str | None = None,
    applied: bool | None = None,
    durability: CompactionDurability | None = None,
    user_visible: bool | None = None,
) -> dict[str, Any]:
    """Return normalized user-facing semantics for a compaction event."""

    normalized_status = str(status or "").lower()
    normalized_source = str(source or "").lower()
    normalized_reason = str(skip_reason or reason or "").strip() or None

    if applied is None:
        applied = normalized_status in {"completed", "emergency_ephemeral"}
    if durability is None:
        if normalized_status == "completed":
            durability = "durable"
        elif normalized_status == "emergency_ephemeral":
            durability = "request_scoped"
        else:
            durability = "none"
    if user_visible is None:
        if normalized_source == "manual":
            user_visible = True
        elif normalized_status in {"started", "observed", "completed", "emergency_ephemeral"}:
            user_visible = True
        elif normalized_status in {"failed", "error", "cancelled", "timed_out"}:
            user_visible = True
        elif normalized_status == "skipped":
            user_visible = (
                normalized_reason not in BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS
            )
        else:
            user_visible = False

    payload: dict[str, Any] = {
        "applied": bool(applied),
        "durability": durability,
        "user_visible": bool(user_visible),
    }
    if normalized_status == "skipped" and normalized_reason:
        payload["skip_reason"] = normalized_reason
    return payload


def compaction_result_payload(
    result: Any,
    *,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
    remaining_budget_tokens: int | None = None,
) -> dict[str, Any]:
    kept_entries = getattr(result, "kept_entries", None) or []
    payload: dict[str, Any] = {
        "removed_count": int(getattr(result, "removed_count", 0) or 0),
        "kept_count": len(kept_entries),
        "chunk_count": int(getattr(result, "chunks_processed", 0) or 0),
        "summary_len": len(str(getattr(result, "summary", "") or "")),
        "summary_source": str(getattr(result, "summary_source", "unknown") or "unknown"),
        "coverage_status": str(getattr(result, "coverage_status", "unknown") or "unknown"),
        "missing_obligation_count": len(getattr(result, "missing_obligations", None) or []),
        "critical_carry_forward_count": len(getattr(result, "critical_carry_forward", None) or []),
        "state_kind": str(getattr(result, "summary_format", "text") or "text"),
    }
    if tokens_before is None:
        tokens_before = getattr(result, "tokens_before", None)
    if tokens_after is None:
        tokens_after = getattr(result, "tokens_after", None)
    if remaining_budget_tokens is None:
        remaining_budget_tokens = getattr(result, "remaining_budget_tokens", None)
    if tokens_before is not None:
        payload["tokens_before"] = int(tokens_before)
    if tokens_after is not None:
        payload["tokens_after"] = int(tokens_after)
    if remaining_budget_tokens is not None:
        payload["remaining_budget_tokens"] = int(remaining_budget_tokens)
    skip_reason = str(getattr(result, "skip_reason", "") or "")
    if skip_reason:
        payload["skip_reason"] = skip_reason
    quality_report = getattr(result, "quality_report", None)
    if isinstance(quality_report, dict) and quality_report:
        payload["quality_report"] = dict(quality_report)
    return payload


def flush_receipt_status(receipt: Any) -> str:
    if receipt is None:
        return "not_requested"
    if flush_receipt_allows_destructive_compaction(receipt):
        return "safe"
    result_status = str(_receipt_value(receipt, "result_status", "") or "")
    if result_status in NOOP_FLUSH_RESULT_STATUSES:
        return "noop_no_memory"
    if result_status in ARCHIVE_ONLY_FLUSH_RESULT_STATUSES:
        return "archive_only"
    if result_status in ARCHIVED_DEGRADED_FLUSH_RESULT_STATUSES:
        return "degraded_forensic"
    return "unsafe"


def flush_receipt_is_successful_flush(receipt: Any) -> bool:
    """Return true when the flush pipeline completed without needing retry.

    This is intentionally weaker than destructive-compaction safety. A no-op
    LLM result means "nothing durable to write" and should not be retried as a
    flush failure, while it still must not authorize destructive compaction.
    """

    if receipt is None:
        return False
    if flush_receipt_allows_destructive_compaction(receipt):
        return True
    result_status = str(_receipt_value(receipt, "result_status", "") or "")
    return result_status in NOOP_FLUSH_RESULT_STATUSES


def normalize_flush_compaction_safety_mode(
    value: Any,
    *,
    legacy_requires_safe_receipt: bool = False,
) -> FlushCompactionSafetyMode:
    if value is None:
        return "block" if legacy_requires_safe_receipt else "protect"
    raw = str(value).strip().lower().replace("-", "_")
    if raw in {"", "protect", "protected"}:
        return "protect"
    if raw in {"best_effort", "besteffort", "legacy"}:
        return "best_effort"
    if raw in {"block", "strict", "require_safe_receipt"}:
        return "block"
    if raw in {"off", "disabled", "none", "false", "0"}:
        return "off"
    return "protect"


def flush_compaction_safety_mode(config: Any) -> FlushCompactionSafetyMode:
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is None:
        memory_cfg = config
    legacy_requires_safe_receipt = bool(
        getattr(memory_cfg, "flush_compaction_requires_safe_receipt", False)
    )
    mode = normalize_flush_compaction_safety_mode(
        getattr(memory_cfg, "flush_compaction_safety_mode", None),
        legacy_requires_safe_receipt=legacy_requires_safe_receipt,
    )
    if legacy_requires_safe_receipt and mode == "protect":
        return "block"
    return mode


def flush_compaction_decision(
    receipt: Any,
    *,
    safety_mode: Any = "protect",
) -> FlushCompactionDecision:
    mode = normalize_flush_compaction_safety_mode(safety_mode)
    if mode == "off":
        return "disabled"
    if flush_receipt_allows_destructive_compaction(receipt):
        return "safe_destructive"
    if mode == "block":
        return "emergency_ephemeral"
    return "degraded_forensic"


def flush_receipt_status_for_compaction(receipt: Any, config: Any) -> str:
    decision = flush_compaction_decision(
        receipt,
        safety_mode=flush_compaction_safety_mode(config),
    )
    if decision == "disabled":
        return "not_required"
    if decision == "safe_destructive":
        return "safe"
    result_status = str(_receipt_value(receipt, "result_status", "") or "")
    if result_status in NOOP_FLUSH_RESULT_STATUSES:
        return "noop_no_memory"
    if result_status in ARCHIVE_ONLY_FLUSH_RESULT_STATUSES:
        return "archive_only"
    if result_status in FAILED_FLUSH_RESULT_STATUSES:
        return "unsafe"
    if decision == "degraded_forensic":
        return "degraded_forensic"
    return "unsafe"


def _receipt_value(receipt: Any, name: str, default: Any) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name, default)
    return getattr(receipt, name, default)


def _receipt_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def flush_receipt_allows_destructive_compaction(receipt: Any) -> bool:
    if _receipt_value(receipt, "mode", None) != "llm":
        return False
    if _receipt_int(_receipt_value(receipt, "indexed_chunk_count", 0)) <= 0:
        return False
    integrity_status = str(
        _receipt_value(receipt, "integrity_status", "unverified") or "unverified"
    )
    if integrity_status != "ok":
        return False
    output_coverage_status = str(
        _receipt_value(receipt, "output_coverage_status", "unverified") or "unverified"
    )
    if output_coverage_status not in SAFE_FLUSH_OUTPUT_COVERAGE_STATUSES:
        return False
    if _receipt_int(_receipt_value(receipt, "invalid_candidate_count", 0)) > 0:
        return False
    if _receipt_value(receipt, "candidate_missing_ids", []):
        return False
    if (
        _receipt_int(_receipt_value(receipt, "obligation_count", 0)) <= 0
        and not _receipt_value(receipt, "obligation_missing_ids", [])
    ):
        return True
    obligation_status = str(
        _receipt_value(receipt, "obligation_status", "unverified") or "unverified"
    )
    if obligation_status not in SAFE_FLUSH_OBLIGATION_STATUSES:
        return False
    return not _receipt_value(receipt, "obligation_missing_ids", [])


def durable_receipt_allows_destructive_compaction(receipt: Any) -> bool:
    scope = str(_receipt_value(receipt, "scope", "") or "")
    status = str(_receipt_value(receipt, "status", "") or "")
    if scope == "checkpoint":
        source_path = str(_receipt_value(receipt, "source_path", "") or "")
        content_hash = str(_receipt_value(receipt, "content_hash", "") or "")
        return status == "checkpoint_saved" and bool(source_path) and bool(content_hash)
    if scope == "flush":
        target_path = str(_receipt_value(receipt, "target_path", "") or "")
        return status == "flush_appended" and bool(target_path)
    if scope == "preimage":
        target_path = str(_receipt_value(receipt, "target_path", "") or "")
        content_hash = str(_receipt_value(receipt, "content_hash", "") or "")
        return (
            status == "preimage_saved"
            and target_path.startswith("memory/.raw_fallbacks/")
            and bool(content_hash)
        )
    if scope == "repair":
        target_path = str(_receipt_value(receipt, "target_path", "") or "")
        content_hash = str(_receipt_value(receipt, "content_hash", "") or "")
        reason = str(_receipt_value(receipt, "reason", "") or "")
        archived_reasons = (
            ARCHIVE_ONLY_FLUSH_RESULT_STATUSES | ARCHIVED_DEGRADED_FLUSH_RESULT_STATUSES
        )
        return (
            status == "repair_pending"
            and reason in archived_reasons
            and target_path.startswith("memory/.raw_fallbacks/")
            and bool(content_hash)
        )
    return flush_receipt_allows_destructive_compaction(receipt)


def _receipt_has_archive_evidence(receipt: Any) -> bool:
    content_hash = str(_receipt_value(receipt, "content_hash", "") or "")
    flushed_paths = _receipt_value(receipt, "flushed_paths", []) or []
    if isinstance(flushed_paths, str):
        flushed_paths = [flushed_paths]
    return bool(content_hash) and any(
        str(path).startswith("memory/.raw_fallbacks/") for path in flushed_paths
    )


def compaction_safety_allows_destructive_compaction(
    receipt: Any,
    *,
    deterministic_receipt_safe: bool = False,
) -> bool:
    if deterministic_receipt_safe:
        return True
    if flush_receipt_allows_destructive_compaction(receipt):
        return True
    result_status = str(_receipt_value(receipt, "result_status", "") or "")
    return (
        result_status in ARCHIVE_ONLY_FLUSH_RESULT_STATUSES
        or result_status in ARCHIVED_DEGRADED_FLUSH_RESULT_STATUSES
    ) and _receipt_has_archive_evidence(receipt)


def _semantic_memory_status(receipt: Any) -> SemanticMemoryStatus:
    if receipt is None:
        return "pending"
    if flush_receipt_allows_destructive_compaction(receipt):
        return "healthy"
    result_status = str(_receipt_value(receipt, "result_status", "") or "")
    if result_status in NOOP_FLUSH_RESULT_STATUSES:
        return "healthy"
    if result_status in ARCHIVE_ONLY_FLUSH_RESULT_STATUSES:
        return "degraded"
    if result_status in ARCHIVED_DEGRADED_FLUSH_RESULT_STATUSES:
        return "failed"
    if result_status in FAILED_FLUSH_RESULT_STATUSES:
        return "failed"
    return "degraded"


def compaction_memory_status(
    receipt: Any,
    *,
    deterministic_receipt_safe: bool = False,
    required: bool = True,
) -> CompactionMemoryStatus:
    if not required:
        return CompactionMemoryStatus(
            safety_status="not_required",
            semantic_status="not_required",
            allows_destructive_compaction=True,
        )
    if deterministic_receipt_safe:
        return CompactionMemoryStatus(
            safety_status="safe",
            semantic_status=_semantic_memory_status(receipt),
            allows_destructive_compaction=True,
        )
    if flush_receipt_allows_destructive_compaction(receipt):
        return CompactionMemoryStatus(
            safety_status="safe",
            semantic_status="healthy",
            allows_destructive_compaction=True,
        )
    if compaction_safety_allows_destructive_compaction(receipt):
        return CompactionMemoryStatus(
            safety_status="degraded_archive",
            semantic_status=_semantic_memory_status(receipt),
            allows_destructive_compaction=True,
        )
    return CompactionMemoryStatus(
        safety_status="unsafe",
        semantic_status=_semantic_memory_status(receipt),
        allows_destructive_compaction=False,
    )


def pre_compaction_flush_enabled(config: Any) -> bool:
    return flush_trigger_enabled(config, "pre_compaction")


def pre_compaction_flush_requires_safe_receipt(config: Any) -> bool:
    return flush_compaction_safety_mode(config) == "block"


def flush_receipt_to_dict(receipt: Any) -> dict[str, Any]:
    if receipt is None:
        return {}
    to_dict = getattr(receipt, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(receipt, Mapping):
        return dict(receipt)
    return dict(vars(receipt))


async def mark_compaction_flush_status_with_retry(
    mark_status: Any,
    *,
    session_key: str,
    compaction_id: str,
    status: str,
    log: Any,
    failed_event: str,
    updated_event: str,
    skipped_event: str,
    retry_delays: tuple[float, ...] = (0.0, 0.05, 0.25),
) -> None:
    for delay_seconds in retry_delays:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        try:
            updated = await mark_status(session_key, compaction_id, status)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                failed_event,
                session_key=session_key,
                compaction_id=compaction_id,
                status=status,
                error=str(exc),
            )
            return
        if updated:
            log.info(
                updated_event,
                session_key=session_key,
                compaction_id=compaction_id,
                status=status,
            )
            return
    log.debug(
        skipped_event,
        session_key=session_key,
        compaction_id=compaction_id,
        status=status,
        reason="summary_not_found",
    )
