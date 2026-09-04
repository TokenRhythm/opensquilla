"""Durable memory-health projection shared by Gateway read surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opensquilla.gateway.memory_repair_service import list_repair_queue
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.keys import normalize_agent_id

_HEALTH_SCAN_LIMIT = 1000
_SAFETY_ERROR_STATUSES = {"checkpoint_failed", "receipt_orphaned"}
_HASH_MISMATCH_MARKERS = ("hash_mismatch", "hash mismatch")
_SEMANTIC_WARNING_AGE_MS = 24 * 60 * 60 * 1000


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _is_safety_error_receipt(row: Any) -> bool:
    status = str(_row_value(row, "status", "") or "").lower()
    reason = str(_row_value(row, "reason", "") or "").lower()
    if status in _SAFETY_ERROR_STATUSES:
        return True
    return any(marker in status or marker in reason for marker in _HASH_MISMATCH_MARKERS)


async def _recent_durable_receipts(storage: Any, *, agent_id: str) -> list[Any]:
    agent_prefix = f"agent:{normalize_agent_id(agent_id)}:" if agent_id else None
    list_recent = getattr(storage, "list_recent_memory_durable_receipts", None)
    if callable(list_recent):
        return list(
            await list_recent(
                limit=_HEALTH_SCAN_LIMIT,
                session_key_prefix=agent_prefix,
            )
        )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    receipt_rows: list[Any] = []
    for status in (*_SAFETY_ERROR_STATUSES, "hash_mismatch"):
        receipt_rows.extend(await list_receipts(status=status, limit=_HEALTH_SCAN_LIMIT))
    if agent_prefix is not None:
        receipt_rows = [
            row
            for row in receipt_rows
            if str(_row_value(row, "session_key", "") or "").startswith(agent_prefix)
        ]
    receipt_rows.sort(
        key=lambda row: (
            int(_row_value(row, "created_at", 0) or 0),
            str(_row_value(row, "receipt_id", "") or ""),
        ),
        reverse=True,
    )
    return list(receipt_rows[:_HEALTH_SCAN_LIMIT])


def _semantic_repair_status(backlog_count: int, oldest_pending_ms: int | None) -> str:
    if backlog_count <= 0:
        return "healthy"
    if backlog_count > 10:
        return "warning"
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if oldest_pending_ms is None or now_ms - oldest_pending_ms > _SEMANTIC_WARNING_AGE_MS:
        return "warning"
    return "degraded"


async def memory_health_from_durable_ledger(
    session_manager: Any,
    *,
    agent_id: str,
) -> dict[str, Any]:
    """Project durable safety and semantic-repair health for one agent."""

    storage = get_session_storage(session_manager)
    if storage is None:
        return {
            "memorySafety": {"status": "ok"},
            "semanticMemory": {"status": "healthy", "repairBacklogCount": 0},
        }

    recent_rows = await _recent_durable_receipts(storage, agent_id=agent_id)
    safety_status = "error" if any(_is_safety_error_receipt(row) for row in recent_rows) else "ok"
    pending_rows = await list_repair_queue(
        storage,
        limit=_HEALTH_SCAN_LIMIT,
        agent_id=agent_id,
        due_only=False,
    )
    backlog_count = len(pending_rows)
    oldest_pending_ms = min(
        (
            int(created_at)
            for row in pending_rows
            if (created_at := _row_value(row, "created_at", None)) is not None
        ),
        default=None,
    )
    return {
        "memorySafety": {"status": safety_status},
        "semanticMemory": {
            "status": _semantic_repair_status(backlog_count, oldest_pending_ms),
            "repairBacklogCount": backlog_count,
        },
    }


__all__ = ["memory_health_from_durable_ledger"]
