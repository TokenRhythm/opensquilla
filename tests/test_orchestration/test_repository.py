from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    DelegatedTaskRecord,
    OrchestrationMode,
    OrchestrationRunRecord,
    RunLifecycle,
    SessionLifecycle,
    TaskOutcome,
)
from opensquilla.orchestration.repository import (
    ConcurrentActivationError,
    OrchestrationRepository,
)
from opensquilla.orchestration.service import OrchestrationService

pytestmark = pytest.mark.asyncio


def _id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return allocate


async def _repository(path) -> OrchestrationRepository:
    return await OrchestrationRepository.open(
        path,
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )


async def _seed_session(repo: OrchestrationRepository) -> None:
    await repo.create_run(
        OrchestrationRunRecord(
            run_id="run-1",
            root_session_id="root-1",
            root_task_id="task-root",
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=frozenset({"read_file", "apply_patch"}),
        )
    )
    await repo.create_session(
        AgentSessionRecord(
            session_id="session-1",
            run_id="run-1",
            profile="worker",
            parent_session_id="root-1",
            effective_tools=frozenset({"read_file", "apply_patch"}),
            runtime_context={"run_mode": "safe", "active_model": "model-a"},
        )
    )
    await repo.create_task(
        DelegatedTaskRecord(
            task_id="task-1",
            run_id="run-1",
            task_key="inspect-runtime",
            owner_session_id="session-1",
            description="Inspect the runtime",
            parent_task_id="task-root",
        )
    )


async def test_repository_enforces_one_nonterminal_activation_per_session(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    try:
        await _seed_session(repo)
        await repo.create_activation(
            AgentActivationRecord(
                activation_id="activation-1",
                session_id="session-1",
                task_id="task-1",
                phase=ActivationPhase.RUNNING,
            )
        )

        with pytest.raises(ConcurrentActivationError):
            await repo.create_activation(
                AgentActivationRecord(
                    activation_id="activation-2",
                    session_id="session-1",
                    task_id="task-1",
                    phase=ActivationPhase.QUEUED,
                )
            )
    finally:
        await repo.close()


async def test_released_activation_allows_a_new_cold_restore_attempt(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    try:
        await _seed_session(repo)
        await repo.create_activation(
            AgentActivationRecord(
                activation_id="activation-1",
                session_id="session-1",
                task_id="task-1",
                phase=ActivationPhase.RUNNING,
            )
        )
        await repo.release_activation("activation-1", reason="completed")

        restored = await repo.create_activation(
            AgentActivationRecord(
                activation_id="activation-2",
                session_id="session-1",
                task_id="task-1",
                phase=ActivationPhase.QUEUED,
            )
        )

        assert restored.activation_id == "activation-2"
        assert restored.phase is ActivationPhase.QUEUED
    finally:
        await repo.close()


async def test_inbox_sequence_and_acknowledgement_are_durable(tmp_path) -> None:
    path = tmp_path / "orchestration.db"
    repo = await _repository(path)
    await _seed_session(repo)

    first = await repo.enqueue_message(
        "session-1",
        kind="user",
        payload={"text": "one"},
    )
    second = await repo.enqueue_message(
        "session-1",
        kind="child_result",
        payload={"text": "two"},
    )
    await repo.acknowledge_message(first.message_id)
    await repo.close()

    reopened = await _repository(path)
    try:
        pending = await reopened.list_pending_messages("session-1")

        assert first.sequence < second.sequence
        assert [message.message_id for message in pending] == [second.message_id]
        assert pending[0].payload == {"text": "two"}
    finally:
        await reopened.close()


async def test_task_key_is_unique_inside_one_run(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    try:
        await _seed_session(repo)

        with pytest.raises(ValueError, match="task key already exists"):
            await repo.create_task(
                DelegatedTaskRecord(
                    task_id="task-2",
                    run_id="run-1",
                    task_key="inspect-runtime",
                    owner_session_id="session-1",
                    description="Duplicate inspection",
                    parent_task_id="task-root",
                )
            )
    finally:
        await repo.close()


async def test_runtime_session_key_survives_repository_reopen(tmp_path) -> None:
    path = tmp_path / "orchestration.db"
    repo = await _repository(path)
    await _seed_session(repo)
    await repo.close()

    reopened = await _repository(path)
    try:
        session = await reopened.get_session("session-1")
        assert session is not None
        assert session.runtime_session_key == "session-1"
        assert session.runtime_context == {
            "run_mode": "safe",
            "active_model": "model-a",
        }
    finally:
        await reopened.close()


async def test_session_recall_tasks_are_scoped_to_parent_and_fixed_profile(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    try:
        await repo.create_run(
            OrchestrationRunRecord(
                run_id="recall-run",
                root_session_id="root-1",
                root_task_id="root-task",
                mode=OrchestrationMode.COMPLEX,
                worker_template_tools=frozenset({"read_file"}),
            )
        )
        await repo.create_session(
            AgentSessionRecord(
                session_id="root-1",
                run_id="recall-run",
                profile="inherit",
                runtime_session_key="agent:main:root",
            )
        )
        for session_id, profile in (
            ("worker-1", "worker"),
            ("explorer-1", "explorer"),
        ):
            await repo.create_session(
                AgentSessionRecord(
                    session_id=session_id,
                    run_id="recall-run",
                    profile=profile,
                    parent_session_id="root-1",
                )
            )
            await repo.create_task(
                DelegatedTaskRecord(
                    task_id=f"task-{session_id}",
                    run_id="recall-run",
                    task_key=f"key-{session_id}",
                    owner_session_id=session_id,
                    parent_task_id="root-task",
                    description=f"Work for {profile}",
                    outcome=TaskOutcome.SUCCEEDED,
                    result={"summary": "done"},
                )
            )

        tasks = await repo.list_session_recall_tasks(
            parent_runtime_session_key="agent:main:root",
            profile="worker",
        )

        assert [task.owner_session_id for task in tasks] == ["worker-1"]
    finally:
        await repo.close()


async def test_completed_run_archives_only_after_twenty_four_hours(tmp_path) -> None:
    now = 200_000_000
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: now,
        id_factory=_id_factory(),
    )
    try:
        await _seed_session(repo)
        await repo.finish_task(
            "task-1",
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done"},
        )
        await repo.mark_final_synthesis_completed("run-1")
        assert await repo.complete_run_if_eligible("run-1") is True

        assert await repo.archive_eligible_runs(cutoff=now - 86_400_000) == []
        archived = await repo.archive_eligible_runs(cutoff=now)
        assert archived == ["run-1"]
        run = await repo.get_run("run-1")
        session = await repo.get_session("session-1")
        assert run is not None and run.lifecycle is RunLifecycle.ARCHIVED
        assert session is not None and session.lifecycle is SessionLifecycle.ARCHIVED
    finally:
        await repo.close()


async def test_child_result_delivery_and_processing_are_order_independent_and_idempotent(
    tmp_path,
) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    try:
        await _seed_session(repo)
        await repo.enqueue_message(
            "session-1",
            kind="child_result",
            payload={"task_id": "task-1"},
        )
        await repo.mark_child_result_processed("task-1")
        await repo.mark_child_result_processed("task-1")
        await repo.acknowledge_child_result_message("task-1")
    finally:
        await repo.close()


async def test_final_synthesis_settlement_is_idempotent_after_completion(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    service = OrchestrationService(repo)
    try:
        await _seed_session(repo)
        await repo.finish_task(
            "task-1",
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done"},
        )

        assert await service.mark_final_synthesis_completed("run-1") is True
        assert await service.mark_final_synthesis_completed("run-1") is False
    finally:
        await repo.close()


async def test_create_run_bundle_rolls_back_every_record_on_failure(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    run = OrchestrationRunRecord(
        run_id="run-atomic",
        root_session_id="root-atomic",
        root_task_id="task-atomic",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=frozenset({"read_file"}),
    )
    invalid_session = AgentSessionRecord(
        session_id="root-atomic",
        run_id="missing-run",
        profile="inherit",
        effective_tools=frozenset({"read_file"}),
    )
    task = DelegatedTaskRecord(
        task_id="task-atomic",
        run_id="run-atomic",
        task_key="root",
        owner_session_id="root-atomic",
        description="Root",
    )
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.create_run_bundle(run=run, root_session=invalid_session, root_task=task)

        assert await repo.get_run("run-atomic") is None
        assert await repo.get_session("root-atomic") is None
        assert await repo.get_task("task-atomic") is None
    finally:
        await repo.close()


async def test_delegation_bundle_rolls_back_session_and_task_on_activation_failure(
    tmp_path,
) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    await _seed_session(repo)
    session = AgentSessionRecord(
        session_id="session-atomic",
        run_id="run-1",
        profile="worker",
        parent_session_id="session-1",
        depth=2,
        effective_tools=frozenset({"read_file"}),
    )
    task = DelegatedTaskRecord(
        task_id="task-atomic",
        run_id="run-1",
        task_key="atomic-child",
        owner_session_id=session.session_id,
        parent_task_id="task-1",
        description="Atomic child",
    )
    invalid_activation = AgentActivationRecord(
        activation_id="activation-atomic",
        session_id=session.session_id,
        task_id="missing-task",
        phase=ActivationPhase.STARTING,
    )
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.create_delegation_bundle(
                session=session,
                task=task,
                activation=invalid_activation,
                max_direct_children=8,
            )

        assert await repo.get_session(session.session_id) is None
        assert await repo.get_task(task.task_id) is None
    finally:
        await repo.close()


async def test_child_result_claim_allows_only_one_delivery_owner(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    await _seed_session(repo)
    await repo.enqueue_message(
        "session-1",
        kind="child_result",
        payload={"task_id": "task-1", "session_id": "session-1"},
    )
    try:
        first, second = await asyncio.gather(
            repo.claim_child_result_messages(
                owner="delivery-a",
                lease_expires_at=2_000,
                limit=10,
            ),
            repo.claim_child_result_messages(
                owner="delivery-b",
                lease_expires_at=2_000,
                limit=10,
            ),
        )

        assert len(first) + len(second) == 1
        claimed = (first or second)[0]
        assert claimed.delivery_owner in {"delivery-a", "delivery-b"}
        assert claimed.delivery_attempts == 1
    finally:
        await repo.close()


async def test_activation_claim_is_atomic_without_a_runtime_lease(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    await _seed_session(repo)
    await repo.create_activation(
        AgentActivationRecord(
            activation_id="activation-lease",
            session_id="session-1",
            task_id="task-1",
            phase=ActivationPhase.STARTING,
        )
    )
    try:
        claimed, rejected = await asyncio.gather(
            repo.claim_activation("activation-lease"),
            repo.claim_activation("activation-lease"),
        )

        assert claimed is not None
        assert claimed.phase is ActivationPhase.RUNNING
        assert rejected is None
        current = await repo.get_activation("activation-lease")
        assert current is not None
        assert current.phase is ActivationPhase.RUNNING
    finally:
        await repo.close()


async def test_retry_bundle_rolls_back_task_when_new_activation_fails(tmp_path) -> None:
    repo = await _repository(tmp_path / "orchestration.db")
    await _seed_session(repo)
    await repo.finish_task(
        "task-1",
        outcome=TaskOutcome.FAILED,
        result={"error": "first attempt"},
    )
    invalid_activation = AgentActivationRecord(
        activation_id="activation-retry",
        session_id="missing-session",
        task_id="task-1",
        phase=ActivationPhase.STARTING,
    )
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.retry_task_bundle(
                task_id="task-1",
                session=await repo.get_session("session-1"),
                session_is_new=False,
                description="Retry",
                parent_task_id="task-root",
                retry_of_activation_id="activation-old",
                replaces_session_id=None,
                background=True,
                effective_tools=frozenset({"read_file"}),
                runtime_context={"run_mode": "safe"},
                activation=invalid_activation,
                run_id="run-1",
                parent_session_id="root-1",
                depth=1,
                max_direct_children=8,
            )

        task = await repo.get_task("task-1")
        assert task is not None
        assert task.outcome is TaskOutcome.FAILED
        assert task.background is False
        assert task.description == "Inspect the runtime"
    finally:
        await repo.close()


async def test_initialize_upgrades_pre_refactor_inbox_columns(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(path)
    await conn.execute(
        """
        CREATE TABLE agent_inbox_messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            acknowledged_at INTEGER,
            schema_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE (session_id, sequence)
        )
        """
    )
    await conn.commit()
    await conn.close()

    repo = await _repository(path)
    try:
        async with repo._read_transaction("test_schema") as read_conn:
            rows = await read_conn.execute_fetchall("PRAGMA table_info(agent_inbox_messages)")
        columns = {str(row[1]) for row in rows}
        assert {
            "processed_at",
            "delivery_owner",
            "delivery_lease_expires_at",
            "delivery_attempts",
            "idempotency_key",
        } <= columns
    finally:
        await repo.close()
