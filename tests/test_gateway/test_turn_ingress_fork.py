"""Atomic RPC contracts for WebChat prefix-fork turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_sessions import _fixed_redo_anchor_payload
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    FixedFourTierDecisionRecord,
    FixedFourTierRequestClaim,
    FixedFourTierState,
)
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope

PARENT_KEY = "agent:main:webchat:atomic-fork"
CLIENT_REQUEST_ID = "atomic-fork-request"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


@dataclass
class _ForkStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    handler_started: asyncio.Event
    release_handler: asyncio.Event

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_fork_stack(
    db_path: Path,
    *,
    fixed_four_tier_v2: bool = False,
    naming_enabled: bool = False,
) -> AsyncIterator[_ForkStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def _turn_handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="atomic-fork-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": naming_enabled},
            **(
                {
                    "llm_ensemble": {
                        "enabled": True,
                        "mode": "single",
                        "selection_mode": "four_tier_mapping",
                    }
                }
                if fixed_four_tier_v2
                else {}
            ),
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    stack = _ForkStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        handler_started=handler_started,
        release_handler=release_handler,
    )
    try:
        yield stack
    finally:
        release_handler.set()
        for reservations in list(runtime._reservations_by_session.values()):
            for reservation in list(reservations):
                await runtime.abort_reservation(reservation)
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def _seed_parent(stack: _ForkStack) -> str:
    await stack.manager.create(
        PARENT_KEY,
        agent_id="main",
        display_name="Atomic fork parent",
    )
    with turn_context_scope(
        {
            "turn_id": "parent-turn-a",
            "client_message_id": "parent-message-a",
            "surface_id": "web:parent",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
    ):
        await stack.manager.append_message(PARENT_KEY, "user", "A marker")
    middle = await stack.manager.append_message(PARENT_KEY, "assistant", "B marker")
    await stack.manager.append_message(PARENT_KEY, "user", "C marker")
    return middle.message_id


async def _seed_fixed_redo_parent(
    stack: _ForkStack,
    *,
    execution_status: str = "succeeded",
    trace_committed: bool = True,
    state_committed: bool = True,
    task_turn_index: int = 0,
) -> str:
    from opensquilla.engine.routing.fixed_four_tier_v2 import (
        FixedFourTierTaskState,
        FixedFourTierV2Router,
        RoutingRequest,
    )

    parent = await stack.manager.create(
        PARENT_KEY,
        agent_id="main",
        display_name="Fixed-v2 redo parent",
    )
    task_start = None
    if task_turn_index > 0:
        task_start = await stack.manager.append_message(
            PARENT_KEY,
            "user",
            "earlier task request",
        )
        await stack.manager.append_message(PARENT_KEY, "assistant", "earlier answer")
    anchor = await stack.manager.append_message(PARENT_KEY, "user", "repeat exactly")
    await stack.manager.append_message(PARENT_KEY, "assistant", "parent answer")
    task_start = task_start or anchor
    route_id = "route-parent"
    claim_id = "claim-parent"
    now_ms = time.time_ns() // 1_000_000
    acquired, _claim = await stack.storage.claim_fixed_four_tier_request(
        FixedFourTierRequestClaim(
            claim_id=claim_id,
            session_id=parent.session_id,
            session_key=parent.session_key,
            session_epoch=parent.epoch,
            request_id="request-parent",
            execution_id="execution-parent",
            input_message_id=anchor.message_id,
            claimed_at_ms=now_ms,
            updated_at_ms=now_ms,
            lease_expires_at_ms=now_ms + 60_000,
        )
    )
    assert acquired is True
    prior_state = (
        FixedFourTierTaskState(
            task_id="task-parent",
            tier="c1",
            turn_count=task_turn_index,
            version=0,
            task_start_input_message_id=task_start.message_id,
        )
        if task_turn_index > 0
        else None
    )
    core_decision, next_state = FixedFourTierV2Router(
        mock_seed=43,
        route_id_factory=lambda: route_id,
        task_id_factory=lambda: "task-parent",
        clock_ms=lambda: now_ms + 1,
    ).decide(
        RoutingRequest(
            session_id=parent.session_id,
            request_id="request-parent",
            message="repeat exactly",
            input_message_id=anchor.message_id,
            control_event="redo" if prior_state is not None else "new_task",
        ),
        prior_state,
    )
    route_trace = core_decision.trace(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
    )
    route_trace["state_committed"] = False
    await stack.storage.stage_fixed_four_tier_decision(
        FixedFourTierDecisionRecord(
            route_id=core_decision.route_id,
            session_id=parent.session_id,
            session_key=parent.session_key,
            session_epoch=parent.epoch,
            claim_id=claim_id,
            request_id="request-parent",
            execution_id="execution-parent",
            input_message_id=anchor.message_id,
            task_id=core_decision.task_id,
            intent=core_decision.intent.trace(),
            tier=core_decision.tier.trace(),
            previous_tier=core_decision.previous_tier,
            final_tier=core_decision.final_tier,
            task_turn_index=core_decision.task_turn_index,
            task_start_input_message_id=next_state.task_start_input_message_id,
            context_action=core_decision.context_action,
            selected_provider="openrouter",
            selected_model="deepseek/deepseek-v4-flash",
            config_version=core_decision.schema_version,
            route_trace=route_trace,
            decided_at_ms=core_decision.decided_at_ms,
            updated_at_ms=now_ms + 1,
        )
    )
    if state_committed:
        await stack.storage.commit_fixed_four_tier_decision(
            route_id=route_id,
            state=FixedFourTierState(
                session_id=parent.session_id,
                session_key=parent.session_key,
                session_epoch=parent.epoch,
                version=1,
                task_id=core_decision.task_id,
                tier=core_decision.final_tier,
                task_turn_count=task_turn_index + 1,
                task_start_input_message_id=task_start.message_id,
                last_request_id="request-parent",
                last_route_id=route_id,
                updated_at_ms=now_ms + 2,
            ),
            expected_version=None,
            route_trace={**route_trace, "state_committed": trace_committed},
            updated_at_ms=now_ms + 2,
        )
    if execution_status != "pending":
        await stack.storage.settle_fixed_four_tier_decision(
            route_id=route_id,
            execution_status=execution_status,
            preflight_status="failed" if not state_committed else None,
            response_id="response-parent",
            route_trace=(
                {
                    **route_trace,
                    "state_committed": False,
                    "preflight": {"status": "failed"},
                }
                if not state_committed
                else None
            ),
            updated_at_ms=now_ms + 3,
        )
    return anchor.message_id


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sessions",
                "transcript_entries",
                "agent_tasks",
                "turn_ingress_receipts",
            )
        }
    finally:
        connection.close()


def _fork_params(
    fork_before_message_id: str,
    *,
    fork_param_name: str = "forkBeforeMessageId",
) -> dict[str, str]:
    return {
        "sessionKey": PARENT_KEY,
        "message": "B edited",
        fork_param_name: fork_before_message_id,
        "clientRequestId": CLIENT_REQUEST_ID,
    }


def _fixed_redo_params(
    anchor_message_id: str,
    *,
    message: str = "repeat exactly",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sessionKey": PARENT_KEY,
        "message": message,
        "forkBeforeMessageId": anchor_message_id,
        "clientRequestId": CLIENT_REQUEST_ID,
        "routingControl": {
            "mode": "four_tier_mapping",
            "intent": "redo",
            "redoOfMessageId": anchor_message_id,
        },
    }
    if attachments is not None:
        params["attachments"] = attachments
    return params


def test_fixed_redo_anchor_parser_does_not_reinterpret_plain_json_text() -> None:
    plain_json = '{"text":"not an attachment envelope","other":true}'

    assert _fixed_redo_anchor_payload(plain_json) == (plain_json, False)


def test_fixed_redo_anchor_parser_recognizes_only_canonical_attachment_envelopes() -> None:
    envelope = '{"text":"repeat exactly","attachments":[{"name":"photo.png","mime":"image/png"}]}'

    assert _fixed_redo_anchor_payload(envelope) == ("repeat exactly", True)


@pytest.mark.parametrize("legacy_mode", ["fixed_four_tier_v2", "fixed-four-tier-v2"])
def test_fixed_redo_control_rejects_development_mode_names(legacy_mode: str) -> None:
    with pytest.raises(ValueError, match="four_tier_mapping"):
        rpc_sessions._fixed_four_tier_routing_control(
            {
                "routingControl": {
                    "mode": legacy_mode,
                    "intent": "redo",
                    "redoOfMessageId": "anchor",
                }
            },
            fork_before_message_id="anchor",
        )


@pytest.mark.asyncio
async def test_fixed_v2_redo_accepts_only_exact_settled_committed_parent_route(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(stack)

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        child_key = response.payload["sessionKey"]
        assert child_key != PARENT_KEY
        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == ["repeat exactly"]
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert task.details["metadata"]["fixed_four_tier_v2_control_event"] == "redo"
        assert task.details["metadata"]["fixed_four_tier_v2_redo_parent_session_key"] == PARENT_KEY
        assert task.details["metadata"]["fixed_four_tier_v2_redo_of_message_id"] == (
            anchor_message_id
        )
        assert task.details["input_provenance"]["action"] == "redo"
        assert task.details["input_provenance"]["source"] == "web_regenerate"


@pytest.mark.asyncio
async def test_fixed_v2_multiturn_redo_maps_parent_task_start_into_child(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo-multiturn.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(
            stack,
            task_turn_index=1,
        )
        parent_entries = await stack.manager.get_transcript(PARENT_KEY)
        parent_task_start_id = parent_entries[0].message_id

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-multiturn",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        child_key = response.payload["sessionKey"]
        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == [
            "earlier task request",
            "earlier answer",
            "repeat exactly",
        ]
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        child_task_start_id = task.details["metadata"][
            "fixed_four_tier_v2_redo_child_task_start_input_message_id"
        ]
        assert child_task_start_id == child_entries[0].message_id
        assert child_task_start_id != parent_task_start_id
        cached_envelope = stack.runtime._last_envelope_by_session[child_key]
        assert (
            "fixed_four_tier_v2_redo_child_task_start_input_message_id"
            not in cached_envelope.metadata
        )


@pytest.mark.asyncio
async def test_fixed_v2_multiturn_redo_fails_before_acceptance_without_child_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo-mapping-missing.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(
            stack,
            task_turn_index=1,
        )
        prepare_prefix_branch = stack.manager.prepare_prefix_branch

        async def _prepare_without_mapping(*args: Any, **kwargs: Any) -> Any:
            plan = await prepare_prefix_branch(*args, **kwargs)
            return replace(plan, source_to_child_message_ids=())

        monkeypatch.setattr(
            stack.manager,
            "prepare_prefix_branch",
            _prepare_without_mapping,
        )

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-mapping-missing",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "FOUR_TIER_MAPPING_REDO_TASK_BOUNDARY_UNAVAILABLE"
        assert response.error.accepted is False
        assert len(await stack.storage.list_sessions()) == 1
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_status", "trace_committed", "state_committed", "expected_code"),
    [
        ("pending", True, True, "FOUR_TIER_MAPPING_REDO_PARENT_BUSY"),
        ("succeeded", False, True, "FOUR_TIER_MAPPING_REDO_ROUTE_UNAVAILABLE"),
        ("failed", False, False, "FOUR_TIER_MAPPING_REDO_ROUTE_UNAVAILABLE"),
    ],
)
async def test_fixed_v2_redo_rejects_unsettled_or_uncommitted_parent_route(
    tmp_path: Path,
    execution_status: str,
    trace_committed: bool,
    state_committed: bool,
    expected_code: str,
) -> None:
    async with _open_fork_stack(
        tmp_path / f"fixed-redo-{execution_status}-{trace_committed}.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(
            stack,
            execution_status=execution_status,
            trace_committed=trace_committed,
            state_committed=state_committed,
        )
        if not state_committed:
            parent = await stack.storage.get_session(PARENT_KEY)
            assert parent is not None
            assert (
                await stack.storage.get_fixed_four_tier_decision_by_input_message(
                    session_id=parent.session_id,
                    input_message_id=anchor_message_id,
                )
                is None
            )

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-rejected",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == expected_code
        assert response.error.accepted is False
        assert len(await stack.storage.list_sessions()) == 1
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
async def test_fixed_v2_redo_rejects_a_busy_parent_without_creating_a_child(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo-busy-parent.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(stack)
        await stack.runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="busy-parent-test",
                agent_id="main",
                session_key=PARENT_KEY,
                input_provenance={"kind": "test"},
            ),
            "parent still running",
        )
        await stack.wait_until_running()

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-busy-parent",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "FOUR_TIER_MAPPING_REDO_PARENT_BUSY"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert len(await stack.storage.list_sessions()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "attachments", "expected_code"),
    [
        ("changed prompt", None, "FOUR_TIER_MAPPING_REDO_TEXT_MISMATCH"),
        (
            "repeat exactly",
            [{"type": "image/png", "name": "photo.png", "data": "AA=="}],
            "FOUR_TIER_MAPPING_REDO_ATTACHMENTS_UNSUPPORTED",
        ),
    ],
)
async def test_fixed_v2_redo_fails_closed_on_text_or_attachment_change(
    tmp_path: Path,
    message: str,
    attachments: list[dict[str, Any]] | None,
    expected_code: str,
) -> None:
    async with _open_fork_stack(
        tmp_path / f"fixed-redo-{expected_code}.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(stack)

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-invalid-payload",
            "chat.send",
            _fixed_redo_params(
                anchor_message_id,
                message=message,
                attachments=attachments,
            ),
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == expected_code
        assert response.error.accepted is False
        assert len(await stack.storage.list_sessions()) == 1
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
async def test_fixed_v2_redo_fails_before_acceptance_without_atomic_prefix_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo-atomic-unavailable.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(stack)
        monkeypatch.setattr(stack.manager, "prepare_prefix_branch", None)

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-atomic-unavailable",
            "chat.send",
            _fixed_redo_params(anchor_message_id),
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "FOUR_TIER_MAPPING_REDO_ATOMIC_UNAVAILABLE"
        assert response.error.retryable is False
        assert response.error.accepted is False
        assert len(await stack.storage.list_sessions()) == 1
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
async def test_fixed_redo_control_is_ignored_when_fixed_mode_is_not_active(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(tmp_path / "legacy-control-isolation.db") as stack:
        fork_before_message_id = await _seed_parent(stack)
        params: dict[str, Any] = _fork_params(fork_before_message_id)
        params["routingControl"] = {
            "mode": "four_tier_mapping",
            "intent": "redo",
            "redoOfMessageId": fork_before_message_id,
        }

        response = await get_dispatcher().dispatch(
            "rpc-legacy-fork-with-control",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        child_entries = await stack.manager.get_transcript(response.payload["sessionKey"])
        assert [entry.content for entry in child_entries] == ["A marker", "B edited"]
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert "fixed_four_tier_v2_control_event" not in task.details["metadata"]
        assert task.details["input_provenance"].get("action") != "redo"


@pytest.mark.asyncio
async def test_public_sessions_send_cannot_forge_web_fixed_redo_capability(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(
        tmp_path / "fixed-redo-forged-source.db",
        fixed_four_tier_v2=True,
    ) as stack:
        anchor_message_id = await _seed_fixed_redo_parent(stack)
        params = _fixed_redo_params(anchor_message_id)
        params["key"] = params.pop("sessionKey")
        params["_source"] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
            "source_kind": "webui",
        }

        response = await get_dispatcher().dispatch(
            "rpc-fixed-redo-forged-source",
            "sessions.send",
            params,
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "INVALID_REQUEST"
        assert len(await stack.storage.list_sessions()) == 1
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixed_four_tier_v2", "expected_naming_calls"),
    [(True, 0), (False, 1)],
)
async def test_fixed_v2_suppresses_the_extra_naming_llm_only_for_its_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_four_tier_v2: bool,
    expected_naming_calls: int,
) -> None:
    naming_llm = AsyncMock()
    monkeypatch.setattr(rpc_sessions, "generate_session_title", naming_llm)
    async with _open_fork_stack(
        tmp_path / f"naming-{fixed_four_tier_v2}.db",
        fixed_four_tier_v2=fixed_four_tier_v2,
        naming_enabled=True,
    ) as stack:
        await stack.manager.create(
            PARENT_KEY,
            agent_id="main",
            display_name="WebChat",
        )

        response = await get_dispatcher().dispatch(
            "rpc-fixed-naming-gate",
            "chat.send",
            {
                "sessionKey": PARENT_KEY,
                "message": "first user request",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )
        await stack.wait_until_running()
        await asyncio.sleep(0)

        assert response.ok is True
        assert naming_llm.await_count == expected_naming_calls


@pytest.mark.asyncio
async def test_atomic_acceptance_freezes_routing_config_before_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_fork_stack(
        tmp_path / "routing-config-acceptance-snapshot.db",
        fixed_four_tier_v2=True,
    ) as stack:
        await stack.manager.create(PARENT_KEY, agent_id="main")
        seen_configs: list[Any] = []

        async def _turn_handler(run: Any) -> None:
            seen_configs.append(run.accepted_config)
            stack.handler_started.set()
            await stack.release_handler.wait()

        stack.runtime._turn_handler = _turn_handler
        accept_turn = stack.storage.accept_turn

        async def _accept_then_change_live_config(*args: Any, **kwargs: Any) -> Any:
            result = await accept_turn(*args, **kwargs)
            stack.context.config.llm_ensemble.enabled = False
            return result

        monkeypatch.setattr(stack.storage, "accept_turn", _accept_then_change_live_config)

        response = await get_dispatcher().dispatch(
            "rpc-routing-config-acceptance-snapshot",
            "chat.send",
            {
                "sessionKey": PARENT_KEY,
                "message": "accepted under fixed routing",
                "clientRequestId": "routing-config-acceptance-snapshot",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert stack.context.config.llm_ensemble.enabled is False
        assert len(seen_configs) == 1
        assert seen_configs[0].llm_ensemble.enabled is True
        assert seen_configs[0].llm_ensemble.selection_mode == "four_tier_mapping"


@pytest.mark.asyncio
@pytest.mark.parametrize("fork_param_name", ["forkBeforeMessageId", "fork_before_message_id"])
async def test_chat_send_fork_atomically_accepts_child_prefix_message_task_and_receipt(
    tmp_path: Path,
    fork_param_name: str,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)

        response = await get_dispatcher().dispatch(
            "rpc-fork-success",
            "chat.send",
            _fork_params(fork_before_message_id, fork_param_name=fork_param_name),
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["replayed"] is False
        child_key = response.payload["sessionKey"]
        assert child_key != PARENT_KEY

        parent_entries = await stack.manager.get_transcript(PARENT_KEY)
        assert [entry.content for entry in parent_entries] == [
            "A marker",
            "B marker",
            "C marker",
        ]
        child = await stack.storage.get_session(child_key)
        assert child is not None
        assert child.parent_session_key == PARENT_KEY
        assert child.forked_from_parent is True
        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == ["A marker", "B edited"]
        assert child_entries[0].turn_context == {
            "turn_id": "parent-turn-a",
            "client_message_id": "parent-message-a",
            "surface_id": "web:parent",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert child_entries[-1].turn_context == {
            "turn_id": response.payload["task_id"],
            "client_message_id": response.payload["client_message_id"],
            "surface_id": response.payload["surface_id"],
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert child_entries[-1].message_id == response.payload["message_id"]
        assert response.payload["user_message_id"] == response.payload["message_id"]
        assert response.payload["turn_id"] == response.payload["task_id"]
        assert isinstance(response.payload["client_message_id"], str)
        assert response.payload["client_message_id"]
        assert response.payload["surface_id"].startswith("webchat:")

        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task.session_key == child_key
        assert task.details["persisted_user_message_id"] == child_entries[-1].message_id
        assert task.details["fresh_user_session"] is False
        receipt = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=PARENT_KEY,
            client_request_id=CLIENT_REQUEST_ID,
        )
        assert receipt is not None
        assert receipt.receipt.accepted_session_key == child_key
        assert receipt.receipt.session_id == child.session_id
        assert receipt.receipt.message_id == child_entries[-1].message_id
        assert receipt.receipt.task_id == task.task_id
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 5,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_chat_send_fork_replays_same_child_without_duplicate_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)
        params = _fork_params(fork_before_message_id)

        first = await get_dispatcher().dispatch(
            "rpc-fork-first",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()
        replay = await get_dispatcher().dispatch(
            "rpc-fork-replay",
            "chat.send",
            params,
            stack.context,
        )

        assert first.ok is True
        assert replay.ok is True
        assert replay.payload["accepted"] is True
        assert replay.payload["replayed"] is True
        assert replay.payload["sessionKey"] == first.payload["sessionKey"]
        assert replay.payload["session_id"] == first.payload["session_id"]
        assert replay.payload["message_id"] == first.payload["message_id"]
        assert replay.payload["task_id"] == first.payload["task_id"]
        assert [
            entry.content
            for entry in await stack.manager.get_transcript(replay.payload["sessionKey"])
        ] == ["A marker", "B edited"]
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 5,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_chat_send_fork_storage_busy_leaves_no_child_turn_or_reservation(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)
        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(stack.db_path, isolation_level=None, timeout=0.0)
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-fork-busy",
                "chat.send",
                _fork_params(fork_before_message_id),
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            assert response.error.retry_after_ms is not None
            assert [entry.content for entry in await stack.manager.get_transcript(PARENT_KEY)] == [
                "A marker",
                "B marker",
                "C marker",
            ]
            assert _table_counts(stack.db_path) == {
                "sessions": 1,
                "transcript_entries": 3,
                "agent_tasks": 0,
                "turn_ingress_receipts": 0,
            }
            assert stack.runtime._reservations_by_session == {}
            assert stack.runtime._tasks == {}
            assert stack.runtime._pending_by_session == {}
            assert stack.runtime._running_by_session == {}
            assert stack.handler_started.is_set() is False
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()
