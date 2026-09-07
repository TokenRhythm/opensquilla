from __future__ import annotations

import pytest

from opensquilla.gateway.memory_health import memory_health_from_durable_ledger


@pytest.mark.asyncio
async def test_memory_health_without_storage_is_safe_and_empty() -> None:
    assert await memory_health_from_durable_ledger(None, agent_id="main") == {
        "memorySafety": {"status": "ok"},
        "semanticMemory": {"status": "healthy", "repairBacklogCount": 0},
    }
