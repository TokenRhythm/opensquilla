from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
from opensquilla.engine.types import DoneEvent, TextDeltaEvent
from opensquilla.gateway.boot import dispatch_task_runtime_turn
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskStatus, SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage


@dataclass(frozen=True)
class _TimingObservation:
    done: int
    assistant_rows: int
    task_status: str
    committed: int


class _FinalizerGatedTurnRunner:
    def __init__(
        self,
        *,
        storage: SessionStorage,
        session_id: str,
        finalizer_entered: asyncio.Event,
        release_finalizer: asyncio.Event,
    ) -> None:
        self._storage = storage
        self._session_id = session_id
        self._finalizer_entered = finalizer_entered
        self._release_finalizer = release_finalizer

    async def run(self, _message: str, session_key: str, **kwargs: Any):
        answer = "durable assistant answer"
        yield TextDeltaEvent(text=answer)
        yield DoneEvent(text=answer, text_snapshot=answer)

        self._finalizer_entered.set()
        await self._release_finalizer.wait()
        turn_id = str(kwargs["root_turn_id"])
        await self._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=self._session_id,
                session_key=session_key,
                message_id=f"assistant-{turn_id}",
                role="assistant",
                content=answer,
                turn_context={"turn_id": turn_id},
            )
        )


def _read_timing_observation(
    db_path: Path,
    *,
    session_id: str,
    task_id: str,
    emitted_events: list[str],
) -> _TimingObservation:
    database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        assistant_rows = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM transcript_entries
                WHERE session_id = ? AND role = 'assistant'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        task_row = connection.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert task_row is not None
    return _TimingObservation(
        done=emitted_events.count("session.event.done"),
        assistant_rows=assistant_rows,
        task_status=str(task_row[0]),
        committed=emitted_events.count(TURN_COMMITTED_EVENT),
    )


@pytest.mark.asyncio
async def test_turn_committed_follows_transcript_and_task_sqlite_commits(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "turn-committed-timing.sqlite"
    session_key = "agent:main:turn-committed-sqlite-timing"
    session_id = "session-turn-committed-sqlite-timing"
    finalizer_entered = asyncio.Event()
    release_finalizer = asyncio.Event()
    success_write_entered = asyncio.Event()
    release_success_write = asyncio.Event()
    emitted_events: list[str] = []

    storage = await SessionStorage.open(str(db_path))
    await storage.upsert_session(
        SessionNode(
            session_key=session_key,
            session_id=session_id,
            agent_id="main",
        )
    )
    real_update_agent_task = storage.update_agent_task

    async def _gate_success_write(task_id: str, **fields: Any):
        if fields.get("status") == AgentTaskStatus.SUCCEEDED:
            success_write_entered.set()
            await release_success_write.wait()
        return await real_update_agent_task(task_id, **fields)

    storage.update_agent_task = _gate_success_write  # type: ignore[method-assign]

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted_events.append(event_name)

    turn_runner = _FinalizerGatedTurnRunner(
        storage=storage,
        session_id=session_id,
        finalizer_entered=finalizer_entered,
        release_finalizer=release_finalizer,
    )
    config = GatewayConfig(
        workspace_dir=str(tmp_path),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=2.0,
    )

    async def _run_turn(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=None,
            turn_runner=turn_runner,
            event_emitter=_emit,
        )

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_run_turn,
        event_emitter=_emit,
        running_heartbeat_interval_s=None,
    )
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="sqlite-timing-test",
        agent_id="main",
        session_key=session_key,
        session_id=session_id,
        input_provenance={"kind": "test"},
    )

    handle = await runtime.enqueue(envelope, "hello")
    observations: list[_TimingObservation] = []
    try:
        await asyncio.wait_for(finalizer_entered.wait(), timeout=2.0)
        observations.append(
            _read_timing_observation(
                db_path,
                session_id=session_id,
                task_id=handle.task_id,
                emitted_events=emitted_events,
            )
        )

        release_finalizer.set()
        await asyncio.wait_for(success_write_entered.wait(), timeout=2.0)
        observations.append(
            _read_timing_observation(
                db_path,
                session_id=session_id,
                task_id=handle.task_id,
                emitted_events=emitted_events,
            )
        )

        release_success_write.set()
        completed = await runtime.wait(handle.task_id, timeout=2.0)
        assert completed.status == AgentTaskStatus.SUCCEEDED
        observations.append(
            _read_timing_observation(
                db_path,
                session_id=session_id,
                task_id=handle.task_id,
                emitted_events=emitted_events,
            )
        )
    finally:
        release_finalizer.set()
        release_success_write.set()
        await runtime.shutdown(cancel=False, timeout=2.0)
        await storage.close()

    assert observations == [
        _TimingObservation(done=1, assistant_rows=0, task_status="running", committed=0),
        _TimingObservation(done=1, assistant_rows=1, task_status="running", committed=0),
        _TimingObservation(done=1, assistant_rows=1, task_status="succeeded", committed=1),
    ]

    reopened = await SessionStorage.open(str(db_path))
    try:
        durable_task = await reopened.get_agent_task(handle.task_id)
        durable_transcript = await reopened.get_recent_transcript(session_id, 10)
    finally:
        await reopened.close()

    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.SUCCEEDED
    assistant_entries = [entry for entry in durable_transcript if entry.role == "assistant"]
    assert len(assistant_entries) == 1
    assert assistant_entries[0].content == "durable assistant answer"


@pytest.mark.asyncio
async def test_double_terminal_write_failure_recovers_running_task_as_abandoned(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "turn-committed-double-write-failure.sqlite"
    session_key = "agent:main:turn-committed-double-write-failure"
    session_id = "session-turn-committed-double-write-failure"
    finalizer_entered = asyncio.Event()
    release_finalizer = asyncio.Event()
    release_finalizer.set()
    emitted_events: list[str] = []
    terminal_attempts = 0

    storage = await SessionStorage.open(str(db_path))
    await storage.upsert_session(
        SessionNode(
            session_key=session_key,
            session_id=session_id,
            agent_id="main",
        )
    )
    real_update_agent_task = storage.update_agent_task

    async def _fail_terminal_writes(task_id: str, **fields: Any):
        nonlocal terminal_attempts
        if fields.get("status") == AgentTaskStatus.SUCCEEDED:
            terminal_attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return await real_update_agent_task(task_id, **fields)

    storage.update_agent_task = _fail_terminal_writes  # type: ignore[method-assign]

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted_events.append(event_name)

    turn_runner = _FinalizerGatedTurnRunner(
        storage=storage,
        session_id=session_id,
        finalizer_entered=finalizer_entered,
        release_finalizer=release_finalizer,
    )
    config = GatewayConfig(
        workspace_dir=str(tmp_path),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=2.0,
    )

    async def _run_turn(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=None,
            turn_runner=turn_runner,
            event_emitter=_emit,
        )

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_run_turn,
        event_emitter=_emit,
        running_heartbeat_interval_s=None,
    )
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="sqlite-double-write-failure-test",
        agent_id="main",
        session_key=session_key,
        session_id=session_id,
        input_provenance={"kind": "test"},
    )

    handle = await runtime.enqueue(envelope, "hello")
    try:
        in_memory_task = await runtime.wait(handle.task_id, timeout=2.0)
        persisted_before_restart = _read_timing_observation(
            db_path,
            session_id=session_id,
            task_id=handle.task_id,
            emitted_events=emitted_events,
        )
    finally:
        release_finalizer.set()
        await runtime.shutdown(cancel=False, timeout=2.0)
        await storage.close()

    assert terminal_attempts == 2
    assert in_memory_task.status == AgentTaskStatus.SUCCEEDED
    assert emitted_events.count("task.succeeded") == 1
    assert emitted_events.count(TURN_COMMITTED_EVENT) == 0
    assert persisted_before_restart == _TimingObservation(
        done=1,
        assistant_rows=1,
        task_status="running",
        committed=0,
    )

    reopened = await SessionStorage.open(str(db_path))
    try:
        recovered_task = await reopened.get_agent_task(handle.task_id)
    finally:
        await reopened.close()

    assert recovered_task is not None
    assert recovered_task.status == AgentTaskStatus.ABANDONED
    assert recovered_task.terminal_reason == "process_restart"
