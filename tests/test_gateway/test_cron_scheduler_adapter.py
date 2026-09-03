from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.cron_scheduler import (
    GatewayCronCallbacks,
    GatewayCronSchedulerAdapter,
)
from opensquilla.gateway.rpc import RpcContext


def _callbacks() -> GatewayCronCallbacks:
    return GatewayCronCallbacks(
        list_jobs=AsyncMock(return_value=[{"id": "daily"}]),
        status=AsyncMock(return_value={"id": "daily"}),
        create=AsyncMock(return_value={"id": "daily"}),
        update=AsyncMock(return_value={"id": "daily", "enabled": False}),
        remove=AsyncMock(return_value=None),
        run=AsyncMock(return_value={"runId": "run-1", "success": True}),
        runs=AsyncMock(return_value=[{"id": "run-1", "success": True}]),
        subscribe=AsyncMock(return_value={"ok": True, "topic": "cron:daily"}),
        unsubscribe=AsyncMock(return_value={"ok": True, "topic": "cron:*"}),
    )


@pytest.mark.asyncio
async def test_cron_adapter_projects_typed_queries_and_mutations() -> None:
    callbacks = _callbacks()
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayCronSchedulerAdapter(context, callbacks)

    assert await adapter.list_jobs({"agentId": " main "}) == [{"id": "daily"}]
    assert await adapter.status({"id": " daily "}) == {"id": "daily"}
    assert await adapter.create({"name": "Daily"}) == {"id": "daily"}
    assert await adapter.update({"id": " daily ", "enabled": False}) == {
        "id": "daily",
        "enabled": False,
    }

    cast(AsyncMock, callbacks.list_jobs).assert_awaited_once_with(
        {"agentId": "main"}, context
    )
    cast(AsyncMock, callbacks.status).assert_awaited_once_with({"id": "daily"}, context)
    cast(AsyncMock, callbacks.create).assert_awaited_once_with(
        {"name": "Daily"}, context
    )
    cast(AsyncMock, callbacks.update).assert_awaited_once_with(
        {"enabled": False, "id": "daily"}, context
    )


@pytest.mark.asyncio
async def test_cron_add_and_create_share_one_application_implementation() -> None:
    callbacks = _callbacks()
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayCronSchedulerAdapter(context, callbacks)

    await adapter.create({"name": "Daily"})
    await adapter.create({"name": "Weekly"})

    assert cast(AsyncMock, callbacks.create).await_count == 2
    assert cast(AsyncMock, callbacks.update).await_count == 0


@pytest.mark.asyncio
async def test_cron_adapter_preserves_legacy_run_id_and_subscription_topics() -> None:
    callbacks = _callbacks()
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayCronSchedulerAdapter(context, callbacks)

    assert await adapter.runs({"job_id": " daily ", "limit": 3}) == [
        {"id": "run-1", "success": True}
    ]
    await adapter.subscribe({"jobId": " daily "})
    await adapter.unsubscribe(None)

    cast(AsyncMock, callbacks.runs).assert_awaited_once_with(
        {"id": "daily", "limit": 3}, context
    )
    cast(AsyncMock, callbacks.subscribe).assert_awaited_once_with(
        {"jobId": "daily"}, context
    )
    cast(AsyncMock, callbacks.unsubscribe).assert_awaited_once_with(None, context)
