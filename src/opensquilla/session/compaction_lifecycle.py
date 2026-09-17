"""Shared compaction lifecycle helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import uuid4

from opensquilla.compaction_status import (
    BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS as BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS,
)
from opensquilla.compaction_status import (
    STALE_COMPACTION_REASONS as STALE_COMPACTION_REASONS,
)
from opensquilla.compaction_status import (
    compaction_failure_status as compaction_failure_status,
)

CompactionDurability = Literal["durable", "request_scoped", "none"]

COMPACTION_TRIGGERED_EVENT: Final[str] = "compaction.triggered"
COMPACTION_CHUNK_SUMMARIZED_EVENT: Final[str] = "compaction.chunk_summarized"
COMPACTION_SUMMARY_VERIFIED_EVENT: Final[str] = "compaction.summary_verified"
COMPACTION_PERSISTED_EVENT: Final[str] = "compaction.persisted"
COMPACTION_REPLAYED_EVENT: Final[str] = "compaction.replayed"
COMPACTION_COVERAGE_UNKNOWN: Final[str] = "unknown"


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


class ConsumerAdmissionStaleError(RuntimeError):
    """The frozen consumer envelope no longer describes the active request."""


def new_compaction_id() -> str:
    """Return an opaque id used to correlate one compaction attempt's events."""

    return f"cmp_{uuid4().hex}"


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


def _receipt_value(receipt: Any, name: str, default: Any) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name, default)
    return getattr(receipt, name, default)


def durable_receipt_allows_destructive_compaction(receipt: Any) -> bool:
    return (
        _receipt_value(receipt, "scope", "") == "checkpoint"
        and _receipt_value(receipt, "status", "") == "checkpoint_saved"
        and bool(_receipt_value(receipt, "source_path", ""))
        and bool(_receipt_value(receipt, "content_hash", ""))
    )
