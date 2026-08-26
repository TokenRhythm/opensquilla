from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    FixedFourTierV2Router,
    RoutingRequest,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    FixedFourTierDecisionRecord,
    FixedFourTierRequestClaim,
    FixedFourTierState,
)
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope
from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventStart


def _claim(
    session: Any,
    *,
    claim_id: str,
    execution_id: str,
    claimed_at_ms: int,
    lease_expires_at_ms: int,
    input_message_id: str = "input-1",
) -> FixedFourTierRequestClaim:
    return FixedFourTierRequestClaim(
        claim_id=claim_id,
        session_id=session.session_id,
        session_key=session.session_key,
        session_epoch=session.epoch,
        request_id="request-1",
        execution_id=execution_id,
        input_message_id=input_message_id,
        claimed_at_ms=claimed_at_ms,
        updated_at_ms=claimed_at_ms,
        lease_expires_at_ms=lease_expires_at_ms,
    )


def _decision(session: Any, claim: FixedFourTierRequestClaim) -> FixedFourTierDecisionRecord:
    core_decision, next_state = FixedFourTierV2Router(
        mock_seed=7,
        route_id_factory=lambda: "route-1",
        task_id_factory=lambda: "task-1",
        clock_ms=lambda: claim.claimed_at_ms,
    ).decide(
        RoutingRequest(
            session_id=session.session_id,
            request_id=claim.request_id,
            message="durable request",
            input_message_id=claim.input_message_id,
        )
    )
    provider = "openrouter"
    model = "deepseek/deepseek-v4-flash"
    route_trace = core_decision.trace(provider=provider, model=model)
    route_trace.update(
        {
            "claim_id": claim.claim_id,
            "execution_id": claim.execution_id,
            "execution_status": "pending",
            "preflight": {"status": "pending"},
            "dispatch": {
                "physical_request_started": False,
                "physical_request_count": 0,
            },
        }
    )
    return FixedFourTierDecisionRecord(
        route_id=core_decision.route_id,
        claim_id=claim.claim_id,
        session_id=session.session_id,
        session_key=session.session_key,
        session_epoch=session.epoch,
        request_id=claim.request_id,
        execution_id=claim.execution_id,
        input_message_id=claim.input_message_id,
        task_id=core_decision.task_id,
        decided_at_ms=core_decision.decided_at_ms,
        updated_at_ms=claim.claimed_at_ms,
        intent=core_decision.intent.trace(),
        tier=core_decision.tier.trace(),
        previous_tier=core_decision.previous_tier,
        final_tier=core_decision.final_tier,
        task_turn_index=core_decision.task_turn_index,
        task_start_input_message_id=next_state.task_start_input_message_id,
        context_action=core_decision.context_action,
        selected_provider=provider,
        selected_model=model,
        deployment_version="0731",
        config_version=core_decision.schema_version,
        route_trace=route_trace,
    )


async def test_request_claim_is_atomic_for_concurrent_executions() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-race")
        now_ms = time.time_ns() // 1_000_000
        first = _claim(
            session,
            claim_id="claim-a",
            execution_id="execution-a",
            claimed_at_ms=now_ms,
            lease_expires_at_ms=now_ms + 60_000,
        )
        second = _claim(
            session,
            claim_id="claim-b",
            execution_id="execution-b",
            claimed_at_ms=now_ms,
            lease_expires_at_ms=now_ms + 60_000,
        )

        results = await asyncio.gather(
            storage.claim_fixed_four_tier_request(first),
            storage.claim_fixed_four_tier_request(second),
        )

        assert sorted(acquired for acquired, _claim_record in results) == [False, True]
        winner = next(record for acquired, record in results if acquired)
        loser_view = next(record for acquired, record in results if not acquired)
        assert loser_view.claim_id == winner.claim_id
        assert loser_view.execution_id == winner.execution_id
    finally:
        await storage.close()


async def test_expired_materialized_claim_reconciles_decision_to_terminal_failure() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-expiry")
        claim = _claim(
            session,
            claim_id="claim-expired",
            execution_id="execution-expired",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
        )
        acquired, _ = await storage.claim_fixed_four_tier_request(claim)
        assert acquired is True
        await storage.stage_fixed_four_tier_decision(_decision(session, claim))

        reconciled = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=2_001,
        )
        decision = await storage.get_fixed_four_tier_decision_by_route("route-1")

        assert reconciled is not None
        assert reconciled.status == "failed"
        assert reconciled.error_code == "execution_lease_expired"
        assert reconciled.terminal_at_ms == 2_001
        assert decision is not None
        assert decision.execution_status == "failed"
        assert decision.preflight_status == "failed"
        assert decision.error_code == "execution_lease_expired"
        assert decision.terminal_at_ms == 2_001
        assert decision.route_trace["lease_reconciliation"]["status"] == "expired"
    finally:
        await storage.close()


async def test_terminal_settlement_persists_actual_identity_usage_and_first_terminal_time() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-execution-audit")
        claim = _claim(
            session,
            claim_id="claim-audit",
            execution_id="execution-audit",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        staged_decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(staged_decision)
        route_trace = {
            **staged_decision.route_trace,
            "claim_id": claim.claim_id,
            "execution_id": claim.execution_id,
            "execution_status": "succeeded",
            "dispatch": {
                "physical_request_started": True,
                "physical_request_count": 1,
                "executed_provider": "openrouter",
                "executed_model": "deepseek/deepseek-v4-flash",
            },
            "executed_provider": "openrouter",
            "executed_model": "deepseek/deepseek-v4-flash",
            "provider_usage": {
                "input_tokens": 100,
                "cache_read_tokens": 20,
                "cache_write_tokens": 5,
                "output_tokens": 30,
                "reasoning_tokens": 10,
                "billed_cost_usd": 0.0123,
                "cost_source": "provider_reported",
            },
        }

        assert await storage.settle_fixed_four_tier_decision(
            route_id="route-1",
            execution_status="succeeded",
            response_id="response-1",
            route_trace=route_trace,
            updated_at_ms=3_000,
        )
        assert await storage.settle_fixed_four_tier_decision(
            route_id="route-1",
            execution_status="succeeded",
            response_id="stale-response",
            route_trace={
                **route_trace,
                "claim_id": claim.claim_id,
                "execution_id": claim.execution_id,
                "execution_status": "succeeded",
                "dispatch": {
                    "physical_request_started": False,
                    "physical_request_count": 0,
                },
                "provider_usage": {"input_tokens": 999},
            },
            updated_at_ms=2_000,
        )
        decision = await storage.get_fixed_four_tier_decision_by_route("route-1")
        claim_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=4_000,
        )

        assert decision is not None
        assert decision.response_id == "response-1"
        assert decision.executed_provider == "openrouter"
        assert decision.executed_model == "deepseek/deepseek-v4-flash"
        assert decision.usage_summary == route_trace["provider_usage"]
        assert decision.terminal_at_ms == 3_000
        assert decision.updated_at_ms == 3_000
        assert decision.route_trace == route_trace
        assert claim_view is not None
        assert claim_view.status == "succeeded"
        assert claim_view.terminal_at_ms == 3_000
    finally:
        await storage.close()


async def test_session_delete_purges_fixed_route_claims_and_decisions() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-delete")
        now_ms = time.time_ns() // 1_000_000
        claim = _claim(
            session,
            claim_id="claim-delete",
            execution_id="execution-delete",
            claimed_at_ms=now_ms,
            lease_expires_at_ms=now_ms + 60_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        await storage.stage_fixed_four_tier_decision(_decision(session, claim))

        await storage.delete_session(session.session_key)

        assert (
            await storage.reconcile_stale_fixed_four_tier_request(
                session_id=session.session_id,
                request_id=claim.request_id,
            )
            is None
        )
        assert await storage.get_fixed_four_tier_decision_by_route("route-1") is None
    finally:
        await storage.close()


async def test_claim_terminal_retry_is_an_immutable_idempotent_noop() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-terminal")
        claim = _claim(
            session,
            claim_id="claim-terminal",
            execution_id="execution-terminal",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        assert await storage.settle_fixed_four_tier_request_claim(
            claim_id=claim.claim_id,
            execution_status="failed",
            error_code="first_failure",
            updated_at_ms=3_000,
        )
        assert await storage.settle_fixed_four_tier_request_claim(
            claim_id=claim.claim_id,
            execution_status="failed",
            error_code="stale_failure",
            updated_at_ms=2_000,
        )

        settled = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=4_000,
        )
        assert settled is not None
        assert settled.status == "failed"
        assert settled.error_code == "first_failure"
        assert settled.terminal_at_ms == 3_000
        assert settled.updated_at_ms == 3_000
    finally:
        await storage.close()


async def test_crash_reconcile_recovers_response_actual_identity_usage_and_cost() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-crash-evidence")
        input_entry = await manager.append_message(
            session.session_key,
            role="user",
            content="durable request",
        )
        claim = _claim(
            session,
            claim_id="claim-crash-evidence",
            execution_id="execution-crash-evidence",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
            input_message_id=input_entry.message_id,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        await storage.commit_fixed_four_tier_decision(
            route_id=decision.route_id,
            state=FixedFourTierState(
                session_id=session.session_id,
                session_key=session.session_key,
                session_epoch=session.epoch,
                version=1,
                task_id=decision.task_id,
                tier=decision.final_tier,
                task_turn_count=1,
                task_start_input_message_id=input_entry.message_id,
                last_request_id=claim.request_id,
                last_route_id=decision.route_id,
                updated_at_ms=1_500,
            ),
            expected_version=None,
            route_trace=decision.route_trace,
            updated_at_ms=1_500,
        )
        await storage.start_usage_event(
            UsageEventStart(
                event_id="attempt-crash-evidence",
                execution_id=claim.execution_id,
                call_index=0,
                session_id=session.session_id,
                session_epoch=session.epoch,
                turn_id=claim.execution_id,
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
                started_at_ms=1_600,
            )
        )
        await storage.finalize_usage_event(
            "attempt-crash-evidence",
            UsageEventCompletion(
                completed_at_ms=1_700,
                input_tokens=100,
                output_tokens=30,
                reasoning_tokens=7,
                cache_read_tokens=20,
                cache_write_tokens=5,
                total_tokens=130,
                cost_nanos=12_300_000,
                billed_cost_nanos=12_300_000,
                cost_source="provider_billed",
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
            ),
        )
        foreign_response = await manager.append_message(
            session.session_key,
            role="assistant",
            content="response for a different queued execution",
            turn_usage={"input_tokens": 999, "output_tokens": 999},
        )
        assert await manager.merge_message_turn_context(
            session.session_key,
            foreign_response.message_id,
            {
                "schema": "fixed_four_tier_v2_response_binding_v1",
                "turn_id": "different-execution",
                "execution_id": "different-execution",
                "route_id": "different-route",
                "request_id": "different-request",
                "execution_status": "succeeded",
            },
        )
        # Simulate a worker kill immediately after the first durable assistant
        # append: the route binding is part of that INSERT, with no later merge
        # or settlement write available to recovery.
        with turn_context_scope(
            {
                "client_message_id": "client-causal-id",
                "surface_id": "web",
                "intent": "user",
                "disposition": "accepted",
                "revision": 3,
                "schema": "fixed_four_tier_v2_response_binding_v1",
                # Preserve the TaskRuntime causal turn identity; crash
                # reconciliation keys off the separate route execution id.
                "turn_id": "task-runtime-causal-turn",
                "execution_id": claim.execution_id,
                "route_id": decision.route_id,
                "request_id": claim.request_id,
                "execution_status": "succeeded",
                "error_code": None,
            }
        ):
            response = await manager.append_message(
                session.session_key,
                role="assistant",
                content="durable response",
                turn_usage={"input_tokens": 100, "output_tokens": 30},
            )
        response_entry = next(
            entry
            for entry in await manager.get_transcript(session.session_key)
            if entry.message_id == response.message_id
        )
        assert response_entry.turn_context is not None
        assert response_entry.turn_context["client_message_id"] == "client-causal-id"
        assert response_entry.turn_context["surface_id"] == "web"
        assert response_entry.turn_context["revision"] == 3
        assert response_entry.turn_context["turn_id"] == "task-runtime-causal-turn"
        assert response_entry.turn_context["execution_id"] == claim.execution_id
        reconcile_at_ms = time.time_ns() // 1_000_000 + 1

        settled_claim = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=reconcile_at_ms,
        )
        settled_decision = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)

        assert settled_claim is not None
        assert settled_claim.status == "succeeded"
        assert settled_claim.error_code is None
        assert settled_decision is not None
        assert settled_decision.execution_status == "succeeded"
        assert settled_decision.response_id == response.message_id
        assert settled_decision.executed_provider == "openrouter"
        assert settled_decision.executed_model == "deepseek/deepseek-v4-flash"
        assert settled_decision.usage_summary is not None
        assert settled_decision.usage_summary["input_tokens"] == 100
        assert settled_decision.usage_summary["billed_cost_usd"] == 0.0123
        assert settled_decision.route_trace["attempt_ids"] == ["attempt-crash-evidence"]
        assert settled_decision.route_trace["dispatch"]["physical_request_started"] is True
        assert settled_decision.route_trace["lease_reconciliation"]["outcome"] == (
            "durable_response_binding"
        )
    finally:
        await storage.close()


async def test_crash_reconcile_preserves_usage_when_outcome_is_unknown() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-crash-unknown")
        input_entry = await manager.append_message(
            session.session_key,
            role="user",
            content="durable request without response",
        )
        claim = _claim(
            session,
            claim_id="claim-crash-unknown",
            execution_id="execution-crash-unknown",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
            input_message_id=input_entry.message_id,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        await storage.start_usage_event(
            UsageEventStart(
                event_id="attempt-crash-unknown",
                execution_id=claim.execution_id,
                call_index=0,
                session_id=session.session_id,
                session_epoch=session.epoch,
                turn_id=claim.execution_id,
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
                started_at_ms=1_500,
            )
        )
        await storage.finalize_usage_event(
            "attempt-crash-unknown",
            UsageEventCompletion(
                completed_at_ms=1_600,
                input_tokens=40,
                output_tokens=8,
                total_tokens=48,
                cost_nanos=1_000_000,
                billed_cost_nanos=1_000_000,
                cost_source="provider_billed",
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
            ),
        )

        claim_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=time.time_ns() // 1_000_000 + 1,
        )
        decision_view = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)

        assert claim_view is not None
        assert claim_view.status == "failed"
        assert claim_view.error_code == "execution_outcome_unknown_after_crash"
        assert decision_view is not None
        assert decision_view.execution_status == "failed"
        assert decision_view.error_code == "execution_outcome_unknown_after_crash"
        assert decision_view.executed_provider == "openrouter"
        assert decision_view.executed_model == "deepseek/deepseek-v4-flash"
        assert decision_view.usage_summary is not None
        assert decision_view.usage_summary["input_tokens"] == 40
        assert decision_view.route_trace["dispatch"]["physical_request_started"] is True
        assert decision_view.route_trace["lease_reconciliation"]["outcome"] == (
            "provider_attempt_outcome_unknown"
        )
    finally:
        await storage.close()


async def test_crash_reconcile_uses_failed_partial_response_binding_status() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-crash-partial-failure")
        input_entry = await manager.append_message(
            session.session_key,
            role="user",
            content="request that partially fails",
        )
        claim = _claim(
            session,
            claim_id="claim-crash-partial-failure",
            execution_id="execution-crash-partial-failure",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
            input_message_id=input_entry.message_id,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        with turn_context_scope(
            {
                "schema": "fixed_four_tier_v2_response_binding_v1",
                "turn_id": "task-runtime-causal-failed-turn",
                "execution_id": claim.execution_id,
                "route_id": decision.route_id,
                "request_id": claim.request_id,
                "execution_status": "failed",
                "error_code": "provider_timeout",
            }
        ):
            partial_response = await manager.append_message(
                session.session_key,
                role="assistant",
                content="partial answer",
                turn_usage={"input_tokens": 12, "output_tokens": 3},
            )

        claim_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=time.time_ns() // 1_000_000 + 1,
        )
        decision_view = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)

        assert claim_view is not None
        assert claim_view.status == "failed"
        assert claim_view.error_code == "provider_timeout"
        assert decision_view is not None
        assert decision_view.execution_status == "failed"
        assert decision_view.error_code == "provider_timeout"
        assert decision_view.response_id == partial_response.message_id
        assert decision_view.route_trace["lease_reconciliation"]["outcome"] == (
            "durable_response_binding"
        )
    finally:
        await storage.close()


async def test_restart_reconciles_crashed_pending_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "fixed-route-restart.db"
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage)
    session = await manager.create("agent:main:fixed-restart")
    claim = _claim(
        session,
        claim_id="claim-restart",
        execution_id="execution-restart",
        claimed_at_ms=1_000,
        lease_expires_at_ms=2_000,
    )
    assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
    await storage.stage_fixed_four_tier_decision(_decision(session, claim))
    await storage.close()

    restarted = await SessionStorage.open(str(db_path))
    try:
        decision = await restarted.get_fixed_four_tier_decision_by_route("route-1")
        claim_view = await restarted.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
        )

        assert decision is not None
        assert decision.execution_status == "failed"
        assert decision.error_code == "execution_lease_expired"
        assert claim_view is not None
        assert claim_view.status == "failed"
        assert claim_view.error_code == "execution_lease_expired"
    finally:
        await restarted.close()


@pytest.mark.parametrize(
    "trace_patch",
    [
        {"schema_version": "unknown"},
        {"route_id": "different-route"},
    ],
    ids=["incompatible-schema", "mismatched-row-identity"],
)
async def test_fixed_route_getters_fail_closed_on_incompatible_persisted_trace(
    trace_patch: dict[str, Any],
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-corrupt-trace")
        claim = _claim(
            session,
            claim_id="claim-corrupt-trace",
            execution_id="execution-corrupt-trace",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        await storage.commit_fixed_four_tier_decision(
            route_id=decision.route_id,
            state=FixedFourTierState(
                session_id=session.session_id,
                session_key=session.session_key,
                session_epoch=session.epoch,
                version=1,
                task_id=decision.task_id,
                tier=decision.final_tier,
                task_turn_count=1,
                task_start_input_message_id=claim.input_message_id,
                last_request_id=claim.request_id,
                last_route_id=decision.route_id,
                updated_at_ms=1_500,
            ),
            expected_version=None,
            route_trace=decision.route_trace,
            updated_at_ms=1_500,
        )

        incompatible_trace = {**decision.route_trace, **trace_patch}
        async with storage._write_transaction("test_corrupt_fixed_route_trace") as conn:
            await conn.execute(
                "UPDATE fixed_four_tier_decisions SET route_trace = ? WHERE route_id = ?",
                (json.dumps(incompatible_trace), decision.route_id),
            )

        error = "persisted four_tier_mapping route trace is incompatible"
        with pytest.raises(ValueError, match=error):
            await storage.get_fixed_four_tier_decision_by_request(
                session_id=session.session_id,
                request_id=claim.request_id,
            )
        with pytest.raises(ValueError, match=error):
            await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        with pytest.raises(ValueError, match=error):
            await storage.get_fixed_four_tier_decision_by_input_message(
                session_id=session.session_id,
                input_message_id=claim.input_message_id,
            )
    finally:
        await storage.close()


async def test_crash_reconciliation_does_not_settle_a_corrupted_pending_trace() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-corrupt-pending")
        claim = _claim(
            session,
            claim_id="claim-corrupt-pending",
            execution_id="execution-corrupt-pending",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)

        incompatible_trace = {**decision.route_trace, "schema_version": "unknown"}
        async with storage._write_transaction("test_corrupt_pending_route_trace") as conn:
            await conn.execute(
                "UPDATE fixed_four_tier_decisions SET route_trace = ? WHERE route_id = ?",
                (json.dumps(incompatible_trace), decision.route_id),
            )

        with pytest.raises(
            ValueError,
            match="persisted four_tier_mapping route trace is incompatible",
        ):
            await storage.reconcile_stale_fixed_four_tier_request(
                session_id=session.session_id,
                request_id=claim.request_id,
                now_ms=2_001,
            )

        async with storage.conn.execute(
            "SELECT status FROM fixed_four_tier_request_claims WHERE claim_id = ?",
            (claim.claim_id,),
        ) as cursor:
            claim_row = await cursor.fetchone()
        async with storage.conn.execute(
            "SELECT execution_status FROM fixed_four_tier_decisions WHERE route_id = ?",
            (decision.route_id,),
        ) as cursor:
            decision_row = await cursor.fetchone()
        assert claim_row is not None
        assert claim_row["status"] == "materialized"
        assert decision_row is not None
        assert decision_row["execution_status"] == "pending"
    finally:
        await storage.close()
