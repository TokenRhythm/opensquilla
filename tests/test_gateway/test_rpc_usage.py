"""Tests for /api/usage RPC handlers — focused on cache_read / cache_write totals."""

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.engine.usage import UsageTracker
from opensquilla.gateway import rpc_usage
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_usage import _handle_usage_cost, _handle_usage_status
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


def _ctx(*, session_manager=None, usage_tracker=None) -> RpcContext:
    return RpcContext(
        conn_id="test",
        session_manager=session_manager,
        usage_tracker=usage_tracker,
        config=SimpleNamespace(llm=SimpleNamespace(model="claude-opus-4-7")),
    )


def test_usage_status_tracker_only_path_surfaces_cache_totals() -> None:
    """When session_manager is None, cache numbers must come from the in-memory tracker."""
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:abc",
        input_tokens=1000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        cache_read_tokens=200,
        cache_write_tokens=80,
    )

    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    assert payload["totalCacheReadTokens"] == 200
    assert payload["totalCacheWriteTokens"] == 80
    assert payload["totalSessions"] == 1

    [row] = payload["sessions"]
    # camelCase keys
    assert row["cacheReadTokens"] == 200
    assert row["cacheWriteTokens"] == 80
    # snake_case aliases for the legacy UI
    assert row["cache_read_tokens"] == 200
    assert row["cache_write_tokens"] == 80
    assert row["costSource"] == "opensquilla_estimate"
    assert row["cost_source"] == "opensquilla_estimate"
    assert row["costEphemeral"] is True
    assert row["cost_ephemeral"] is True
    assert row["billedCostUsd"] == 0.0
    assert row["estimatedCostUsd"] == row["costUsd"]
    assert row["deploymentBreakdown"][0]["model"] == "claude-opus-4-7"


def test_usage_status_surfaces_provider_model_deployment_breakdown() -> None:
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:shared",
        input_tokens=100,
        output_tokens=10,
        model_id="shared-model",
        provider="provider-a",
        billed_cost=0.01,
    )
    tracker.add(
        "agent:webchat:shared",
        input_tokens=200,
        output_tokens=20,
        model_id="shared-model",
        provider="provider-b",
        billed_cost=0.02,
    )

    payload = asyncio.run(
        _handle_usage_status(None, _ctx(session_manager=None, usage_tracker=tracker))
    )

    [row] = payload["sessions"]
    assert len(row["modelBreakdown"]) == 1
    deployments = {
        (item["provider"], item["model"]): item
        for item in row["deploymentBreakdown"]
    }
    assert set(deployments) == {
        ("provider-a", "shared-model"),
        ("provider-b", "shared-model"),
    }
    assert sum(item["billedCostUsd"] for item in deployments.values()) == pytest.approx(
        row["billedCostUsd"]
    )


def test_usage_status_merges_deployment_breakdown_into_persisted_session() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:persisted-shared",
        status="running",
        input_tokens=300,
        output_tokens=30,
        total_cost_usd=0.03,
        billed_cost_usd=0.03,
        estimated_cost_component_usd=0.0,
        cost_source="provider_billed",
        cache_read=0,
        cache_write=0,
        model="shared-model",
    )
    tracker = UsageTracker()
    tracker.add(
        session.session_key,
        input_tokens=100,
        output_tokens=10,
        model_id="shared-model",
        provider="provider-a",
        billed_cost=0.01,
    )
    tracker.add(
        session.session_key,
        input_tokens=200,
        output_tokens=20,
        model_id="shared-model",
        provider="provider-b",
        billed_cost=0.02,
    )

    payload = asyncio.run(
        _handle_usage_status(
            None,
            _ctx(
                session_manager=_FakeSessionManager([session]),
                usage_tracker=tracker,
            ),
        )
    )

    [row] = payload["sessions"]
    assert {
        (item["provider"], item["model"])
        for item in row["deploymentBreakdown"]
    } == {
        ("provider-a", "shared-model"),
        ("provider-b", "shared-model"),
    }


def test_usage_status_tracker_row_source_matches_breakdown_when_billed() -> None:
    """Tracker rows with real billed data must match breakdown cost sources.

    A tracker-only row must not claim ``opensquilla_estimate`` while each
    per-model breakdown item reports ``provider_billed``.
    """
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:billed",
        input_tokens=29213,
        output_tokens=400,
        model_id="anthropic/claude-4.7-opus",
        cache_read_tokens=11588,
        cache_write_tokens=17772,
        billed_cost=0.1254,
    )
    tracker.add(
        "agent:webchat:billed",
        input_tokens=9323,
        output_tokens=0,
        model_id="z-ai/glm-5.1",
        billed_cost=0.0111,
    )

    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    # Row now reports the real billed total + provider_billed source.
    assert row["costSource"] == "provider_billed"
    assert row["cost_source"] == "provider_billed"
    assert row["costUsd"] == 0.1365
    assert row["billedCostUsd"] == 0.1365
    # Per-model breakdown items also provider_billed; sum equals row cost.
    breakdown = row["modelBreakdown"]
    assert all(item["costSource"] == "provider_billed" for item in breakdown)
    breakdown_sum = sum(item["costUsd"] for item in breakdown)
    assert breakdown_sum == row["costUsd"]


def test_usage_status_tracker_row_preserves_confirmed_zero_receipt() -> None:
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:confirmed-zero",
        input_tokens=1_000,
        output_tokens=50,
        model_id="anthropic/claude-4.7-opus",
        billed_cost=0.0,
        cost_source="provider_billed",
    )

    payload = asyncio.run(
        _handle_usage_status(
            None,
            _ctx(session_manager=None, usage_tracker=tracker),
        )
    )

    [row] = payload["sessions"]
    assert row["costUsd"] == 0.0
    assert row["billedCostUsd"] == 0.0
    assert row["estimatedCostUsd"] == 0.0
    assert row["costSource"] == "provider_billed"


def test_usage_status_tracker_row_estimated_is_the_unbilled_component() -> None:
    """estimatedCostUsd is the estimated component of the total, so a billed
    row keeps costUsd == billedCostUsd + estimatedCostUsd (matching the
    per-model breakdown and the persisted rollup, not the full list-price
    estimate over all tokens)."""
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:billed",
        input_tokens=29213,
        output_tokens=400,
        model_id="anthropic/claude-4.7-opus",
        cache_read_tokens=11588,
        cache_write_tokens=17772,
        billed_cost=0.1254,
    )

    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["costSource"] == "provider_billed"
    assert row["estimatedCostUsd"] == pytest.approx(
        row["costUsd"] - row["billedCostUsd"], abs=1e-9
    )
    for item in row["modelBreakdown"]:
        assert item["costUsd"] == pytest.approx(
            item["billedCostUsd"] + item["estimatedCostUsd"]
        )


def test_usage_status_tracker_row_source_mixed_when_some_models_unbilled() -> None:
    """Mix of billed + unbilled models in the tracker → row gets 'mixed'
    source (not provider_billed and not opensquilla_estimate). The row
    cost must equal the breakdown sum (billed for billed models +
    estimate for unbilled), not just the billed-only portion — otherwise
    the row visibly under-reports against its own breakdown.
    """
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:mixed",
        input_tokens=1000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        billed_cost=0.05,
    )
    tracker.add(
        "agent:webchat:mixed",
        input_tokens=2000,
        output_tokens=80,
        model_id="deepseek-v4-pro",
        # no billed_cost — provider didn't return a price for this call
    )

    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["costSource"] == "mixed"
    # billed_cost_usd reflects only the truly billed portion.
    assert row["billedCostUsd"] == 0.05
    # Key invariant: row.cost_usd == sum of breakdown costs, i.e. billed
    # (for billed models) + estimate (for unbilled models). Setting this to
    # billed_cost only under-reports by the unbilled portion.
    breakdown_sum = sum(item["costUsd"] for item in row["modelBreakdown"])
    assert row["costUsd"] == pytest.approx(breakdown_sum)
    # And the unbilled model contributed a non-zero estimate.
    assert row["costUsd"] > 0.05


class _FakeSessionManager:
    def __init__(self, sessions):
        self._sessions = sessions

    async def list_sessions(self):
        return self._sessions


class _FakeTranscriptSessionManager(_FakeSessionManager):
    def __init__(self, sessions, transcript):
        super().__init__(sessions)
        self._transcript = transcript
        self.transcript_calls = 0

    async def get_transcript(self, session_key):
        self.transcript_calls += 1
        return self._transcript


def test_usage_status_session_manager_path_reads_cache_fields() -> None:
    """When session_manager has records, getattr on cache_read/cache_write must flow through."""
    session = SimpleNamespace(
        session_key="agent:webchat:xyz",
        status="running",
        input_tokens=5000,
        output_tokens=200,
        estimated_cost_usd=0.04,
        cache_read=300,
        cache_write=120,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    assert payload["totalCacheReadTokens"] == 300
    assert payload["totalCacheWriteTokens"] == 120
    [row] = payload["sessions"]
    assert row["cacheReadTokens"] == 300
    assert row["cacheWriteTokens"] == 120
    assert row["costUsd"] == 0.04
    assert row["estimatedCostUsd"] == 0.04
    assert row["billedCostUsd"] == 0.0
    assert row["costSource"] == "opensquilla_estimate"
    assert row["costEphemeral"] is False


def test_usage_status_reports_context_pressure_from_session_context_not_lifetime_usage() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:compact",
        status="running",
        input_tokens=1_137_000,
        output_tokens=18_000,
        context_tokens=36_809,
        compaction_count=0,
        model="z-ai/glm-5.1",
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    context_status = row["contextStatus"]
    assert context_status == row["context_status"]
    assert context_status["contextTokens"] == 36_809
    assert context_status["context_tokens"] == 36_809
    assert context_status["contextWindowTokens"] == 200_000
    assert context_status["context_window_tokens"] == 200_000
    assert context_status["compactionCount"] == 0
    assert context_status["pressure"] < 0.25
    assert context_status["pressure"] < row["inputTokens"] / context_status["contextWindowTokens"]


def test_usage_status_only_estimates_transcript_context_for_requested_session() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:requested",
        status="running",
        input_tokens=500_000,
        output_tokens=10_000,
        model="z-ai/glm-5.1",
    )
    sm = _FakeTranscriptSessionManager(
        [session],
        [
            SimpleNamespace(token_count=1_500),
            SimpleNamespace(content="apple book chair"),
        ],
    )
    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())

    unrequested = asyncio.run(_handle_usage_status(None, ctx))
    assert sm.transcript_calls == 0
    assert unrequested["sessions"][0]["contextStatus"] is None

    requested = asyncio.run(_handle_usage_status({"sessionKey": "agent:webchat:requested"}, ctx))
    assert sm.transcript_calls == 1
    context_status = requested["sessions"][0]["contextStatus"]
    assert context_status["tokenSource"] == "transcript_estimate"
    assert context_status["contextTokens"] >= 1_500


def test_usage_status_requested_compacted_session_prefers_active_transcript_context() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:compact",
        status="finished",
        input_tokens=1_296_184,
        output_tokens=1_000,
        context_tokens=1_296_184,
        compaction_count=1,
        model="deepseek-v4-flash",
    )
    sm = _FakeTranscriptSessionManager(
        [session],
        [
            SimpleNamespace(token_count=36_000),
            SimpleNamespace(token_count=184),
        ],
    )
    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())

    unrequested = asyncio.run(_handle_usage_status(None, ctx))
    assert sm.transcript_calls == 0
    assert unrequested["sessions"][0]["contextStatus"]["contextTokens"] == 1_296_184

    requested = asyncio.run(_handle_usage_status({"sessionKey": "agent:webchat:compact"}, ctx))
    assert sm.transcript_calls == 1
    context_status = requested["sessions"][0]["contextStatus"]
    assert context_status["tokenSource"] == "transcript_estimate"
    assert context_status["contextTokens"] == 36_184
    assert context_status["pressure"] < 0.1


def test_usage_status_exposes_session_timestamp_aliases() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:timed",
        status="finished",
        input_tokens=500,
        output_tokens=20,
        estimated_cost_usd=0.01,
        cache_read=12,
        cache_write=3,
        model="claude-opus-4-7",
        created_at=1000,
        updated_at=2000,
        started_at=3000,
        ended_at=4000,
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["createdAt"] == 1000
    assert row["created_at"] == 1000
    assert row["updatedAt"] == 2000
    assert row["updated_at"] == 2000
    assert row["startedAt"] == 3000
    assert row["started_at"] == 3000
    assert row["endedAt"] == 4000
    assert row["ended_at"] == 4000


def test_usage_status_tracker_only_rows_have_current_timestamp_aliases(monkeypatch) -> None:
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:live",
        input_tokens=100,
        output_tokens=10,
        model_id="claude-opus-4-7",
    )

    monkeypatch.setattr(rpc_usage, "_now_ms", lambda: 123456)
    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["createdAt"] == 123456
    assert row["created_at"] == row["createdAt"]
    assert row["updatedAt"] == 123456
    assert row["updated_at"] == row["updatedAt"]
    assert row["startedAt"] is None
    assert row["started_at"] is None
    assert row["endedAt"] is None
    assert row["ended_at"] is None


def test_usage_status_exposes_persisted_cost_components_and_source() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:mixed",
        status="running",
        input_tokens=5000,
        output_tokens=200,
        total_cost_usd=0.07,
        billed_cost_usd=0.06,
        estimated_cost_component_usd=0.01,
        cost_source="mixed",
        missing_cost_entries=0,
        cache_read=300,
        cache_write=120,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["costUsd"] == 0.07
    assert row["billedCostUsd"] == 0.06
    assert row["estimatedCostUsd"] == 0.01
    assert row["costSource"] == "mixed"
    assert row["missingCostEntries"] == 0
    assert row["cost_ephemeral"] is False


def test_usage_status_exposes_estimate_basis_and_price_source_when_present() -> None:
    """Session-row provenance keys are additive: present (dual-case) when the
    persisted session record carries them, without disturbing any existing
    cost field or value."""
    session = SimpleNamespace(
        session_key="agent:webchat:provenance",
        status="running",
        input_tokens=1000,
        output_tokens=50,
        total_cost_usd=0.02,
        billed_cost_usd=0.0,
        estimated_cost_component_usd=0.02,
        cost_source="opensquilla_estimate",
        missing_cost_entries=0,
        estimate_basis="cache_blind",
        price_source="config_override",
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["estimateBasis"] == "cache_blind"
    assert row["estimate_basis"] == "cache_blind"
    assert row["priceSource"] == "config_override"
    assert row["price_source"] == "config_override"
    # The new additive keys must not disturb any existing cost field.
    assert row["costUsd"] == 0.02
    assert row["costSource"] == "opensquilla_estimate"


def test_usage_status_estimate_basis_and_price_source_default_none_when_absent() -> None:
    """Persisted rows that predate provenance tracking must not error — the
    new keys default to ``None`` rather than being omitted or raising."""
    session = SimpleNamespace(
        session_key="agent:webchat:no-provenance",
        status="running",
        input_tokens=500,
        output_tokens=20,
        estimated_cost_usd=0.01,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([session])

    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["estimateBasis"] is None
    assert row["estimate_basis"] is None
    assert row["priceSource"] is None
    assert row["price_source"] is None


def test_usage_status_tracker_breakdown_items_carry_provenance_keys_unchanged() -> None:
    """Per-model breakdown items already carry costSource/estimateBasis/priceSource
    dual-case keys from the tracker's cost-field helper — the RPC layer must
    spread them through untouched rather than stripping or renaming them."""
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:breakdown-provenance",
        input_tokens=1000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        cache_read_tokens=200,
        cache_write_tokens=80,
    )

    ctx = _ctx(session_manager=None, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    [item] = row["modelBreakdown"]
    assert "estimateBasis" in item
    assert "estimate_basis" in item
    assert "priceSource" in item
    assert "price_source" in item
    assert item["estimateBasis"] == item["estimate_basis"]
    assert item["priceSource"] == item["price_source"]


def test_usage_status_merges_tracker_and_session_manager_cache_totals() -> None:
    """Tracker-only sessions (no session_manager record) must still contribute cache totals."""
    db_session = SimpleNamespace(
        session_key="agent:webchat:db",
        status="running",
        input_tokens=1000,
        output_tokens=50,
        estimated_cost_usd=0.01,
        cache_read=400,
        cache_write=200,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([db_session])

    tracker = UsageTracker()
    tracker.add(
        "tracker-only-session",
        input_tokens=500,
        output_tokens=20,
        model_id="claude-opus-4-7",
        cache_read_tokens=50,
        cache_write_tokens=10,
    )

    ctx = _ctx(session_manager=sm, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    # cache_read = 400 (db) + 50 (tracker) = 450
    # cache_write = 200 (db) + 10 (tracker) = 210
    assert payload["totalCacheReadTokens"] == 450
    assert payload["totalCacheWriteTokens"] == 210
    assert payload["totalSessions"] == 2
    rows = {row["session"]: row for row in payload["sessions"]}
    assert rows["agent:webchat:db"]["costSource"] == "opensquilla_estimate"
    assert rows["tracker-only-session"]["costSource"] == "opensquilla_estimate"
    assert rows["tracker-only-session"]["costEphemeral"] is True


def test_usage_status_prefers_persisted_row_over_same_session_tracker_row() -> None:
    db_session = SimpleNamespace(
        session_key="agent:webchat:same",
        status="running",
        input_tokens=1000,
        output_tokens=50,
        total_cost_usd=0.004,
        billed_cost_usd=0.004,
        estimated_cost_component_usd=0.0,
        cost_source="provider_billed",
        missing_cost_entries=0,
        cache_read=0,
        cache_write=0,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([db_session])
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:same",
        input_tokens=9000,
        output_tokens=9000,
        model_id="claude-opus-4-7",
    )

    ctx = _ctx(session_manager=sm, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["session"] == "agent:webchat:same"
    assert row["costUsd"] == 0.004
    assert row["billedCostUsd"] == 0.004
    assert row["costSource"] == "provider_billed"
    assert row["costEphemeral"] is False


def test_usage_status_overlays_tracker_when_persisted_row_is_still_empty() -> None:
    """Cover the done-event/read-after-write race seen in live meta runs."""

    db_session = SimpleNamespace(
        session_key="agent:webchat:stale",
        status="running",
        input_tokens=0,
        output_tokens=0,
        total_cost_usd=0.0,
        billed_cost_usd=0.0,
        estimated_cost_component_usd=0.0,
        cost_source="none",
        missing_cost_entries=0,
        cache_read=0,
        cache_write=0,
        model="deepseek/deepseek-v4-pro",
    )
    sm = _FakeSessionManager([db_session])
    tracker = UsageTracker()
    tracker.add(
        "agent:webchat:stale",
        input_tokens=97_223,
        output_tokens=25_486,
        model_id="deepseek/deepseek-v4-pro-20260423",
        cache_read_tokens=32_768,
        billed_cost=0.132452324,
    )

    ctx = _ctx(session_manager=sm, usage_tracker=tracker)
    payload = asyncio.run(_handle_usage_status(None, ctx))

    [row] = payload["sessions"]
    assert row["session"] == "agent:webchat:stale"
    assert row["inputTokens"] == 97_223
    assert row["outputTokens"] == 25_486
    assert row["cacheReadTokens"] == 32_768
    assert row["costUsd"] == 0.132452
    assert row["billedCostUsd"] == 0.132452
    assert row["costSource"] == "provider_billed"
    assert row["costEphemeral"] is True
    assert payload["totalCostUsd"] == 0.132452


def test_usage_status_reads_real_session_manager_dict_rows_and_deduplicates_tracker() -> None:
    async def scenario():
        storage = SessionStorage(":memory:")
        await storage.connect()
        manager = SessionManager(storage)
        try:
            await manager.create("agent:webchat:real")
            await manager.update(
                "agent:webchat:real",
                input_tokens=1000,
                output_tokens=50,
                total_cost_usd=0.004,
                billed_cost_usd=0.004,
                estimated_cost_component_usd=0.0,
                cost_source="provider_billed",
                missing_cost_entries=0,
                cache_read=12,
                cache_write=3,
                model="claude-opus-4-7",
            )
            tracker = UsageTracker()
            tracker.add(
                "agent:webchat:real",
                input_tokens=9000,
                output_tokens=9000,
                model_id="claude-opus-4-7",
            )
            ctx = _ctx(session_manager=manager, usage_tracker=tracker)
            return await _handle_usage_status(None, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    [row] = payload["sessions"]
    assert payload["totalSessions"] == 1
    assert row["session"] == "agent:webchat:real"
    assert row["inputTokens"] == 1000
    assert row["outputTokens"] == 50
    assert row["cacheReadTokens"] == 12
    assert row["cacheWriteTokens"] == 3
    assert row["costUsd"] == 0.004
    assert row["billedCostUsd"] == 0.004
    assert row["costSource"] == "provider_billed"
    assert row["costEphemeral"] is False


def test_usage_cost_breakdown_carries_cache_fields() -> None:
    """The `usage.cost` RPC must surface cache numbers per-row in the breakdown."""
    session = SimpleNamespace(
        session_key="agent:webchat:xyz",
        input_tokens=5000,
        output_tokens=200,
        estimated_cost_usd=0.04,
        cache_read=300,
        cache_write=120,
        model="claude-opus-4-7",
    )
    sm = _FakeSessionManager([session])
    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())

    payload = asyncio.run(_handle_usage_cost(None, ctx))
    [row] = payload["breakdown"]
    assert row["cacheReadTokens"] == 300
    assert row["cacheWriteTokens"] == 120
    assert row["costUsd"] == 0.04
    assert row["estimatedCostUsd"] == 0.04
    assert row["costSource"] == "opensquilla_estimate"


def test_usage_cost_exposes_session_timestamp_aliases() -> None:
    session = SimpleNamespace(
        session_key="agent:webchat:timed-cost",
        input_tokens=500,
        output_tokens=20,
        estimated_cost_usd=0.01,
        cache_read=12,
        cache_write=3,
        model="claude-opus-4-7",
        created_at=1100,
        updated_at=2200,
        started_at=3300,
        ended_at=4400,
    )
    sm = _FakeSessionManager([session])
    ctx = _ctx(session_manager=sm, usage_tracker=UsageTracker())

    payload = asyncio.run(_handle_usage_cost(None, ctx))

    [row] = payload["breakdown"]
    assert row["createdAt"] == 1100
    assert row["created_at"] == 1100
    assert row["updatedAt"] == 2200
    assert row["updated_at"] == 2200
    assert row["startedAt"] == 3300
    assert row["started_at"] == 3300
    assert row["endedAt"] == 4400
    assert row["ended_at"] == 4400


def test_usage_status_no_data_returns_zeros() -> None:
    """Empty environment: all totals are 0, no error."""
    ctx = _ctx(session_manager=None, usage_tracker=UsageTracker())
    payload = asyncio.run(_handle_usage_status(None, ctx))

    assert payload["totalCacheReadTokens"] == 0
    assert payload["totalCacheWriteTokens"] == 0
    assert payload["totalSessions"] == 0
