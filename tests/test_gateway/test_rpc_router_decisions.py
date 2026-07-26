"""router.decisions.list / router.feedback.submit RPC handlers.

The list surface is a read/observe view over the V017 ``router_decisions``
table; the feedback surface is dormant plumbing (deferred F7 follow-up).
All fixture data is synthetic.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.engine.steps.router_decision_record import (
    get_decision_writer,
    set_decision_writer,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import ERROR_INVALID_REQUEST, ERROR_UNAUTHORIZED
from opensquilla.gateway.rpc import get_dispatcher, validate_classification
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_router import (
    _bounded_limit,
    _handle_router_decisions_list,
    _handle_router_feedback_submit,
)
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.router_decision_writer import (
    RouterDecisionWriter,
    open_router_decision_writer,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1].parent / "migrations"

EXPECTED_WIRE_KEYS = {
    "decisionId",
    "sessionKey",
    "turnIndex",
    "tsMs",
    "classifier",
    "proposedTier",
    "confidence",
    "probs",
    "flags",
    "finalTier",
    "requestedProvider",
    "requestedModel",
    "provider",
    "model",
    "executedProvider",
    "executedModel",
    "fallbackReason",
    "thinkingLevel",
    "source",
    "trail",
    "baselineModel",
    "savingsPct",
    "executedKind",
    "ensembleProfile",
    "fallbackHops",
}


def _base_record(**overrides) -> dict:
    record = {
        "decision_id": "d" * 32,
        "session_key": "agent:main:webchat:s1",
        "turn_index": 0,
        "ts_ms": 1_000_000,
        "classifier": "v4_phase3",
        "proposed_tier": "c1",
        "confidence": 0.91,
        "probs": [0.05, 0.91, 0.03, 0.01],
        "flags": ["code", "multi_step"],
        "final_tier": "c2",
        "requested_provider": "openrouter",
        "requested_model": "deepseek/deepseek-chat",
        "provider": "openrouter",
        "model": "deepseek/deepseek-chat",
        "executed_provider": "deepseek",
        "executed_model": "deepseek-chat",
        "fallback_reason": "",
        "thinking_level": "medium",
        "source": "v4_phase3",
        "trail": [
            {"stage": "classify", "tier": "c1", "route_class": "R1"},
            {"stage": "final", "tier": "c2", "route_class": "R2"},
        ],
        "baseline_model": "anthropic/claude-sonnet",
        "savings_pct": 42.5,
        "executed_kind": "single",
        "ensemble_profile": None,
        "fallback_hops": 0,
    }
    record.update(overrides)
    return record


@pytest.fixture
def writer(tmp_path: Path):
    """Real migrated DB + registered process-wide writer, torn down after."""
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    w = open_router_decision_writer(db)
    previous = get_decision_writer()
    set_decision_writer(w)
    try:
        yield w
    finally:
        set_decision_writer(previous)
        w.close()


def _read_only_principal() -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )


# ---------------------------------------------------------------------------
# router.decisions.list
# ---------------------------------------------------------------------------


async def test_decisions_list_empty_without_writer() -> None:
    previous = get_decision_writer()
    set_decision_writer(None)
    try:
        payload = await _handle_router_decisions_list({}, RpcContext(conn_id="test"))
    finally:
        set_decision_writer(previous)
    assert payload == {"decisions": []}


async def test_live_router_enable_after_disabled_boot_persists_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer must be ready before a restart-free router enable.

    ``onboarding.router.configure`` hot-applies the enabled flag.  A gateway
    that booted with routing disabled therefore cannot defer construction of
    the process-wide decision writer until a restart: the shared turn hook
    would otherwise stage nothing and ``router.decisions.list`` would stay
    empty even though routed calls were executing.
    """
    from types import SimpleNamespace

    from opensquilla.engine.routing import RoutingDecision
    from opensquilla.engine.steps.router_decision_record import (
        schedule_router_decision_flush,
        stage_router_decision,
    )
    from opensquilla.gateway.boot import build_services
    from opensquilla.gateway.rpc_onboarding import _router_configure

    monkeypatch.setattr(
        "opensquilla.sandbox.integration.configure_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            effective=SimpleNamespace(as_dict=lambda: {})
        ),
    )
    config_path = tmp_path / "config.toml"
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
        squilla_router={"enabled": False},
    )
    config.config_path = str(config_path)
    previous = get_decision_writer()
    services = await build_services(
        config=config,
        session_db_path=str(tmp_path / "sessions.sqlite"),
        seed_agent_workspaces=False,
    )
    try:
        assert services.router_decision_writer is not None
        assert get_decision_writer() is services.router_decision_writer

        rpc_ctx = RpcContext(
            conn_id="live-enable",
            config=config,
            provider_selector=services.provider_selector,
        )
        result = await _router_configure({"mode": "recommended"}, rpc_ctx)
        assert result["restartRequired"] is False
        assert config.squilla_router.enabled is True

        tier = config.squilla_router.default_tier
        tier_cfg = config.squilla_router.tiers[tier]
        model = str(tier_cfg["model"])
        provider = str(tier_cfg.get("provider") or config.llm.provider)
        turn_ctx = SimpleNamespace(
            session_key="agent:main:webchat:live-enable",
            metadata={
                "routed_provider": provider,
                "routed_model": model,
                "executed_provider": provider,
                "executed_model": model,
            },
        )
        stage_router_decision(
            turn_ctx,
            decision=RoutingDecision(
                tier=tier,
                model=model,
                confidence=1.0,
                source="synthetic_live_enable",
            ),
            routing_extra={
                "base_tier": tier,
                "final_tier": tier,
                "model_version": "synthetic_live_enable",
            },
        )
        flush_task = schedule_router_decision_flush(turn_ctx.metadata)
        assert flush_task is not None
        await flush_task

        payload = await _handle_router_decisions_list(
            {"sessionKey": turn_ctx.session_key},
            rpc_ctx,
        )
        assert len(payload["decisions"]) == 1
        decision = payload["decisions"][0]
        assert decision["sessionKey"] == turn_ctx.session_key
        assert decision["requestedProvider"] == provider
        assert decision["requestedModel"] == model
        assert decision["executedProvider"] == provider
        assert decision["executedModel"] == model
    finally:
        await services.close()
        set_decision_writer(previous)


async def test_decisions_list_writer_wait_does_not_block_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingWriter:
        def list_decisions(self, **kwargs):
            started.set()
            release.wait(timeout=1.0)
            return []

    previous = get_decision_writer()
    set_decision_writer(_BlockingWriter())
    task = asyncio.create_task(
        _handle_router_decisions_list({}, RpcContext(conn_id="test"))
    )
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        loop = asyncio.get_running_loop()
        before = loop.time()
        await asyncio.sleep(0.05)
        assert loop.time() - before < 0.2
    finally:
        release.set()
        await task
        set_decision_writer(previous)


async def test_decisions_list_returns_camelcase_envelope(
    writer: RouterDecisionWriter,
) -> None:
    assert writer.record_decision(_base_record()) is True
    payload = await _handle_router_decisions_list({}, RpcContext(conn_id="test"))

    assert set(payload) == {"decisions"}
    (decision,) = payload["decisions"]
    assert set(decision) == EXPECTED_WIRE_KEYS
    assert decision["decisionId"] == "d" * 32
    assert decision["sessionKey"] == "agent:main:webchat:s1"
    assert decision["turnIndex"] == 0
    assert decision["tsMs"] == 1_000_000
    assert decision["classifier"] == "v4_phase3"
    assert decision["proposedTier"] == "c1"
    assert decision["confidence"] == 0.91
    assert decision["finalTier"] == "c2"
    assert decision["provider"] == "openrouter"
    assert decision["model"] == "deepseek/deepseek-chat"
    assert decision["thinkingLevel"] == "medium"
    assert decision["source"] == "v4_phase3"
    assert decision["baselineModel"] == "anthropic/claude-sonnet"
    assert decision["executedKind"] == "single"
    assert decision["ensembleProfile"] is None
    assert decision["fallbackHops"] == 0
    # JSON columns come back as structured JSON, not serialized strings.
    assert decision["probs"] == [0.05, 0.91, 0.03, 0.01]
    assert decision["flags"] == ["code", "multi_step"]
    assert decision["trail"][0] == {"stage": "classify", "tier": "c1", "route_class": "R1"}


async def test_decisions_list_savings_pct_verbatim_passthrough(
    writer: RouterDecisionWriter,
) -> None:
    """C2: savingsPct surfaces the stored column value untouched."""
    writer.record_decision(_base_record(decision_id="s1", savings_pct=42.5))
    writer.record_decision(
        _base_record(decision_id="s2", ts_ms=1_000_001, savings_pct=None)
    )
    payload = await _handle_router_decisions_list({}, RpcContext(conn_id="test"))
    by_id = {d["decisionId"]: d for d in payload["decisions"]}
    assert by_id["s1"]["savingsPct"] == 42.5
    assert by_id["s2"]["savingsPct"] is None


async def test_decisions_list_orders_newest_first_and_pages_with_before_ts(
    writer: RouterDecisionWriter,
) -> None:
    for index in range(3):
        writer.record_decision(
            _base_record(decision_id=f"p{index}", ts_ms=1_000 * (index + 1))
        )
    ctx = RpcContext(conn_id="test")

    payload = await _handle_router_decisions_list({}, ctx)
    assert [d["tsMs"] for d in payload["decisions"]] == [3_000, 2_000, 1_000]

    oldest_seen = payload["decisions"][0]["tsMs"]  # page after the newest row
    page = await _handle_router_decisions_list({"beforeTs": oldest_seen}, ctx)
    assert [d["tsMs"] for d in page["decisions"]] == [2_000, 1_000]


async def test_decisions_list_filters_by_session_key(
    writer: RouterDecisionWriter,
) -> None:
    writer.record_decision(_base_record(decision_id="a1", session_key="agent:a"))
    writer.record_decision(_base_record(decision_id="b1", session_key="agent:b"))
    payload = await _handle_router_decisions_list(
        {"sessionKey": "agent:a"}, RpcContext(conn_id="test")
    )
    assert [d["decisionId"] for d in payload["decisions"]] == ["a1"]
    assert all(d["sessionKey"] == "agent:a" for d in payload["decisions"])


async def test_decisions_list_respects_limit(writer: RouterDecisionWriter) -> None:
    for index in range(3):
        writer.record_decision(
            _base_record(decision_id=f"l{index}", ts_ms=1_000 * (index + 1))
        )
    payload = await _handle_router_decisions_list(
        {"limit": 1}, RpcContext(conn_id="test")
    )
    assert [d["decisionId"] for d in payload["decisions"]] == ["l2"]


def test_decisions_list_limit_is_clamped() -> None:
    assert _bounded_limit(None) == 50
    assert _bounded_limit(-1) == 50
    assert _bounded_limit("oops") == 50
    assert _bounded_limit("5000") == 200
    assert _bounded_limit(5000) == 200
    assert _bounded_limit("12") == 12


async def test_decisions_list_allows_read_only_dispatch(
    writer: RouterDecisionWriter,
) -> None:
    writer.record_decision(_base_record())
    ctx = RpcContext(conn_id="test", principal=_read_only_principal())
    res = await get_dispatcher().dispatch("r1", "router.decisions.list", {}, ctx)
    assert res.error is None, res.error
    assert len(res.payload["decisions"]) == 1


# ---------------------------------------------------------------------------
# router.feedback.submit (live F7 intake)
# ---------------------------------------------------------------------------


async def test_feedback_submit_records_to_sidecar(
    writer: RouterDecisionWriter, tmp_path: Path, monkeypatch
) -> None:
    """A rating resolves through V017 and lands in the per-agent sidecar."""
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    writer.record_decision(_base_record())
    before = writer.list_decisions()

    payload = await _handle_router_feedback_submit(
        {"decisionId": "d" * 32, "rating": "down"},
        RpcContext(conn_id="test"),
    )

    assert payload == {"accepted": True, "recorded": "down"}
    # The decision table itself is never mutated by feedback.
    assert writer.list_decisions() == before

    from opensquilla.squilla_router.self_learning.feedback import load_feedback_map

    fb = load_feedback_map("main", home=tmp_path)
    assert fb["d" * 32].rating == "down"
    assert fb["d" * 32].executed_kind == "single"


async def test_feedback_submit_uses_configured_retention_days(
    writer: RouterDecisionWriter, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    writer.record_decision(_base_record())

    from opensquilla.squilla_router.self_learning.feedback import (
        load_feedback_map,
        write_feedback,
    )

    write_feedback(
        "main",
        decision_id="old-rating",
        session_key="agent:main:webchat:s1",
        turn_index=1,
        rating="up",
        now=datetime.now(UTC) - timedelta(days=40),
        retention_days=60,
    )
    cfg = GatewayConfig(
        squilla_router={"self_learning": {"retention_days": 60}}
    )

    payload = await _handle_router_feedback_submit(
        {"decisionId": "d" * 32, "rating": "down"},
        RpcContext(conn_id="test", config=cfg),
    )

    assert payload == {"accepted": True, "recorded": "down"}
    assert "old-rating" in load_feedback_map("main", home=tmp_path)


async def test_feedback_submit_unknown_decision_is_soft_failure(
    writer: RouterDecisionWriter, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    payload = await _handle_router_feedback_submit(
        {"decisionId": "f" * 32, "rating": "up"},
        RpcContext(conn_id="test"),
    )
    assert payload == {"accepted": False, "reason": "decision_not_found"}


async def test_feedback_submit_last_write_wins_and_neutral_revokes(
    writer: RouterDecisionWriter, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    writer.record_decision(_base_record())
    from opensquilla.squilla_router.self_learning.feedback import load_feedback_map

    ctx = RpcContext(conn_id="test")
    await _handle_router_feedback_submit({"decisionId": "d" * 32, "rating": "down"}, ctx)
    await _handle_router_feedback_submit({"decisionId": "d" * 32, "rating": "up"}, ctx)
    fb = load_feedback_map("main", home=tmp_path)
    assert fb["d" * 32].rating == "up"  # revision wins

    await _handle_router_feedback_submit(
        {"decisionId": "d" * 32, "rating": "neutral"}, ctx
    )
    assert load_feedback_map("main", home=tmp_path) == {}  # revoked


async def test_feedback_submit_preserves_ensemble_kind(
    writer: RouterDecisionWriter, tmp_path: Path, monkeypatch
) -> None:
    """executed_kind rides from V017 into the sidecar for downstream gating."""
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    writer.record_decision(_base_record(decision_id="e" * 32, executed_kind="ensemble"))

    await _handle_router_feedback_submit(
        {"decisionId": "e" * 32, "rating": "down"},
        RpcContext(conn_id="test"),
    )

    from opensquilla.squilla_router.self_learning.feedback import load_feedback_map

    fb = load_feedback_map("main", home=tmp_path)
    assert fb["e" * 32].executed_kind == "ensemble"


async def test_feedback_submit_rejects_free_text_decision_id() -> None:
    ctx = RpcContext(conn_id="test")
    res = await get_dispatcher().dispatch(
        "r1",
        "router.feedback.submit",
        {"decisionId": "this routing was wrong today", "rating": "down"},
        ctx,
    )
    assert res.error is not None
    assert res.error.code == ERROR_INVALID_REQUEST


async def test_feedback_submit_rejects_free_text_rating() -> None:
    ctx = RpcContext(conn_id="test")
    res = await get_dispatcher().dispatch(
        "r1",
        "router.feedback.submit",
        {"decisionId": "d" * 32, "rating": "amazing model, keep it!"},
        ctx,
    )
    assert res.error is not None
    assert res.error.code == ERROR_INVALID_REQUEST


async def test_feedback_submit_denies_read_only_dispatch() -> None:
    ctx = RpcContext(conn_id="test", principal=_read_only_principal())
    res = await get_dispatcher().dispatch(
        "r1",
        "router.feedback.submit",
        {"decisionId": "d" * 32, "rating": "up"},
        ctx,
    )
    assert res.error is not None
    assert res.error.code == ERROR_UNAUTHORIZED


def test_feedback_handler_is_dormant_static() -> None:
    """The handler module must not touch routing, calibration, or selection.

    The read-only ``router.selflearning.status`` handler may import the
    self-learning *state readers* (gates evaluation, pointer/state/store
    reads) — those observe the loop without feeding routing. What stays
    forbidden is anything that could route, calibrate, or mutate loop state:
    the routing engines themselves, and the self-learning mutation surfaces
    (training, promotion pointer writes, sample writes).
    """
    source = Path("src/opensquilla/gateway/rpc_router.py").read_text()
    assert "RoutingHistoryStore" not in source
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    allowed_readonly = (
        "squilla_router.self_learning.gates",
        "squilla_router.self_learning.promotion",
        "squilla_router.self_learning.state",
        "squilla_router.self_learning.store",
        # Feedback intake is this module's own job: append-only sidecar writes,
        # still nothing that routes, calibrates, or trains.
        "squilla_router.self_learning.feedback",
    )
    forbidden = ("smart_routing", "router_control", "squilla_router", "calibration", "routing")
    for line in import_lines:
        if any(mod in line for mod in allowed_readonly):
            continue
        assert not any(token in line for token in forbidden), line
    # The status handler must stay read-only: no training/mutation imports.
    # ("train" as a bare token would false-positive on "training"/"trainedAt",
    # so the mutation modules are matched as import paths.)
    for mutating in (
        "self_learning.orchestrator",
        "self_learning.train",
        "write_sample",
        "write_active_atomic",
        "promote_candidate",
        "rollback_active",
        "quarantine_candidate",
    ):
        assert mutating not in source, mutating


# ---------------------------------------------------------------------------
# Scope classification / boot audit
# ---------------------------------------------------------------------------


def test_router_rpc_scope_contract() -> None:
    assert METHOD_SCOPES["router.decisions.list"] == READ_SCOPE
    assert METHOD_SCOPES["router.feedback.submit"] == WRITE_SCOPE


def test_router_rpc_methods_pass_boot_scope_audit() -> None:
    registry = get_dispatcher()
    assert "router.decisions.list" in registry.methods()
    assert "router.feedback.submit" in registry.methods()
    # Same audit boot runs at the end of opensquilla.gateway.rpc.__init__;
    # raises ScopeDriftError on declared-vs-table drift.
    validate_classification(registry)
