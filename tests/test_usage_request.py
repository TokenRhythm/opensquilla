"""Search adapters cross the accounting boundary without importing its owner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.search.providers.exa import ExaSearchProvider
from opensquilla.session.storage import SessionStorage
from opensquilla.usage_request import accounted_request


def scope_for(name):
    sink = SimpleNamespace(start=AsyncMock(), finalize=AsyncMock(), mark_unknown=AsyncMock())
    return UsageAccountingScope(sink, UsageExecutionContext(name, name, session_id=name))


async def request(client):
    return await accounted_request(
        client, "POST", "https://api.exa.ai/search", provider="exa", operation="web_search",
        base_url="https://api.exa.ai", json={"query": "fixture"},
    )


def response_client():
    return SimpleNamespace(post=AsyncMock(return_value=httpx.Response(
        200, json={"results": [], "costDollars": {"total": "0.005"}},
    )))


@pytest.mark.asyncio
async def test_unscoped_request_is_direct_and_does_not_forward_accounting_fields():
    client = response_client()
    response = await request(client)
    assert response is client.post.return_value
    client.post.assert_awaited_once_with(
        "https://api.exa.ai/search", json={"query": "fixture"}
    )
    failure = httpx.ConnectError("fixture")
    client.post.side_effect = failure
    with pytest.raises(httpx.ConnectError) as raised:
        await request(client)
    assert raised.value is failure


@pytest.mark.asyncio
async def test_search_dispatch_commits_one_reservation_and_one_receipt(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    sink = SessionUsageEventSink(storage)
    requests = []

    async def transport(request):
        requests.append(request)
        [started] = await storage.query_usage_events(None, None, statuses=("started",))
        assert started.session_id == "search-owner"
        return httpx.Response(200, json={"results": [], "costDollars": {"total": "0.005"}})

    provider = ExaSearchProvider(api_key="fixture", transport=httpx.MockTransport(transport))
    scope = UsageAccountingScope(
        sink, UsageExecutionContext("search-run", "search-run", session_id="search-owner")
    )
    try:
        with bind_usage_accounting_scope(scope):
            assert await provider.search("fixture") == []
        assert len(requests) == 1
        [event] = await storage.query_usage_events(None, None)
        assert event.status == "finalized" and event.estimated_cost_nanos == 5_000_000
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert settlement["authority"] == "provider_estimate"
        assert settlement["usd_amount"] == "0.005"
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.asyncio
async def test_failed_reservation_prevents_http_dispatch():
    scope = scope_for("blocked")
    scope.sink.start.side_effect = RuntimeError("ledger unavailable")
    client = response_client()
    with (
        bind_usage_accounting_scope(scope),
        pytest.raises(RuntimeError, match="ledger unavailable"),
    ):
        await request(client)
    client.post.assert_not_awaited()
    scope.sink.finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_scopes_restore_after_exception_and_none_inherits():
    outer, inner = scope_for("outer"), scope_for("inner")
    client = response_client()
    with bind_usage_accounting_scope(outer):
        with bind_usage_accounting_scope(None):
            await request(client)
        with pytest.raises(ValueError, match="fixture"):
            with bind_usage_accounting_scope(inner):
                await request(client)
                raise ValueError("fixture")
        await request(client)
    await request(client)
    assert outer.sink.start.await_count == outer.sink.finalize.await_count == 2
    assert inner.sink.start.await_count == inner.sink.finalize.await_count == 1
    assert client.post.await_count == 4
    assert [call.args[0].execution_id for call in outer.sink.start.await_args_list] == [
        "outer", "outer"
    ]


@pytest.mark.asyncio
async def test_parallel_scopes_and_inherited_tasks_keep_their_own_owner():
    first, second = scope_for("first"), scope_for("second")
    barrier = asyncio.Barrier(2)
    client = response_client()

    async def run(scope):
        with bind_usage_accounting_scope(scope):
            await barrier.wait()
            await asyncio.gather(request(client), request(client))

    await asyncio.gather(run(first), run(second))
    for scope in (first, second):
        calls = [call.args[0] for call in scope.sink.start.await_args_list]
        assert len(calls) == scope.sink.finalize.await_count == 2
        assert {call.execution_id for call in calls} == {scope.context.execution_id}
        assert len({call.event_id for call in calls}) == 2
    await request(client)
    assert first.sink.start.await_count == second.sink.start.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_scope_closes_its_call_and_restores_direct_requests():
    scope = scope_for("cancelled")
    dispatched = asyncio.Event()
    blocked = SimpleNamespace(post=AsyncMock())
    direct = response_client()

    async def block(*args, **kwargs):
        dispatched.set()
        await asyncio.Event().wait()

    blocked.post.side_effect = block

    async def run():
        try:
            with bind_usage_accounting_scope(scope):
                await request(blocked)
        except asyncio.CancelledError:
            await request(direct)
            raise

    task = asyncio.create_task(run())
    await dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scope.sink.start.await_count == scope.sink.mark_unknown.await_count == 1
    scope.sink.finalize.assert_not_awaited()
    direct.post.assert_awaited_once()
