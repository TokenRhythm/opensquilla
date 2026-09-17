from __future__ import annotations

import pytest

from opensquilla.gateway.memory_health import memory_health_from_durable_ledger


@pytest.mark.asyncio
async def test_memory_health_without_storage_is_safe_and_empty() -> None:
    assert await memory_health_from_durable_ledger(None, agent_id="main") == {
        "memorySafety": {"status": "ok"},
    }


@pytest.mark.parametrize("status, expected", [
    ("checkpoint_saved", "ok"),
    ("checkpoint_failed", "error"),
    ("receipt_orphaned", "error"),
    ("hash_mismatch", "error"),
])
async def test_memory_health_reports_only_requested_agents_checkpoint_safety(
    tmp_path, status, expected,
) -> None:
    from types import SimpleNamespace

    from opensquilla.session.models import MemoryDurableReceipt
    from opensquilla.session.storage import SessionStorage

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        for receipt_id, agent_id, scope, receipt_status in (
            ("requested", "main", "checkpoint", status),
            ("other-agent", "other", "checkpoint", "checkpoint_failed"),
            ("other-scope", "main", "extension", "hash_mismatch"),
        ):
            await storage.upsert_memory_durable_receipt(MemoryDurableReceipt(
                receipt_id=receipt_id, session_key=f"agent:{agent_id}:webchat:synthetic",
                session_id="synthetic-session", scope=scope, status=receipt_status,
                idempotency_key=receipt_id,
            ))
        health = await memory_health_from_durable_ledger(
            SimpleNamespace(storage=storage), agent_id="main",
        )
        assert health == {"memorySafety": {"status": expected}}
    finally:
        await storage.close()
