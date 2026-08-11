"""Generation, atomicity and lifecycle contracts for durable session Goals."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from opensquilla.session.goals import (
    GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY,
    GOAL_OBJECTIVE_UPDATE_DETAIL_KEY,
    ClaimCurrentGoalMutation,
    ClaimGoalMutation,
    ExpectedGoal,
    GoalClaimCandidate,
    GoalCommandRequest,
    GoalConflictError,
    GoalObjectiveUpdate,
    GoalTurnContext,
    GoalValidationError,
    StartGoalMutation,
    automatic_goal_task_id,
    goal_snapshot,
    new_goal,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionIntent,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:goals"
SESSION_ID = "session-goals"
SOURCE_SCOPE = "gateway:goals"


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SessionStorage]:
    value = SessionStorage(":memory:")
    await value.connect()
    await value.upsert_session(
        SessionNode(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            agent_id="main",
            created_at=100,
            updated_at=100,
            epoch=0,
        )
    )
    yield value
    await value.close()


@asynccontextmanager
async def _shared_storage_pair(
    db_path: Path,
) -> AsyncIterator[tuple[SessionStorage, SessionStorage]]:
    """Open two independent connections without triggering Goal recovery between them."""

    first = SessionStorage(str(db_path))
    second = SessionStorage(str(db_path))
    await first.connect()
    try:
        await first.upsert_session(
            SessionNode(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                agent_id="main",
                created_at=100,
                updated_at=100,
                epoch=0,
            )
        )
        await second.connect()
        try:
            yield first, second
        finally:
            await second.close()
    finally:
        await first.close()


async def _run_ordered_write_race(
    first_storage: SessionStorage,
    first_operation: Callable[[], Awaitable[object]],
    second_storage: SessionStorage,
    second_operation: Callable[[], Awaitable[object]],
) -> tuple[object | BaseException, object | BaseException]:
    """Start two real transactions together while choosing only their linearization order.

    Both operations must arrive at ``BEGIN IMMEDIATE`` before either is allowed
    to acquire SQLite's write lock.  The first connection then acquires that
    lock before the second attempts it; the second still performs its normal
    cross-connection busy retry and observes the first transaction's commit.
    """

    arrived = 0
    both_ready = asyncio.Event()
    first_acquired = asyncio.Event()
    original_first_begin = first_storage._begin_immediate
    original_second_begin = second_storage._begin_immediate

    async def rendezvous() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), timeout=1)

    async def begin_first(conn, operation, deadline, started) -> None:
        await rendezvous()
        try:
            await original_first_begin(conn, operation, deadline, started)
        finally:
            first_acquired.set()

    async def begin_second(conn, operation, deadline, started) -> None:
        await rendezvous()
        await asyncio.wait_for(first_acquired.wait(), timeout=1)
        await original_second_begin(conn, operation, deadline, started)

    first_storage._begin_immediate = begin_first
    second_storage._begin_immediate = begin_second
    try:
        first_result, second_result = await asyncio.gather(
            first_operation(),
            second_operation(),
            return_exceptions=True,
        )
    finally:
        first_storage._begin_immediate = original_first_begin
        second_storage._begin_immediate = original_second_begin
    return first_result, second_result


def _command(
    action: str,
    *,
    request_id: str | None = None,
    fingerprint: str | None = None,
) -> GoalCommandRequest:
    fingerprint_seed = fingerprint or action
    return GoalCommandRequest(
        source_scope=SOURCE_SCOPE,
        request_session_key=SESSION_KEY,
        client_request_id=request_id or str(uuid.uuid4()),
        action=action,
        request_fingerprint=hashlib.sha256(fingerprint_seed.encode()).hexdigest(),
    )


def _expected(goal) -> ExpectedGoal:
    return ExpectedGoal(
        session_id=goal.session_id,
        epoch=goal.session_epoch,
        goal_id=goal.goal_id,
        state_revision=goal.state_revision,
    )


async def _set_goal(
    storage: SessionStorage,
    *,
    goal_id: str = "goal-1",
    task_id: str = "task-1",
    message_id: str = "message-1",
    command: GoalCommandRequest | None = None,
    objective: str = "Ship the safe Goal runtime.",
):
    command = command or _command("set")
    task = AgentTaskRecord(
        task_id=task_id,
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="goal",
        status=AgentTaskStatus.QUEUED,
        created_at=200,
        updated_at=200,
    )
    entry = TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="user",
        content=objective,
        created_at=200,
    )
    goal = new_goal(
        goal_id=goal_id,
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        objective=objective,
        task_id=task_id,
        created_at_ms=200,
    )
    return await storage.accept_turn(
        entry,
        expected_epoch=0,
        updated_at=200,
        task_record=task,
        source_scope=SOURCE_SCOPE,
        request_session_key=SESSION_KEY,
        client_request_id=command.client_request_id,
        request_fingerprint=command.request_fingerprint,
        goal_mutation=StartGoalMutation(goal=goal, command=command),
    )


async def _apply_user_goal_mutation(
    storage: SessionStorage,
    *,
    action: str,
    expected: ExpectedGoal,
    command: GoalCommandRequest,
):
    if action == "pause":
        return await storage.pause_goal(
            session_key=SESSION_KEY,
            expected=expected,
            command=command,
            now_ms=300,
        )
    if action == "edit":
        return await storage.edit_goal(
            session_key=SESSION_KEY,
            expected=expected,
            objective="Ship the revised race-safe Goal runtime.",
            command=command,
            now_ms=300,
        )
    if action == "clear":
        return await storage.clear_goal(
            session_key=SESSION_KEY,
            expected=expected,
            command=command,
        )
    raise AssertionError(f"unsupported test mutation: {action}")


async def _terminalize_owner_with_usage(storage: SessionStorage, *, task_id: str) -> None:
    await storage.conn.execute(
        """
        INSERT INTO usage_events (
            event_id, execution_id, call_index, turn_id, session_id,
            session_epoch, started_at_ms, completed_at_ms, status,
            input_tokens, output_tokens, total_tokens, origin
        ) VALUES (?, ?, 0, ?, ?, 0, 200, 275, 'finalized', 7, 5, 12, ?)
        """,
        ("race-usage", "race-execution", task_id, SESSION_ID, "test"),
    )
    await storage.conn.commit()
    await storage.update_agent_task(
        task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=275,
    )


async def test_goal_set_atomically_accepts_goal_message_task_and_receipts(
    storage: SessionStorage,
) -> None:
    command = _command("set")
    result = await _set_goal(storage, command=command)

    assert result.replayed is False
    assert result.goal is not None
    assert result.goal_context is not None
    assert result.goal.active_task_id == "task-1"
    assert result.goal_context.objective_snapshot == "Ship the safe Goal runtime."
    assert result.goal_command_response == {
        "accepted": True,
        "clientRequestId": command.client_request_id,
        "sessionKey": SESSION_KEY,
        "sessionId": SESSION_ID,
        "epoch": 0,
        "taskId": "task-1",
        "userMessageId": "message-1",
        "previousGoalId": None,
        "goal": result.goal_command_response["goal"],
    }
    task = await storage.get_agent_task("task-1")
    assert task is not None
    assert task.details["goal_context"] == result.goal_context.as_task_detail()
    transcript = await storage.get_transcript(SESSION_ID)
    assert [(row.role, row.content) for row in transcript] == [
        ("user", "Ship the safe Goal runtime.")
    ]
    assert await storage.get_goal_command_receipt(command) is not None

    replay = await _set_goal(storage, command=command)
    assert replay.replayed is True
    assert replay.goal_command_response == result.goal_command_response
    assert len(await storage.get_transcript(SESSION_ID)) == 1


async def test_meta_receipt_replay_preserves_goal_context_and_candidate(
    storage: SessionStorage,
) -> None:
    set_command = _command("set")
    accepted = await _set_goal(storage, command=set_command)
    assert accepted.goal is not None and accepted.goal_context is not None
    await storage.stage_meta_launch_draft(
        session_key=SESSION_KEY,
        client_request_id=set_command.client_request_id,
        meta_skill_name="meta-test",
        launch_text="/meta meta-test -- synthetic stale context draft",
    )

    context_replay = await storage.replay_turn_ingress_receipt(
        source_scope=SOURCE_SCOPE,
        request_session_key=SESSION_KEY,
        client_request_id=set_command.client_request_id,
    )

    assert context_replay is not None
    assert context_replay.goal_context == accepted.goal_context
    assert context_replay.goal_candidate is None
    assert await storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []

    candidate = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id=accepted.goal.goal_id,
    )
    followup_request_id = str(uuid.uuid4())
    followup = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="candidate-replay-message",
            role="user",
            content="Queue this Goal follow-up.",
            created_at=300,
        ),
        expected_epoch=0,
        updated_at=300,
        task_record=AgentTaskRecord(
            task_id="candidate-replay-task",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            created_at=300,
            updated_at=300,
        ),
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id=followup_request_id,
        request_fingerprint=hashlib.sha256(b"candidate-replay").hexdigest(),
        goal_mutation=ClaimCurrentGoalMutation(),
    )
    assert followup.goal_context is None
    assert followup.goal_candidate == candidate
    await storage.stage_meta_launch_draft(
        session_key=SESSION_KEY,
        client_request_id=followup_request_id,
        meta_skill_name="meta-test",
        launch_text="/meta meta-test -- synthetic stale candidate draft",
    )

    candidate_replay = await storage.replay_turn_ingress_receipt(
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id=followup_request_id,
    )

    assert candidate_replay is not None
    assert candidate_replay.goal_context is None
    assert candidate_replay.goal_candidate == candidate
    assert await storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []


async def test_goal_and_meta_control_admission_fails_before_any_turn_write(
    storage: SessionStorage,
) -> None:
    request_id = "mixed-goal-meta-control"
    intent, _ = await storage.stage_meta_control_intent(
        session_key=SESSION_KEY,
        control_kind="manual",
        correlation_id=f"request:{request_id}",
        meta_skill_name="meta-test",
    )
    control = {
        "version": 1,
        "intent_id": intent.intent_id,
        "kind": "manual",
        "name": "meta-test",
        "correlation_id": intent.correlation_id,
    }
    await storage.stage_meta_launch_draft(
        session_key=SESSION_KEY,
        client_request_id=request_id,
        meta_skill_name="meta-test",
        launch_text="/meta meta-test -- must remain staged",
    )
    candidate = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id="synthetic-goal",
    )

    with pytest.raises(
        ValueError,
        match="Goal turns cannot consume a MetaSkill control intent",
    ):
        await storage.accept_turn(
            TranscriptEntry(
                session_id=SESSION_ID,
                session_key=SESSION_KEY,
                message_id="mixed-goal-meta-message",
                role="user",
                content="/meta meta-test -- must remain staged",
                created_at=300,
                turn_context={"meta_control": control},
            ),
            expected_epoch=0,
            updated_at=300,
            task_record=AgentTaskRecord(
                task_id="mixed-goal-meta-task",
                session_key=SESSION_KEY,
                status=AgentTaskStatus.QUEUED,
                created_at=300,
                updated_at=300,
                details={"metadata": {"meta_control": control}},
            ),
            source_scope="gateway:sessions.send",
            request_session_key=SESSION_KEY,
            client_request_id=request_id,
            request_fingerprint=hashlib.sha256(b"mixed-goal-meta").hexdigest(),
            meta_control_intent_id=intent.intent_id,
            goal_mutation=ClaimGoalMutation(candidate=candidate),
        )

    assert await storage.get_transcript(SESSION_ID) == []
    assert await storage.get_agent_task("mixed-goal-meta-task") is None
    assert await storage.get_turn_ingress_receipt(
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id=request_id,
    ) is None
    assert await storage.get_goal(SESSION_KEY) is None
    drafts = await storage.list_meta_launch_drafts(session_key=SESSION_KEY)
    assert [draft.client_request_id for draft in drafts] == [request_id]
    preserved_intent = await storage.get_meta_control_intent(
        session_key=SESSION_KEY,
        control_kind="manual",
        correlation_id=intent.correlation_id,
    )
    assert preserved_intent is not None
    assert preserved_intent.status == "staged"
    assert preserved_intent.accepted_task_id is None
    session = await storage.get_session(SESSION_KEY)
    assert session is not None and session.updated_at == 100
    assert storage.conn.in_transaction is False


async def test_goal_set_conflict_rolls_back_every_turn_artifact(
    storage: SessionStorage,
) -> None:
    await _set_goal(storage)
    command = _command("set", fingerprint="sha256:second")
    with pytest.raises(GoalConflictError) as exc_info:
        await _set_goal(
            storage,
            goal_id="goal-2",
            task_id="task-2",
            message_id="message-2",
            command=command,
            objective="A conflicting Goal",
        )
    assert exc_info.value.code == "GOAL_ACTIVE"
    assert await storage.get_agent_task("task-2") is None
    assert len(await storage.get_transcript(SESSION_ID)) == 1
    assert await storage.get_goal_command_receipt(command) is None


async def test_new_goal_replaces_only_a_settled_complete_goal(
    storage: SessionStorage,
) -> None:
    first = await _set_goal(storage)
    assert first.goal_context is not None
    completed = await storage.commit_goal_terminal(
        first.goal_context,
        status="complete",
        now_ms=250,
    )
    assert completed.active_task_id == "task-1"

    busy_command = _command("set", fingerprint="sha256:busy-replacement")
    with pytest.raises(GoalConflictError) as exc_info:
        await _set_goal(
            storage,
            goal_id="goal-busy",
            task_id="task-busy",
            message_id="message-busy",
            command=busy_command,
            objective="Must wait for terminal settlement.",
        )
    assert exc_info.value.code == "GOAL_BUSY"
    assert await storage.get_goal_command_receipt(busy_command) is None

    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=300,
    )
    settled = await storage.settle_goal_task(
        first.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=310,
    )
    assert settled is not None and settled.active_task_id is None

    second = await _set_goal(
        storage,
        goal_id="goal-2",
        task_id="task-2",
        message_id="message-2",
        command=_command("set", fingerprint="sha256:replacement"),
        objective="Ship the next Goal safely.",
    )
    assert second.goal is not None
    assert second.goal.goal_id == "goal-2"
    assert second.goal_command_response is not None
    assert second.goal_command_response["previousGoalId"] == "goal-1"


async def test_goal_set_request_id_reuse_with_new_fingerprint_conflicts(
    storage: SessionStorage,
) -> None:
    request_id = str(uuid.uuid4())
    await _set_goal(
        storage,
        command=_command("set", request_id=request_id, fingerprint="sha256:first"),
    )
    with pytest.raises(GoalConflictError) as exc_info:
        await _set_goal(
            storage,
            command=_command("set", request_id=request_id, fingerprint="sha256:other"),
        )
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"


async def test_goal_set_rolls_back_if_command_receipt_persistence_fails(
    storage: SessionStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("synthetic receipt failure")

    monkeypatch.setattr(
        SessionStorage,
        "_insert_goal_command_receipt_on_conn",
        staticmethod(fail_receipt),
    )
    command = _command("set")
    with pytest.raises(RuntimeError, match="synthetic receipt failure"):
        await _set_goal(storage, command=command)

    assert await storage.get_goal(SESSION_KEY) is None
    assert await storage.get_agent_task("task-1") is None
    assert await storage.get_transcript(SESSION_ID) == []
    assert await storage.get_turn_ingress_receipt(
        source_scope=SOURCE_SCOPE,
        request_session_key=SESSION_KEY,
        client_request_id=command.client_request_id,
    ) is None


async def test_concurrent_goal_set_has_exactly_one_winner(tmp_path) -> None:
    db_path = tmp_path / "concurrent-goal-set.db"
    first = SessionStorage(str(db_path))
    second = SessionStorage(str(db_path))
    await first.connect()
    await first.upsert_session(
        SessionNode(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            epoch=0,
            created_at=100,
            updated_at=100,
        )
    )
    await second.connect()
    try:
        results = await asyncio.gather(
            _set_goal(
                first,
                goal_id="goal-a",
                task_id="task-a",
                message_id="message-a",
                command=_command("set", fingerprint="concurrent-a"),
                objective="Goal A",
            ),
            _set_goal(
                second,
                goal_id="goal-b",
                task_id="task-b",
                message_id="message-b",
                command=_command("set", fingerprint="concurrent-b"),
                objective="Goal B",
            ),
            return_exceptions=True,
        )
        accepted = [item for item in results if not isinstance(item, BaseException)]
        rejected = [item for item in results if isinstance(item, GoalConflictError)]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].code == "GOAL_ACTIVE"
        persisted = await first.get_goal(SESSION_KEY)
        assert persisted is not None
        assert persisted.goal_id in {"goal-a", "goal-b"}
    finally:
        await second.close()
        await first.close()


@pytest.mark.parametrize("command_action", ["pause", "edit", "clear"])
@pytest.mark.parametrize("terminal_status", ["complete", "blocked"])
@pytest.mark.parametrize("first_actor", ["command", "terminal"])
async def test_goal_command_race_with_terminal_commit_is_linearizable(
    tmp_path: Path,
    command_action: str,
    terminal_status: str,
    first_actor: str,
) -> None:
    async with _shared_storage_pair(tmp_path / "terminal-command-race.db") as (
        command_storage,
        terminal_storage,
    ):
        accepted = await _set_goal(command_storage)
        assert accepted.goal is not None and accepted.goal_context is not None
        expected = _expected(accepted.goal)
        command = _command(
            command_action,
            fingerprint=f"race:{command_action}:{terminal_status}:{first_actor}",
        )

        async def command_operation():
            return await _apply_user_goal_mutation(
                command_storage,
                action=command_action,
                expected=expected,
                command=command,
            )

        async def terminal_operation():
            return await terminal_storage.commit_goal_terminal(
                accepted.goal_context,
                status=terminal_status,
                blocked_reason=(
                    "The deterministic race is blocked."
                    if terminal_status == "blocked"
                    else None
                ),
                now_ms=310,
            )

        if first_actor == "command":
            command_result, terminal_result = await _run_ordered_write_race(
                command_storage,
                command_operation,
                terminal_storage,
                terminal_operation,
            )
        else:
            terminal_result, command_result = await _run_ordered_write_race(
                terminal_storage,
                terminal_operation,
                command_storage,
                command_operation,
            )

        current = await command_storage.get_goal(SESSION_KEY)
        command_won = not isinstance(command_result, BaseException)
        assert (await command_storage.get_goal_command_receipt(command) is not None) == (
            command_won
        )
        if first_actor == "terminal":
            assert not isinstance(terminal_result, BaseException)
            assert isinstance(command_result, GoalConflictError)
            assert command_result.code == "STALE_GOAL"
        elif command_action == "pause":
            # Pause preserves its owner, so the in-flight task may still
            # linearize a structured terminal result after the pause.
            assert not isinstance(command_result, BaseException)
            assert not isinstance(terminal_result, BaseException)
        else:
            assert not isinstance(command_result, BaseException)
            assert isinstance(terminal_result, GoalConflictError)
            assert terminal_result.code == (
                "GOAL_NOT_FOUND" if command_action == "clear" else "STALE_GOAL"
            )

        if first_actor == "command" and command_action == "clear":
            assert current is None
        else:
            assert current is not None
            if not isinstance(terminal_result, BaseException):
                assert current.status == terminal_status
                assert current.terminal_task_id == accepted.goal_context.task_id
                assert current.blocked_reason == (
                    "The deterministic race is blocked."
                    if terminal_status == "blocked"
                    else None
                )
            else:
                assert command_action == "edit"
                assert current.status == "active"
                assert current.objective_revision == 2
                assert current.objective == "Ship the revised race-safe Goal runtime."
                assert current.terminal_task_id is None
            assert current.state_revision == expected.state_revision + (
                2
                if first_actor == "command" and command_action == "pause"
                else 1
            )


@pytest.mark.parametrize("command_action", ["pause", "edit", "clear"])
@pytest.mark.parametrize("first_actor", ["command", "progress"])
async def test_progress_race_respects_state_and_progress_revision_domains(
    tmp_path: Path,
    command_action: str,
    first_actor: str,
) -> None:
    async with _shared_storage_pair(tmp_path / "progress-command-race.db") as (
        command_storage,
        progress_storage,
    ):
        accepted = await _set_goal(command_storage)
        assert accepted.goal is not None and accepted.goal_context is not None
        expected = _expected(accepted.goal)
        command = _command(
            command_action,
            fingerprint=f"race:{command_action}:progress:{first_actor}",
        )

        async def command_operation():
            return await _apply_user_goal_mutation(
                command_storage,
                action=command_action,
                expected=expected,
                command=command,
            )

        async def progress_operation():
            return await progress_storage.update_goal_progress(
                accepted.goal_context,
                explanation="Race-safe progress",
                steps=[{"step": "Linearize writes", "status": "in_progress"}],
                now_ms=310,
            )

        if first_actor == "command":
            command_result, progress_result = await _run_ordered_write_race(
                command_storage,
                command_operation,
                progress_storage,
                progress_operation,
            )
        else:
            progress_result, command_result = await _run_ordered_write_race(
                progress_storage,
                progress_operation,
                command_storage,
                command_operation,
            )

        assert not isinstance(command_result, BaseException)
        assert await command_storage.get_goal_command_receipt(command) is not None
        if first_actor == "command" and command_action in {"edit", "clear"}:
            assert isinstance(progress_result, GoalConflictError)
            assert progress_result.code == (
                "GOAL_NOT_FOUND" if command_action == "clear" else "STALE_GOAL"
            )
        else:
            assert not isinstance(progress_result, BaseException)

        current = await command_storage.get_goal(SESSION_KEY)
        if command_action == "clear":
            assert current is None
        else:
            assert current is not None
            assert current.state_revision == expected.state_revision + 1
            if command_action == "pause":
                assert current.status == "paused"
                assert current.progress_revision == 1
                assert current.progress_json == {
                    "explanation": "Race-safe progress",
                    "steps": [
                        {"status": "in_progress", "step": "Linearize writes"}
                    ],
                }
            else:
                assert current.status == "active"
                assert current.objective_revision == 2
                assert current.objective == "Ship the revised race-safe Goal runtime."
                assert current.progress_json is None
                assert current.progress_revision == (1 if first_actor == "command" else 2)


@pytest.mark.parametrize("command_action", ["pause", "edit", "clear"])
@pytest.mark.parametrize("first_actor", ["command", "settlement"])
async def test_settlement_race_accounts_owner_exactly_once_or_noops_after_clear(
    tmp_path: Path,
    command_action: str,
    first_actor: str,
) -> None:
    async with _shared_storage_pair(tmp_path / "settlement-command-race.db") as (
        command_storage,
        settlement_storage,
    ):
        accepted = await _set_goal(command_storage)
        assert accepted.goal is not None and accepted.goal_context is not None
        expected = _expected(accepted.goal)
        await _terminalize_owner_with_usage(
            command_storage,
            task_id=accepted.goal_context.task_id,
        )
        command = _command(
            command_action,
            fingerprint=f"race:{command_action}:settlement:{first_actor}",
        )

        async def command_operation():
            return await _apply_user_goal_mutation(
                command_storage,
                action=command_action,
                expected=expected,
                command=command,
            )

        async def settlement_operation():
            return await settlement_storage.settle_goal_task(
                accepted.goal_context,
                max_turns=50,
                runtime_budget_seconds=3_600,
                now_ms=320,
            )

        if first_actor == "command":
            command_result, settlement_result = await _run_ordered_write_race(
                command_storage,
                command_operation,
                settlement_storage,
                settlement_operation,
            )
            assert not isinstance(command_result, BaseException)
            assert await command_storage.get_goal_command_receipt(command) is not None
            if command_action == "clear":
                assert settlement_result is None
            else:
                assert not isinstance(settlement_result, BaseException)
                assert settlement_result is not None
        else:
            settlement_result, command_result = await _run_ordered_write_race(
                settlement_storage,
                settlement_operation,
                command_storage,
                command_operation,
            )
            assert not isinstance(settlement_result, BaseException)
            assert settlement_result is not None
            assert isinstance(command_result, GoalConflictError)
            assert command_result.code == "STALE_GOAL"
            assert await command_storage.get_goal_command_receipt(command) is None

        current = await command_storage.get_goal(SESSION_KEY)
        if first_actor == "command" and command_action == "clear":
            assert current is None
        else:
            assert current is not None
            assert current.active_task_id is None
            assert current.turns_settled == 1
            assert current.total_tokens == 12
            assert current.active_time_ms == 75
            assert current.state_revision == expected.state_revision + (
                2 if first_actor == "command" else 1
            )
            assert current.status == (
                "paused"
                if first_actor == "command" and command_action == "pause"
                else "active"
            )
            assert current.objective_revision == (
                2
                if first_actor == "command" and command_action == "edit"
                else 1
            )
            if current.objective_revision == 2:
                assert current.objective == "Ship the revised race-safe Goal runtime."

        assert (
            await settlement_storage.settle_goal_task(
                accepted.goal_context,
                max_turns=50,
                runtime_budget_seconds=3_600,
                now_ms=400,
            )
            is None
        )
        after_replay = await command_storage.get_goal(SESSION_KEY)
        if current is not None:
            assert after_replay is not None
            assert after_replay.state_revision == current.state_revision
            assert after_replay.turns_settled == 1
            assert after_replay.total_tokens == 12


async def test_edit_invalidates_old_objective_tools_but_old_task_still_settles(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    progressed = await storage.update_goal_progress(
        accepted.goal_context,
        explanation="Working",
        steps=[{"step": "Inspect", "status": "in_progress"}],
        now_ms=250,
    )
    assert progressed.progress_revision == 1
    assert progressed.state_revision == accepted.goal.state_revision
    async with storage.conn.execute(
        "SELECT progress_json FROM session_goals WHERE goal_id = ?",
        (progressed.goal_id,),
    ) as cursor:
        raw_progress = (await cursor.fetchone())[0]
    assert raw_progress == (
        '{"explanation":"Working","steps":'
        '[{"status":"in_progress","step":"Inspect"}]}'
    )

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(progressed),
        objective="Ship the revised Goal runtime.",
        command=_command("edit"),
        now_ms=300,
    )
    assert edited.goal is not None
    assert edited.goal.objective_revision == 2
    assert edited.goal.progress_revision == 2
    assert edited.goal.progress_json is None
    with pytest.raises(GoalConflictError) as exc_info:
        await storage.commit_goal_terminal(
            accepted.goal_context,
            status="complete",
            now_ms=310,
        )
    assert exc_info.value.code == "STALE_GOAL"

    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=350,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=360,
    )
    assert settled is not None
    assert settled.objective_revision == 2
    assert settled.status == "active"
    assert settled.active_task_id is None
    assert settled.turns_settled == 1
    assert settled.active_time_ms == 150


async def test_running_edit_adoption_switches_tool_authority_only_after_apply(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="Ship the safely adopted Goal objective.",
        command=_command("edit", fingerprint="running-adoption-rev2"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert edited.goal is not None
    assert edited.goal.objective_revision == 2

    pending_task = await storage.get_agent_task(original_context.task_id)
    assert pending_task is not None and pending_task.details is not None
    assert GoalTurnContext.from_task_detail(
        pending_task.details.get("goal_context")
    ) == original_context
    assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in pending_task.details
    pending = GoalObjectiveUpdate.from_task_detail(
        pending_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending is not None
    assert pending.status == "pending"
    assert pending.context.task_id == original_context.task_id
    assert pending.context.goal_id == original_context.goal_id
    assert pending.context.objective_revision == 2
    assert pending.context.objective_snapshot == (
        "Ship the safely adopted Goal objective."
    )

    claimed = await storage.claim_goal_objective_update(pending, now_ms=310)
    assert claimed is not None and claimed.status == "claimed"
    claimed_task = await storage.get_agent_task(original_context.task_id)
    assert claimed_task is not None and claimed_task.details is not None
    assert GoalTurnContext.from_task_detail(
        claimed_task.details.get("goal_context")
    ) == original_context
    assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in claimed_task.details

    applied = await storage.apply_goal_objective_update(
        claimed,
        iteration=2,
        model_call_id="model-call-rev2",
        now_ms=320,
    )
    assert applied is not None and applied.status == "applied"
    applied_task = await storage.get_agent_task(original_context.task_id)
    assert applied_task is not None and applied_task.details is not None
    assert GoalTurnContext.from_task_detail(
        applied_task.details.get("goal_context")
    ) == original_context
    assert GoalTurnContext.from_task_detail(
        applied_task.details.get(GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY)
    ) == applied.context
    applied_detail = applied_task.details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY]
    assert applied_detail["appliedIteration"] == 2
    assert applied_detail["modelCallId"] == "model-call-rev2"

    progressed = await storage.update_goal_progress(
        applied.context,
        explanation="The revised objective is now authoritative.",
        steps=[{"step": "Adopt revision two", "status": "completed"}],
        now_ms=330,
    )
    assert progressed.objective_revision == 2
    assert progressed.progress_revision == 2
    completed = await storage.commit_goal_terminal(
        applied.context,
        status="complete",
        now_ms=340,
    )
    assert completed.status == "complete"
    assert completed.terminal_task_id == original_context.task_id


@pytest.mark.parametrize(
    (
        "task_status",
        "error_class",
        "usage_limited",
        "expected_status",
        "expected_reason",
    ),
    [
        (
            AgentTaskStatus.CANCELLED,
            None,
            False,
            "paused",
            "user_cancelled",
        ),
        (
            AgentTaskStatus.FAILED,
            "provider_failure",
            False,
            "blocked",
            "provider_failure",
        ),
        (
            AgentTaskStatus.FAILED,
            "insufficient_credits",
            True,
            "usage_limited",
            "usage_limited",
        ),
    ],
)
async def test_unapplied_edit_keeps_owner_terminal_failure_classification(
    storage: SessionStorage,
    task_status: AgentTaskStatus,
    error_class: str | None,
    usage_limited: bool,
    expected_status: str,
    expected_reason: str,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )
    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="Ship the revised objective after owner settlement.",
        command=_command("edit", fingerprint=f"terminal-{task_status.value}"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert edited.goal is not None
    assert edited.goal.objective_revision == 2

    await storage.update_agent_task(
        original_context.task_id,
        status=task_status,
        error_class=error_class,
        terminal_reason=error_class,
        finished_at=350,
    )
    settled = await storage.settle_goal_task(
        original_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        usage_limited=usage_limited,
        now_ms=360,
    )

    assert settled is not None
    assert settled.objective_revision == 2
    assert settled.status == expected_status
    assert settled.active_task_id is None
    if expected_status == "blocked":
        assert settled.blocked_reason == expected_reason
        assert settled.terminal_reason == "turn_error"
    else:
        assert settled.pause_reason == expected_reason
        assert settled.terminal_reason == expected_reason


async def test_newer_running_edit_supersedes_claimed_but_unapplied_revision(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )

    revision_two = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="Use revision two only if it reaches the provider.",
        command=_command("edit", fingerprint="running-adoption-race-rev2"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert revision_two.goal is not None
    task_after_rev2 = await storage.get_agent_task(original_context.task_id)
    assert task_after_rev2 is not None and task_after_rev2.details is not None
    pending_two = GoalObjectiveUpdate.from_task_detail(
        task_after_rev2.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending_two is not None
    claimed_two = await storage.claim_goal_objective_update(pending_two, now_ms=310)
    assert claimed_two is not None and claimed_two.status == "claimed"

    revision_three = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(revision_two.goal),
        objective="Revision three supersedes the unconsumed edit.",
        command=_command("edit", fingerprint="running-adoption-race-rev3"),
        adoption_task_id=original_context.task_id,
        now_ms=320,
    )
    assert revision_three.goal is not None
    assert revision_three.goal.objective_revision == 3
    task_after_rev3 = await storage.get_agent_task(original_context.task_id)
    assert task_after_rev3 is not None and task_after_rev3.details is not None
    pending_three = GoalObjectiveUpdate.from_task_detail(
        task_after_rev3.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending_three is not None and pending_three.status == "pending"
    assert pending_three.context.objective_revision == 3
    assert pending_three.context.objective_snapshot == (
        "Revision three supersedes the unconsumed edit."
    )

    assert (
        await storage.apply_goal_objective_update(
            claimed_two,
            iteration=2,
            model_call_id="stale-model-call-rev2",
            now_ms=330,
        )
        is None
    )
    still_pending = await storage.get_agent_task(original_context.task_id)
    assert still_pending is not None and still_pending.details is not None
    assert GoalObjectiveUpdate.from_task_detail(
        still_pending.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    ) == pending_three
    assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in still_pending.details

    claimed_three = await storage.claim_goal_objective_update(
        pending_three,
        now_ms=340,
    )
    assert claimed_three is not None and claimed_three.status == "claimed"
    applied_three = await storage.apply_goal_objective_update(
        claimed_three,
        iteration=3,
        model_call_id="model-call-rev3",
        now_ms=350,
    )
    assert applied_three is not None and applied_three.status == "applied"
    final_task = await storage.get_agent_task(original_context.task_id)
    assert final_task is not None and final_task.details is not None
    assert GoalTurnContext.from_task_detail(
        final_task.details.get("goal_context")
    ) == original_context
    assert GoalTurnContext.from_task_detail(
        final_task.details.get(GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY)
    ) == applied_three.context
    progressed = await storage.update_goal_progress(
        applied_three.context,
        explanation=None,
        steps=[{"step": "Honor revision three", "status": "in_progress"}],
        now_ms=360,
    )
    assert progressed.objective_revision == 3


async def test_clear_revokes_pending_edit_without_touching_task_or_transcript(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )
    manager = SessionManager(storage, inject_time_prefix=False)
    transcript_before = await manager.get_transcript(SESSION_KEY)

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="This pending edit will be removed before adoption.",
        command=_command("edit", fingerprint="clear-pending-adoption"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert edited.goal is not None
    pending_task = await storage.get_agent_task(original_context.task_id)
    assert pending_task is not None and pending_task.details is not None
    pending = GoalObjectiveUpdate.from_task_detail(
        pending_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending is not None and pending.status == "pending"

    cleared = await storage.clear_goal(
        session_key=SESSION_KEY,
        expected=_expected(edited.goal),
        command=_command("clear", fingerprint="clear-pending-adoption"),
    )
    assert cleared.goal is None
    assert await storage.get_goal(SESSION_KEY) is None
    task_after_clear = await storage.get_agent_task(original_context.task_id)
    assert task_after_clear is not None
    assert task_after_clear.status == AgentTaskStatus.RUNNING
    assert task_after_clear.details is not None
    assert GoalTurnContext.from_task_detail(
        task_after_clear.details.get("goal_context")
    ) == original_context
    revoked = GoalObjectiveUpdate.from_task_detail(
        task_after_clear.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert revoked is not None and revoked.status == "revoked"
    assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in task_after_clear.details
    assert await storage.claim_goal_objective_update(pending, now_ms=310) is None
    assert await manager.get_transcript(SESSION_KEY) == transcript_before


async def test_clear_after_claim_rejects_late_apply_without_advancing_authority(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="The Agent may have assembled this claimed edit.",
        command=_command("edit", fingerprint="clear-claimed-adoption"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert edited.goal is not None
    pending_task = await storage.get_agent_task(original_context.task_id)
    assert pending_task is not None and pending_task.details is not None
    pending = GoalObjectiveUpdate.from_task_detail(
        pending_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending is not None and pending.status == "pending"
    claimed = await storage.claim_goal_objective_update(pending, now_ms=310)
    assert claimed is not None and claimed.status == "claimed"

    cleared = await storage.clear_goal(
        session_key=SESSION_KEY,
        expected=_expected(edited.goal),
        command=_command("clear", fingerprint="clear-claimed-adoption"),
    )
    assert cleared.goal is None
    assert await storage.get_goal(SESSION_KEY) is None
    assert (
        await storage.apply_goal_objective_update(
            claimed,
            iteration=2,
            model_call_id="late-model-call-after-clear",
            now_ms=320,
        )
        is None
    )

    task_after_late_apply = await storage.get_agent_task(original_context.task_id)
    assert task_after_late_apply is not None
    assert task_after_late_apply.status == AgentTaskStatus.RUNNING
    assert task_after_late_apply.details is not None
    assert GoalTurnContext.from_task_detail(
        task_after_late_apply.details.get("goal_context")
    ) == original_context
    assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in task_after_late_apply.details
    revoked = GoalObjectiveUpdate.from_task_detail(
        task_after_late_apply.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert revoked is not None and revoked.status == "revoked"
    with pytest.raises(GoalConflictError) as exc_info:
        await storage.update_goal_progress(
            claimed.context,
            explanation=None,
            steps=[{"step": "Must not recreate the Goal", "status": "completed"}],
            now_ms=330,
        )
    assert exc_info.value.code == "GOAL_NOT_FOUND"
    assert await storage.get_goal(SESSION_KEY) is None


async def test_clear_after_applied_edit_keeps_task_evidence_but_deletes_goal_authority(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    original_context = accepted.goal_context
    await storage.update_agent_task(
        original_context.task_id,
        status=AgentTaskStatus.RUNNING,
        started_at=210,
    )

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        objective="This edit becomes current-task evidence before Clear.",
        command=_command("edit", fingerprint="clear-applied-adoption"),
        adoption_task_id=original_context.task_id,
        now_ms=300,
    )
    assert edited.goal is not None
    pending_task = await storage.get_agent_task(original_context.task_id)
    assert pending_task is not None and pending_task.details is not None
    pending = GoalObjectiveUpdate.from_task_detail(
        pending_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert pending is not None
    claimed = await storage.claim_goal_objective_update(pending, now_ms=310)
    assert claimed is not None
    applied = await storage.apply_goal_objective_update(
        claimed,
        iteration=2,
        model_call_id="model-call-before-clear",
        now_ms=320,
    )
    assert applied is not None and applied.status == "applied"

    cleared = await storage.clear_goal(
        session_key=SESSION_KEY,
        expected=_expected(edited.goal),
        command=_command("clear", fingerprint="clear-applied-adoption"),
    )
    assert cleared.goal is None
    assert await storage.get_goal(SESSION_KEY) is None
    task_after_clear = await storage.get_agent_task(original_context.task_id)
    assert task_after_clear is not None
    assert task_after_clear.status == AgentTaskStatus.RUNNING
    assert task_after_clear.details is not None
    persisted_update = GoalObjectiveUpdate.from_task_detail(
        task_after_clear.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
    )
    assert persisted_update is not None and persisted_update.status == "applied"
    assert GoalTurnContext.from_task_detail(
        task_after_clear.details.get(GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY)
    ) == applied.context
    with pytest.raises(GoalConflictError) as exc_info:
        await storage.commit_goal_terminal(
            applied.context,
            status="complete",
            now_ms=330,
        )
    assert exc_info.value.code == "GOAL_NOT_FOUND"
    assert await storage.get_goal(SESSION_KEY) is None


async def test_revoked_claim_is_not_reclaimable_after_storage_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "goal-clear-restart.db"
    first = SessionStorage(str(db_path))
    await first.connect()
    try:
        await first.upsert_session(
            SessionNode(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                agent_id="main",
                created_at=100,
                updated_at=100,
                epoch=0,
            )
        )
        accepted = await _set_goal(first)
        assert accepted.goal is not None and accepted.goal_context is not None
        original_context = accepted.goal_context
        await first.update_agent_task(
            original_context.task_id,
            status=AgentTaskStatus.RUNNING,
            started_at=210,
        )
        edited = await first.edit_goal(
            session_key=SESSION_KEY,
            expected=_expected(accepted.goal),
            objective="Do not reinject this edit after restart.",
            command=_command("edit", fingerprint="clear-claimed-restart"),
            adoption_task_id=original_context.task_id,
            now_ms=300,
        )
        assert edited.goal is not None
        pending_task = await first.get_agent_task(original_context.task_id)
        assert pending_task is not None and pending_task.details is not None
        pending = GoalObjectiveUpdate.from_task_detail(
            pending_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
        )
        assert pending is not None
        claimed = await first.claim_goal_objective_update(pending, now_ms=310)
        assert claimed is not None and claimed.status == "claimed"
        await first.clear_goal(
            session_key=SESSION_KEY,
            expected=_expected(edited.goal),
            command=_command("clear", fingerprint="clear-claimed-restart"),
        )
    finally:
        await first.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        assert await restarted.get_goal(SESSION_KEY) is None
        recovered_task = await restarted.get_agent_task(original_context.task_id)
        assert recovered_task is not None and recovered_task.details is not None
        recovered_update = GoalObjectiveUpdate.from_task_detail(
            recovered_task.details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
        )
        assert recovered_update is not None and recovered_update.status == "revoked"
        assert GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY not in recovered_task.details
        assert (
            await restarted.claim_goal_objective_update(pending, now_ms=400)
            is None
        )
        assert (
            await restarted.apply_goal_objective_update(
                claimed,
                iteration=2,
                model_call_id="restarted-late-model-call",
                now_ms=410,
            )
            is None
        )
        assert [task.task_id for task in await restarted.list_agent_tasks()] == [
            original_context.task_id
        ]
    finally:
        await restarted.close()


async def test_goal_tool_writes_require_exact_durable_task_context(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    forged = replace(
        accepted.goal_context,
        objective_snapshot="A forged objective snapshot",
    )

    with pytest.raises(GoalConflictError) as terminal_exc:
        await storage.commit_goal_terminal(
            forged,
            status="complete",
            now_ms=250,
        )
    assert terminal_exc.value.code == "STALE_GOAL"

    with pytest.raises(GoalConflictError) as progress_exc:
        await storage.update_goal_progress(
            forged,
            explanation="Forged",
            steps=[{"step": "Should not persist", "status": "in_progress"}],
            now_ms=260,
        )
    assert progress_exc.value.code == "STALE_GOAL"

    current = await storage.get_goal(SESSION_KEY)
    assert current is not None
    assert current.status == "active"
    assert current.progress_revision == 0
    assert current.progress_json is None


async def test_paused_owning_task_can_complete_and_completion_beats_guardrail(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    paused = await storage.pause_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        command=_command("pause"),
        now_ms=250,
    )
    assert paused.goal is not None and paused.goal.active_task_id == "task-1"
    completed = await storage.commit_goal_terminal(
        accepted.goal_context,
        status="complete",
        now_ms=300,
    )
    assert completed.status == "complete"
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=61_000,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=1,
        runtime_budget_seconds=60,
        now_ms=61_100,
    )
    assert settled is not None
    assert settled.status == "complete"
    assert settled.active_task_id is None


async def test_terminal_replay_after_settlement_is_fenced_to_exact_owner_task(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    first_context = accepted.goal_context

    blocked = await storage.commit_goal_terminal(
        first_context,
        status="blocked",
        blocked_reason="Needs another pass",
        now_ms=250,
    )
    assert blocked.terminal_task_id == first_context.task_id
    await storage.update_agent_task(
        first_context.task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=300,
    )
    first_settlement = await storage.settle_goal_task(
        first_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=310,
    )
    assert first_settlement is not None
    assert first_settlement.active_task_id is None
    assert first_settlement.terminal_task_id == first_context.task_id

    replay = await storage.commit_goal_terminal(
        first_context,
        status="blocked",
        blocked_reason="Needs another pass",
        now_ms=320,
    )
    assert replay.state_revision == first_settlement.state_revision
    with pytest.raises(GoalConflictError) as opposite_exc:
        await storage.commit_goal_terminal(
            first_context,
            status="complete",
            now_ms=330,
        )
    assert opposite_exc.value.code == "STALE_GOAL"

    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(first_settlement),
        command=_command("resume"),
        now_ms=340,
    )
    assert resumed.goal is not None
    assert resumed.goal.terminal_task_id is None
    second_task_id = automatic_goal_task_id(
        resumed.goal.goal_id,
        resumed.goal.objective_revision,
        1,
    )
    continued = await storage.accept_goal_continuation(
        expected=_expected(resumed.goal),
        expected_continuation_seq=0,
        task_record=AgentTaskRecord(
            task_id=second_task_id,
            session_key=SESSION_KEY,
            run_kind="goal",
            status=AgentTaskStatus.QUEUED,
            created_at=350,
            updated_at=350,
        ),
        now_ms=350,
    )
    await storage.commit_goal_terminal(
        continued.context,
        status="blocked",
        blocked_reason="Still needs work",
        now_ms=360,
    )
    await storage.update_agent_task(
        second_task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=350,
        finished_at=400,
    )
    second_settlement = await storage.settle_goal_task(
        continued.context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=410,
    )
    assert second_settlement is not None
    assert second_settlement.terminal_task_id == second_task_id

    with pytest.raises(GoalConflictError) as stale_owner_exc:
        await storage.commit_goal_terminal(
            first_context,
            status="blocked",
            blocked_reason="Needs another pass",
            now_ms=420,
        )
    assert stale_owner_exc.value.code == "STALE_GOAL"
    exact_replay = await storage.commit_goal_terminal(
        continued.context,
        status="blocked",
        blocked_reason="Still needs work",
        now_ms=430,
    )
    assert exact_replay.state_revision == second_settlement.state_revision


async def test_system_pause_preserves_owner_until_terminal_settlement(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None

    paused = await storage.pause_goal_for_system(
        session_key=SESSION_KEY,
        goal_id=accepted.goal.goal_id,
        expected_state_revision=accepted.goal.state_revision,
        reason="lease_revoked",
        now_ms=250,
    )
    assert paused.status == "paused"
    assert paused.pause_reason == "lease_revoked"
    assert paused.active_task_id == "task-1"
    assert (
        await storage.pause_goal_for_system(
            session_key=SESSION_KEY,
            goal_id=accepted.goal.goal_id,
            expected_state_revision=accepted.goal.state_revision,
            reason="feature_disabled",
            now_ms=260,
        )
        is None
    )

    completed = await storage.commit_goal_terminal(
        accepted.goal_context,
        status="complete",
        now_ms=300,
    )
    assert completed.status == "complete"
    assert completed.active_task_id == "task-1"


async def test_resume_preserves_owner_and_resets_only_window_counters(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    paused_with_owner = await storage.pause_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        command=_command("pause"),
        now_ms=250,
    )
    assert paused_with_owner.goal is not None
    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(paused_with_owner.goal),
        command=_command("resume"),
        now_ms=260,
    )
    assert resumed.goal is not None
    assert resumed.goal.status == "active"
    assert resumed.goal.active_task_id == "task-1"
    assert resumed.goal.turns_started == 1
    assert resumed.goal.window_turns_started == 0
    assert resumed.goal.window_active_time_ms == 0
    assert resumed.response["goal"]["executionState"] == "queued"
    assert [task.task_id for task in await storage.list_agent_tasks()] == ["task-1"]

    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=500,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=510,
    )
    assert settled is not None
    assert settled.turns_started == 1
    assert settled.active_time_ms == 300
    assert settled.window_turns_started == 0
    assert settled.window_active_time_ms == 300


async def test_resume_is_a_state_transition_during_plan_and_session_work(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=300,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=310,
    )
    assert settled is not None and settled.active_task_id is None
    paused = await storage.pause_goal(
        session_key=SESSION_KEY,
        expected=_expected(settled),
        command=_command("pause"),
        now_ms=320,
    )
    assert paused.goal is not None

    session = await storage.get_session(SESSION_KEY)
    assert session is not None
    await storage.set_collaboration_mode(
        SESSION_KEY,
        "plan",
        expected_revision=session.collaboration_revision,
    )
    await storage.create_agent_task(
        AgentTaskRecord(
            task_id="unrelated-busy-task",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            created_at=330,
            updated_at=330,
        )
    )

    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(paused.goal),
        command=_command("resume"),
        now_ms=340,
    )

    assert resumed.goal is not None
    assert resumed.goal.status == "active"
    assert resumed.goal.active_task_id is None
    assert resumed.goal.window_turns_started == 0
    assert {task.task_id for task in await storage.list_agent_tasks()} == {
        "task-1",
        "unrelated-busy-task",
    }


async def test_edit_reactivates_only_a_settled_complete_goal(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    progressed = await storage.update_goal_progress(
        accepted.goal_context,
        explanation="The first objective was delivered.",
        steps=[{"step": "Deliver it", "status": "completed"}],
        now_ms=225,
    )
    completed = await storage.commit_goal_terminal(
        accepted.goal_context,
        status="complete",
        now_ms=250,
    )
    assert completed.progress_revision == progressed.progress_revision

    with pytest.raises(GoalConflictError) as busy_exc:
        await storage.edit_goal(
            session_key=SESSION_KEY,
            expected=_expected(completed),
            objective="Extend the completed objective.",
            command=_command("edit", fingerprint="busy-complete-edit"),
            now_ms=260,
        )
    assert busy_exc.value.code == "GOAL_BUSY"

    await _terminalize_owner_with_usage(storage, task_id="task-1")
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=300,
    )
    assert settled is not None and settled.status == "complete"
    lifetime = {
        "created_at_ms": settled.created_at_ms,
        "turns_started": settled.turns_started,
        "turns_settled": settled.turns_settled,
        "active_time_ms": settled.active_time_ms,
        "input_tokens": settled.input_tokens,
        "output_tokens": settled.output_tokens,
        "total_tokens": settled.total_tokens,
        "continuation_seq": settled.continuation_seq,
    }

    edited = await storage.edit_goal(
        session_key=SESSION_KEY,
        expected=_expected(settled),
        objective="Extend the completed objective.",
        command=_command("edit", fingerprint="settled-complete-edit"),
        now_ms=350,
    )

    assert edited.goal is not None
    reactivated = edited.goal
    assert reactivated.goal_id == settled.goal_id
    assert reactivated.status == "active"
    assert reactivated.objective == "Extend the completed objective."
    assert reactivated.objective_revision == settled.objective_revision + 1
    assert reactivated.progress_revision == settled.progress_revision + 1
    assert reactivated.progress_json is None
    assert reactivated.active_task_id is None
    assert reactivated.terminal_task_id is None
    assert reactivated.pause_reason is None
    assert reactivated.blocked_reason is None
    assert reactivated.terminal_reason is None
    assert reactivated.finished_at_ms is None
    assert reactivated.window_turns_started == 0
    assert reactivated.window_active_time_ms == 0
    assert {
        "created_at_ms": reactivated.created_at_ms,
        "turns_started": reactivated.turns_started,
        "turns_settled": reactivated.turns_settled,
        "active_time_ms": reactivated.active_time_ms,
        "input_tokens": reactivated.input_tokens,
        "output_tokens": reactivated.output_tokens,
        "total_tokens": reactivated.total_tokens,
        "continuation_seq": reactivated.continuation_seq,
    } == lifetime


async def test_resume_blocker_is_historical_until_first_started_turn_settles(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.commit_goal_terminal(
        accepted.goal_context,
        status="blocked",
        blocked_reason="A dependency is unavailable.",
        now_ms=250,
    )
    await storage.update_agent_task(
        accepted.goal_context.task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=300,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=310,
    )
    assert settled is not None
    assert settled.blocked_reason == "A dependency is unavailable."

    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(settled),
        command=_command("resume"),
        now_ms=320,
    )
    assert resumed.goal is not None
    assert resumed.goal.status == "active"
    assert resumed.goal.blocked_reason == "A dependency is unavailable."
    assert goal_snapshot(resumed.goal)["blockedReason"] is None

    task_id = automatic_goal_task_id(
        resumed.goal.goal_id,
        resumed.goal.objective_revision,
        1,
    )
    continued = await storage.accept_goal_continuation(
        expected=_expected(resumed.goal),
        expected_continuation_seq=0,
        task_record=AgentTaskRecord(
            task_id=task_id,
            session_key=SESSION_KEY,
            run_kind="goal",
            status=AgentTaskStatus.QUEUED,
            created_at=330,
            updated_at=330,
        ),
        now_ms=330,
    )
    await storage.update_agent_task(
        task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=340,
        finished_at=400,
    )
    after_resumed_turn = await storage.settle_goal_task(
        continued.context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=410,
    )
    assert after_resumed_turn is not None
    assert after_resumed_turn.status == "active"
    assert after_resumed_turn.blocked_reason is None


async def test_new_terminal_blocker_replaces_historical_resume_reason(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.commit_goal_terminal(
        accepted.goal_context,
        status="blocked",
        blocked_reason="Old blocker.",
        now_ms=250,
    )
    await storage.update_agent_task(
        accepted.goal_context.task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=300,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=310,
    )
    assert settled is not None
    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(settled),
        command=_command("resume"),
        now_ms=320,
    )
    assert resumed.goal is not None
    task_id = automatic_goal_task_id(
        resumed.goal.goal_id,
        resumed.goal.objective_revision,
        1,
    )
    continued = await storage.accept_goal_continuation(
        expected=_expected(resumed.goal),
        expected_continuation_seq=0,
        task_record=AgentTaskRecord(
            task_id=task_id,
            session_key=SESSION_KEY,
            run_kind="goal",
            status=AgentTaskStatus.QUEUED,
            created_at=330,
            updated_at=330,
        ),
        now_ms=330,
    )

    replaced = await storage.commit_goal_terminal(
        continued.context,
        status="blocked",
        blocked_reason="New blocker.",
        now_ms=340,
    )
    assert replaced.status == "blocked"
    assert replaced.blocked_reason == "New blocker."
    assert goal_snapshot(replaced)["blockedReason"] == "New blocker."

    await storage.update_agent_task(
        task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=335,
        finished_at=360,
    )
    final = await storage.settle_goal_task(
        continued.context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=370,
    )
    assert final is not None
    assert final.blocked_reason == "New blocker."


async def test_activation_failure_compensation_has_no_orphan_owner(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None

    compensated = await storage.compensate_goal_activation_failure(
        accepted.goal_context,
        now_ms=250,
    )
    assert compensated is not None
    assert compensated.status == "paused"
    assert compensated.pause_reason == "activation_failed"
    assert compensated.active_task_id is None
    task = await storage.get_agent_task("task-1")
    assert task is not None
    assert task.status == AgentTaskStatus.ABANDONED
    assert task.terminal_reason == "activation_failed"
    assert (
        await storage.compensate_goal_activation_failure(
            accepted.goal_context,
            now_ms=300,
        )
        is None
    )


async def test_failed_goal_task_blocks_without_creating_a_retry(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.FAILED,
        error_class="provider_failure",
        started_at=200,
        finished_at=275,
    )

    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=300,
    )
    assert settled is not None
    assert settled.status == "blocked"
    assert settled.blocked_reason == "provider_failure"
    assert settled.active_task_id is None
    tasks = await storage.list_agent_tasks(session_key=SESSION_KEY, limit=10)
    assert [task.task_id for task in tasks] == ["task-1"]


async def test_legacy_checkpoint_failure_remains_resumable_after_upgrade(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.FAILED,
        error_class="goal_checkpoint_required",
        started_at=200,
        finished_at=275,
    )

    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=300,
    )

    assert settled is not None
    assert settled.status == "paused"
    assert settled.pause_reason == "goal_checkpoint_required"
    assert settled.terminal_reason == "goal_checkpoint_required"
    assert settled.blocked_reason is None
    assert settled.active_task_id is None

    resumed = await storage.resume_goal(
        session_key=SESSION_KEY,
        expected=_expected(settled),
        command=_command("resume"),
        now_ms=310,
    )
    assert resumed.goal is not None
    assert resumed.goal.status == "active"
    assert resumed.goal.pause_reason is None
    assert resumed.goal.terminal_reason is None


async def test_settlement_accounts_finalized_usage_once(storage: SessionStorage) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.conn.execute(
        """
        INSERT INTO usage_events (
            event_id, execution_id, call_index, turn_id, session_id,
            session_epoch, started_at_ms, completed_at_ms, status,
            input_tokens, output_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens, total_tokens, origin
        ) VALUES (?, ?, 0, ?, ?, 0, 200, 250, 'finalized', 7, 5, 2, 3, 1, 12, ?)
        """,
        ("usage-1", "execution-1", "task-1", SESSION_ID, "test"),
    )
    await storage.conn.execute(
        """
        INSERT INTO usage_events (
            event_id, execution_id, call_index, turn_id, session_id,
            session_epoch, started_at_ms, status, input_tokens, total_tokens, origin
        ) VALUES (?, ?, 0, ?, ?, 0, 200, 'started', 99, 99, ?)
        """,
        ("usage-incomplete", "execution-2", "task-1", SESSION_ID, "test"),
    )
    await storage.conn.commit()
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=450,
    )
    with pytest.raises(GoalConflictError) as exc_info:
        await storage.settle_goal_task(
            replace(accepted.goal_context, objective_snapshot="forged objective"),
            max_turns=50,
            runtime_budget_seconds=3_600,
            now_ms=490,
        )
    assert exc_info.value.code == "STALE_GOAL"
    first = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=500,
    )
    assert first is not None
    assert (
        first.input_tokens,
        first.output_tokens,
        first.reasoning_tokens,
        first.cache_read_tokens,
        first.cache_write_tokens,
        first.total_tokens,
    ) == (7, 5, 2, 3, 1, 12)
    assert first.active_time_ms == 250
    assert await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=600,
    ) is None
    assert (await storage.get_goal(SESSION_KEY)).total_tokens == 12


async def test_continuation_acceptance_is_atomic_and_sequence_fenced(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=250,
    )
    settled = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
    )
    assert settled is not None
    next_task_id = automatic_goal_task_id(
        settled.goal_id,
        settled.objective_revision,
        1,
    )
    with pytest.raises(GoalValidationError, match="task id"):
        await storage.accept_goal_continuation(
            expected=_expected(settled),
            expected_continuation_seq=0,
            task_record=AgentTaskRecord(
                task_id="not-the-deterministic-id",
                session_key=SESSION_KEY,
                status=AgentTaskStatus.QUEUED,
            ),
        )
    assert await storage.get_agent_task("not-the-deterministic-id") is None

    next_task = AgentTaskRecord(
        task_id=next_task_id,
        session_key=SESSION_KEY,
        run_kind="goal",
        status=AgentTaskStatus.QUEUED,
        created_at=300,
        updated_at=300,
    )
    continued = await storage.accept_goal_continuation(
        expected=_expected(settled),
        expected_continuation_seq=0,
        task_record=next_task,
        now_ms=300,
    )
    assert continued.goal.active_task_id == next_task_id
    assert continued.goal.continuation_seq == 1
    assert continued.context.automatic is True
    assert continued.context.continuation_seq == 1

    with pytest.raises(GoalConflictError) as exc_info:
        await storage.accept_goal_continuation(
            expected=_expected(settled),
            expected_continuation_seq=0,
            task_record=AgentTaskRecord(
                task_id="task-duplicate",
                session_key=SESSION_KEY,
            ),
        )
    assert exc_info.value.code == "STALE_GOAL"
    assert await storage.get_agent_task("task-duplicate") is None


async def test_busy_user_turn_carries_candidate_then_claims_at_activation(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=250,
    )
    idle_goal = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
    )
    assert idle_goal is not None

    await storage.create_agent_task(
        AgentTaskRecord(
            task_id="blocker",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.RUNNING,
            created_at=300,
            updated_at=300,
            started_at=300,
        )
    )
    candidate = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id=idle_goal.goal_id,
    )
    user_task = AgentTaskRecord(
        task_id="queued-user",
        session_key=SESSION_KEY,
        status=AgentTaskStatus.QUEUED,
        created_at=310,
        updated_at=310,
    )
    turn_command_id = "followup-request"
    acceptance = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="followup-message",
            role="user",
            content="Please also verify reset.",
            created_at=310,
        ),
        expected_epoch=0,
        updated_at=310,
        task_record=user_task,
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id=turn_command_id,
        request_fingerprint="sha256:followup",
        goal_mutation=ClaimGoalMutation(candidate=candidate),
    )
    assert acceptance.goal_context is None
    assert acceptance.goal_candidate == candidate
    queued = await storage.get_agent_task("queued-user")
    assert queued.details["goal_candidate"] == candidate.as_task_detail()
    replay = await storage.get_turn_ingress_receipt(
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id=turn_command_id,
    )
    assert replay is not None
    assert replay.goal_context is None
    assert replay.goal_candidate == candidate

    await storage.update_agent_task(
        "blocker",
        status=AgentTaskStatus.SUCCEEDED,
        finished_at=320,
    )
    claimed = await storage.claim_goal_for_queued_task(
        candidate=candidate,
        task_id="queued-user",
        frozen_collaboration_mode="default",
        now_ms=330,
    )
    assert claimed is not None
    assert claimed.goal.active_task_id == "queued-user"
    claimed_task = await storage.get_agent_task("queued-user")
    assert "goal_candidate" not in claimed_task.details
    assert claimed_task.details["goal_context"] == claimed.context.as_task_detail()


async def test_queued_goal_claim_requires_the_durable_matching_candidate(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=250,
    )
    idle_goal = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=260,
    )
    assert idle_goal is not None
    candidate = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id=idle_goal.goal_id,
    )

    await storage.create_agent_task(
        AgentTaskRecord(
            task_id="ordinary-queued-task",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            created_at=300,
            updated_at=300,
        )
    )
    assert (
        await storage.claim_goal_for_queued_task(
            candidate=candidate,
            task_id="ordinary-queued-task",
            frozen_collaboration_mode="default",
            now_ms=310,
        )
        is None
    )
    current = await storage.get_goal(SESSION_KEY)
    assert current is not None
    assert current.active_task_id is None

    mismatched = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id="another-goal",
    )
    await storage.update_agent_task(
        "ordinary-queued-task",
        details={"goal_candidate": mismatched.as_task_detail()},
    )
    assert (
        await storage.claim_goal_for_queued_task(
            candidate=candidate,
            task_id="ordinary-queued-task",
            frozen_collaboration_mode="default",
            now_ms=320,
        )
        is None
    )

    await storage.update_agent_task(
        "ordinary-queued-task",
        details={"goal_candidate": candidate.as_task_detail()},
    )
    claimed = await storage.claim_goal_for_queued_task(
        candidate=candidate,
        task_id="ordinary-queued-task",
        frozen_collaboration_mode="default",
        now_ms=330,
    )
    assert claimed is not None
    assert claimed.goal.active_task_id == "ordinary-queued-task"


async def test_idle_user_turn_returns_frozen_context_not_candidate(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=250,
    )
    idle_goal = await storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
        now_ms=260,
    )
    assert idle_goal is not None
    result = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="idle-followup-message",
            role="user",
            content="Continue with the Goal.",
            created_at=300,
        ),
        expected_epoch=0,
        updated_at=300,
        task_record=AgentTaskRecord(
            task_id="idle-followup-task",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            created_at=300,
            updated_at=300,
        ),
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id="idle-followup-request",
        request_fingerprint="sha256:idle-followup",
        goal_mutation=ClaimCurrentGoalMutation(),
    )
    assert result.goal_context is not None
    assert result.goal_context.task_id == "idle-followup-task"
    assert result.goal_candidate is None


async def test_collected_input_preserves_owned_context_without_candidate(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None

    result = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="owned-collect-message",
            role="user",
            content="Add this detail to the queued Goal turn.",
            created_at=210,
        ),
        expected_epoch=0,
        updated_at=210,
        task_record=AgentTaskRecord(
            task_id="task-1",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            details={"message_count": 1},
            created_at=210,
            updated_at=210,
        ),
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id="owned-collect-request",
        request_fingerprint="sha256:owned-collect",
        merge_into_task=True,
        goal_mutation=ClaimCurrentGoalMutation(),
    )

    assert result.goal_context == accepted.goal_context
    assert result.goal_candidate is None
    task = await storage.get_agent_task("task-1")
    assert task is not None and task.details is not None
    assert task.details["goal_context"] == accepted.goal_context.as_task_detail()
    assert "goal_candidate" not in task.details
    goal = await storage.get_goal(SESSION_KEY)
    assert goal is not None
    assert goal.active_task_id == "task-1"
    assert goal.turns_started == 1


async def test_stale_user_candidate_is_removed_and_turn_remains_ordinary(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None
    stale = GoalClaimCandidate(
        session_id=SESSION_ID,
        epoch=0,
        goal_id="stale-goal-id",
    )
    result = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="stale-message",
            role="user",
            content="This should remain ordinary.",
            created_at=310,
        ),
        expected_epoch=0,
        updated_at=310,
        task_record=AgentTaskRecord(
            task_id="stale-task",
            session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED,
            details={"goal_candidate": stale.as_task_detail()},
            created_at=310,
            updated_at=310,
        ),
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id="stale-request",
        request_fingerprint="sha256:stale",
        goal_mutation=ClaimGoalMutation(candidate=stale),
    )
    assert result.goal is None
    assert result.goal_context is None
    assert result.goal_candidate is None
    task = await storage.get_agent_task("stale-task")
    assert task is not None
    assert "goal_candidate" not in task.details


async def test_clear_receipt_survives_reset_but_session_delete_cascades(
    storage: SessionStorage,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None
    clear_command = _command("clear")
    cleared = await storage.clear_goal(
        session_key=SESSION_KEY,
        expected=_expected(accepted.goal),
        command=clear_command,
    )
    assert cleared.response["goal"] is None
    assert cleared.response["previousGoalId"] == "goal-1"
    assert await storage.get_goal(SESSION_KEY) is None
    await storage.increment_epoch(SESSION_KEY)
    assert await storage.get_goal_command_receipt(clear_command) is not None
    await storage.delete_session(SESSION_KEY)
    assert await storage.get_goal_command_receipt(clear_command) is None


async def test_manager_reset_removes_active_goal_and_fences_old_owner(
    storage: SessionStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command("set")
    accepted = await _set_goal(storage, command=command)
    assert accepted.goal is not None and accepted.goal_context is not None
    manager = SessionManager(storage, inject_time_prefix=False)

    async def skip_archive(
        _node: SessionNode,
        _entries: list[TranscriptEntry],
        _summaries: list[object],
    ) -> None:
        return None

    monkeypatch.setattr(manager, "write_session_archive", skip_archive)
    reset, rotated = await manager.apply_intent(
        SESSION_KEY,
        SessionIntent.RESET_SAME_KEY,
    )

    assert rotated is True
    assert reset.session_id != SESSION_ID
    assert reset.epoch == 1
    assert await storage.get_goal(SESSION_KEY) is None
    receipt = await storage.get_goal_command_receipt(command)
    assert receipt is not None
    assert receipt.response == accepted.goal_command_response

    with pytest.raises(GoalConflictError) as terminal_exc:
        await storage.commit_goal_terminal(
            accepted.goal_context,
            status="complete",
            now_ms=400,
        )
    assert terminal_exc.value.code == "GOAL_NOT_FOUND"
    with pytest.raises(GoalConflictError) as progress_exc:
        await storage.update_goal_progress(
            accepted.goal_context,
            explanation="stale",
            steps=[],
            now_ms=410,
        )
    assert progress_exc.value.code == "GOAL_NOT_FOUND"

    await storage.update_agent_task(
        accepted.goal_context.task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=420,
    )
    assert (
        await storage.settle_goal_task(
            accepted.goal_context,
            max_turns=50,
            runtime_budget_seconds=3_600,
            now_ms=430,
        )
        is None
    )


async def test_atomic_turn_reset_removes_active_goal_and_preserves_receipt(
    storage: SessionStorage,
) -> None:
    command = _command("set")
    accepted = await _set_goal(storage, command=command)
    assert accepted.goal_context is not None
    manager = SessionManager(storage, inject_time_prefix=False)
    prepared = await manager.prepare_intent(
        SESSION_KEY,
        SessionIntent.RESET_SAME_KEY,
    )

    async def skip_archive(_snapshot: object) -> None:
        return None

    await storage.accept_turn(
        TranscriptEntry(
            session_id=prepared.node.session_id,
            session_key=SESSION_KEY,
            message_id="reset-message",
            role="user",
            content="Start the reset generation.",
            created_at=400,
        ),
        expected_epoch=prepared.expected_epoch,
        updated_at=400,
        task_record=None,
        source_scope="gateway:sessions.send",
        request_session_key=SESSION_KEY,
        client_request_id="reset-turn-request",
        request_fingerprint=hashlib.sha256(b"reset-turn").hexdigest(),
        session_node=prepared.node,
        reset_from_session_id=prepared.previous_session_id,
        reset_archive_writer=skip_archive,
    )

    current = await storage.get_session(SESSION_KEY)
    assert current is not None
    assert current.session_id == prepared.node.session_id
    assert current.epoch == 1
    assert await storage.get_goal(SESSION_KEY) is None
    receipt = await storage.get_goal_command_receipt(command)
    assert receipt is not None
    assert receipt.response == accepted.goal_command_response

    with pytest.raises(GoalConflictError) as terminal_exc:
        await storage.commit_goal_terminal(
            accepted.goal_context,
            status="complete",
            now_ms=410,
        )
    assert terminal_exc.value.code == "GOAL_NOT_FOUND"
    with pytest.raises(GoalConflictError) as progress_exc:
        await storage.update_goal_progress(
            accepted.goal_context,
            explanation=None,
            steps=[],
            now_ms=420,
        )
    assert progress_exc.value.code == "GOAL_NOT_FOUND"
    await storage.update_agent_task(
        accepted.goal_context.task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=200,
        finished_at=430,
    )
    assert (
        await storage.settle_goal_task(
            accepted.goal_context,
            max_turns=50,
            runtime_budget_seconds=3_600,
            now_ms=440,
        )
        is None
    )


async def test_full_fork_does_not_inherit_goal_or_receipts(
    storage: SessionStorage,
) -> None:
    command = _command("set")
    accepted = await _set_goal(storage, command=command)
    assert accepted.goal is not None
    parent_before = accepted.goal.model_dump()
    manager = SessionManager(storage, inject_time_prefix=False)
    child_key = "agent:main:webchat:goal-full-child"

    child = await manager.branch(
        SESSION_KEY,
        child_key,
        fork_transcript=True,
    )

    assert child.forked_from_parent is True
    assert await storage.get_goal(child_key) is None
    parent_after = await storage.get_goal(SESSION_KEY)
    assert parent_after is not None
    assert parent_after.model_dump() == parent_before
    parent_receipt = await storage.get_goal_command_receipt(command)
    assert parent_receipt is not None
    assert parent_receipt.response == accepted.goal_command_response
    assert (
        await storage.get_goal_command_receipt(
            replace(command, request_session_key=child_key)
        )
        is None
    )
    child_transcript = await manager.get_transcript(child_key)
    assert [entry.content for entry in child_transcript] == [
        "Ship the safe Goal runtime."
    ]


async def test_prepared_prefix_fork_does_not_inherit_goal_or_mutate_parent(
    storage: SessionStorage,
) -> None:
    command = _command("set")
    accepted = await _set_goal(storage, command=command)
    assert accepted.goal is not None
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.append_message(SESSION_KEY, "assistant", "Synthetic checkpoint.")
    fork_before = await manager.append_message(
        SESSION_KEY,
        "user",
        "Do not copy this marker.",
    )
    parent_goal = await storage.get_goal(SESSION_KEY)
    assert parent_goal is not None
    parent_before = parent_goal.model_dump()
    child_key = "agent:main:webchat:goal-prefix-child"
    prepared = await manager.prepare_prefix_branch(
        SESSION_KEY,
        child_key,
        fork_before_message_id=fork_before.message_id,
    )
    await storage.accept_turn(
        TranscriptEntry(
            session_id=prepared.node.session_id,
            session_key=child_key,
            message_id="prefix-child-message",
            role="user",
            content="Continue from the selected prefix.",
            created_at=9_000_000_000_000,
        ),
        expected_epoch=prepared.expected_epoch,
        updated_at=9_000_000_000_000,
        task_record=None,
        source_scope="gateway:sessions.send",
        request_session_key=child_key,
        client_request_id="prefix-child-request",
        request_fingerprint=hashlib.sha256(b"prefix-child").hexdigest(),
        session_node=prepared.node,
        initial_transcript_entries=prepared.initial_transcript_entries,
    )

    assert await storage.get_goal(child_key) is None
    parent_after = await storage.get_goal(SESSION_KEY)
    assert parent_after is not None
    assert parent_after.model_dump() == parent_before
    parent_receipt = await storage.get_goal_command_receipt(command)
    assert parent_receipt is not None
    assert parent_receipt.response == accepted.goal_command_response
    assert (
        await storage.get_goal_command_receipt(
            replace(command, request_session_key=child_key)
        )
        is None
    )
    child_transcript = await manager.get_transcript(child_key)
    assert [entry.content for entry in child_transcript] == [
        "Ship the safe Goal runtime.",
        "Synthetic checkpoint.",
        "Continue from the selected prefix.",
    ]


async def test_restart_pauses_active_goal_and_releases_owner(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    first = SessionStorage(str(db_path))
    await first.connect()
    await first.upsert_session(
        SessionNode(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            epoch=0,
            created_at=100,
            updated_at=100,
        )
    )
    await _set_goal(first)
    await first.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        goal = await restarted.get_goal(SESSION_KEY)
        assert goal is not None
        assert goal.status == "paused"
        assert goal.pause_reason == "process_restart"
        assert goal.active_task_id is None
        assert goal.turns_settled == 1
        task = await restarted.get_agent_task("task-1")
        assert task is not None and task.status == AgentTaskStatus.ABANDONED
    finally:
        await restarted.close()


async def test_restart_settlement_accounts_owner_once_without_downtime(
    storage: SessionStorage,
) -> None:
    await _set_goal(storage)
    await storage.update_agent_task(
        "task-1",
        status=AgentTaskStatus.RUNNING,
        started_at=200,
        updated_at=350,
    )
    await storage.conn.execute(
        """
        INSERT INTO usage_events (
            event_id, execution_id, call_index, turn_id, session_id,
            session_epoch, started_at_ms, completed_at_ms, status,
            input_tokens, output_tokens, total_tokens, origin
        ) VALUES (?, ?, 0, ?, ?, 0, 200, 340, 'finalized', 11, 7, 18, ?)
        """,
        ("usage-restart", "execution-restart", "task-1", SESSION_ID, "test"),
    )
    await storage.conn.commit()

    assert await storage.mark_abandoned_agent_tasks(now_ms=1_000) == 1
    recovered = await storage.get_goal(SESSION_KEY)
    assert recovered is not None
    assert recovered.status == "paused"
    assert recovered.active_task_id is None
    assert recovered.turns_settled == 1
    # Recovery uses the last durable heartbeat (350), not restart time (1000).
    assert recovered.active_time_ms == 150
    assert recovered.window_active_time_ms == 150
    assert (recovered.input_tokens, recovered.output_tokens, recovered.total_tokens) == (
        11,
        7,
        18,
    )

    revision = recovered.state_revision
    assert await storage.mark_abandoned_agent_tasks(now_ms=1_100) == 0
    replay = await storage.get_goal(SESSION_KEY)
    assert replay is not None
    assert replay.state_revision == revision
    assert replay.turns_settled == 1
    assert replay.total_tokens == 18


async def test_restart_uses_feature_disabled_pause_classification(
    tmp_path,
) -> None:
    db_path = tmp_path / "feature-disabled.db"
    first = SessionStorage(str(db_path))
    await first.connect()
    await first.upsert_session(
        SessionNode(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            epoch=0,
            created_at=100,
            updated_at=100,
        )
    )
    await _set_goal(first)
    await first.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect(goal_pause_reason="feature_disabled")
    try:
        goal = await restarted.get_goal(SESSION_KEY)
        assert goal is not None
        assert goal.status == "paused"
        assert goal.pause_reason == "feature_disabled"
        assert goal.terminal_reason == "feature_disabled"
        assert goal.active_task_id is None
    finally:
        await restarted.close()

    invalid = SessionStorage(":memory:")
    with pytest.raises(ValueError, match="goal_pause_reason"):
        await invalid.connect(goal_pause_reason="untrusted_reason")
    await invalid.close()
