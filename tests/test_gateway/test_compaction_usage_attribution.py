"""Compaction receipts retain their physical owner across task rollups and replay."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent as AgentErrorEvent
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.adapters.session_maintenance import (
    GatewaySessionMaintenancePorts,
    build_gateway_session_maintenance_adapter,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.gateway.usage_query import query_usage_ledger
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.protocol import ProviderMetadata
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.session.compaction import call_compaction_provider
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))

    def no_http(*args, **kwargs):
        raise AssertionError("Compaction accounting tests must not send HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)


class RecordingSink(SessionUsageEventSink):
    def __init__(self, storage):
        super().__init__(storage)
        self.receipts = []

    async def finalize(self, call, result):
        await super().finalize(call, result)
        self.receipts.append((call, result))


class SummaryProvider:
    provider_name = "openai"

    def __init__(self, attempts):
        self.attempts = iter(attempts)
        self.calls = 0

    def provider_metadata(self):
        return ProviderMetadata(
            provider_name="openai", provider_kind="openrouter", provider_id="openrouter",
            model="test/summary", base_url="https://openrouter.ai/api/v1",
        )

    async def chat(self, messages, tools=None, config=None):
        self.calls += 1
        for event in next(self.attempts):
            yield event


def success(amount="0.02", **kwargs):
    return [
        TextDeltaEvent(text="A complete synthetic summary."),
        DoneEvent(
            input_tokens=12, output_tokens=2, billed_cost=float(amount),
            cost_source="provider_billed", model="test/summary", provider="openrouter", **kwargs,
        ),
    ]


def failure(*, receipt, code="400"):
    return ErrorEvent(
        code=code,
        model_usage_breakdown=[{
            "provider": "openrouter", "model": "test/summary", "input_tokens": 12,
            "output_tokens": 2, "billed_cost": 0.01, "cost_source": "provider_billed",
        }] if receipt else [],
    )


async def summarize(provider):
    plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
        provider=provider, provider_id="openrouter", model="test/summary",
        max_output_tokens=128, context_window_tokens=8192, provider_request_max_chars=120_000,
    ),))
    return await call_compaction_provider("Synthetic history", "", plan, timeout=2)


def automatic_scope(sink, owner, run):
    return UsageAccountingScope(sink, UsageExecutionContext(
        execution_id=run, agent_run_id=run, turn_id=run, session_id=owner.session_id,
        session_epoch=owner.epoch, agent_id="main", run_kind="agent",
    ))


def child(parent, name, *, side=False):
    origin = {"sidechat": {"parentSessionId": parent.session_id, "parentEpoch": parent.epoch}}
    if not side:
        origin = {
            "kind": "subagent", "parent_session_id": parent.session_id,
            "parent_session_epoch": parent.epoch,
        }
    return SessionNode(
        session_key=f"agent:main:{'sidechat' if side else 'subagent'}:{name}",
        session_id=name, parent_session_key=parent.session_key, origin=origin,
    )


async def report(storage):
    rows = await storage.query_usage_events(None, None, statuses=("finalized", "unknown"))
    timestamp = max(row.completed_at_ms or row.started_at_ms for row in rows) + 1000
    return await query_usage_ledger(
        storage, {"range": {"preset": "all"}, "timezone": "UTC"}, now_ms=timestamp
    )


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("owner_kind", ["root", "child", "side", "side-child"])
async def test_compaction_keeps_session_owner_and_parent_rollup(tmp_path, manual, owner_kind):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    sink = RecordingSink(storage)
    try:
        await storage.initialize_usage_ledger(1)
        root = SessionNode(session_key="agent:main:webchat:root", session_id="root")
        side = child(root, "side", side=True)
        owners = {"root": root, "child": child(root, "child"), "side": side,
                  "side-child": child(side, "side-child")}
        for owner in owners.values():
            await storage.upsert_session(owner)
        owner = owners[owner_kind]
        with bind_usage_accounting_scope(automatic_scope(sink, root, "parent-run")):
            await summarize(SummaryProvider([success("0.01")]))
        provider = SummaryProvider([success()])
        if manual:
            ports = GatewaySessionMaintenancePorts(SimpleNamespace(
                session_manager=SessionManager(storage), usage_event_sink=sink,
            ))
            async with ports.account(owner.session_key):
                assert await summarize(provider)
        else:
            with bind_usage_accounting_scope(automatic_scope(sink, owner, "automatic-summary")):
                assert await summarize(provider)
        before = await report(storage)
        assert provider.calls == 1
        [group] = before["taskGroups"]
        assert group["rootSessionId"] == root.session_id
        assert group["totals"]["costUsdExact"] == "0.03"
        branch = {"root": "self", "child": "subsessions", "side": "sideChats",
                  "side-child": "sideChats"}[owner_kind]
        assert Decimal(group["branches"][branch]["costUsdExact"]) == (
            Decimal("0.03") if owner_kind == "root" else Decimal("0.02")
        )
        call, _result = sink.receipts[-1]
        assert (call.session_id, call.session_epoch) == (owner.session_id, owner.epoch)
        assert call.run_kind == ("session_compaction" if manual else "agent")
        assert before["eventCount"] == 2
        for call, result in tuple(sink.receipts):
            await sink.finalize(call, result)
        after = await report(storage)
        assert after["totals"] == before["totals"]
        assert after["accountingRevision"] == before["accountingRevision"]
        assert after["taskGroups"] == before["taskGroups"]
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.parametrize("receipt", [False, True])
async def test_compaction_retry_records_each_attempt_once(tmp_path, monkeypatch, receipt):
    monkeypatch.setattr("opensquilla.session.compaction.sleep_before_retry", AsyncMock())
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    sink = RecordingSink(storage)
    try:
        owner = SessionNode(session_key="agent:main:webchat:retry", session_id="retry")
        await storage.upsert_session(owner)
        provider = SummaryProvider([[failure(receipt=receipt, code="503")], success()])
        with bind_usage_accounting_scope(automatic_scope(sink, owner, "retry-run")):
            assert await summarize(provider)
        assert provider.calls == 2
        rows = await storage.query_usage_events(None, None, statuses=("finalized", "unknown"))
        assert len(rows) == len({row.event_id for row in rows}) == 2
        assert sorted(row.call_index for row in rows) == [1, 2]
        assert sum(row.status == "unknown" for row in rows) == int(not receipt)
        result = await report(storage)
        assert Decimal(result["totals"]["billedCostUsdExact"]) == (
            Decimal("0.03") if receipt else Decimal("0.02")
        )
        assert result["totals"]["missingCostEntries"] == int(not receipt)
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.parametrize("outcome", ["refusal", "incomplete", "error_receipt", "error_unknown"])
async def test_failed_summary_retains_receipt_or_explicit_unknown(tmp_path, outcome):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    sink = RecordingSink(storage)
    try:
        owner = SessionNode(session_key="agent:main:webchat:failure", session_id="failure")
        await storage.upsert_session(owner)
        events = (
            success(refusal=True) if outcome == "refusal"
            else success(stop_reason="length") if outcome == "incomplete"
            else [failure(receipt=outcome == "error_receipt")]
        )
        with bind_usage_accounting_scope(automatic_scope(sink, owner, "failure-run")):
            assert await summarize(SummaryProvider([events])) is None
        result = await report(storage)
        assert result["eventCount"] == 1
        assert result["totals"]["missingCostEntries"] == int(outcome == "error_unknown")
        assert Decimal(result["totals"]["billedCostUsdExact"]) == {
            "refusal": Decimal("0.02"), "incomplete": Decimal("0.02"),
            "error_receipt": Decimal("0.01"), "error_unknown": Decimal(0),
        }[outcome]
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.parametrize("old_lineage", [False, True])
async def test_manual_compaction_scope_does_not_follow_epoch_reset_before_first_call(
    tmp_path, old_lineage,
):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    sink = RecordingSink(storage)
    try:
        root = SessionNode(session_key="agent:main:webchat:reset", session_id="root")
        owner = child(root, "reset-side", side=True)
        await storage.upsert_session(root)
        await storage.upsert_session(owner)
        if old_lineage:
            with bind_usage_accounting_scope(automatic_scope(sink, owner, "old-run")):
                await summarize(SummaryProvider([success("0.01")]))
        ports = GatewaySessionMaintenancePorts(SimpleNamespace(
            session_manager=SessionManager(storage), usage_event_sink=sink,
        ))
        async with ports.account(owner.session_key):
            await storage.advance_reset_epoch(owner.session_key)
            assert await summarize(SummaryProvider([success()]))
        rows = await storage.query_usage_events(None, None)
        assert {row.session_epoch for row in rows} == {0}
        reset_owner = await storage.get_session(owner.session_key)
        with bind_usage_accounting_scope(automatic_scope(sink, reset_owner, "new-run")):
            await summarize(SummaryProvider([success("0.04")]))
        groups = (await report(storage))["taskGroups"]
        assert len(groups) == 2
        old, new = sorted(groups, key=lambda group: group["rootSessionEpoch"])
        assert new["rootSessionId"] == owner.session_id
        assert new["rootSessionEpoch"] == 1
        assert new["totals"]["costUsdExact"] == "0.04"
        if old_lineage:
            assert old["rootSessionId"] == root.session_id
            assert old["branches"]["sideChats"]["costUsdExact"] == "0.03"
            assert old["attributionStatus"] == "complete"
        else:
            # No old-generation origin survived the reset. Do not infer its
            # parent from the newer session row, even though its key matches.
            assert old["rootSessionId"] == owner.session_id
            assert old["branches"]["unattributed"]["costUsdExact"] == "0.02"
            assert old["attributionStatus"] == "partial"
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.parametrize("manual", [False, True])
async def test_real_compaction_entrypoints_account_sidechat_descendant(
    tmp_path, monkeypatch, manual,
):
    """Only provider output is fake: trigger, compaction, storage and query are real."""
    from opensquilla import token_estimation

    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = "synthetic-summary"
    config = GatewayConfig(
        workspace_dir=str(workspace),
        llm={"provider": "openai", "model": model, "api_key": "synthetic-unused",
             "base_url": "https://example.invalid/v1", "context_window_tokens": 8192,
             "max_tokens": 512, "thinking": "off"},
        compaction={"protected_recent_messages": 2},
        prompt={"mode": "minimal"}, squilla_router={"enabled": False},
        llm_ensemble={"enabled": False},
    )
    config.attachments.media_root = str(tmp_path / "media")
    calls = []

    async def reply(self, messages, tools=None, config=None):
        is_summary = config.candidate_output_mode == "inert_artifact"
        calls.append(is_summary)
        yield TextDeltaEvent(text="Earlier discussion reviewed; continue the current task.")
        yield DoneEvent(
            stop_reason="stop", input_tokens=12, output_tokens=2,
            billed_cost=0.02 if is_summary else 0.01, cost_source="provider_billed",
            provider="openai", model=model,
        )

    monkeypatch.setattr(OpenAIProvider, "chat", reply)
    storage = await SessionStorage.open(str(tmp_path / "entrypoints.db"))
    sink = RecordingSink(storage)
    try:
        await storage.initialize_usage_ledger(1)
        root = SessionNode(session_key="agent:main:webchat:entrypoint", session_id="root")
        side = child(root, "entry-side", side=True)
        owner = child(side, "entry-child")
        for node in (root, side, owner):
            await storage.upsert_session(node)
        manager = SessionManager(storage, inject_time_prefix=False,
                                 checkpoint_workspace_dir=workspace)
        for index in range(12):
            await manager.append_message(
                owner.session_key, "user" if index % 2 == 0 else "assistant",
                "Earlier discussion details were reviewed. " * 50,
            )
        selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
            provider="openai", model=model, api_key="synthetic-unused",
            base_url="https://example.invalid/v1",
        )))
        runner = TurnRunner(
            provider_selector=selector, session_manager=manager, config=config,
            model_catalog=ModelCatalog(), usage_event_sink=sink,
        )
        if manual:
            adapter = build_gateway_session_maintenance_adapter(RpcContext(
                conn_id="compaction-accounting", config=config, session_manager=manager,
                provider_selector=selector, turn_runner=runner, usage_event_sink=sink,
            ))
            outcome = await adapter.compact({"key": owner.session_key})
            assert outcome["applied"] is True
        else:
            events = [event async for event in runner.run(
                "Continue the current task.", owner.session_key,
                ToolContext(is_owner=True, caller_kind=CallerKind.WEB,
                            session_key=owner.session_key, workspace_dir=str(workspace)),
                persist_input=True, history_has_persisted_user=False, no_memory_capture=True,
                max_iterations=1, max_provider_retries=0, length_capped_continuations=0,
                expected_session_id=owner.session_id, expected_session_epoch=owner.epoch,
            )]
            assert not any(isinstance(event, AgentErrorEvent) for event in events), events
        assert calls.count(True) >= 1
        assert calls.count(False) == int(not manual)
        before = await report(storage)
        [group] = before["taskGroups"]
        assert group["rootSessionId"] == root.session_id
        expected = Decimal("0.02") * calls.count(True) + Decimal("0.01") * calls.count(False)
        assert Decimal(group["totals"]["costUsdExact"]) == expected
        assert Decimal(group["branches"]["sideChats"]["costUsdExact"]) == expected
        assert before["eventCount"] == len(calls) == len(sink.receipts)
        assert {(call.session_id, call.session_epoch) for call, _ in sink.receipts} == {
            (owner.session_id, owner.epoch),
        }
        for call, receipt in tuple(sink.receipts):
            await sink.finalize(call, receipt)
        after = await report(storage)
        assert after["eventCount"] == before["eventCount"]
        assert after["taskGroups"] == before["taskGroups"]
        assert after["accountingRevision"] == before["accountingRevision"]
    finally:
        await sink.close()
        await storage.close()
