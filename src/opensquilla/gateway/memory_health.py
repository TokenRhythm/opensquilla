"""Durable memory-health projection shared by Gateway read surfaces."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.keys import normalize_agent_id

_HEALTH_SCAN_LIMIT = 1000
_SAFETY_ERROR_STATUSES = {"checkpoint_failed", "receipt_orphaned"}
_HASH_MISMATCH_MARKERS = ("hash_mismatch", "hash mismatch")


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
                scope="checkpoint",
            )
        )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    receipt_rows: list[Any] = []
    for status in (*_SAFETY_ERROR_STATUSES, "hash_mismatch"):
        receipt_rows.extend(
            await list_receipts(scope="checkpoint", status=status, limit=_HEALTH_SCAN_LIMIT)
        )
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


async def memory_health_from_durable_ledger(
    session_manager: Any,
    *,
    agent_id: str,
) -> dict[str, Any]:
    """Project deterministic checkpoint safety for one agent."""

    storage = get_session_storage(session_manager)
    if storage is None:
        return {
            "memorySafety": {"status": "ok"},
        }

    recent_rows = await _recent_durable_receipts(storage, agent_id=agent_id)
    safety_status = "error" if any(_is_safety_error_receipt(row) for row in recent_rows) else "ok"
    return {"memorySafety": {"status": safety_status}}


__all__ = ["memory_health_from_durable_ledger"]
