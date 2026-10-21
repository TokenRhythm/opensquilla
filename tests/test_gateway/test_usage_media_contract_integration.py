"""One physical media request across metering, revisions, RPC and v4 framing."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.contracts.generated.v4.usage_query import Response
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.engine.usage_http import reserve_direct_usage_call
from opensquilla.gateway.rpc_usage import _query_usage
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider.metered_http import response_measurement
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.usage_metering import UsageMeter, UsageSettlementRevision


@pytest.mark.asyncio
async def test_media_revision_and_estimate_survive_rpc_v4_contract(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    sink = SessionUsageEventSink(storage)
    try:
        await storage.initialize_usage_ledger(1)
        key = "agent:main:webchat:media-contract"
        await storage.upsert_session(SessionNode(session_key=key, session_id="owner"))
        scope = UsageAccountingScope(sink, UsageExecutionContext("run", "run", session_id="owner"))
        with bind_usage_accounting_scope(scope):
            video = await reserve_direct_usage_call(
                provider="tokenrhythm",
                model="wan3.0-video",
                operation_kind="video_generation",
                base_url="https://tokenrhythm.studio/v1",
            )
            await video.finalize_measurement(
                meters=(UsageMeter("requested_video_seconds", "second", "2", source="request"),),
                settlement=UsageSettlementRevision("initial", "unknown", "pending"),
            )
            meters, receipt = response_measurement(
                provider="tokenrhythm",
                operation="video_generation",
                base_url="https://tokenrhythm.studio/v1",
                payload={
                    "billing": {"pending": False, "total_cost_cny": "1.2"},
                    "usage": {"output_seconds": 2},
                },
            )
            receipt = replace(receipt, revision_id="confirmed", previous_revision_id="initial")
            await sink.revise(video.call, receipt, meters=meters)
            replay = await sink.revise(video.call, receipt, meters=meters)
            assert replay["transition_applied"] is False
            search = await reserve_direct_usage_call(
                provider="exa",
                model="search",
                operation_kind="web_search",
                base_url="https://api.exa.ai",
            )
            meters, estimate = response_measurement(
                provider="exa",
                operation="web_search",
                base_url="https://api.exa.ai",
                payload={"costDollars": {"total": "0.005"}},
            )
            await search.finalize_measurement(meters=meters, settlement=estimate)
        events = await storage.query_usage_events(from_ms=0, to_ms=10**15)
        assert len(events) == 2
        # Freeze the RPC read strictly after the finalized fixture timestamps.
        snapshot_time = (max(event.completed_at_ms for event in events) + 1_000) / 1_000
        monkeypatch.setattr("opensquilla.gateway.usage_query.time.time", lambda: snapshot_time)
        payload = await _query_usage(
            {"range": {"preset": "all"}, "timezone": "UTC"},
            SimpleNamespace(session_manager=SessionManager(storage)),
        )
        wire = Response.model_validate(
            {"type": "res", "id": "fixture", "ok": True, "payload": payload}
        ).model_dump(mode="json")
        decoded = json.loads(json.dumps(wire))
        totals = decoded["payload"]["totals"]
        assert Decimal(totals["billedCostUsdExact"]) == Decimal("0.172043010752688172")
        assert Decimal(totals["estimatedCostUsdExact"]) == Decimal("0.005")
        assert totals["eventCount"] == 2
        assert totals["missingCostEntries"] == 0
        assert totals["nativeBilledByCurrency"]["CNY"]["normalizationRatesNativePerUsd"] == [
            "6.975"
        ]
        assert decoded["payload"]["settlementSummary"]["provider_confirmed"] == 1
        assert decoded["payload"]["settlementSummary"]["provider_estimate"] == 1
        assert len(decoded["payload"]["taskGroups"]) == 1
        assert decoded["payload"]["taskGroups"][0]["totals"]["eventCount"] == 2
        schema = json.loads(
            (
                Path(__file__).parents[2]
                / "contracts/gateway/v4/conversation/usage-query.schema.json"
            ).read_text()
        )
        assert schema["$defs"]["Result"]["additionalProperties"] is True
    finally:
        await sink.close()
        await storage.close()
