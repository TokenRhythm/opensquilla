"""Router decision-record hook: staging, executed-fact flush, rehydration.

Covers the audit requirements around ``executed_kind``: a persisted record
must never name a model that did not execute — ensemble-wrapped turns are
recorded as ``executed_kind='ensemble'`` with the trace profile, and
selector-fallback turns carry the realigned model plus the hop count.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing import RoutingDecision
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.steps.router_decision_record import (
    DECISION_ID_METADATA_KEY,
    PENDING_RECORD_KEY,
    build_trail,
    drain_pending_flushes,
    flush_router_decision,
    rehydrate_history_from_writer,
    schedule_router_decision_flush,
    set_decision_writer,
    stage_router_decision,
)
from opensquilla.engine.steps.squilla_router import (
    apply_squilla_router,
    seed_routing_history,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.observability.decision_log import (
    DecisionEntry,
    load_entries,
    write_decision_entry,
)
from opensquilla.persistence.router_decision_writer import RouterDecisionWriter

PROMPT_SENTINEL = "our merger with Acme closes friday, draft the announcement"


class _FakeWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_decision(self, record: dict[str, Any]) -> bool:
        self.records.append(dict(record))
        return True


@pytest.fixture(autouse=True)
def _reset_hook_state():
    squilla_router._history_store.clear()
    squilla_router._strategy = None
    squilla_router._strategy_key = None
    set_decision_writer(None)
    yield
    squilla_router._history_store.clear()
    squilla_router._strategy = None
    squilla_router._strategy_key = None
    set_decision_writer(None)


def _ctx(message: str = PROMPT_SENTINEL, session_key: str = "agent:main:main") -> TurnContext:
    config = GatewayConfig()
    config.squilla_router.rollout_phase = "full"
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
    )


def _decision(tier: str = "c2", model: str = "deepseek/deepseek-chat") -> RoutingDecision:
    return RoutingDecision(tier=tier, model=model, confidence=0.87, source="v4_phase3")


ROUTING_EXTRA = {
    "route_class": "R2",
    "base_tier": "c1",
    "final_tier": "c2",
    "final_route_class": "R2",
    "confidence_gate_applied": False,
    "confidence_threshold": 0.5,
    "confidence_default_tier": "c1",
    "complaint_upgrade_applied": False,
    "complaint_terms": [],
    "anti_downgrade_applied": True,
    "previous_tier": "c2",
    "kv_cache_window_seconds": 600.0,
    "probabilities": [0.1, 0.2, 0.6, 0.1],
    "flags": ["code"],
    "model_version": "v4_phase3-2024",
    # Free-text fields that must never reach the persisted record:
    "error": PROMPT_SENTINEL,
    "prompt_hint": PROMPT_SENTINEL,
}


def test_stage_is_noop_without_writer() -> None:
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)
    assert DECISION_ID_METADATA_KEY not in ctx.metadata
    assert PENDING_RECORD_KEY not in ctx.metadata


def test_stage_then_flush_hands_record_to_writer_single() -> None:
    writer = _FakeWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    ctx.metadata["thinking_level"] = "medium"
    ctx.metadata["baseline_model"] = "anthropic/claude-sonnet"
    ctx.metadata["savings_pct"] = 42.5
    ctx.metadata["routed_provider"] = "openrouter"

    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)
    decision_id = ctx.metadata[DECISION_ID_METADATA_KEY]
    assert isinstance(decision_id, str) and len(decision_id) == 32
    assert writer.records == []  # nothing handed over until flush

    flush_router_decision(ctx.metadata)
    assert len(writer.records) == 1
    record = writer.records[0]
    assert record["decision_id"] == decision_id
    assert record["session_key"] == "agent:main:main"
    assert record["classifier"] == "v4_phase3-2024"
    assert record["proposed_tier"] == "c1"
    assert record["final_tier"] == "c2"
    assert record["provider"] == "openrouter"
    assert record["requested_provider"] == "openrouter"
    assert record["requested_model"] == "deepseek/deepseek-chat"
    assert record["executed_provider"] == "openrouter"
    assert record["executed_model"] == "deepseek/deepseek-chat"
    assert record["fallback_reason"] is None
    assert record["thinking_level"] == "medium"
    assert record["baseline_model"] == "anthropic/claude-sonnet"
    assert record["savings_pct"] == 42.5  # C2: today's value, verbatim
    assert record["executed_kind"] == "single"
    assert record["ensemble_profile"] is None
    assert record["fallback_hops"] == 0
    # Pop-once: a second flush hands nothing.
    flush_router_decision(ctx.metadata)
    assert len(writer.records) == 1


def test_flush_records_ensemble_execution_facts() -> None:
    writer = _FakeWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    ctx.metadata["ensemble_enabled"] = True
    ctx.metadata["routed_model_before_ensemble"] = "deepseek/deepseek-chat"
    flush_router_decision(
        ctx.metadata,
        ensemble_trace={
            "mode": "b5_fusion",
            "profile": "static_openrouter_b5",
            "fallback_used": True,
            "fallback_code": "ensemble_insufficient_proposers",
            "fallback_reason": "ensemble quorum not reached",
            "final_request": {
                "execution": {"provider": "openai", "model": "gpt-5-mini"}
            },
        },
    )
    record = writer.records[0]
    assert record["executed_kind"] == "ensemble"
    assert record["ensemble_profile"] == "static_openrouter_b5"
    assert record["executed_provider"] == "openai"
    assert record["executed_model"] == "gpt-5-mini"
    assert record["fallback_reason"] == "ensemble_insufficient_proposers"


def test_flush_realigns_model_and_counts_fallback_hops() -> None:
    writer = _FakeWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    # Selector fallback executed a different model and counted two hops.
    ctx.metadata["routed_model"] = "qwen/qwen-plus"
    ctx.metadata["executed_provider"] = "dashscope"
    ctx.metadata["executed_model"] = "qwen/qwen-plus"
    ctx.metadata["router_fallback_hops"] = 2
    flush_router_decision(ctx.metadata)
    record = writer.records[0]
    assert record["model"] == "qwen/qwen-plus"
    assert record["fallback_hops"] == 2
    assert record["requested_model"] == "deepseek/deepseek-chat"
    assert record["executed_provider"] == "dashscope"
    assert record["executed_model"] == "qwen/qwen-plus"
    assert record["fallback_reason"] == "selector_fallback"
    assert record["executed_kind"] == "single"


def test_staged_record_never_contains_prompt_text() -> None:
    writer = _FakeWriter()
    set_decision_writer(writer)
    ctx = _ctx(message=PROMPT_SENTINEL)
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)
    flush_router_decision(ctx.metadata)
    assert PROMPT_SENTINEL not in repr(writer.records[0])


def test_build_trail_is_enum_and_number_only() -> None:
    trail = build_trail(ROUTING_EXTRA, final_tier="c2")
    stages = [entry["stage"] for entry in trail]
    assert stages == [
        "classify",
        "confidence_gate",
        "complaint_upgrade",
        "anti_downgrade",
        "final",
    ]
    for entry in trail:
        for value in entry.values():
            assert isinstance(value, (bool, int, float)) or (
                isinstance(value, str) and " " not in value
            )
    assert PROMPT_SENTINEL not in repr(trail)


async def test_step_stages_record_when_writer_registered(monkeypatch) -> None:
    class FakeStrategy:
        async def classify(
            self,
            message: str,
            valid_tiers: list[str],
            routing_history: list[dict] | None = None,
        ) -> tuple[str, float, str, dict]:
            return "c1", 0.91, "v4_phase3", {
                "route_class": "R1",
                "thinking_mode": "T1",
                "prompt_policy": "P1",
                "probabilities": [0.05, 0.91, 0.03, 0.01],
            }

    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _config: FakeStrategy())
    writer = _FakeWriter()
    set_decision_writer(writer)

    ctx = await apply_squilla_router(_ctx())

    assert isinstance(ctx.metadata.get(DECISION_ID_METADATA_KEY), str)
    pending = ctx.metadata[PENDING_RECORD_KEY]
    assert pending["proposed_tier"] == "c1"
    assert pending["final_tier"] == ctx.metadata["routed_tier"]
    assert pending["savings_pct"] == ctx.metadata["savings_pct"]
    assert PROMPT_SENTINEL not in repr(pending)
    assert writer.records == []  # step stages; turn finalize flushes

    flush_router_decision(ctx.metadata)
    assert writer.records[0]["decision_id"] == ctx.metadata[DECISION_ID_METADATA_KEY]


async def test_step_public_surface_unchanged_without_writer(monkeypatch) -> None:
    class FakeStrategy:
        async def classify(
            self,
            message: str,
            valid_tiers: list[str],
            routing_history: list[dict] | None = None,
        ) -> tuple[str, float, str, dict]:
            return "c1", 0.91, "v4_phase3", {"route_class": "R1"}

    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _config: FakeStrategy())
    ctx = await apply_squilla_router(_ctx())
    assert ctx.metadata.get("routed_tier") == "c1"
    assert DECISION_ID_METADATA_KEY not in ctx.metadata
    assert PENDING_RECORD_KEY not in ctx.metadata


# ---------------------------------------------------------------------------
# Scheduled (off-loop) flush — the turn-finalize path in engine/runtime.py
# ---------------------------------------------------------------------------


async def test_schedule_flush_writes_off_the_event_loop_thread() -> None:
    """The SQLite insert must not run on the loop thread — the writer commits
    under busy_timeout=5000, so an inline contended write would freeze every
    session for up to 5s."""

    class _ThreadRecordingWriter(_FakeWriter):
        def __init__(self) -> None:
            super().__init__()
            self.write_threads: list[int] = []

        def record_decision(self, record: dict[str, Any]) -> bool:
            self.write_threads.append(threading.get_ident())
            return super().record_decision(record)

    writer = _ThreadRecordingWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    task = schedule_router_decision_flush(ctx.metadata)
    assert task is not None
    await task
    assert len(writer.records) == 1
    assert writer.write_threads[0] != threading.get_ident()
    # Executed facts are stamped exactly like the inline flush.
    record = writer.records[0]
    assert record["executed_kind"] == "single"
    assert record["fallback_hops"] == 0
    # Pop-once: rescheduling after the record was consumed is a no-op.
    assert schedule_router_decision_flush(ctx.metadata) is None
    assert len(writer.records) == 1


def test_schedule_flush_without_running_loop_writes_inline() -> None:
    """Sync callers with no loop to protect keep the old inline behavior."""
    writer = _FakeWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    assert schedule_router_decision_flush(ctx.metadata) is None
    assert len(writer.records) == 1
    assert writer.records[0]["executed_kind"] == "single"


async def test_schedule_flush_write_failure_is_logged_not_raised() -> None:
    """Persistence is best-effort observability: a locked database must never
    surface as a turn failure or an unretrieved-task exception."""

    class _LockedWriter:
        def record_decision(self, record: dict[str, Any]) -> bool:
            raise sqlite3.OperationalError("database is locked")

    set_decision_writer(_LockedWriter())  # type: ignore[arg-type]
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    task = schedule_router_decision_flush(ctx.metadata)
    assert task is not None
    await task  # must not raise


async def test_schedule_flush_noop_without_staged_record() -> None:
    writer = _FakeWriter()
    set_decision_writer(writer)
    assert schedule_router_decision_flush({}) is None
    assert writer.records == []


# ---------------------------------------------------------------------------
# Rehydration
# ---------------------------------------------------------------------------


def _synthetic_writer(tmp_path: Path) -> RouterDecisionWriter:
    """Writer over a synthetic (hand-created) router_decisions table."""
    db = str(tmp_path / "synthetic.sqlite")
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE router_decisions ("
        " decision_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,"
        " turn_index INTEGER, ts_ms INTEGER NOT NULL, classifier TEXT,"
        " proposed_tier TEXT, confidence REAL, probs TEXT, flags TEXT,"
        " final_tier TEXT, requested_provider TEXT, requested_model TEXT,"
        " provider TEXT, model TEXT, executed_provider TEXT, executed_model TEXT,"
        " fallback_reason TEXT, thinking_level TEXT,"
        " source TEXT, trail TEXT, baseline_model TEXT, savings_pct REAL,"
        " executed_kind TEXT, ensemble_profile TEXT,"
        " fallback_hops INTEGER NOT NULL DEFAULT 0)"
    )
    return RouterDecisionWriter(conn)


def test_rehydrate_seeds_history_store_from_synthetic_table(tmp_path: Path) -> None:
    writer = _synthetic_writer(tmp_path)
    now_ms = int(time.time() * 1000)
    for index in range(7):
        writer.record_decision(
            {
                "decision_id": f"a{index}",
                "session_key": "agent:sticky",
                "turn_index": index,
                "ts_ms": now_ms - (7 - index) * 1000,
                "proposed_tier": "c1",
                "final_tier": "c3" if index == 6 else "c1",
            }
        )
    # Outside the 1800s window — must not be rehydrated.
    writer.record_decision(
        {
            "decision_id": "stale",
            "session_key": "agent:stale",
            "ts_ms": now_ms - 3600 * 1000,
            "final_tier": "c3",
        }
    )

    seeded = rehydrate_history_from_writer(writer)
    assert seeded == 1
    history = squilla_router._history_store.get("agent:sticky")
    assert history is not None and len(history) == 5  # last <=5 records
    last = history[-1]
    assert last["final_tier"] == "c3"
    assert last["final_route_class"] == "R3"
    assert last["rehydrated"] is True
    # _ts is on the current monotonic clock and recent enough for the
    # anti-downgrade window check.
    assert last["_ts"] <= time.monotonic()
    assert time.monotonic() - last["_ts"] < 60
    assert squilla_router._history_store.get("agent:stale") is None
    writer.close()


def test_seed_routing_history_never_clobbers_live_history() -> None:
    squilla_router._history_store.set("agent:live", [{"turn_index": 0, "final_tier": "c2"}])
    seeded = seed_routing_history(
        {
            "agent:live": [{"turn_index": 9, "final_tier": "c0"}],
            "agent:cold": [{"turn_index": 0, "final_tier": "c1"}],
            "": [{"turn_index": 0}],
        }
    )
    assert seeded == 1
    assert squilla_router._history_store.get("agent:live") == [
        {"turn_index": 0, "final_tier": "c2"}
    ]
    assert squilla_router._history_store.get("agent:cold") == [
        {"turn_index": 0, "final_tier": "c1"}
    ]


# ---------------------------------------------------------------------------
# JSONL decision log: additive decision_id join key
# ---------------------------------------------------------------------------


def test_decision_entry_round_trips_decision_id(tmp_path: Path) -> None:
    entry = DecisionEntry(
        turn_id="turn-1",
        session_key="agent:main:main",
        prompt_hash="p" * 16,
        system_prompt_hash="s" * 16,
        tool_list_hash="t" * 16,
        tool_choice="auto",
        tokens_input=10,
        tokens_output=5,
        model="deepseek/deepseek-chat",
        provider="OpenAICompatProvider",
        latency_ms=100,
        ts="2026-01-01T00:00:00Z",
        decision_id="b" * 32,
    )
    path = write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(path)
    assert loaded[0].decision_id == "b" * 32


async def test_drain_pending_flushes_lands_inflight_records_before_close() -> None:
    """Shutdown drains scheduled flush tasks before the writer connection
    closes — a turn finishing near shutdown must not lose its record to a
    cancelled task or write on a closed connection."""
    gate = threading.Event()

    class _GatedWriter(_FakeWriter):
        def record_decision(self, record: dict[str, Any]) -> bool:
            gate.wait(timeout=5)
            return super().record_decision(record)

    writer = _GatedWriter()
    set_decision_writer(writer)
    ctx = _ctx()
    stage_router_decision(ctx, decision=_decision(), routing_extra=ROUTING_EXTRA)

    task = schedule_router_decision_flush(ctx.metadata)
    assert task is not None
    assert not task.done()

    gate.set()
    await drain_pending_flushes()

    assert task.done()
    assert len(writer.records) == 1


async def test_drain_pending_flushes_noop_when_nothing_inflight() -> None:
    await drain_pending_flushes()  # must not raise or hang
