"""Accepted restart inputs survive optional startup and resident pressure."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskDependencyError, TaskRuntime
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import SessionStorage
from tests.test_gateway.test_steer_restart_recovery import _seed_steering_input

KEY = "agent:main:webchat:deferred-restart"
SESSION_ID = "session-deferred-restart"
MESSAGE_ID = "message-deferred-restart"


async def _open_restarted(storage_path: Path, *, resumed: bool) -> SessionStorage:
    storage = SessionStorage(str(storage_path))
    await storage.connect()
    await _seed_steering_input(
        storage, session_key=KEY, session_id=SESSION_ID, task_id="old-turn",
        message_id=MESSAGE_ID, message="continue the accepted browser task",
        task_status=AgentTaskStatus.RUNNING,
        task_details={"session_epoch": 0, "required_services": ["desktop_browser"]},
    )
    if resumed:
        await storage.mark_abandoned_agent_tasks(now_ms=200)
        stranded = await storage.list_stranded_steer_inputs()

        async def unused_handler(_run: Any) -> None:
            raise AssertionError("inert reservation must not execute")

        inert = TaskRuntime(
            storage=storage, turn_handler=unused_handler,
            service_snapshot=lambda: {"desktop_browser": {"status": "ready"}},
        )
        envelope = inert._restart_recovery_envelope(
            stranded[0].target_task, [stranded[0].entry],
        )
        assert envelope is not None
        reservation = await inert.reserve(
            envelope, "continue the accepted browser task", run_kind="web_turn",
            persisted_user_message_id=MESSAGE_ID, persisted_user_message_ids=[MESSAGE_ID],
        )
        assert await storage.promote_stranded_steer_inputs(
            target_task_id="old-turn", message_ids=[MESSAGE_ID],
            task_record=reservation.task_record,
            expected_session_id=SESSION_ID, expected_session_epoch=0,
        ) == [MESSAGE_ID]
        await inert.abort_reservation(reservation)
        await inert.shutdown(cancel=False)
    await storage.close()
    restarted = SessionStorage(str(storage_path))
    await restarted.connect()
    return restarted


async def _assert_executed_once(
    storage: SessionStorage, runtime: TaskRuntime, runs: list[Any],
) -> None:
    receipt = await storage.get_turn_ingress_receipt(
        source_scope="rpc:web:steer.v2", request_session_key=KEY,
        client_request_id=f"request-{MESSAGE_ID}",
    )
    assert receipt is not None
    record = await runtime.wait(receipt.receipt.task_id, timeout=2)
    assert record is not None and record.status == AgentTaskStatus.SUCCEEDED
    again = await runtime.recover_stranded_steers()
    assert again["promoted"] == again["resumed"] == again["deferred"] == 0
    assert len(runs) == 1
    assert runs[0].message == "continue the accepted browser task"
    tasks = await storage.list_agent_tasks(session_key=KEY)
    assert len(tasks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["stranded", "resumed"])
@pytest.mark.parametrize("initial_status", ["starting", "degraded", "disabled"])
async def test_optional_service_warmup_wakes_durable_restart_once(
    tmp_path: Path, resumed: bool, initial_status: str,
) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=resumed)
    services = {"desktop_browser": {"status": initial_status}}
    runs: list[Any] = []

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, service_snapshot=lambda: services)
    try:
        recovery = await runtime.recover_stranded_steers()
        assert recovery["deferred"] == 1
        assert recovery["promoted"] == recovery["resumed"] == 0
        assert runs == [] and runtime._resident_count == 0
        worker = runtime._steer_recovery_task
        assert worker is not None and not worker.done()
        before = await storage.get_canonical_transcript_entry(SESSION_ID, MESSAGE_ID)
        assert before is not None and before.turn_context is not None
        assert before.turn_context["disposition"] == ("promoted" if resumed else "steering")

        async def synthetic_browser_warmup() -> str:
            return "ready"

        setattr(synthetic_browser_warmup, "_optional_service_name", "desktop_browser")
        container = boot.ServiceContainer(
            config=GatewayConfig(), task_runtime=runtime, optional_services=services,
            optional_generation=1, deferred_warmups=[synthetic_browser_warmup],
        )
        await boot._run_deferred_warmups(container)
        await asyncio.wait_for(worker, timeout=2)
        await _assert_executed_once(storage, runtime, runs)
        assert runtime._steer_recovery_task is None
    finally:
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["stranded", "resumed"])
async def test_resident_release_wakes_restart_without_polling(
    tmp_path: Path, resumed: bool,
) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=resumed)
    runs: list[Any] = []

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler, max_resident_tasks=1,
        service_snapshot=lambda: {"desktop_browser": {"status": "ready"}},
    )
    held = await runtime.reserve(
        RouteEnvelope(source_kind=SourceKind.WEB, source_name="synthetic", agent_id="main",
                      session_key="agent:main:webchat:other-resident"), "other accepted work",
    )
    try:
        recovery = await runtime.recover_stranded_steers()
        assert recovery["deferred"] == 1 and runtime._resident_count == 1
        worker = runtime._steer_recovery_task
        assert worker is not None and not worker.done()
        await runtime.abort_reservation(held)
        await asyncio.wait_for(worker, timeout=2)
        await _assert_executed_once(storage, runtime, runs)
    finally:
        await runtime.abort_reservation(held)
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["stranded", "resumed"])
async def test_shutdown_fences_late_optional_readiness(tmp_path: Path, resumed: bool) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=resumed)
    services = {"desktop_browser": {"status": "starting"}}
    runs: list[Any] = []

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, service_snapshot=lambda: services)
    try:
        recovery = await runtime.recover_stranded_steers()
        assert recovery["deferred"] == 1
        worker = runtime._steer_recovery_task
        assert worker is not None
        await asyncio.sleep(0)  # Let the owner reach its notification wait.
        closed = await runtime.shutdown(cancel=False, timeout=1)
        assert closed.clean and worker.done()
        services["desktop_browser"]["status"] = "ready"
        runtime.notify_recovery_dependencies_changed()
        after = await runtime.recover_stranded_steers()
        assert after["task_ids"] == [] and runs == []
        assert runtime._steer_recovery_task is None
        entry = await storage.get_canonical_transcript_entry(SESSION_ID, MESSAGE_ID)
        assert entry is not None and entry.turn_context is not None
        assert entry.turn_context["disposition"] == ("promoted" if resumed else "steering")
    finally:
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()


@pytest.mark.asyncio
async def test_notification_during_first_recovery_pass_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=False)
    services = {"desktop_browser": {"status": "starting"}}
    runs: list[Any] = []

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, service_snapshot=lambda: services)
    original = runtime._recover_stranded_steers_once

    async def complete_warmup_during_pass() -> dict[str, Any]:
        result = await original()
        if result["deferred"]:
            services["desktop_browser"]["status"] = "ready"
            runtime.notify_recovery_dependencies_changed()
        return result

    monkeypatch.setattr(runtime, "_recover_stranded_steers_once", complete_warmup_during_pass)
    try:
        assert (await runtime.recover_stranded_steers())["deferred"] == 1
        worker = runtime._steer_recovery_task
        assert worker is not None
        await asyncio.wait_for(worker, timeout=2)
        await _assert_executed_once(storage, runtime, runs)
    finally:
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()


@pytest.mark.asyncio
async def test_optional_dependency_does_not_block_independent_restart_input(tmp_path: Path) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=False)
    await _seed_steering_input(
        storage, session_key="agent:main:webchat:plain-restart", session_id="plain-session",
        task_id="plain-old-task", message_id="plain-message", message="independent accepted text",
        task_status=AgentTaskStatus.FAILED, task_details={"session_epoch": 0},
    )
    runs: list[Any] = []

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler,
        service_snapshot=lambda: {"desktop_browser": {"status": "starting"}},
    )
    try:
        recovery = await runtime.recover_stranded_steers()
        assert recovery["deferred"] == recovery["promoted"] == 1
        await runtime.wait(recovery["task_ids"][0], timeout=2)
        assert [run.message for run in runs] == ["independent accepted text"]
        entry = await storage.get_canonical_transcript_entry(SESSION_ID, MESSAGE_ID)
        assert entry is not None and entry.turn_context is not None
        assert entry.turn_context["disposition"] == "steering"
    finally:
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()


@pytest.mark.asyncio
async def test_resumed_post_reservation_error_releases_lease_without_busy_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await _open_restarted(tmp_path / "sessions.db", resumed=True)
    services = {"desktop_browser": {"status": "starting"}}
    runs: list[Any] = []
    failed = asyncio.Event()
    attempts = 0

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, service_snapshot=lambda: services)
    original = runtime._restore_durable_accepted_model_routing

    async def fail_after_reservation(*_args: Any) -> None:
        nonlocal attempts
        attempts += 1
        assert runtime._resident_count == 1
        failed.set()
        # A downstream error with this type must not enter the pre-reservation
        # defer branch and bypass cleanup of the now-held resident lease.
        raise TaskDependencyError(
            session_key=KEY, kind="DEPENDENCY_STARTING", services=("desktop_browser",),
        )

    try:
        assert (await runtime.recover_stranded_steers())["deferred"] == 1
        worker = runtime._steer_recovery_task
        assert worker is not None
        monkeypatch.setattr(
            runtime, "_restore_durable_accepted_model_routing", fail_after_reservation
        )
        services["desktop_browser"]["status"] = "ready"
        runtime.notify_recovery_dependencies_changed()
        await asyncio.wait_for(failed.wait(), timeout=2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert attempts == 1 and runtime._resident_count == 0
        assert runtime._reservations_by_session == {} and runs == []
        assert not worker.done()
        monkeypatch.setattr(runtime, "_restore_durable_accepted_model_routing", original)
        runtime.notify_recovery_dependencies_changed()
        await asyncio.wait_for(worker, timeout=2)
        await _assert_executed_once(storage, runtime, runs)
    finally:
        await runtime.shutdown(cancel=True, timeout=2)
        await storage.close()
