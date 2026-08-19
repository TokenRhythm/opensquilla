from __future__ import annotations

import inspect
from typing import Any

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import capture_model_routing_config
from opensquilla.gateway.session_model_routing import accepted_model_routing_audit
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    SessionStatus,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage


async def _seed_steering_input(
    storage: SessionStorage,
    *,
    session_key: str,
    session_id: str,
    task_id: str,
    message_id: str,
    message: str,
    task_status: AgentTaskStatus,
    task_details: dict[str, Any] | None = None,
) -> None:
    await storage.upsert_session(
        SessionNode(
            session_key=session_key,
            session_id=session_id,
            agent_id="main",
            status=SessionStatus.RUNNING,
            created_at=100,
            updated_at=100,
            started_at=100,
        )
    )
    await storage.create_agent_task(
        AgentTaskRecord(
            task_id=task_id,
            session_key=session_key,
            agent_id="main",
            source_kind="web",
            queue_mode="followup",
            run_kind="web_turn",
            status=task_status,
            created_at=110,
            updated_at=120,
            started_at=120,
            finished_at=130 if task_status == AgentTaskStatus.CANCELLED else None,
            terminal_reason=(
                "cancelled" if task_status == AgentTaskStatus.CANCELLED else None
            ),
            details={
                "source_name": "webui",
                "input_provenance": {"kind": "webui"},
                "metadata": {},
                "session_id": session_id,
                **(task_details or {}),
            },
        )
    )
    await storage.accept_turn(
        TranscriptEntry(
            session_id=session_id,
            session_key=session_key,
            message_id=message_id,
            role="user",
            content=message,
            created_at=140,
            turn_context={
                "turn_id": task_id,
                "target_turn_id": task_id,
                "client_request_id": f"request-{message_id}",
                "client_message_id": f"client-{message_id}",
                "surface_id": "webui",
                "intent": "steer",
                "disposition": "steering",
                "revision": 1,
            },
        ),
        expected_epoch=0,
        updated_at=140,
        task_record=None,
        receipt_task_id=task_id,
        source_scope="rpc:web:steer.v2",
        request_session_key=session_key,
        client_request_id=f"request-{message_id}",
        request_fingerprint=f"fingerprint-{message_id}",
    )


@pytest.mark.asyncio
async def test_restart_promotes_stranded_steer_once_and_moves_receipt(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    session_key = "agent:main:webchat:steer-restart"
    session_id = "session-steer-restart"
    old_task_id = "turn-before-restart"
    message_id = "steer-after-restart"

    first = SessionStorage(str(db_path))
    await first.connect()
    await _seed_steering_input(
        first,
        session_key=session_key,
        session_id=session_id,
        task_id=old_task_id,
        message_id=message_id,
        message="continue with the corrected constraint",
        task_status=AgentTaskStatus.RUNNING,
    )
    await first.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)

    runtime = TaskRuntime(storage=restarted, turn_handler=_handler)
    recovery = await runtime.recover_stranded_steers()
    assert recovery["promoted"] == 1
    assert recovery["resumed"] == 0
    assert len(recovery["task_ids"]) == 1
    promoted_task_id = recovery["task_ids"][0]
    record = await runtime.wait(promoted_task_id, timeout=2)
    assert record.status == AgentTaskStatus.SUCCEEDED
    assert runs == ["continue with the corrected constraint"]

    entry = await restarted.get_canonical_transcript_entry(session_id, message_id)
    assert entry is not None
    assert entry.turn_context == {
        "turn_id": promoted_task_id,
        "target_turn_id": old_task_id,
        "client_request_id": f"request-{message_id}",
        "client_message_id": f"client-{message_id}",
        "surface_id": "webui",
        "intent": "steer",
        "disposition": "promoted",
        "revision": 2,
        "promoted_from_turn_id": old_task_id,
        "promoted_turn_id": promoted_task_id,
        "recovery": "process_restart_followup",
    }
    receipt = await restarted.get_turn_ingress_receipt(
        source_scope="rpc:web:steer.v2",
        request_session_key=session_key,
        client_request_id=f"request-{message_id}",
    )
    assert receipt is not None
    assert receipt.receipt.task_id == promoted_task_id

    repeated = await runtime.recover_stranded_steers()
    assert repeated["promoted"] == 0
    assert repeated["resumed"] == 0
    assert runs == ["continue with the corrected constraint"]
    await runtime.shutdown(cancel=False)
    await restarted.close()


@pytest.mark.asyncio
async def test_restart_promotion_uses_transcript_admission_order_for_same_ms(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "steer-fifo.db"))
    await storage.connect()
    session_key = "agent:main:webchat:steer-restart-fifo"
    session_id = "session-steer-restart-fifo"
    task_id = "turn-before-restart-fifo"
    await _seed_steering_input(
        storage,
        session_key=session_key,
        session_id=session_id,
        task_id=task_id,
        message_id="z-first-admitted",
        message="first correction",
        task_status=AgentTaskStatus.FAILED,
    )
    await storage.accept_turn(
        TranscriptEntry(
            session_id=session_id,
            session_key=session_key,
            message_id="a-second-admitted",
            role="user",
            content="second correction",
            created_at=140,
            turn_context={
                "turn_id": task_id,
                "target_turn_id": task_id,
                "client_request_id": "request-a-second-admitted",
                "client_message_id": "client-a-second-admitted",
                "surface_id": "webui",
                "intent": "steer",
                "disposition": "steering",
                "revision": 1,
            },
        ),
        expected_epoch=0,
        updated_at=141,
        task_record=None,
        receipt_task_id=task_id,
        source_scope="rpc:web:steer.v2",
        request_session_key=session_key,
        client_request_id="request-a-second-admitted",
        request_fingerprint="fingerprint-a-second-admitted",
    )
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    recovery = await runtime.recover_stranded_steers()
    assert recovery["promoted"] == 2
    assert len(recovery["task_ids"]) == 1
    await runtime.wait(recovery["task_ids"][0], timeout=2.0)
    assert runs == ["first correction\n\nsecond correction"]
    await storage.close()


@pytest.mark.asyncio
async def test_user_stopped_stranded_steer_is_cancelled_not_promoted(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    session_key = "agent:main:webchat:steer-user-stop"
    session_id = "session-steer-user-stop"
    task_id = "turn-user-stopped"
    message_id = "steer-user-stopped"
    await _seed_steering_input(
        storage,
        session_key=session_key,
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        message="do not lose this draft",
        task_status=AgentTaskStatus.RUNNING,
        task_details={
            # This marker is persisted before the runtime signals cancellation.
            # Simulate a process death while disposition cleanup is still
            # pending and before the task reaches durable CANCELLED.
            "cancellation_requested": {
                "source": "webui_stop",
                "reason": "user_abort",
            }
        },
    )
    await storage.close()
    storage = SessionStorage(str(db_path))
    await storage.connect()

    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    recovery = await runtime.recover_stranded_steers()
    assert recovery["cancelled"] == 1
    assert recovery["promoted"] == 0
    assert recovery["task_ids"] == []
    assert runs == []

    entry = await storage.get_canonical_transcript_entry(session_id, message_id)
    assert entry is not None
    assert entry.turn_context is not None
    assert entry.turn_context["disposition"] == "cancelled"
    assert entry.turn_context["failure_code"] == "TURN_CANCELLED"
    assert entry.turn_context["recovery"] == "restore_to_composer"
    receipt = await storage.get_turn_ingress_receipt(
        source_scope="rpc:web:steer.v2",
        request_session_key=session_key,
        client_request_id=f"request-{message_id}",
    )
    assert receipt is not None
    assert receipt.receipt.task_id == task_id
    await storage.close()


@pytest.mark.asyncio
async def test_restart_closes_terminal_application_evidence_without_replaying(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "steer-applied-evidence.db"))
    await storage.connect()
    session_key = "agent:main:webchat:steer-applied-evidence"
    session_id = "session-steer-applied-evidence"
    task_id = "turn-steer-applied-evidence"
    message_id = "message-steer-applied-evidence"
    await _seed_steering_input(
        storage,
        session_key=session_key,
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        message="already entered the model call",
        task_status=AgentTaskStatus.FAILED,
        task_details={
            "applied_steer_evidence": [
                {
                    "message_id": message_id,
                    "applied_iteration": 3,
                    "model_call_id": "call-applied-before-storage-failure",
                }
            ]
        },
    )
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    recovery = await runtime.recover_stranded_steers()

    assert recovery["applied"] == 1
    assert recovery["promoted"] == 0
    assert recovery["task_ids"] == []
    assert runs == []
    entry = await storage.get_canonical_transcript_entry(session_id, message_id)
    assert entry is not None
    assert entry.turn_context is not None
    assert entry.turn_context["disposition"] == "applied"
    assert entry.turn_context["applied_iteration"] == 3
    assert (
        entry.turn_context["model_call_id"]
        == "call-applied-before-storage-failure"
    )
    await storage.close()


@pytest.mark.asyncio
async def test_restart_rejects_route_that_cannot_be_safely_reconstructed(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    session_key = "agent:main:channel:steer-restart"
    session_id = "session-channel-steer-restart"
    task_id = "turn-channel-before-restart"
    message_id = "steer-channel-before-restart"
    await _seed_steering_input(
        storage,
        session_key=session_key,
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        message="this channel reply target is not durable",
        task_status=AgentTaskStatus.FAILED,
    )
    await storage.update_agent_task(
        task_id,
        source_kind="channel",
        terminal_reason="provider_failed",
        finished_at=150,
    )

    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    recovery = await runtime.recover_stranded_steers()
    assert recovery["rejected"] == 1
    assert recovery["promoted"] == 0
    assert runs == []
    entry = await storage.get_canonical_transcript_entry(session_id, message_id)
    assert entry is not None
    assert entry.turn_context is not None
    assert entry.turn_context["disposition"] == "rejected"
    assert entry.turn_context["failure_code"] == "STEER_RESTART_ROUTE_UNAVAILABLE"
    assert entry.turn_context["recovery"] == "resend_as_followup"
    await storage.close()


@pytest.mark.asyncio
async def test_restart_resumes_same_promoted_task_if_crash_preceded_activation(
    tmp_path,
) -> None:
    db_path = tmp_path / "sessions.db"
    session_key = "agent:main:webchat:steer-double-restart"
    session_id = "session-steer-double-restart"
    old_task_id = "turn-first-process"
    message_id = "steer-first-process"

    first = SessionStorage(str(db_path))
    await first.connect()
    await _seed_steering_input(
        first,
        session_key=session_key,
        session_id=session_id,
        task_id=old_task_id,
        message_id=message_id,
        message="recover this only once",
        task_status=AgentTaskStatus.RUNNING,
    )
    await first.mark_abandoned_agent_tasks(now_ms=200)
    stranded = await first.list_stranded_steer_inputs()
    assert len(stranded) == 1

    inert_runtime = TaskRuntime(storage=first, turn_handler=lambda _run: None)  # type: ignore[arg-type]
    envelope = inert_runtime._restart_recovery_envelope(  # noqa: SLF001
        stranded[0].target_task,
        [stranded[0].entry],
    )
    assert envelope is not None
    reservation = await inert_runtime.reserve(
        envelope,
        "recover this only once",
        mode="followup",
        run_kind="web_turn",
        persisted_user_message_id=message_id,
        persisted_user_message_ids=[message_id],
        update_envelope_cache=False,
    )
    gateway_config = GatewayConfig(workspace_dir=str(tmp_path / "workspace"))
    accepted_routing = capture_model_routing_config(
        gateway_config,
        session_mode="router",
        session_routing_revision=12,
        session_routing_source="session",
    )
    assert reservation.task_record.details is not None
    reservation.task_record.details["accepted_model_routing"] = (
        accepted_model_routing_audit(accepted_routing, run_kind="web_turn")
    )
    claimed = await first.promote_stranded_steer_inputs(
        target_task_id=old_task_id,
        message_ids=[message_id],
        task_record=reservation.task_record,
    )
    assert claimed == [message_id]
    promoted_task_id = reservation.task_id
    await inert_runtime.abort_reservation(reservation)
    await first.close()

    second = SessionStorage(str(db_path))
    await second.connect()
    runs: list[tuple[str, Any]] = []

    async def _handler(run: Any) -> None:
        runs.append((run.message, run.accepted_config))

    def _current_routing_provider(*, session_key: str, run_kind: str) -> Any:
        assert session_key == "agent:main:webchat:steer-double-restart"
        assert run_kind == "recovery_model_routing_base"
        return capture_model_routing_config(
            gateway_config,
            session_mode="direct",
            session_routing_revision=13,
            session_routing_source="session",
        )

    runtime = TaskRuntime(
        storage=second,
        turn_handler=_handler,
        accepted_config_provider=_current_routing_provider,
    )
    recovery = await runtime.recover_stranded_steers()
    assert recovery["promoted"] == 0
    assert recovery["resumed"] == 1
    assert recovery["task_ids"] == [promoted_task_id]
    record = await runtime.wait(promoted_task_id, timeout=2)
    assert record.status == AgentTaskStatus.SUCCEEDED
    assert len(runs) == 1
    recovered_message, recovered_routing = runs[0]
    assert recovered_message == "recover this only once"
    assert recovered_routing.session_mode == "router"
    assert recovered_routing.session_routing_revision == 12
    assert recovered_routing.session_routing_source == "session"
    tasks = await second.list_agent_tasks(session_key=session_key)
    assert {task.task_id for task in tasks} == {old_task_id, promoted_task_id}
    await second.close()


def test_gateway_runs_steer_restart_recovery_before_readiness() -> None:
    source = inspect.getsource(boot.start_gateway_server)
    recovery = source.index("await recover_stranded_steers()")
    ready = source.index("app.state.gateway_ready = True")
    assert recovery < ready
