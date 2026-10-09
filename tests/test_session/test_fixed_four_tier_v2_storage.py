from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import opensquilla.session.storage as storage_module
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
from opensquilla.session.storage import FixedFourTierStateConflictError, SessionStorage
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
    request_id: str = "request-1",
) -> FixedFourTierRequestClaim:
    return FixedFourTierRequestClaim(
        claim_id=claim_id,
        session_id=session.session_id,
        session_key=session.session_key,
        session_epoch=session.epoch,
        request_id=request_id,
        execution_id=execution_id,
        input_message_id=input_message_id,
        claimed_at_ms=claimed_at_ms,
        updated_at_ms=claimed_at_ms,
        lease_expires_at_ms=lease_expires_at_ms,
    )


def _decision(
    session: Any,
    claim: FixedFourTierRequestClaim,
    *,
    route_id: str = "route-1",
    task_id: str = "task-1",
) -> FixedFourTierDecisionRecord:
    core_decision, next_state = FixedFourTierV2Router(
        mock_seed=7,
        route_id_factory=lambda: route_id,
        task_id_factory=lambda: task_id,
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
            "session_id": session.session_id,
            "session_epoch": session.epoch,
            "claim_id": claim.claim_id,
            "execution_id": claim.execution_id,
            "session_key_hash": hashlib.sha256(session.session_key.encode("utf-8")).hexdigest(),
            "input_message_id": claim.input_message_id,
            "redo_parent_route_id": None,
            "task_start_input_message_id": next_state.task_start_input_message_id,
            "state_version_before": None,
            "state_version_after": None,
            "execution_status": "pending",
            "response_id": None,
            "state_committed": False,
            "reasoning": "max",
            "deployment_version": "0731",
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
        reasoning="max",
        deployment_version="0731",
        config_version=core_decision.schema_version,
        route_trace=route_trace,
    )


def _committed_trace(
    trace: dict[str, Any],
    *,
    state_version: int,
) -> dict[str, Any]:
    preflight = dict(trace.get("preflight") or {})
    preflight["status"] = "passed"
    trace.update(
        {
            "preflight": preflight,
            "state_committed": True,
            "state_version_after": state_version,
        }
    )
    return trace


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


async def test_request_claim_rejects_non_current_schema() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-schema")
        claim = _claim(
            session,
            claim_id="claim-schema",
            execution_id="execution-schema",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        claim.schema_version = 2

        with pytest.raises(ValueError, match="claim must use schema version 1"):
            await storage.claim_fixed_four_tier_request(claim)

        assert (
            await storage.reconcile_stale_fixed_four_tier_request(
                session_id=session.session_id,
                request_id=claim.request_id,
                now_ms=1_001,
            )
            is None
        )
    finally:
        await storage.close()


async def test_request_claim_rejects_forged_lifecycle_fields() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-forgery")
        claim = _claim(
            session,
            claim_id="claim-forgery",
            execution_id="execution-forgery",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        claim.route_id = "forged-route"
        claim.terminal_at_ms = 999
        claim.error_code = "forged-error"

        with pytest.raises(ValueError, match="request claim must be pristine"):
            await storage.claim_fixed_four_tier_request(claim)

        assert (
            await storage.reconcile_stale_fixed_four_tier_request(
                session_id=session.session_id,
                request_id=claim.request_id,
                now_ms=1_001,
            )
            is None
        )
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
        committed_trace = _committed_trace(staged_decision.route_trace, state_version=1)
        await storage.commit_fixed_four_tier_decision(
            route_id=staged_decision.route_id,
            state=FixedFourTierState(
                session_id=session.session_id,
                session_key=session.session_key,
                session_epoch=session.epoch,
                version=1,
                task_id=staged_decision.task_id,
                tier=staged_decision.final_tier,
                task_turn_count=staged_decision.task_turn_index + 1,
                task_start_input_message_id=staged_decision.task_start_input_message_id,
                last_request_id=staged_decision.request_id,
                last_route_id=staged_decision.route_id,
                updated_at_ms=2_000,
            ),
            expected_version=None,
            route_trace=committed_trace,
            updated_at_ms=2_000,
        )
        route_trace = {
            **committed_trace,
            "claim_id": claim.claim_id,
            "execution_id": claim.execution_id,
            "execution_status": "succeeded",
            "response_id": "response-1",
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


async def test_stage_rejects_row_trace_conflict_before_writing() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-stage-trace-conflict")
        claim = _claim(
            session,
            claim_id="claim-stage-trace-conflict",
            execution_id="execution-stage-trace-conflict",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        decision.route_trace["execution_status"] = "failed"

        with pytest.raises(ValueError, match="route trace is incompatible"):
            await storage.stage_fixed_four_tier_decision(decision)

        assert await storage.get_fixed_four_tier_decision_by_route(decision.route_id) is None
    finally:
        await storage.close()


async def test_stage_rejects_self_consistent_forged_terminal_lifecycle() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-stage-terminal-forgery")
        claim = _claim(
            session,
            claim_id="claim-stage-terminal-forgery",
            execution_id="execution-stage-terminal-forgery",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        decision.preflight_status = "passed"
        decision.state_committed = True
        decision.state_version_after = 1
        decision.execution_status = "succeeded"
        decision.response_id = "forged-response"
        decision.route_trace.update(
            {
                "preflight": {"status": "passed"},
                "state_committed": True,
                "state_version_after": 1,
                "execution_status": "succeeded",
                "response_id": "forged-response",
            }
        )

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="must be pristine",
        ):
            await storage.stage_fixed_four_tier_decision(decision)

        assert await storage.get_fixed_four_tier_decision_by_route(decision.route_id) is None
        assert await storage.get_fixed_four_tier_state(session.session_id) is None
    finally:
        await storage.close()


async def test_stage_rejects_new_legacy_schema_record() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-new-legacy-schema")
        claim = _claim(
            session,
            claim_id="claim-new-legacy-schema",
            execution_id="execution-new-legacy-schema",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        decision.config_version = "fixed-four-tier-v2-mock-v2"
        decision.route_trace["schema_version"] = "fixed-four-tier-v2-mock-v2"
        decision.route_trace.pop("classifier_backend")
        decision.route_trace.pop("classifier_identity")

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="must use the current trace schema",
        ):
            await storage.stage_fixed_four_tier_decision(decision)

        assert await storage.get_fixed_four_tier_decision_by_route(decision.route_id) is None
    finally:
        await storage.close()


async def test_commit_rejects_row_trace_conflict_before_state_write() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-commit-trace-conflict")
        claim = _claim(
            session,
            claim_id="claim-commit-trace-conflict",
            execution_id="execution-commit-trace-conflict",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)

        with pytest.raises(ValueError, match="route trace is incompatible"):
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

        assert await storage.get_fixed_four_tier_state(session.session_id) is None
        persisted = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert persisted is not None
        assert persisted.state_committed is False
        assert persisted.preflight_status == "pending"
    finally:
        await storage.close()


async def test_commit_binds_decision_prior_version_to_cas_expectation() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-prior-version-binding")
        claim = _claim(
            session,
            claim_id="claim-prior-version-binding",
            execution_id="execution-prior-version-binding",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        decision.state_version_before = 7
        decision.route_trace["state_version_before"] = 7
        await storage.stage_fixed_four_tier_decision(decision)

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="inconsistent prior state version",
        ):
            await storage.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=FixedFourTierState(
                    session_id=session.session_id,
                    session_key=session.session_key,
                    session_epoch=session.epoch,
                    version=1,
                    task_id=decision.task_id,
                    tier=decision.final_tier,
                    task_turn_count=decision.task_turn_index + 1,
                    task_start_input_message_id=decision.task_start_input_message_id,
                    last_request_id=decision.request_id,
                    last_route_id=decision.route_id,
                    updated_at_ms=1_500,
                ),
                expected_version=None,
                route_trace=_committed_trace(decision.route_trace, state_version=1),
                updated_at_ms=1_500,
            )

        assert await storage.get_fixed_four_tier_state(session.session_id) is None
    finally:
        await storage.close()


async def test_commit_rejects_non_current_state_schema() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-state-schema")
        claim = _claim(
            session,
            claim_id="claim-state-schema",
            execution_id="execution-state-schema",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        state = FixedFourTierState(
            session_id=session.session_id,
            session_key=session.session_key,
            session_epoch=session.epoch,
            version=1,
            task_id=decision.task_id,
            tier=decision.final_tier,
            task_turn_count=decision.task_turn_index + 1,
            task_start_input_message_id=decision.task_start_input_message_id,
            last_request_id=decision.request_id,
            last_route_id=decision.route_id,
            updated_at_ms=1_500,
            schema_version=2,
        )

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="task state must use schema version 1",
        ):
            await storage.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=state,
                expected_version=None,
                route_trace=_committed_trace(decision.route_trace, state_version=1),
                updated_at_ms=1_500,
            )

        assert await storage.get_fixed_four_tier_state(session.session_id) is None
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("task_id", "wrong-task"),
        ("tier", "wrong-tier"),
        ("task_turn_count", 99),
        ("task_start_input_message_id", "wrong-task-start"),
        ("last_request_id", "wrong-request"),
        ("last_route_id", "wrong-route"),
    ],
)
async def test_commit_binds_authoritative_state_to_staged_decision(
    field_name: str,
    invalid_value: Any,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create(f"agent:main:fixed-state-binding-{field_name}")
        claim = _claim(
            session,
            claim_id=f"claim-state-binding-{field_name}",
            execution_id=f"execution-state-binding-{field_name}",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        valid_state = FixedFourTierState(
            session_id=session.session_id,
            session_key=session.session_key,
            session_epoch=session.epoch,
            version=1,
            task_id=decision.task_id,
            tier=decision.final_tier,
            task_turn_count=decision.task_turn_index + 1,
            task_start_input_message_id=decision.task_start_input_message_id,
            last_request_id=decision.request_id,
            last_route_id=decision.route_id,
            updated_at_ms=1_500,
        )
        invalid_state = valid_state.model_copy(update={field_name: invalid_value})

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="next task state conflicts with its decision",
        ):
            await storage.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=invalid_state,
                expected_version=None,
                route_trace=_committed_trace(decision.route_trace, state_version=1),
                updated_at_ms=1_500,
            )

        assert await storage.get_fixed_four_tier_state(session.session_id) is None
    finally:
        await storage.close()


async def test_settle_rejects_row_trace_conflict_before_terminal_write() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-settle-trace-conflict")
        claim = _claim(
            session,
            claim_id="claim-settle-trace-conflict",
            execution_id="execution-settle-trace-conflict",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)

        with pytest.raises(ValueError, match="route trace is incompatible"):
            await storage.settle_fixed_four_tier_decision(
                route_id=decision.route_id,
                execution_status="failed",
                preflight_status="failed",
                error_code="terminal-failure",
                route_trace={
                    **decision.route_trace,
                    "preflight": {"status": "failed"},
                },
                updated_at_ms=2_000,
            )

        persisted = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert persisted is not None
        assert persisted.execution_status == "pending"
        assert persisted.error_code is None
    finally:
        await storage.close()


async def test_settle_rejects_success_before_task_state_commit() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-premature-success")
        claim = _claim(
            session,
            claim_id="claim-premature-success",
            execution_id="execution-premature-success",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        premature_trace = {
            **decision.route_trace,
            "preflight": {"status": "failed"},
            "execution_status": "succeeded",
            "response_id": "premature-response",
        }

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="cannot succeed before task state commit",
        ):
            await storage.settle_fixed_four_tier_decision(
                route_id=decision.route_id,
                execution_status="succeeded",
                preflight_status="failed",
                response_id="premature-response",
                route_trace=premature_trace,
                updated_at_ms=2_000,
            )

        persisted = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert persisted is not None
        assert persisted.execution_status == "pending"
        assert persisted.state_committed is False
    finally:
        await storage.close()


async def test_decision_settlement_cannot_conflict_with_terminal_claim() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-terminal-claim-conflict")
        claim = _claim(
            session,
            claim_id="claim-terminal-conflict",
            execution_id="execution-terminal-conflict",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        committed_trace = _committed_trace(decision.route_trace, state_version=1)
        await storage.commit_fixed_four_tier_decision(
            route_id=decision.route_id,
            state=FixedFourTierState(
                session_id=session.session_id,
                session_key=session.session_key,
                session_epoch=session.epoch,
                version=1,
                task_id=decision.task_id,
                tier=decision.final_tier,
                task_turn_count=decision.task_turn_index + 1,
                task_start_input_message_id=decision.task_start_input_message_id,
                last_request_id=decision.request_id,
                last_route_id=decision.route_id,
                updated_at_ms=1_500,
            ),
            expected_version=None,
            route_trace=committed_trace,
            updated_at_ms=1_500,
        )
        assert await storage.settle_fixed_four_tier_request_claim(
            claim_id=claim.claim_id,
            execution_status="failed",
            error_code="claim-failed-first",
            updated_at_ms=1_600,
        )

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="no active request claim",
        ):
            await storage.settle_fixed_four_tier_decision(
                route_id=decision.route_id,
                execution_status="succeeded",
                response_id="late-response",
                route_trace={
                    **committed_trace,
                    "execution_status": "succeeded",
                    "response_id": "late-response",
                },
                updated_at_ms=1_700,
            )

        persisted = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert persisted is not None
        assert persisted.execution_status == "pending"
        claim_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=1_800,
        )
        assert claim_view is not None
        assert claim_view.status == "failed"
    finally:
        await storage.close()


async def test_pending_settlement_cannot_persist_partial_execution_evidence() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-pending-partial")
        claim = _claim(
            session,
            claim_id="claim-pending-partial",
            execution_id="execution-pending-partial",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        partial_trace = {
            **decision.route_trace,
            "executed_provider": "openrouter",
            "executed_model": "deepseek/deepseek-v4-flash",
            "dispatch": {
                "physical_request_started": True,
                "physical_request_count": 1,
                "executed_provider": "openrouter",
                "executed_model": "deepseek/deepseek-v4-flash",
            },
        }

        with pytest.raises(
            FixedFourTierStateConflictError,
            match="pending four_tier_mapping settlement cannot mutate",
        ):
            await storage.settle_fixed_four_tier_decision(
                route_id=decision.route_id,
                execution_status="pending",
                route_trace=partial_trace,
                updated_at_ms=1_500,
            )

        restored = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert restored is not None
        assert restored.executed_provider is None
        assert restored.executed_model is None
    finally:
        await storage.close()


async def test_recent_decisions_are_bounded_committed_session_epoch_history() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-recent-history")
        other_session = await manager.create("agent:main:fixed-recent-history-other")
        state_versions: dict[str, int] = {}

        async def record(
            owner: Any,
            *,
            label: str,
            decided_at_ms: int,
            committed: bool = True,
        ) -> FixedFourTierDecisionRecord:
            claim = _claim(
                owner,
                claim_id=f"claim-{label}",
                execution_id=f"execution-{label}",
                request_id=f"request-{label}",
                input_message_id=f"input-{label}",
                claimed_at_ms=decided_at_ms,
                lease_expires_at_ms=10_000,
            )
            assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
            decision = _decision(
                owner,
                claim,
                route_id=f"route-{label}",
                task_id=f"task-{owner.session_id}",
            )
            expected_version = state_versions.get(owner.session_id)
            decision.state_version_before = expected_version
            decision.route_trace["state_version_before"] = expected_version
            await storage.stage_fixed_four_tier_decision(decision)
            if not committed:
                return decision

            next_version = (expected_version or 0) + 1
            await storage.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=FixedFourTierState(
                    session_id=owner.session_id,
                    session_key=owner.session_key,
                    session_epoch=owner.epoch,
                    version=next_version,
                    task_id=decision.task_id,
                    tier=decision.final_tier,
                    task_turn_count=decision.task_turn_index + 1,
                    task_start_input_message_id=decision.input_message_id,
                    last_request_id=decision.request_id,
                    last_route_id=decision.route_id,
                    updated_at_ms=decided_at_ms + 1,
                ),
                expected_version=expected_version,
                route_trace=_committed_trace(
                    decision.route_trace,
                    state_version=next_version,
                ),
                updated_at_ms=decided_at_ms + 1,
            )
            state_versions[owner.session_id] = next_version
            return decision

        for label, decided_at_ms in (
            ("below-since", 999),
            ("at-since", 1_000),
            ("one", 1_100),
            ("two", 1_200),
            ("three", 1_300),
            ("four", 1_400),
            ("five", 1_500),
            ("at-before", 2_000),
            ("above-before", 2_001),
        ):
            await record(session, label=label, decided_at_ms=decided_at_ms)
        await record(
            session,
            label="staged",
            decided_at_ms=1_450,
            committed=False,
        )
        await record(other_session, label="other-session", decided_at_ms=1_475)

        recent = await manager.list_recent_fixed_four_tier_decisions(
            session_id=session.session_id,
            session_epoch=session.epoch,
            since_ms=1_000,
            before_ms=2_000,
        )

        assert [decision.route_id for decision in recent] == [
            "route-two",
            "route-three",
            "route-four",
            "route-five",
            "route-at-before",
        ]
        assert [decision.decided_at_ms for decision in recent] == sorted(
            decision.decided_at_ms for decision in recent
        )
        assert len(recent) == 5

        lower_boundary = await manager.list_recent_fixed_four_tier_decisions(
            session_id=session.session_id,
            session_epoch=session.epoch,
            since_ms=1_000,
            before_ms=1_001,
        )
        assert [decision.route_id for decision in lower_boundary] == ["route-at-since"]

        same_millisecond_boundary = await manager.list_recent_fixed_four_tier_decisions(
            session_id=session.session_id,
            session_epoch=session.epoch,
            since_ms=2_000,
            before_ms=2_000,
        )
        assert [decision.route_id for decision in same_millisecond_boundary] == ["route-at-before"]

        wrong_epoch = await manager.list_recent_fixed_four_tier_decisions(
            session_id=session.session_id,
            session_epoch=session.epoch + 1,
            since_ms=1_000,
            before_ms=2_000,
        )
        assert wrong_epoch == []
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


async def test_claim_only_settlement_cannot_claim_success() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-claim-success-forgery")
        claim = _claim(
            session,
            claim_id="claim-success-forgery",
            execution_id="execution-success-forgery",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True

        with pytest.raises(ValueError, match="claim terminal status"):
            await storage.settle_fixed_four_tier_request_claim(
                claim_id=claim.claim_id,
                execution_status="succeeded",
                updated_at_ms=2_000,
            )

        restored = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=2_001,
        )
        assert restored is not None
        assert restored.status == "claimed"
    finally:
        await storage.close()


async def test_terminal_claim_reconciliation_keeps_decision_trace_consistent() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-terminal-claim-reconcile")
        claim = _claim(
            session,
            claim_id="claim-terminal-reconcile",
            execution_id="execution-terminal-reconcile",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        assert await storage.settle_fixed_four_tier_request_claim(
            claim_id=claim.claim_id,
            execution_status="failed",
            error_code="claim-failed",
            updated_at_ms=2_000,
        )

        await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=2_001,
        )
        restored = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)

        assert restored is not None
        assert restored.execution_status == "failed"
        assert restored.preflight_status == "failed"
        assert restored.error_code == "claim-failed"
        assert restored.route_trace["execution_status"] == "failed"
        assert restored.route_trace["preflight"]["status"] == "failed"
        assert restored.route_trace["error_code"] == "claim-failed"
    finally:
        await storage.close()


async def test_terminal_claim_reconciliation_rejects_cross_bound_identity() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-terminal-cross-binding")
        claim = _claim(
            session,
            claim_id="claim-terminal-cross-binding",
            execution_id="execution-terminal-cross-binding",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        assert await storage.settle_fixed_four_tier_request_claim(
            claim_id=claim.claim_id,
            execution_status="failed",
            error_code="claim-failed",
            updated_at_ms=2_000,
        )
        async with storage._write_transaction("test_corrupt_terminal_claim_identity") as conn:
            await conn.execute(
                """
                UPDATE fixed_four_tier_request_claims
                SET execution_id = ?
                WHERE claim_id = ?
                """,
                ("execution-owned-by-another-turn", claim.claim_id),
            )

        with pytest.raises(ValueError, match="does not own its decision: execution_id"):
            await storage.reconcile_stale_fixed_four_tier_request(
                session_id=session.session_id,
                request_id=claim.request_id,
                now_ms=2_001,
            )

        restored = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        assert restored is not None
        assert restored.execution_status == "pending"
        async with storage.conn.execute(
            "SELECT status FROM fixed_four_tier_request_claims WHERE claim_id = ?",
            (claim.claim_id,),
        ) as cursor:
            persisted_claim = await cursor.fetchone()
        assert persisted_claim is not None
        assert persisted_claim["status"] == "failed"
    finally:
        await storage.close()


async def test_orphan_reconciliation_records_failed_preflight_consistently() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-orphan-reconcile")
        claim = _claim(
            session,
            claim_id="claim-orphan-reconcile",
            execution_id="execution-orphan-reconcile",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        async with storage._write_transaction("test_delete_route_claim") as conn:
            await conn.execute(
                "DELETE FROM fixed_four_tier_request_claims WHERE claim_id = ?",
                (claim.claim_id,),
            )

        await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=claim.request_id,
            now_ms=2_000,
        )
        restored = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)

        assert restored is not None
        assert restored.execution_status == "failed"
        assert restored.preflight_status == "failed"
        assert restored.state_committed is False
        assert restored.error_code == "execution_claim_missing"
        assert restored.route_trace["execution_status"] == "failed"
        assert restored.route_trace["preflight"]["status"] == "failed"
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
            route_trace=_committed_trace(decision.route_trace, state_version=1),
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
        pytest.param({"schema_version": "unknown"}, id="incompatible-schema"),
        pytest.param({"route_id": "different-route"}, id="route-id"),
        pytest.param({"session_id": "different-session"}, id="session-id"),
        pytest.param({"session_epoch": 2}, id="session-epoch"),
        pytest.param({"claim_id": "different-claim"}, id="claim-id"),
        pytest.param({"execution_id": "different-execution"}, id="execution-id"),
        pytest.param({"session_key_hash": "0" * 64}, id="session-key-hash"),
        pytest.param({"input_message_id": "different-input"}, id="input-message-id"),
        pytest.param(
            {"task_start_input_message_id": "different-task-start"},
            id="task-start-input-message-id",
        ),
        pytest.param({"redo_parent_route_id": "different-parent"}, id="redo-parent-route-id"),
        pytest.param({"state_version_before": 7}, id="state-version-before"),
        pytest.param({"provider": "different-provider"}, id="selected-provider"),
        pytest.param({"model": "different-model"}, id="selected-model"),
        pytest.param({"reasoning": "thinking"}, id="reasoning"),
        pytest.param({"deployment_version": "different-deployment"}, id="deployment-version"),
        pytest.param({"execution_status": "failed"}, id="execution-status"),
        pytest.param({"state_committed": False}, id="state-committed"),
        pytest.param({"state_version_after": 2}, id="state-version-after"),
        pytest.param({"response_id": "different-response"}, id="response-id"),
        pytest.param({"error_code": "different-error"}, id="error-code"),
        pytest.param({"executed_provider": "different-provider"}, id="executed-provider"),
        pytest.param({"executed_model": "different-model"}, id="executed-model"),
        pytest.param(
            {"executed_deployment_version": "different-deployment"},
            id="executed-deployment-version",
        ),
        pytest.param({"provider_usage": {"input_tokens": 1}}, id="provider-usage"),
        pytest.param({"preflight": {"status": "failed"}}, id="preflight-status"),
        pytest.param("session_id", id="missing-session-id"),
        pytest.param("session_epoch", id="missing-session-epoch"),
        pytest.param("claim_id", id="missing-claim-id"),
        pytest.param("execution_id", id="missing-execution-id"),
        pytest.param("session_key_hash", id="missing-session-key-hash"),
        pytest.param("input_message_id", id="missing-input-message-id"),
        pytest.param(
            "task_start_input_message_id",
            id="missing-task-start-input-message-id",
        ),
        pytest.param("redo_parent_route_id", id="missing-redo-parent-route-id"),
        pytest.param("state_version_before", id="missing-state-version-before"),
        pytest.param("provider", id="missing-selected-provider"),
        pytest.param("model", id="missing-selected-model"),
        pytest.param("reasoning", id="missing-reasoning"),
        pytest.param("deployment_version", id="missing-deployment-version"),
        pytest.param("execution_status", id="missing-execution-status"),
        pytest.param("state_committed", id="missing-state-committed"),
        pytest.param("response_id", id="missing-response-id"),
        pytest.param("state_version_after", id="missing-state-version-after"),
        pytest.param("preflight", id="missing-preflight"),
    ],
)
async def test_fixed_route_getters_fail_closed_on_incompatible_persisted_trace(
    trace_patch: dict[str, Any] | str,
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
            route_trace=_committed_trace(decision.route_trace, state_version=1),
            updated_at_ms=1_500,
        )

        incompatible_trace = dict(decision.route_trace)
        if isinstance(trace_patch, str):
            incompatible_trace.pop(trace_patch)
        else:
            incompatible_trace.update(trace_patch)
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


@pytest.mark.parametrize(
    "column",
    [
        "route_id",
        "request_id",
        "task_id",
        "decided_at_ms",
        "intent",
        "tier",
        "previous_tier",
        "final_tier",
        "task_turn_index",
        "context_action",
        "config_version",
        "schema_version",
        "session_id",
        "session_key",
        "session_epoch",
        "claim_id",
        "execution_id",
        "input_message_id",
        "task_start_input_message_id",
        "redo_parent_route_id",
        "state_version_before",
        "selected_provider",
        "selected_model",
        "reasoning",
        "deployment_version",
        "state_version_after",
        "preflight_status",
        "state_committed",
        "execution_status",
        "response_id",
        "error_code",
        "executed_provider",
        "executed_model",
        "executed_deployment_version",
        "usage_summary",
    ],
)
async def test_fixed_route_replay_rejects_tampered_duplicate_semantic_column(
    column: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-corrupt-row-semantics")
        claim = _claim(
            session,
            claim_id="claim-corrupt-row-semantics",
            execution_id="execution-corrupt-row-semantics",
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
            route_trace=_committed_trace(decision.route_trace, state_version=1),
            updated_at_ms=1_500,
        )

        if column == "route_id":
            tampered_value: Any = "route-row-tampered"
        elif column == "request_id":
            tampered_value = "request-row-tampered"
        elif column == "task_id":
            tampered_value = "task-row-tampered"
        elif column == "decided_at_ms":
            tampered_value = decision.decided_at_ms + 1
        elif column == "intent":
            tampered_value = {**decision.intent, "version": "tampered-intent-version"}
        elif column == "tier":
            tampered_value = {**decision.tier, "version": "tampered-tier-version"}
        elif column == "previous_tier":
            tampered_value = "c0"
        elif column == "final_tier":
            tampered_value = "c1" if decision.final_tier != "c1" else "c2"
        elif column == "task_turn_index":
            tampered_value = decision.task_turn_index + 1
        elif column == "context_action":
            tampered_value = "keep" if decision.context_action == "reset" else "reset"
        elif column == "config_version":
            tampered_value = "tampered-config-version"
        elif column == "schema_version":
            tampered_value = 2
        elif column == "session_id":
            tampered_value = "session-row-tampered"
        elif column == "session_key":
            tampered_value = "agent:main:fixed-row-tampered"
        elif column == "session_epoch":
            tampered_value = session.epoch + 1
        elif column == "claim_id":
            tampered_value = "claim-row-tampered"
        elif column == "execution_id":
            tampered_value = "execution-row-tampered"
        elif column == "input_message_id":
            tampered_value = "input-row-tampered"
        elif column == "task_start_input_message_id":
            tampered_value = "task-start-row-tampered"
        elif column == "redo_parent_route_id":
            tampered_value = "redo-parent-row-tampered"
        elif column == "state_version_before":
            tampered_value = 7
        elif column == "selected_provider":
            tampered_value = "provider-row-tampered"
        elif column == "selected_model":
            tampered_value = "model-row-tampered"
        elif column == "reasoning":
            tampered_value = "thinking"
        elif column == "deployment_version":
            tampered_value = "deployment-row-tampered"
        elif column == "state_version_after":
            tampered_value = 2
        elif column == "preflight_status":
            tampered_value = "failed"
        elif column == "state_committed":
            tampered_value = 0
        elif column == "execution_status":
            tampered_value = "failed"
        elif column == "response_id":
            tampered_value = "response-row-tampered"
        elif column == "error_code":
            tampered_value = "error-row-tampered"
        elif column == "executed_provider":
            tampered_value = "executed-provider-row-tampered"
        elif column == "executed_model":
            tampered_value = "executed-model-row-tampered"
        elif column == "executed_deployment_version":
            tampered_value = "executed-deployment-row-tampered"
        else:
            tampered_value = {"input_tokens": 1}
        sqlite_value = (
            json.dumps(tampered_value) if isinstance(tampered_value, dict) else tampered_value
        )
        async with storage._write_transaction("test_corrupt_fixed_route_row") as conn:
            await conn.execute(
                f"UPDATE fixed_four_tier_decisions SET {column} = ? WHERE route_id = ?",
                (sqlite_value, decision.route_id),
            )

        persisted_route_id = str(tampered_value) if column == "route_id" else decision.route_id
        error = "persisted four_tier_mapping route trace is incompatible"
        with pytest.raises(ValueError, match=error):
            await storage.get_fixed_four_tier_decision_by_route(persisted_route_id)
        if column == "state_committed":
            history = await manager.list_recent_fixed_four_tier_decisions(
                session_id=(str(tampered_value) if column == "session_id" else session.session_id),
                session_epoch=(int(tampered_value) if column == "session_epoch" else session.epoch),
                since_ms=0,
                before_ms=2_000,
            )
            assert history == []
        else:
            with pytest.raises(ValueError, match=error):
                await manager.list_recent_fixed_four_tier_decisions(
                    session_id=(
                        str(tampered_value) if column == "session_id" else session.session_id
                    ),
                    session_epoch=(
                        int(tampered_value) if column == "session_epoch" else session.epoch
                    ),
                    since_ms=0,
                    before_ms=2_000,
                )
    finally:
        await storage.close()


@pytest.mark.parametrize("legacy_schema", ["fixed-four-tier-v2-mock-v2", "fixed-four-tier-v2-v3"])
async def test_fixed_route_replay_accepts_legacy_trace_with_matching_row(
    legacy_schema: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-legacy-row-trace")
        claim = _claim(
            session,
            claim_id="claim-legacy-row-trace",
            execution_id="execution-legacy-row-trace",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        committed_trace = _committed_trace(decision.route_trace, state_version=1)
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
            route_trace=committed_trace,
            updated_at_ms=1_500,
        )
        legacy_trace = dict(committed_trace)
        legacy_trace["schema_version"] = legacy_schema
        legacy_trace.pop("quality_escalation_used", None)
        legacy_trace.pop("quality_escalation_reason", None)
        if legacy_schema == "fixed-four-tier-v2-mock-v2":
            legacy_trace.pop("classifier_backend")
            legacy_trace.pop("classifier_identity")
            for field_name in (
                "session_id",
                "session_epoch",
                "claim_id",
                "execution_id",
                "session_key_hash",
                "input_message_id",
                "task_start_input_message_id",
                "redo_parent_route_id",
                "state_version_before",
                "provider",
                "model",
                "reasoning",
                "deployment_version",
            ):
                legacy_trace.pop(field_name)
        async with storage._write_transaction("test_seed_legacy_fixed_route") as conn:
            await conn.execute(
                """
                UPDATE fixed_four_tier_decisions
                SET config_version = ?, route_trace = ?
                WHERE route_id = ?
                """,
                (
                    legacy_schema,
                    json.dumps(legacy_trace),
                    decision.route_id,
                ),
            )

        restored = await storage.get_fixed_four_tier_decision_by_route(decision.route_id)
        history = await manager.list_recent_fixed_four_tier_decisions(
            session_id=session.session_id,
            session_epoch=session.epoch,
            since_ms=0,
            before_ms=2_000,
        )

        assert restored is not None
        assert restored.route_trace["schema_version"] == legacy_schema
        assert [record.route_id for record in history] == [decision.route_id]
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


@pytest.mark.parametrize(
    ("record_field", "trace_field", "invalid_value"),
    [
        ("selected_provider", "provider", {"invalid": True}),
        ("state_version_before", "state_version_before", "7"),
        ("task_start_input_message_id", "task_start_input_message_id", 7),
    ],
)
async def test_stage_rejects_matching_but_invalid_supplemental_types(
    record_field: str,
    trace_field: str,
    invalid_value: Any,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create(f"agent:main:fixed-invalid-type-{record_field}")
        claim = _claim(
            session,
            claim_id=f"claim-invalid-type-{record_field}",
            execution_id=f"execution-invalid-type-{record_field}",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        setattr(decision, record_field, invalid_value)
        decision.route_trace[trace_field] = invalid_value

        with pytest.raises(ValueError, match="invalid field types|route trace is incompatible"):
            await storage.stage_fixed_four_tier_decision(decision)

        assert await storage.get_fixed_four_tier_decision_by_route(decision.route_id) is None
    finally:
        await storage.close()


async def test_claim_and_state_commit_strictly_revalidate_mutated_models() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-strict-model-revalidation")
        invalid_claim = _claim(
            session,
            claim_id="claim-invalid-schema-bool",
            execution_id="execution-invalid-schema-bool",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        invalid_claim.schema_version = True  # type: ignore[assignment]
        with pytest.raises(ValueError, match="invalid field types"):
            await storage.claim_fixed_four_tier_request(invalid_claim)

        claim = _claim(
            session,
            claim_id="claim-invalid-state-bool",
            execution_id="execution-invalid-state-bool",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
        )
        assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
        decision = _decision(session, claim)
        await storage.stage_fixed_four_tier_decision(decision)
        state = FixedFourTierState(
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
        )
        state.version = True  # type: ignore[assignment]
        with pytest.raises(ValueError, match="invalid field types"):
            await storage.commit_fixed_four_tier_decision(
                route_id=decision.route_id,
                state=state,
                expected_version=None,
                route_trace=_committed_trace(decision.route_trace, state_version=1),
                updated_at_ms=1_500,
            )
    finally:
        await storage.close()


async def test_expired_claim_cannot_settle_another_claims_decision() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        session = await manager.create("agent:main:fixed-cross-claim-recovery")
        owner_claim = _claim(
            session,
            claim_id="claim-owner",
            execution_id="execution-owner",
            claimed_at_ms=1_000,
            lease_expires_at_ms=20_000,
            request_id="request-owner",
        )
        assert (await storage.claim_fixed_four_tier_request(owner_claim))[0] is True
        owner_decision = _decision(session, owner_claim)
        await storage.stage_fixed_four_tier_decision(owner_decision)

        foreign_claim = _claim(
            session,
            claim_id="claim-foreign",
            execution_id="execution-foreign",
            claimed_at_ms=1_000,
            lease_expires_at_ms=2_000,
            request_id="request-foreign",
        )
        assert (await storage.claim_fixed_four_tier_request(foreign_claim))[0] is True
        async with storage._write_transaction("test_cross_bind_expired_claim") as conn:
            await conn.execute(
                """
                UPDATE fixed_four_tier_request_claims
                SET status = 'materialized', route_id = ?
                WHERE claim_id = ?
                """,
                (owner_decision.route_id, foreign_claim.claim_id),
            )

        foreign_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=foreign_claim.request_id,
            now_ms=2_001,
        )
        owner_view = await storage.get_fixed_four_tier_decision_by_route(owner_decision.route_id)
        owner_claim_view = await storage.reconcile_stale_fixed_four_tier_request(
            session_id=session.session_id,
            request_id=owner_claim.request_id,
            now_ms=2_001,
        )

        assert foreign_view is not None
        assert foreign_view.status == "failed"
        assert foreign_view.error_code == "execution_decision_mismatch"
        assert owner_view is not None
        assert owner_view.execution_status == "pending"
        assert owner_view.claim_id == owner_claim.claim_id
        assert owner_claim_view is not None
        assert owner_claim_view.status == "materialized"
    finally:
        await storage.close()


async def test_startup_reconciliation_rolls_back_claim_and_decision_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "fixed-route-startup-rollback.db"
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage)
    session = await manager.create("agent:main:fixed-startup-rollback")
    claim = _claim(
        session,
        claim_id="claim-startup-rollback",
        execution_id="execution-startup-rollback",
        claimed_at_ms=1_000,
        lease_expires_at_ms=2_000,
    )
    assert (await storage.claim_fixed_four_tier_request(claim))[0] is True
    decision = _decision(session, claim)
    await storage.stage_fixed_four_tier_decision(decision)
    await storage.close()

    original_validate = storage_module._validate_fixed_four_tier_decision_trace
    validation_calls = 0

    def fail_prospective_validation(
        trace: object,
        *,
        persisted_row: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal validation_calls
        validation_calls += 1
        validated = original_validate(trace, persisted_row=persisted_row)
        if validation_calls == 2:
            raise ValueError("forced prospective validation failure")
        return validated

    monkeypatch.setattr(
        storage_module,
        "_validate_fixed_four_tier_decision_trace",
        fail_prospective_validation,
    )
    restarted = SessionStorage(str(db_path))
    with pytest.raises(ValueError, match="forced prospective validation failure"):
        await restarted.connect()
    await restarted.close()

    with sqlite3.connect(db_path) as raw_conn:
        claim_status = raw_conn.execute(
            "SELECT status FROM fixed_four_tier_request_claims WHERE claim_id = ?",
            (claim.claim_id,),
        ).fetchone()
        decision_status = raw_conn.execute(
            "SELECT execution_status FROM fixed_four_tier_decisions WHERE route_id = ?",
            (decision.route_id,),
        ).fetchone()
    assert claim_status == ("materialized",)
    assert decision_status == ("pending",)


async def _quality_committed_storage() -> tuple[Any, Any, Any, Any]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session = await manager.create("agent:main:quality-commit")
    claim = _claim(
        session,
        claim_id="quality-claim",
        execution_id="quality-execution",
        claimed_at_ms=1000,
        lease_expires_at_ms=20000,
    )
    assert (await storage.claim_fixed_four_tier_request(claim))[0]
    record = _decision(session, claim)
    assert record.final_tier == "c1"
    await storage.stage_fixed_four_tier_decision(record)
    state = FixedFourTierState(
        session_id=session.session_id,
        session_key=session.session_key,
        session_epoch=session.epoch,
        version=1,
        task_id=record.task_id,
        tier=record.final_tier,
        task_turn_count=record.task_turn_index + 1,
        task_start_input_message_id=record.task_start_input_message_id,
        last_request_id=record.request_id,
        last_route_id=record.route_id,
        updated_at_ms=2000,
    )
    await storage.commit_fixed_four_tier_decision(
        route_id=record.route_id,
        state=state,
        expected_version=None,
        route_trace=_committed_trace(record.route_trace, state_version=1),
        updated_at_ms=2000,
    )
    return storage, manager, session, record


def _quality_receipt() -> dict[str, Any]:
    return {
        "from_tier": "c1",
        "to_tier": "c2",
        "reason": "validation_failure",
        "used": True,
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "reasoning": "max",
        "deployment_version": "deepseek-v4-pro-0731",
        "additional_requests_reserved": 1,
        "budget_remaining": 0,
        "preflight": "passed",
    }


async def test_quality_upgrade_is_atomic_once_and_retains_original_decision() -> None:
    storage, manager, session, record = await _quality_committed_storage()
    try:
        from opensquilla.engine.routing.fixed_four_tier_v2 import FIXED_FOUR_TIER_DEPLOYMENT_SPECS

        receipt = _quality_receipt()
        receipt["deployment_version"] = next(
            spec[4] for spec in FIXED_FOUR_TIER_DEPLOYMENT_SPECS if spec[0] == "c2"
        )
        upgraded = await manager.commit_fixed_four_tier_quality_upgrade(
            route_id=record.route_id, expected_version=1, quality_retry=receipt, updated_at_ms=3000
        )
        assert upgraded.tier == "c2" and upgraded.version == 2 and upgraded.task_turn_count == 1
        assert upgraded.task_id == record.task_id
        saved = await storage.get_fixed_four_tier_decision_by_route(record.route_id)
        assert saved.final_tier == "c1" and saved.route_trace["quality_retry"] == receipt
        with pytest.raises(FixedFourTierStateConflictError):
            await storage.commit_fixed_four_tier_quality_upgrade(
                route_id=record.route_id,
                expected_version=2,
                quality_retry=receipt,
                updated_at_ms=3001,
            )
        assert (await storage.get_fixed_four_tier_state(session.session_id)).tier == "c2"
        changed = dict(saved.route_trace)
        del changed["quality_retry"]
        with pytest.raises(FixedFourTierStateConflictError, match="immutable"):
            await storage.settle_fixed_four_tier_decision(
                route_id=record.route_id,
                execution_status="succeeded",
                route_trace=changed,
                updated_at_ms=4000,
            )
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "mutation", ["jump", "deployment", "schema", "budget_bool", "already_used"]
)
async def test_quality_upgrade_invalid_evidence_keeps_state_unchanged(mutation: str) -> None:
    storage, manager, session, record = await _quality_committed_storage()
    try:
        from opensquilla.engine.routing.fixed_four_tier_v2 import FIXED_FOUR_TIER_DEPLOYMENT_SPECS

        receipt = _quality_receipt()
        receipt["deployment_version"] = next(
            spec[4] for spec in FIXED_FOUR_TIER_DEPLOYMENT_SPECS if spec[0] == "c2"
        )
        if mutation == "jump":
            receipt["to_tier"] = "c3"
        if mutation == "deployment":
            receipt["model"] = "unapproved/model"
        if mutation == "budget_bool":
            receipt["budget_remaining"] = False
        if mutation == "schema":
            receipt["unexpected"] = True
        if mutation == "already_used":
            # A stale expected version must fail before touching either table.
            expected_version = 2
        else:
            expected_version = 1
        with pytest.raises((ValueError, FixedFourTierStateConflictError)):
            await storage.commit_fixed_four_tier_quality_upgrade(
                route_id=record.route_id,
                expected_version=expected_version,
                quality_retry=receipt,
                updated_at_ms=3000,
            )
        current = await storage.get_fixed_four_tier_state(session.session_id)
        assert current.tier == "c1" and current.version == 1
        saved = await storage.get_fixed_four_tier_decision_by_route(record.route_id)
        assert "quality_retry" not in saved.route_trace
        injected = {**saved.route_trace, "quality_retry": receipt}
        with pytest.raises(FixedFourTierStateConflictError, match="immutable"):
            await storage.settle_fixed_four_tier_decision(
                route_id=record.route_id,
                execution_status="failed",
                route_trace=injected,
                updated_at_ms=4000,
            )
    finally:
        await storage.close()


async def test_quality_upgrade_rejects_legacy_v3_receipt() -> None:
    storage, manager, session, record = await _quality_committed_storage()
    try:
        saved = await storage.get_fixed_four_tier_decision_by_route(record.route_id)
        legacy_trace = dict(saved.route_trace)
        legacy_trace["schema_version"] = "fixed-four-tier-v2-v3"
        legacy_trace.pop("quality_escalation_reason")
        legacy_trace.pop("quality_escalation_used")
        await storage.conn.execute(
            "UPDATE fixed_four_tier_decisions SET route_trace = ?, config_version = ? "
            "WHERE route_id = ?",
            (json.dumps(legacy_trace), "fixed-four-tier-v2-v3", record.route_id),
        )
        await storage.conn.commit()
        assert (
            await storage.get_fixed_four_tier_decision_by_route(record.route_id)
        ).config_version == "fixed-four-tier-v2-v3"
        with pytest.raises(ValueError):
            await storage.commit_fixed_four_tier_quality_upgrade(
                route_id=record.route_id,
                expected_version=1,
                quality_retry=_quality_receipt(),
                updated_at_ms=3000,
            )
        current = await storage.get_fixed_four_tier_state(session.session_id)
        assert current.tier == "c1" and current.version == 1
    finally:
        await storage.close()
