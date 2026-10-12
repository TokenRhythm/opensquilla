"""Semantic status helpers for session flush receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from opensquilla.session.compaction_lifecycle import (
    compaction_memory_status,
    flush_receipt_is_successful_flush,
    flush_receipt_status,
    flush_receipt_to_dict,
)

FlushRepairStatus = Literal["none", "pending", "failed"]


@dataclass(frozen=True)
class FlushStatus:
    """Normalized view of a flush receipt for retry and audit decisions."""

    receipt_status: str
    safety_status: str
    semantic_status: str
    repair_status: FlushRepairStatus
    allows_destructive_compaction: bool
    successful_flush: bool
    has_raw_archive: bool
    result_status: str
    raw_reason: str | None = None
    target_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_flush_receipt(receipt: Any, *, required: bool = True) -> FlushStatus:
    """Classify an existing flush receipt without mutating flush behavior."""

    values = flush_receipt_to_dict(receipt)
    result_status = str(values.get("result_status") or "")
    target_path = _first_path(values.get("flushed_paths")) or _optional_str(
        values.get("target_path")
    )
    has_raw_archive = bool(
        target_path and target_path.startswith("memory/.raw_fallbacks/")
    )
    memory_status = compaction_memory_status(receipt, required=required)
    repair_status: FlushRepairStatus = "none"
    if result_status == "archive_failed":
        repair_status = "failed"
    elif result_status.endswith("_archived") or result_status == "ok_archive_only":
        repair_status = "pending"
    return FlushStatus(
        receipt_status=flush_receipt_status(receipt),
        safety_status=memory_status.safety_status,
        semantic_status=memory_status.semantic_status,
        repair_status=repair_status,
        allows_destructive_compaction=memory_status.allows_destructive_compaction,
        successful_flush=flush_receipt_is_successful_flush(receipt),
        has_raw_archive=has_raw_archive,
        result_status=result_status,
        raw_reason=_optional_str(values.get("raw_reason")),
        target_path=target_path,
    )


def flush_status_details(receipt: Any, *, required: bool = True) -> dict[str, Any]:
    return classify_flush_receipt(receipt, required=required).to_dict()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _first_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and value:
        return _optional_str(value[0])
    return None
